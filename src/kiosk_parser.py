"""Kiosk parser -- OCR-scans a Ducat Kiosk screenshot and returns price data for every
visible Prime part.

Detection strategy:
  1. Find inter-row separator bands: rows where the full grid width is nearly all
     dark (lum < 8 across >90% of pixels). These are the black strips between tile rows.
  2. Tile rows = the spans between those separator bands (filter < 50px tall).
  3. Name label crop = bottom NAME_H_PX pixels of each tile row (fixed pixel count,
     scaled by DPI factor). Names are bottom-aligned; fixed px works better than a
     fraction because the tile height grows but text height stays proportional to DPI.
  4. 6 fixed equal-width columns (Warframe kiosk is always 6-wide).
  5. Binarize on luminance > 100 (text is warm grey ~lum 156, not pure white).
  6. OCR with Tesseract PSM.SINGLE_LINE at 3x upscale.

Grid x2 boundary is resolution-specific because the right panel + scrollbar have
fixed-pixel widths that do not scale proportionally.

Usage::

    python kiosk_parser.py <screenshot_path>

Output: JSON list per visible item::

    {
        "name":           str,
        "raw":            str,
        "matched":        bool,
        "confidence":     float,
        "price":          {"platinum": float, "ducats": int},
        "sold":           {"today": int, "yesterday": int},
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

# Warframe kiosk always shows exactly 6 columns
_KIOSK_COLS = 6

# Grid x2 as a fraction of image width, keyed by image HEIGHT.
# The right panel + scrollbar have fixed pixel widths, so the fraction differs
# between resolutions. Unknown resolutions fall back to 0.520.
_GRID_X2_FRAC: dict[int, float] = {
    1080: 0.531,   # 1020 / 1920  (confirmed)
    1440: 0.517,   # 1323 / 2560  (confirmed)
    2160: 0.520,   # 3840x2160 estimate
}
_GRID_X2_FALLBACK = 0.520

# Name label height at 1080p reference, in pixels.
# Scales linearly with image height (DPI factor).
_NAME_H_1080 = 85


# ---------------------------------------------------------------------------
# Grid geometry
# ---------------------------------------------------------------------------

def _find_runs(mask_1d: np.ndarray, min_run: int = 1) -> list[tuple[int, int]]:
    """Return (start, end) pairs for contiguous True runs in a 1-D boolean array."""
    runs: list[tuple[int, int]] = []
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


def _infer_grid_x(arr: np.ndarray) -> tuple[int, int]:
    """
    Return (grid_x1, grid_x2): the horizontal extent of the item grid.

    x1 is fixed at ~2.3% of width (left UI chrome is consistent).
    x2 uses a per-resolution lookup table because the right panel and scrollbar
    have fixed pixel widths that don't scale proportionally.
    """
    h, w = arr.shape[:2]
    x1 = int(w * 0.023)
    frac = _GRID_X2_FRAC.get(h, _GRID_X2_FALLBACK)
    x2 = int(w * frac)
    return x1, x2


def _detect_tile_rows(arr: np.ndarray, grid_x1: int, grid_x2: int) -> list[tuple[int, int]]:
    """
    Find tile rows by locating fully-dark horizontal separator bands.

    The black strips between tile rows are nearly 100% dark across the full
    grid width -- a much more reliable signal than text density.
    """
    h = arr.shape[0]

    row_dark = np.array([
        (
            0.299 * arr[y, grid_x1:grid_x2, 0] +
            0.587 * arr[y, grid_x1:grid_x2, 1] +
            0.114 * arr[y, grid_x1:grid_x2, 2]
        ).mean()
        for y in range(h)
    ])

    # Separator: full row mean lum < 8 (the pure black inter-row gaps)
    is_sep = row_dark < 8
    sep_runs = _find_runs(is_sep, min_run=5)

    # Only keep separators wide enough to be true inter-row gaps (> h*0.01)
    min_sep = max(8, int(h * 0.01))
    wide_seps = [(s, e) for s, e in sep_runs if e - s >= min_sep]

    # Tile rows are the spans between consecutive wide separators
    min_tile_h = max(50, int(h * 0.05))
    tile_rows: list[tuple[int, int]] = []
    for i in range(len(wide_seps) - 1):
        row_start = wide_seps[i][1] + 1
        row_end   = wide_seps[i + 1][0] - 1
        if row_end - row_start >= min_tile_h:
            tile_rows.append((row_start, row_end))

    return tile_rows


def _tile_x_bounds(grid_x1: int, grid_x2: int, w: int) -> list[tuple[int, int]]:
    """
    Return (x1, x2) for each of the 6 tile columns.
    A small inset avoids the tile border frame artifacts.
    """
    tile_w = (grid_x2 - grid_x1) / _KIOSK_COLS
    inset = max(4, int(tile_w * 0.05))
    return [
        (int(grid_x1 + i * tile_w) + inset,
         int(grid_x1 + (i + 1) * tile_w) - inset)
        for i in range(_KIOSK_COLS)
    ]


def _name_h(image_h: int) -> int:
    """Name label crop height in pixels, scaled from the 1080p reference value."""
    return max(60, round(_NAME_H_1080 * image_h / 1080))


# ---------------------------------------------------------------------------
# Image pre-processing
# ---------------------------------------------------------------------------

def _preprocess_tile(crop: Image, h_img: int) -> Image:
    """
    3x upscale + binarize on luminance > 100.

    Item name text is warm grey (~lum 156), not pure white, so we threshold
    on luminance rather than per-channel RGB.
    """
    cw, ch = crop.size
    crop = crop.resize((cw * 3, ch * 3), Img.LANCZOS)
    gray = np.array(crop.convert("L"))
    binary = (gray > 100).astype(np.uint8) * 255
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
    h, w = arr.shape[:2]

    grid_x1, grid_x2 = _infer_grid_x(arr)
    tile_rows = _detect_tile_rows(arr, grid_x1, grid_x2)
    tile_cols = _tile_x_bounds(grid_x1, grid_x2, w)
    nh = _name_h(h)

    results: list[dict] = []
    seen:    set[str]   = set()

    for ry1, ry2 in tile_rows:
        name_y1 = ry2 - nh
        name_y2 = ry2

        for tx1, tx2 in tile_cols:
            crop      = img_rgb.crop((tx1, name_y1, tx2, name_y2))
            processed = _preprocess_tile(crop, h)
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
        print("  Unmatched raw OCR lines:")
        for u in unmatched:
            print(f"    . '{u['raw']}'")
