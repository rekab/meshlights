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

Optional: override individual SSD1309 registers to chase column-ghosting.
Default values are luma's (which assume SSD1306).

  uv run python utils/screen_debug.py --rotate 2
  uv run python utils/screen_debug.py --precharge 0x11 --vcomh 0x00
  uv run python utils/screen_debug.py --precharge 0x82 --contrast 0x40
  uv run python utils/screen_debug.py --no-charge-pump  # if module has external boost
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from luma.core.interface.serial import i2c
from luma.oled.device import sh1106, ssd1306, ssd1309
from PIL import Image, ImageDraw


DRIVERS = {"ssd1309": ssd1309, "ssd1306": ssd1306, "sh1106": sh1106}


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
    hex_arg = lambda x: int(x, 0)
    ap.add_argument("--addr", type=hex_arg, default=0x3C)
    ap.add_argument("--width", type=int, default=128)
    ap.add_argument("--height", type=int, default=64)
    ap.add_argument("--rotate", type=int, default=0,
                    help="0/1/2/3 — 90° increments")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="seconds per pattern")
    ap.add_argument("--once", action="store_true",
                    help="run through patterns once and exit")
    ap.add_argument("--ghost", action="store_true",
                    help="only show corner_markers + diagonal (the two patterns "
                         "that surface column ghosting)")
    # SSD1309 retune registers (override luma's SSD1306 defaults)
    ap.add_argument("--precharge", type=hex_arg, default=None,
                    help="SETPRECHARGE (0xD9): luma default 0xF1. Try 0x22, "
                         "0x11, 0x82, 0xF1.")
    ap.add_argument("--vcomh", type=hex_arg, default=None,
                    help="SETVCOMDETECT (0xDB): luma default 0x40. Try 0x00, "
                         "0x20, 0x30.")
    ap.add_argument("--contrast", type=hex_arg, default=None,
                    help="Contrast 0x00..0xFF: luma default 0xCF.")
    ap.add_argument("--no-charge-pump", action="store_true",
                    help="Disable internal charge pump (CHARGEPUMP 0x8D 0x10) "
                         "— for modules with external boost circuitry.")
    ap.add_argument("--driver", choices=tuple(DRIVERS), default="ssd1309",
                    help="Try a different controller (some modules labeled "
                         "SSD1309 ship with SH1106 silicon).")
    ap.add_argument("--clock", type=hex_arg, default=None,
                    help="SETDISPLAYCLOCKDIV (0xD5): luma default 0x80. "
                         "Try 0xF0 to push the internal scan rate higher.")
    args = ap.parse_args()

    serial = i2c(port=1, address=args.addr)
    driver_cls = DRIVERS[args.driver]
    oled = driver_cls(serial, width=args.width, height=args.height,
                      rotate=args.rotate)
    print(f"{args.driver} up: {args.width}x{args.height} rotate={args.rotate} "
          f"addr=0x{args.addr:02X}")

    # Apply register overrides AFTER luma init.
    if args.clock is not None:
        oled.command(0xD5, args.clock)
        print(f"  SETDISPLAYCLOCKDIV = 0x{args.clock:02X}")
    if args.precharge is not None:
        oled.command(0xD9, args.precharge)
        print(f"  SETPRECHARGE = 0x{args.precharge:02X}")
    if args.vcomh is not None:
        oled.command(0xDB, args.vcomh)
        print(f"  SETVCOMDETECT = 0x{args.vcomh:02X}")
    if args.contrast is not None:
        oled.contrast(args.contrast)
        print(f"  CONTRAST = 0x{args.contrast:02X}")
    if args.no_charge_pump:
        oled.command(0x8D, 0x10)
        print("  CHARGEPUMP disabled")

    if args.ghost:
        patterns = ["corner_markers", "diagonal"]
    else:
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
