"""Kiosk parser — OCR-scans a Ducat Kiosk screenshot and returns price data for every
visible Prime part.

Strategy: instead of OCR-ing the full image (which causes item art to be read as
garbage text), we:
  1. Detect the horizontal bands where item name labels sit, using white-pixel
     density (names are bright text on a dark background).
  2. Detect vertical tile column boundaries using the same method.
  3. OCR each individual tile name crop with PSM.SINGLE_LINE at 3x upscale.

This works at any resolution and scroll position because the detection is
pixel-density based, not hard-coded coordinates.

Usage (standalone)::

    python kiosk_parser.py <screenshot_path>

Output: JSON list of objects, one per detected unique item, ordered by appearance::

    {
        "name":           str,          # canonical item name from db
        "raw":            str,          # what OCR actually read
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
from itertools import groupby
from operator import itemgetter
from pathlib import Path

import Levenshtein as lev
import numpy as np
import PIL.Image as Img
from PIL.Image import Image
from PIL import ImageEnhance, ImageFilter
from tesserocr import PyTessBaseAPI, PSM

import database as db

# ──────────────────────────────────────────────────────────────────────────────
# Tesseract — single-line mode for per-tile name crops
# ──────────────────────────────────────────────────────────────────────────────

_tess = PyTessBaseAPI(
    path="/usr/share/tessdata",
    psm=PSM.SINGLE_LINE,
    variables={"tessedit_char_whitelist": db.whitelist_chars},
)

# ──────────────────────────────────────────────────────────────────────────────
# Known OCR substitution table — common single-word misreads
# ──────────────────────────────────────────────────────────────────────────────

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

_PRIME_RE  = re.compile(r"\bPr[il1]me\b", re.IGNORECASE)
_NOISE_RE  = re.compile(r"[^A-Za-z2 ]")

# ──────────────────────────────────────────────────────────────────────────────
# Grid geometry detection
# ──────────────────────────────────────────────────────────────────────────────

def _find_runs(mask_1d: np.ndarray, min_run: int = 1) -> list[tuple[int, int]]:
    """Return (start, end) index pairs for contiguous True runs in a 1-D boolean array."""
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
    Find horizontal bands that contain item name labels.

    Name labels are white text (~RGB > 180) on a dark background.
    We look for horizontal runs with a high density of white pixels
    within the kiosk grid x-range.
    """
    h, w = arr.shape[:2]
    region = arr[:, grid_x1:grid_x2, :]
    white = (region[:, :, 0] > 170) & (region[:, :, 1] > 170) & (region[:, :, 2] > 170)
    white_per_row = white.sum(axis=1).astype(float)

    # Normalise by width so threshold is resolution-independent
    norm = white_per_row / (grid_x2 - grid_x1)
    # Name rows have >4 % white pixels (text density)
    text_rows = norm > 0.04

    raw_runs = _find_runs(text_rows, min_run=8)

    # Merge runs that are very close (within 15 px — handles two-line names)
    merged: list[tuple[int, int]] = []
    for s, e in raw_runs:
        if merged and s - merged[-1][1] <= 15:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    # Skip the very top of the image (header / "PRIME PARTS" title)
    merged = [(s, e) for s, e in merged if s > h * 0.15]

    return merged


def _detect_tile_cols(arr: np.ndarray, row_y1: int, row_y2: int,
                      grid_x1: int, grid_x2: int) -> list[int]:
    """
    Find vertical tile column boundaries within a name-label row.

    Between tiles there is a narrow dark gap.  We find dark vertical stripes
    (< 5 % white pixels in the name row height) and use their centres as
    dividers.  Returns a sorted list of x-coordinates including the outer edges.
    """
    region = arr[row_y1:row_y2, grid_x1:grid_x2, :]
    white = (region[:, :, 0] > 170) & (region[:, :, 1] > 170) & (region[:, :, 2] > 170)
    white_per_col = white.sum(axis=0).astype(float) / (row_y2 - row_y1)

    dark_cols = white_per_col < 0.05
    gap_runs = _find_runs(dark_cols, min_run=3)

    dividers = [grid_x1]
    for gs, ge in gap_runs:
        mid = grid_x1 + (gs + ge) // 2
        # Only accept dividers that are not too close to an existing one
        if mid - dividers[-1] > 50:
            dividers.append(mid)
    dividers.append(grid_x2)

    return dividers


def _infer_grid_x(arr: np.ndarray) -> tuple[int, int]:
    """
    Estimate the left/right extents of the kiosk item grid.

    The kiosk grid sits left of the info panel.  We look for the rightmost
    consistently dark vertical stripe (the scrollbar / panel separator) and
    use that as grid_x2.  grid_x1 is estimated as 3 % of image width.
    """
    w = arr.shape[1]
    # Rough heuristic: grid occupies left ~55 % of screen
    grid_x1 = int(w * 0.03)
    grid_x2 = int(w * 0.55)
    return grid_x1, grid_x2


# ──────────────────────────────────────────────────────────────────────────────
# Image pre-processing for a single tile name crop
# ──────────────────────────────────────────────────────────────────────────────

def _preprocess_tile(crop: Image) -> Image:
    """Upscale 3×, greyscale, boost contrast, threshold to BW for clean SINGLE_LINE OCR."""
    w, h = crop.size
    crop = crop.resize((w * 3, h * 3), Img.LANCZOS)
    gray = np.array(crop.convert("L"))
    # Binarise: white text on dark bg → keep pixels above threshold as white
    binary = (gray > 140).astype(np.uint8) * 255
    return Img.fromarray(binary)


# ──────────────────────────────────────────────────────────────────────────────
# OCR one tile name crop
# ──────────────────────────────────────────────────────────────────────────────

def _ocr_tile(tile_img: Image) -> str:
    """Run Tesseract SINGLE_LINE on a pre-processed tile name crop."""
    _tess.SetImage(tile_img)
    return _tess.GetUTF8Text().strip()


# ──────────────────────────────────────────────────────────────────────────────
# Text cleaning
# ──────────────────────────────────────────────────────────────────────────────

def _clean_line(line: str) -> str:
    line = _NOISE_RE.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip()
    return " ".join(_SUBSTITUTIONS.get(w, w) for w in line.split())


# ──────────────────────────────────────────────────────────────────────────────
# Fuzzy matching against database
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# Recommendation logic
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# Main public function
# ──────────────────────────────────────────────────────────────────────────────

def parse_kiosk(image: Image) -> list[dict]:
    """
    Parse a Ducat Kiosk screenshot and return pricing data for every visible item.

    Args:
        image: PIL Image of the kiosk screen (any resolution, RGB or greyscale).

    Returns:
        List of dicts, one per unique matched item, ordered by appearance.
        Items that OCR found but could not match are included with zeroed prices.
    """
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
            if tx2 - tx1 < 40:          # skip slivers
                continue

            crop = img_rgb.crop((tx1, y1, tx2, y2))
            processed = _preprocess_tile(crop)
            raw_text  = _ocr_tile(processed)
            cleaned   = _clean_line(raw_text)

            if not cleaned or len(cleaned) < 4:
                continue

            # If it doesn't look like a Prime item at all, skip
            if not _PRIME_RE.search(cleaned):
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


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry-point
# ──────────────────────────────────────────────────────────────────────────────

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
    print(f"\n── Summary ──────────────────────────────────")
    print(f"  Matched:   {len(matched)}")
    print(f"  Unmatched: {len(unmatched)}")
    if unmatched:
        print(f"  Unmatched raw OCR lines:")
        for u in unmatched:
            print(f"    · '{u['raw']}'")
