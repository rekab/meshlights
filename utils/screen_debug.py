#!/usr/bin/env python3
"""screen_debug.py — SSD1309 diagnostic tool.

Cycles through test patterns to identify why the panel has stripes:
  - solid_white         → if stripes appear here, it's controller-level
                          (stuck/dead pixels, wrong COM hardware config,
                          or the panel really is half-addressed).
  - solid_black         → if pixels are lit, GDDRAM has uninitialized
                          data the controller is leaking through.
  - row_grid            → every 8th row lit. Tells us if all 64 rows
                          are actually being addressed and in order.
  - col_grid            → every 8th column lit. Same for columns.
  - corner_markers      → small markers at each of the 4 corners. If
                          some are missing/displaced, the addressable
                          region is smaller or shifted.
  - diagonal            → single-pixel diagonal. Reveals row/col scan
                          issues that grid lines might mask.

Cycles automatically every 3 s. Ctrl-C to stop.

Run:   uv run python utils/screen_debug.py

Optional: try alternative rotations and COM pin configs to find what
works for THIS specific panel.

  uv run python utils/screen_debug.py --rotate 2
  uv run python utils/screen_debug.py --com-pins 0x22
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1309
from PIL import Image, ImageDraw


def draw_pattern(name, w, h):
    img = Image.new("1", (w, h), 0)
    d = ImageDraw.Draw(img)
    if name == "solid_white":
        d.rectangle((0, 0, w - 1, h - 1), fill=1)
    elif name == "solid_black":
        pass  # already black
    elif name == "row_grid":
        for y in range(0, h, 8):
            d.line((0, y, w - 1, y), fill=1)
    elif name == "col_grid":
        for x in range(0, w, 8):
            d.line((x, 0, x, h - 1), fill=1)
    elif name == "corner_markers":
        for (x, y) in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            x0, y0 = max(0, x - 3), max(0, y - 3)
            x1, y1 = min(w - 1, x + 3), min(h - 1, y + 3)
            d.rectangle((x0, y0, x1, y1), fill=1)
    elif name == "diagonal":
        for i in range(min(w, h)):
            d.point((i, i), fill=1)
            d.point((w - 1 - i, i), fill=1)
    elif name == "halves":
        # Top half white, bottom half black — confirms if upper-half rows
        # are receiving data at all.
        d.rectangle((0, 0, w - 1, h // 2 - 1), fill=1)
    elif name == "checkerboard":
        for y in range(h):
            for x in range(w):
                if (x + y) % 2 == 0:
                    d.point((x, y), fill=1)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x3C)
    ap.add_argument("--width", type=int, default=128)
    ap.add_argument("--height", type=int, default=64)
    ap.add_argument("--rotate", type=int, default=0,
                    help="0/1/2/3 — 90° increments")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="seconds per pattern")
    ap.add_argument("--once", action="store_true",
                    help="run through patterns once and exit")
    args = ap.parse_args()

    serial = i2c(port=1, address=args.addr)
    oled = ssd1309(serial, width=args.width, height=args.height,
                   rotate=args.rotate)
    print(f"SSD1309 up: {args.width}x{args.height} rotate={args.rotate} "
          f"addr=0x{args.addr:02X}")

    patterns = ["solid_white", "solid_black", "halves", "row_grid",
                "col_grid", "corner_markers", "diagonal", "checkerboard"]
    try:
        while True:
            for name in patterns:
                print(f"  showing: {name}")
                img = draw_pattern(name, args.width, args.height)
                oled.display(img)
                time.sleep(args.hold)
            if args.once:
                break
    except KeyboardInterrupt:
        print()
    finally:
        oled.clear()


if __name__ == "__main__":
    main()
