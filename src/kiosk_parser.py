"""Kiosk parser — OCR-scans a Ducat Kiosk screenshot and returns price data for every
visible Prime part.

No grid detection. We OCR the full image, pull out every line that looks like a
Prime part name, fuzzy-match it against the known item database, then return plat
price, ducat value and recent sales volume for each unique hit.

Works regardless of resolution, window size, scroll position or screenshot format.

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
from pathlib import Path

import Levenshtein as lev
import numpy as np
import PIL.Image as Img
from PIL.Image import Image
from PIL import ImageEnhance, ImageFilter
from platformdirs import user_cache_path
from tesserocr import PyTessBaseAPI, PSM

import database as db

# ──────────────────────────────────────────────────────────────────────────────
# Tesseract — full-page mode (PSM 6 = assume uniform block of text)
# ──────────────────────────────────────────────────────────────────────────────

_tess = PyTessBaseAPI(
    path="/usr/share/tessdata",
    psm=PSM.SINGLE_BLOCK,
    variables={"tessedit_char_whitelist": db.whitelist_chars},
)

# ──────────────────────────────────────────────────────────────────────────────
# Known OCR substitution table — common single-word misreads
# ──────────────────────────────────────────────────────────────────────────────

_SUBSTITUTIONS: dict[str, str] = {
    # letter confusion
    "Recelver":   "Receiver",
    "Recetver":   "Receiver",
    "Recelver":   "Receiver",
    "Blucprint":  "Blueprint",
    "Bluepnint":  "Blueprint",
    "Blueprlnt":  "Blueprint",
    "Neuroptics": "Neuroptics",  # keep for safety
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
    "Llnk":       "Link",
    "Prirne":     "Prime",
    "Prlme":      "Prime",
    "Pnme":       "Prime",
    # number/letter confusion in item names
    "Ak8olto":    "Akbolto",
    "Akb0lto":    "Akbolto",
    "Aks1iletto": "Akstiletto",
    "Baz4":       "Baza",
}

# Regex: a line must contain "Prime" (case-insensitive) and be plausible length
_PRIME_RE = re.compile(r"\bPr[il1]me\b", re.IGNORECASE)

# Characters that can never appear in item names — used to strip OCR noise
_NOISE_RE = re.compile(r"[^A-Za-z2 ]")

# ──────────────────────────────────────────────────────────────────────────────
# Image pre-processing
# ──────────────────────────────────────────────────────────────────────────────

def _preprocess(image: Image) -> Image:
    """Convert to greyscale, boost contrast, upscale 2× for better OCR on small text."""
    img = image.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.5)
    img = img.filter(ImageFilter.SHARPEN)
    img = img.resize((img.width * 2, img.height * 2), Img.LANCZOS)
    return img


# ──────────────────────────────────────────────────────────────────────────────
# OCR + line extraction
# ──────────────────────────────────────────────────────────────────────────────

def _ocr_full(image: Image) -> str:
    """Run Tesseract on the full pre-processed image, return raw text."""
    _tess.SetImage(image)
    return _tess.GetUTF8Text()


def _clean_line(line: str) -> str:
    """Strip noise characters and apply substitution table to every word."""
    line = _NOISE_RE.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip()
    words = []
    for word in line.split():
        words.append(_SUBSTITUTIONS.get(word, word))
    return " ".join(words)


def _extract_candidates(raw_text: str) -> list[str]:
    """Return cleaned lines from raw OCR text that look like Prime part names."""
    candidates = []
    for line in raw_text.splitlines():
        if not _PRIME_RE.search(line):
            continue
        cleaned = _clean_line(line)
        # Must have at least 2 words and be a reasonable length
        if len(cleaned.split()) >= 2 and 6 <= len(cleaned) <= 60:
            candidates.append(cleaned)
    return candidates


# ──────────────────────────────────────────────────────────────────────────────
# Fuzzy matching against database
# ──────────────────────────────────────────────────────────────────────────────

def _match_item(raw_name: str) -> tuple[str | None, float]:
    """
    Try to match raw_name to a known db item.

    Strategy (in order):
      1. Exact match (fast path)
      2. Word-level Levenshtein — correct each word independently, then
         try to assemble a known item name from the corrected words.
      3. Full-string Levenshtein against all db item names (slow fallback,
         only reached when word-level assembly fails).

    Returns (canonical_name, confidence) or (None, 0.0).
    """
    # 1. Exact
    if raw_name in db.items:
        return raw_name, 1.0

    # 2. Word-level correction
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

    # Also try without trailing "Blueprint" in case OCR dropped it
    if not assembled.endswith("Blueprint") and assembled + " Blueprint" in db.items:
        return assembled + " Blueprint", 0.85

    # 3. Full-string Levenshtein against all known item names (capped at top-3 chars ratio)
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
    ducats = price.get("ducats", 0)   or 0

    if ducats == 0 and plat == 0:
        return "either"
    if ducats == 0:
        return "plat"
    if plat == 0:
        return "ducats"

    # Rough conversion: ~0.15 plat per ducat at average kiosk rates
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
        List of dicts, one per unique matched item, ordered by first appearance.
        Items that OCR found but could not match are included with zeroed prices
        and raw=<what OCR read> so you can see what went wrong.
    """
    processed = _preprocess(image)
    raw_text  = _ocr_full(processed)
    candidates = _extract_candidates(raw_text)

    results: list[dict] = []
    seen:    set[str]   = set()

    for raw_name in candidates:
        canonical, confidence = _match_item(raw_name)

        # Skip low-confidence / duplicate matches
        if canonical is None or confidence < 0.70:
            # Still include as unknown so the caller can see OCR output
            key = raw_name.lower()
            if key not in seen:
                seen.add(key)
                results.append({
                    "name":           raw_name,
                    "raw":            raw_name,
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

        item = db.items.get(canonical, {})
        price = item.get("price", {"platinum": 0, "ducats": 0})

        results.append({
            "name":           canonical,
            "raw":            raw_name,
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
