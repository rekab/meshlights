#!/usr/bin/env python3
"""dotstar_test.py — bring-up diagnostic for the APA102 strip.

Uses ONLY the vanilla Adafruit DotStar API. Imports nothing from the
meshlights engine, so:
  - if this works but `engine.py` doesn't → the bug is in our code
  - if this doesn't work → it's hardware / wiring / power / SPI / library

Cycles dim red → green → blue → off at low brightness (default 0.05) so
a full-strip fill stays well under ~0.5 A even on a long strip.

What you should see:
  - "RED" prints, strip turns dim red. If it's green or blue, byte
    order is wrong (lib default is BGR — usually correct for APA102).
  - "GREEN" then "BLUE" likewise.
  - If only some LEDs light up, you've got a dropout / length / power
    droop further down the strip.
  - If nothing lights up at all: power, ground, data/clock wires, or
    you wired to the output end of the strip instead of the input
    (look for the arrow on the PCB).

Pixel count defaults to whatever strip.pixels is set to in config.toml
(falls back to 144 if no config is found). Override with --pixels.

Usage:
  uv run python utils/dotstar_test.py
  uv run python utils/dotstar_test.py --pixels 70 --brightness 0.05 --hold 1.0
  Ctrl-C to stop. The strip is blanked on exit.
"""

import argparse
import signal
import sys
import time
import tomllib
from pathlib import Path

try:
    import board
    import adafruit_dotstar
except ImportError as e:
    print(f"adafruit_dotstar import failed: {e}", file=sys.stderr)
    print("  uv sync   # ensure adafruit-circuitpython-dotstar + adafruit-blinka installed", file=sys.stderr)
    sys.exit(1)


def default_pixels_from_config():
    """Read strip.pixels from the repo's config.toml. Falls back to 144 if
    the config isn't found or doesn't set it — kept deliberately permissive
    so this diagnostic works even when config isn't fully set up yet."""
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config.toml"
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return int(data.get("strip", {}).get("pixels", 144))
    except (OSError, ValueError, KeyError):
        return 144


COLORS = [
    ("RED",   (255, 0,   0  )),
    ("GREEN", (0,   255, 0  )),
    ("BLUE",  (0,   0,   255)),
    ("OFF",   (0,   0,   0  )),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_pixels = default_pixels_from_config()
    ap.add_argument("--pixels", type=int, default=default_pixels,
                    help=f"LED count (default {default_pixels} from config.toml strip.pixels)")
    ap.add_argument("--brightness", type=float, default=0.05,
                    help="0..1; 0.05 keeps full-white draw very low even on long strips")
    ap.add_argument("--hold", type=float, default=1.0,
                    help="seconds to hold each color")
    args = ap.parse_args()

    print(f"opening DotStar: {args.pixels} px, brightness={args.brightness}, "
          f"hold={args.hold}s/color")
    try:
        strip = adafruit_dotstar.DotStar(
            board.SCK, board.MOSI, args.pixels,
            brightness=args.brightness, auto_write=False,
        )
    except Exception as e:
        print(f"DotStar open failed: {e}", file=sys.stderr)
        print("checks: is SPI enabled? (ls /dev/spidev*)  "
              "in the 'spi' and 'gpio' groups? (groups)", file=sys.stderr)
        sys.exit(1)
    print("strip opened. hardware SPI0. Ctrl-C to stop.\n")

    stop = False
    def handle_sig(signum, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    cycle = 0
    try:
        while not stop:
            cycle += 1
            for name, color in COLORS:
                if stop:
                    break
                print(f"[cycle {cycle}] {name:<5}  rgb={color}")
                strip.fill(color)
                strip.show()
                end = time.monotonic() + args.hold
                while not stop and time.monotonic() < end:
                    time.sleep(0.05)
    finally:
        try:
            strip.fill((0, 0, 0))
            strip.show()
        except Exception:
            pass
        print("\nstrip blanked. bye.")


if __name__ == "__main__":
    main()
