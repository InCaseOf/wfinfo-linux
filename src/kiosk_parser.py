"""Kiosk parser -- OCR-scans a Ducat Kiosk screenshot and returns price data for every
visible Prime part.

Strategy: instead of OCR-ing the full image (which causes item art to be read as
garbage text), we:
  1. Detect the horizontal bands where item name labels sit, using white-pixel
     density (names are bright text on a dark background).
  2. Detect vertical tile column boundaries using the same method.
  3. OCR each individual tile name crop with PSM.SINGLE_LINE at 3x upscale.

All geometry thresholds are expressed as fractions of image dimensions so the
detection works at any resolution (1080p, 1440p, 4K, ultrawide, etc.).

Usage (standalone)::

    python kiosk_parser.py <screenshot_path>

Output: JSON list of objects, one per detected unique item, ordered by appearance::

    {
        "name":           str,
        "raw":            str,
        "matched":        bool,
        "confidence":     float,
        "price": {
            "platinum":   float,
            "ducats":     int
        },
        "sold": {
            "today":      int,
            "yesterday":  int
        },
        "vaulted":        bool | "partial",
        "recommendation": "plat" | "ducats" | "either"
    }
"""

import json
import re
import sys
from pathlib import Path

import Levenshtein as lev
import numpy as np
import PIL.Image as Img
from PIL.Image import Image
from tesserocr import PyTessBaseAPI, PSM

import database as db


# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------

_tess = PyTessBaseAPI(
    path="/usr/share/tessdata",
    psm=PSM.SINGLE_LINE,
    variables={"tessedit_char_whitelist": db.whitelist_chars},
)


# ---------------------------------------------------------------------------
# OCR substitution table
# ---------------------------------------------------------------------------

_SUBSTITUTIONS: dict[str, str] = {
    "Recelver":   "Receiver",
    "Recetver":   "Receiver",
    "Blucprint":  "Blueprint",
    "Bluepnint":  "Blueprint",
    "Blueprlnt":  "Blueprint",
    "Neumoptics": "Neuroptics",
    "Neuroptlcs": "Neuroptics",
    "Neuroplics": "Neuroptics",
    "Systerns":   "Systems",
    "Systcms":    "Systems",
    "Chassls":    "Chassis",
    "Chassi":     "Chassis",
    "Harnoss":    "Harness",
    "Banel":      "Barrel",
    "Barrol":     "Barrel",
    "Slring":     "String",
    "Slnng":      "String",
    "Stoclk":     "Stock",
    "Stoek":      "Stock",
    "Gnip":       "Grip",
    "Llnk":       "Link",
    "Prirne":     "Prime",
    "Prlme":      "Prime",
    "Pnme":       "Prime",
    "Ak8olto":    "Akbolto",
    "Akb0lto":    "Akbolto",
    "Aks1iletto": "Akstiletto",
    "Baz4":       "Baza",
}

_PRIME_RE = re.compile(r"\bPr[il1]me\b", re.IGNORECASE)
_NOISE_RE = re.compile(r"[^A-Za-z2 ]")


# ---------------------------------------------------------------------------
# Grid geometry detection
# ---------------------------------------------------------------------------

def _find_runs(mask_1d: np.ndarray, min_run: int = 1) -> list[tuple[int, int]]:
    """Return (start, end) pairs for contiguous True runs in a 1-D boolean array."""
    runs = []
    in_run = False
    start = 0
    for i, v in enumerate(mask_1d):
        if v and not in_run:
            in_run, start = True, i
        elif not v and in_run:
            if i - start >= min_run:
                runs.append((start, i - 1))
            in_run = False
    if in_run and len(mask_1d) - start >= min_run:
        runs.append((start, len(mask_1d) - 1))
    return runs


def _detect_name_rows(arr: np.ndarray, grid_x1: int, grid_x2: int) -> list[tuple[int, int]]:
    """
    Find horizontal bands containing item name labels (white text on dark bg).
    All thresholds scale with image height so detection works at any resolution.
    """
    h = arr.shape[0]
    region = arr[:, grid_x1:grid_x2, :]
    white = (region[:, :, 0] > 170) & (region[:, :, 1] > 170) & (region[:, :, 2] > 170)
    norm = white.sum(axis=1).astype(float) / (grid_x2 - grid_x1)

    # min_run scales with height: ~8px at 1080p, ~11px at 1440p
    min_run = max(6, int(h * 0.007))
    raw_runs = _find_runs(norm > 0.04, min_run=min_run)

    # Merge runs within h*0.04 px (handles two-line names at any resolution)
    merge_gap = int(h * 0.04)
    merged: list[list[int]] = []
    for s, e in raw_runs:
        if merged and s - merged[-1][1] <= merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    # Drop UI chrome (top 30%) and thin artefacts (< h*0.01 tall)
    min_height = max(10, int(h * 0.01))
    return [
        (s, e) for s, e in merged
        if s > h * 0.30 and (e - s) >= min_height
    ]


def _detect_tile_cols(arr: np.ndarray, row_y1: int, row_y2: int,
                      grid_x1: int, grid_x2: int) -> list[int]:
    """Find vertical tile column boundaries within a name-label row."""
    w = arr.shape[1]
    region = arr[row_y1:row_y2, grid_x1:grid_x2, :]
    white = (region[:, :, 0] > 170) & (region[:, :, 1] > 170) & (region[:, :, 2] > 170)
    white_per_col = white.sum(axis=0).astype(float) / max(row_y2 - row_y1, 1)

    gap_runs = _find_runs(white_per_col < 0.05, min_run=3)

    # Min tile width scales with resolution (~50px at 1080p, ~130px at 2560p)
    min_tile_w = max(50, int(w * 0.05))
    dividers = [grid_x1]
    for gs, ge in gap_runs:
        mid = grid_x1 + (gs + ge) // 2
        if mid - dividers[-1] > min_tile_w:
            dividers.append(mid)
    dividers.append(grid_x2)
    return dividers


def _infer_grid_x(arr: np.ndarray) -> tuple[int, int]:
    """Estimate left/right extents of the kiosk item grid (resolution-relative)."""
    w = arr.shape[1]
    # Grid: left ~2.3%, right ~51.7% (left of the sell/info panel)
    return int(w * 0.023), int(w * 0.517)


# ---------------------------------------------------------------------------
# Image pre-processing
# ---------------------------------------------------------------------------

def _preprocess_tile(crop: Image) -> Image:
    """3x upscale + hard binarise for clean PSM.SINGLE_LINE OCR."""
    w, h = crop.size
    crop = crop.resize((w * 3, h * 3), Img.LANCZOS)
    gray = np.array(crop.convert("L"))
    binary = (gray > 140).astype(np.uint8) * 255
    return Img.fromarray(binary)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _ocr_tile(tile_img: Image) -> str:
    _tess.SetImage(tile_img)
    return _tess.GetUTF8Text().strip()


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def _clean_line(line: str) -> str:
    line = _NOISE_RE.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip()
    return " ".join(_SUBSTITUTIONS.get(w, w) for w in line.split())


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def _match_item(raw_name: str) -> tuple[str | None, float]:
    if raw_name in db.items:
        return raw_name, 1.0

    corrected_words = []
    for word in raw_name.split():
        if word in db.words:
            corrected_words.append(word)
            continue
        best_r, best_w = 0.0, None
        for w in db.words:
            r = lev.ratio(word, w, score_cutoff=0.75)
            if r > best_r:
                best_r, best_w = r, w
        corrected_words.append(best_w if best_w else word)

    assembled = " ".join(corrected_words)
    if assembled in db.items:
        return assembled, 0.9
    if not assembled.endswith("Blueprint") and assembled + " Blueprint" in db.items:
        return assembled + " Blueprint", 0.85

    best_r, best_name = 0.0, None
    for name in db.items:
        if name == "updated":
            continue
        r = lev.ratio(raw_name, name, score_cutoff=0.70)
        if r > best_r:
            best_r, best_name = r, name
    if best_name and best_r >= 0.70:
        return best_name, best_r

    return None, 0.0


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def _recommendation(price: dict) -> str:
    plat   = price.get("platinum", 0) or 0
    ducats = price.get("ducats",   0) or 0
    if ducats == 0 and plat == 0:
        return "either"
    if ducats == 0:
        return "plat"
    if plat == 0:
        return "ducats"
    ducat_plat_equiv = ducats * 0.15
    if ducat_plat_equiv > plat * 1.1:
        return "ducats"
    if plat > ducat_plat_equiv * 1.5:
        return "plat"
    return "either"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def parse_kiosk(image: Image) -> list[dict]:
    """Parse a Ducat Kiosk screenshot and return pricing data for every visible item."""
    img_rgb = image.convert("RGB")
    arr = np.array(img_rgb)

    grid_x1, grid_x2 = _infer_grid_x(arr)
    name_rows = _detect_name_rows(arr, grid_x1, grid_x2)

    results: list[dict] = []
    seen:    set[str]   = set()

    for y1, y2 in name_rows:
        tile_cols = _detect_tile_cols(arr, y1, y2, grid_x1, grid_x2)

        for i in range(len(tile_cols) - 1):
            tx1, tx2 = tile_cols[i], tile_cols[i + 1]
            if tx2 - tx1 < max(40, int(arr.shape[1] * 0.04)):
                continue

            crop      = img_rgb.crop((tx1, y1, tx2, y2))
            processed = _preprocess_tile(crop)
            raw_text  = _ocr_tile(processed)
            cleaned   = _clean_line(raw_text)

            if not cleaned or len(cleaned) < 4 or not _PRIME_RE.search(cleaned):
                continue

            canonical, confidence = _match_item(cleaned)

            if canonical is None or confidence < 0.70:
                key = cleaned.lower()
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "name":           cleaned,
                        "raw":            raw_text,
                        "matched":        False,
                        "confidence":     round(confidence, 3),
                        "price":          {"platinum": 0, "ducats": 0},
                        "sold":           {"today": 0, "yesterday": 0},
                        "vaulted":        False,
                        "recommendation": "either",
                    })
                continue

            key = canonical.lower()
            if key in seen:
                continue
            seen.add(key)

            item  = db.items.get(canonical, {})
            price = item.get("price", {"platinum": 0, "ducats": 0})

            results.append({
                "name":           canonical,
                "raw":            raw_text,
                "matched":        True,
                "confidence":     round(confidence, 3),
                "price":          price,
                "sold":           item.get("sold", {"today": 0, "yesterday": 0}),
                "vaulted":        item.get("vaulted", False),
                "recommendation": _recommendation(price),
            })

    return results


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python kiosk_parser.py <screenshot.png|jpg>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    with Img.open(path).convert("RGB") as img:
        results = parse_kiosk(img)

    if not results:
        print("No Prime items detected. Check the screenshot contains the kiosk grid.")
        sys.exit(1)

    matched   = [r for r in results if r["matched"]]
    unmatched = [r for r in results if not r["matched"]]

    print(json.dumps(results, indent=2))
    print(f"\n-- Summary ----------------------------------")
    print(f"  Matched:   {len(matched)}")
    print(f"  Unmatched: {len(unmatched)}")
    if unmatched:
        print(f"  Unmatched raw OCR lines:")
        for u in unmatched:
            print(f"    . '{u['raw']}'")
