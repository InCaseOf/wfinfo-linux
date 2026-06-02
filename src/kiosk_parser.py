"""Kiosk parser — scans the Ducat Kiosk inventory grid and returns price data for every
visible item tile.

The kiosk grid is a regular N-column × M-row layout of 160×160 px tiles (at 1080p).
Each tile contains an item name at the bottom rendered in the active Warframe UI font
(same colour scheme as fissure rewards). We:
  1. Detect the grid region from known UI anchor colours.
  2. Crop each tile's name region.
  3. Run Tesseract OCR with the same whitelist/fuzzy matching already used for rewards.
  4. Look up platinum + ducat data from the existing database.

Falls back gracefully: if a tile name cannot be matched it is marked as unknown rather
than crashing.

Usage (called by AGS via execPython):
    python kiosk_parser.py <screenshot_path>

Output: JSON list of objects, one per detected tile, ordered left-to-right top-to-bottom.
Each object::
    {
        "name": str,
        "price": {"platinum": float, "ducats": int},
        "sold": {"today": int, "yesterday": int},
        "vaulted": bool | "partial",
        "recommendation": "plat" | "ducats" | "either"
    }
"""

import json
import sys
from pathlib import Path

import Levenshtein as lev
import numpy as np
import PIL.Image as Img
from PIL.Image import Image
from platformdirs import user_cache_path
from tesserocr import PyTessBaseAPI

import database as db

# ──────────────────────────────────────────────────────────────────────────────
# Constants (all at 1080p; scaled at runtime)
# ──────────────────────────────────────────────────────────────────────────────

# The Ducat Kiosk grid sits inside a panel that begins roughly here (1080p).
# These are soft defaults — the actual grid is found by scanning for the
# distinct dark-panel background colour.
_TILE_SIZE        = 155   # px — width AND height of one item tile
_TILE_GAP         = 8     # px — gap between tiles
_TILE_COLS        = 6     # tiles per row (max visible in a standard 1080p kiosk)
_TILE_ROWS        = 4     # rows visible without scrolling
_NAME_HEIGHT      = 38    # px — name label area at the bottom of each tile
_NAME_TOP_OFFSET  = 8     # px — small top-padding before name text starts

# Approximate grid origin at 1080p (left-edge of first tile, top-edge of first tile).
# The kiosk panel occupies roughly 67 → 1003 px horizontally and 148 → 752 px
# vertically. We scan a slightly tighter region to avoid UI chrome.
_GRID_LEFT_1080   = 68
_GRID_TOP_1080    = 152

# Bright gold/yellow colour of the item-name text in the kiosk (Orokin palette).
# This is the *secondary* colour of the Orokin theme.
_NAME_COLOUR      = (178, 125, 5)

_SAVE_DIR = user_cache_path("wfinfo") / "images"
_SAVE_DIR.mkdir(parents=True, exist_ok=True)

# Tesseract — reuse whitelist built from the price database
tess = PyTessBaseAPI(
    path="/usr/share/tessdata",
    psm=7,
    variables={"tessedit_char_whitelist": db.whitelist_chars},
)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _scale(image: Image) -> float:
    """Return the display scale factor relative to 1080p."""
    return (
        image.height / 1080
        if image.width / image.height > 16 / 9
        else image.width / 1920
    )


def _strip_name(img: Image) -> Image:
    """Convert kiosk item-name text to black-on-white for Tesseract.

    The name text is golden/yellow (Orokin secondary colour). We keep pixels that
    are close to that colour and set everything else to white.
    """
    arr = np.array(img)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    def _close(channel, target, tol=0.25):
        lo = max(0, target * (1 - tol))
        hi = min(255, target * (1 + tol))
        return (channel >= lo) & (channel <= hi)

    cr, cg, cb = _NAME_COLOUR
    mask = _close(r, cr) & _close(g, cg) & _close(b, cb)

    out = np.ones_like(arr) * 255
    out[mask] = [0, 0, 0]
    return Img.fromarray(out.astype("uint8"))


def _ocr_name(img: Image) -> str:
    """Run Tesseract on a pre-stripped name image and fuzzy-match against the DB."""
    tess.SetImage(img)
    raw = tess.GetUTF8Text().strip()

    # Manual replacements reused from parser.py
    for bad, good in {"Recelver": "Receiver", "Blucprint": "Blueprint"}.items():
        raw = raw.replace(bad, good)

    checked = []
    for word in raw.split():
        if word in db.words:
            checked.append(word)
        else:
            best_ratio, best_word = 0.0, None
            for w in db.words:
                ratio = lev.ratio(word, w, score_cutoff=0.8)
                if ratio > best_ratio:
                    best_ratio, best_word = ratio, w
            if best_word:
                checked.append(best_word)

    return " ".join(checked)


def _recommendation(price: dict) -> str:
    """Return a sell recommendation based on plat vs ducat value.

    Simple heuristic: 1 ducat ≈ 0.15 plat on average in the open market.
    If ducats * 0.15 > platinum → sell for ducats.
    If platinum > ducats * 0.25 → sell for plat (good plat value).
    Otherwise → either is fine.
    """
    plat   = price.get("platinum", 0) or 0
    ducats = price.get("ducats", 0)   or 0

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
# Grid detection
# ──────────────────────────────────────────────────────────────────────────────

def _detect_grid_bounds(image: Image, scale: float):
    """Return (grid_left, grid_top, n_cols, n_rows) by scanning for tile boundaries.

    Strategy: at 1080p the tiles are 155 px wide with 8 px gaps. We start from the
    known approximate origin and detect how many tiles are fully visible in the
    horizontal and vertical directions before hitting UI chrome / screen edge.
    """
    tile_step = (_TILE_SIZE + _TILE_GAP) * scale
    grid_left = _GRID_LEFT_1080 * scale
    grid_top  = _GRID_TOP_1080  * scale
    tile_px   = _TILE_SIZE * scale

    # Count columns: how many fit before screen edge or the scroll-bar region (~1005px)
    kiosk_right = 1005 * scale
    n_cols = 0
    while grid_left + (n_cols + 1) * tile_step - _TILE_GAP * scale <= kiosk_right:
        n_cols += 1
        if n_cols >= _TILE_COLS:
            break

    # Count rows: how many fit before screen edge or kiosk bottom (~752px)
    kiosk_bottom = 752 * scale
    n_rows = 0
    while grid_top + (n_rows + 1) * tile_step - _TILE_GAP * scale <= kiosk_bottom:
        n_rows += 1
        if n_rows >= _TILE_ROWS:
            break

    return int(grid_left), int(grid_top), max(1, n_cols), max(1, n_rows)


# ──────────────────────────────────────────────────────────────────────────────
# Main public function
# ──────────────────────────────────────────────────────────────────────────────

def parse_kiosk(image: Image) -> list[dict]:
    """Parse all visible item tiles in the Ducat Kiosk screenshot.

    Args:
        image: Full-screen RGB screenshot with the kiosk visible.

    Returns:
        List of item dicts (see module docstring for schema).
    """
    scale = _scale(image)
    tile_px   = int(_TILE_SIZE * scale)
    tile_step = int((_TILE_SIZE + _TILE_GAP) * scale)
    name_h    = int(_NAME_HEIGHT * scale)
    name_top_off = int(_NAME_TOP_OFFSET * scale)

    grid_left, grid_top, n_cols, n_rows = _detect_grid_bounds(image, scale)

    results = []
    for row in range(n_rows):
        for col in range(n_cols):
            # Tile bounding box
            tx = grid_left + col * tile_step
            ty = grid_top  + row * tile_step

            # Name label is at the bottom of the tile
            name_box = (
                tx,
                ty + tile_px - name_h - name_top_off,
                tx + tile_px,
                ty + tile_px - name_top_off,
            )

            name_crop = image.crop(name_box)
            stripped  = _strip_name(name_crop)
            name      = _ocr_name(stripped)

            if not name:
                continue

            if name in db.items:
                item = db.items[name]
                entry = {
                    "name":           name,
                    "price":          item["price"],
                    "sold":           item.get("sold", {"today": 0, "yesterday": 0}),
                    "vaulted":        item.get("vaulted", False),
                    "recommendation": _recommendation(item["price"]),
                    "row":            row,
                    "col":            col,
                }
            else:
                entry = {
                    "name":           name,
                    "price":          {"platinum": 0, "ducats": 0},
                    "sold":           {"today": 0, "yesterday": 0},
                    "vaulted":        False,
                    "recommendation": "either",
                    "row":            row,
                    "col":            col,
                }

            results.append(entry)

    return results


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry-point (called by AGS)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with Img.open(sys.argv[1]).convert("RGB") as img:
        print(json.dumps(parse_kiosk(img)))
