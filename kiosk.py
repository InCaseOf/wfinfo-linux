#!/usr/bin/env python3
"""Ducat Kiosk CLI scanner.

Takes a screenshot of the current screen, runs the kiosk OCR parser against it,
and prints a formatted table to stdout.

Usage:
    python kiosk.py              # auto-screenshot via grim
    python kiosk.py /path/to.png # use an existing screenshot

Table columns:
    Item Name | Plat | Ducats | Sold Today | Sold Yesterday | Vaulted | Rec
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import PIL.Image as Img

sys.path.insert(0, str(Path(__file__).parent / "src"))
import database as db
from kiosk_parser import parse_kiosk

# ── ANSI colours ──────────────────────────────────────────────────────────────
_R   = "\033[0m"
_BOLD = "\033[1m"
_TEAL = "\033[36m"
_GOLD = "\033[33m"
_DIM  = "\033[2m"
_RED  = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"

REC_COLOUR = {
    "plat":   _TEAL,
    "ducats": _GOLD,
    "either": _DIM,
}

REC_LABEL = {
    "plat":   "PLAT   ",
    "ducats": "DUCATS ",
    "either": "EITHER ",
}


def _screenshot() -> Path:
    """Take a screenshot with grim and return the temp file path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    result = subprocess.run(["grim", tmp.name], capture_output=True)
    if result.returncode != 0:
        print(f"grim failed: {result.stderr.decode().strip()}")
        print("Install grim (pacman -S grim) or pass a screenshot path as argument.")
        sys.exit(1)
    return Path(tmp.name)


def _fmt_vaulted(v) -> str:
    if v is True:
        return f"{_RED}YES{_R}"
    if v == "partial":
        return f"{_YELLOW}PART{_R}"
    return f"{_DIM}no{_R}"


def _print_table(items: list[dict]) -> None:
    if not items:
        print("No items detected. Make sure the Ducat Kiosk grid is fully visible.")
        return

    # Column widths
    name_w = max(len(i["name"]) for i in items)
    name_w = max(name_w, 4)  # at least len("Item")

    # Header
    sep = (
        f"{'─' * (name_w + 2)}┼{'─' * 8}┼{'─' * 8}┼"
        f"{'─' * 12}┼{'─' * 15}┼{'─' * 9}┼{'─' * 9}"
    )
    header = (
        f" {_BOLD}{'Item':<{name_w}}{_R} │"
        f" {'Plat':>5}  │"
        f" {'Ducats':>5}  │"
        f" {'Sold Today':>9}  │"
        f" {'Sold Yesterday':>12}  │"
        f" {'Vaulted':>7}  │"
        f" Rec"
    )
    print()
    print(header)
    print(sep)

    for item in items:
        plat   = item["price"].get("platinum", 0) or 0
        ducats = item["price"].get("ducats", 0)   or 0
        today  = item["sold"].get("today", 0)     or 0
        yest   = item["sold"].get("yesterday", 0) or 0
        vault  = _fmt_vaulted(item.get("vaulted", False))
        rec    = item.get("recommendation", "either")
        rec_str = f"{REC_COLOUR[rec]}{REC_LABEL[rec]}{_R}"

        plat_str   = f"{_TEAL}{plat:>5.0f}{_R}p"
        ducat_str  = f"{_GOLD}{ducats:>5}{_R}d"
        today_str  = f"{today:>9}"
        yest_str   = f"{yest:>12}"

        print(
            f" {item['name']:<{name_w}} │"
            f" {plat_str}  │"
            f" {ducat_str}  │"
            f" {today_str}  │"
            f" {yest_str}  │"
            f" {vault:>7}  │"
            f" {rec_str}"
        )

    print(sep)
    print(f" {_DIM}Rec key: PLAT = sell on market  DUCATS = sell at kiosk  EITHER = ~equal{_R}")
    print()


def main():
    if len(sys.argv) > 1:
        img_path = Path(sys.argv[1])
        if not img_path.exists():
            print(f"File not found: {img_path}")
            sys.exit(1)
        print(f"Using screenshot: {img_path}")
    else:
        print("Taking screenshot... ", end="", flush=True)
        img_path = _screenshot()
        print("done")

    print("Scanning kiosk grid...", end="", flush=True)
    with Img.open(img_path).convert("RGB") as img:
        items = parse_kiosk(img)
    print(f" found {len(items)} item(s)")

    _print_table(items)


if __name__ == "__main__":
    main()
