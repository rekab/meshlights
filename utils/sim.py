#!/usr/bin/env python3
"""sim.py — synthetic-input REPL for debugging Meshlights animations.

Drives the real animation engine and real strip with synthetic input,
no MeshCore connection. Use this when you want to verify rendering
behavior in isolation:
  - is the heartbeat actually at the strip's physical center?
  - which physical LED is index 0? which is 143?
  - what does a comet that ends at the strip's edge look like?
  - how long do sparks ACTUALLY linger after a comet passes?

A background render thread runs the same composition pipeline as
engine.py (animations.py classes, additive NumPy framebuffer, direct
write into the DotStar _post_brightness_buffer, strip.show()). The
main thread reads commands from stdin and mutates the active list.

This grabs the SPI bus — you can't run sim.py and engine.py at the
same time.

Commands (type at the > prompt; 'help' or '?' for this list):
  pixel N [color]      light up LED N (default white) — orientation test
  pixels N,N,N[...]    light up multiple LEDs (default white)
  comet N1,N2,...           spawn a comet, default TXT_MSG colors (blue/gold)
  comet TYPE N1,N2,...      spawn a comet with the given payload-type colors
  randcomet K               K-node random comet, random payload type
  randcomet K TYPE          K-node random comet, fixed payload type
  walkup               spawn a white walkup bloom
  dim [color]          spawn a dim bloom (default cyan)
  clear                kill all active animations (heartbeat resumes)
  bright X             set APA102 brightness (0..1) live
  list                 list active animations + ages
  help | ?
  q | quit | exit

  Valid TYPE names: REQ, RESPONSE, TXT_MSG, ACK, ADVERT, GRP_TXT,
                    GRP_DATA, ANON_REQ, PATH
"""

import random
import sys
import threading
import time
from pathlib import Path

import numpy as np

# Make config/animations/engine importable from utils/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import board
    import adafruit_dotstar  # noqa: F401  (imported via engine.setup_strip)
except ImportError as e:
    print(f"DotStar/blinka import failed: {e}", file=sys.stderr)
    sys.exit(1)

import animations
from animations import (
    BASE_DWELL, BASE_TAIL_DURATION, BASE_TRANSIT,
    DIM_BLOOM_DURATION, WALKUP_BLOOM_DURATION,
    Bloom, Comet, Walkup, render_heartbeat,
)
from config import HEAD_PALETTE, PALETTE, UNKNOWN_HEAD_COLOR, WALKUP_COLOR, load_config


# payload_type → human label, kept in sync with config.PALETTE for logging.
PAYLOAD_LABELS = {
    0x00: "REQ",      0x01: "RESPONSE", 0x02: "TXT_MSG",  0x03: "ACK",
    0x04: "ADVERT",   0x05: "GRP_TXT",  0x06: "GRP_DATA", 0x07: "ANON_REQ",
    0x08: "PATH",
}
_LABEL_TO_TYPE = {v: k for k, v in PAYLOAD_LABELS.items()}


def _payload_type_from_label(label):
    return _LABEL_TO_TYPE.get(label.upper())


def _colors_for_type(ptype):
    """Look up (tail_color, head_color, label) for a payload type. Defaults
    to TXT_MSG (sky blue / warm gold) when ptype is None — matches what
    make_comet defaulted to before payload types entered sim.py."""
    if ptype is None:
        ptype = 0x02   # TXT_MSG
    tail = PALETTE.get(ptype)
    head = HEAD_PALETTE.get(ptype, UNKNOWN_HEAD_COLOR)
    label = PAYLOAD_LABELS.get(ptype, f"0x{ptype:02X}")
    return tail, head, label
from engine import setup_strip


COLOR_ALIASES = {
    "red":     (255, 0,   0  ),
    "green":   (0,   255, 0  ),
    "blue":    (0,   0,   255),
    "cyan":    (0,   255, 255),
    "yellow":  (255, 255, 0  ),
    "magenta": (255, 0,   255),
    "white":   (255, 255, 255),
    "orange":  (255, 140, 0  ),
}


def parse_color(s):
    s = s.strip().lower()
    if s in COLOR_ALIASES:
        return COLOR_ALIASES[s]
    parts = [int(p) for p in s.split(",")]
    if len(parts) != 3:
        raise ValueError(f"need R,G,B triple or one of {sorted(COLOR_ALIASES)}")
    return tuple(parts)


class HoldPattern:
    """Static pattern that holds forever — bypass animations for orientation tests."""

    def __init__(self, pattern):
        self.pattern = pattern.astype(np.float32)
        self.start_time = time.monotonic()

    def render(self, fb, t):
        fb += self.pattern

    def is_done(self, t):
        return False


def make_comet(cfg, nodes, color=(60, 150, 255), head_color=(255, 200, 90),
               intensity=0.8):
    # Defaults match the TXT_MSG / GRP_TXT pair from config.PALETTE +
    # config.HEAD_PALETTE — sky blue tail, warm gold head.
    return Comet(
        nodes=np.array(nodes, dtype=np.int64),
        color=np.array(color, dtype=np.float32),
        head_color=np.array(head_color, dtype=np.float32),
        intensity=intensity,
        tail_duration=BASE_TAIL_DURATION * cfg.tail_duration,
        dwell=BASE_DWELL / cfg.speed,
        transit=BASE_TRANSIT / cfg.speed,
        head_brightness=cfg.head_brightness,
        start_time=time.monotonic(),
    )


def make_walkup(cfg):
    return Walkup(
        color=np.array(WALKUP_COLOR, dtype=np.float32),
        peak=cfg.walkup_peak,
        duration=WALKUP_BLOOM_DURATION,
        start_time=time.monotonic(),
    )


def make_dim_bloom(cfg, color):
    return Bloom(
        color=np.array(color, dtype=np.float32),
        peak=cfg.dim_bloom_peak,
        duration=DIM_BLOOM_DURATION,
        start_time=time.monotonic(),
    )


class Sim:
    def __init__(self, config_path):
        self.cfg = load_config(config_path)
        animations.configure(self.cfg.pixels)
        self.strip, self.pixel_view = setup_strip(self.cfg.brightness, self.cfg.pixels)
        self.active = []
        self.lock = threading.Lock()
        self.stop = False
        self.render_thread = threading.Thread(target=self._render_loop, daemon=True)
        self.render_thread.start()

    def _render_loop(self):
        fb = np.zeros((self.cfg.pixels, 3), dtype=np.float32)
        while not self.stop:
            t = time.monotonic()
            fb.fill(0.0)
            with self.lock:
                snapshot = list(self.active)
            if snapshot:
                for obj in snapshot:
                    obj.render(fb, t)
                    if obj.is_done(t):
                        with self.lock:
                            try:
                                self.active.remove(obj)
                            except ValueError:
                                pass
            else:
                render_heartbeat(fb, t)
            np.clip(fb, 0.0, 255.0, out=fb)
            fb_u8 = fb.astype(np.uint8)
            self.pixel_view[:, 1] = fb_u8[:, 2]
            self.pixel_view[:, 2] = fb_u8[:, 1]
            self.pixel_view[:, 3] = fb_u8[:, 0]
            try:
                self.strip.show()
            except Exception as e:
                print(f"strip.show: {e}", file=sys.stderr)
            time.sleep(1.0 / 30)

    def add(self, obj):
        with self.lock:
            self.active.append(obj)

    def clear(self):
        with self.lock:
            self.active.clear()

    def set_brightness(self, b):
        b5 = max(0, min(31, int(round(31 * b))))
        with self.lock:
            self.pixel_view[:, 0] = 0xE0 | b5

    def list_active(self):
        now = time.monotonic()
        with self.lock:
            return [(type(o).__name__, now - getattr(o, "start_time", now))
                    for o in self.active]

    def shutdown(self):
        self.stop = True
        self.render_thread.join(timeout=1.0)
        self.pixel_view[:, 1:] = 0
        try:
            self.strip.show()
        except Exception:
            pass


HELP = __doc__.split("Commands", 1)[1] if "Commands" in __doc__ else __doc__


def handle(sim, cmd, arg):
    if cmd in ("q", "quit", "exit"):
        return False

    if cmd in ("help", "?"):
        print("Commands" + HELP)

    elif cmd == "clear":
        sim.clear()
        print("cleared.")

    elif cmd == "list":
        items = sim.list_active()
        if not items:
            print("  (none — heartbeat active)")
        else:
            for name, age in items:
                print(f"  {name:12s} age={age:5.2f}s")

    elif cmd == "pixel":
        parts = arg.split(None, 1)
        idx = int(parts[0])
        if not (0 <= idx < sim.cfg.pixels):
            raise ValueError(f"pixel index out of range 0..{sim.cfg.pixels-1}")
        color = parse_color(parts[1]) if len(parts) > 1 else (255, 255, 255)
        p = np.zeros((sim.cfg.pixels, 3), dtype=np.float32)
        p[idx] = color
        sim.add(HoldPattern(p))
        print(f"lit pixel {idx} = {color}")

    elif cmd == "pixels":
        idxs = [int(x) for x in arg.split(",")]
        for i in idxs:
            if not (0 <= i < sim.cfg.pixels):
                raise ValueError(f"pixel index {i} out of range 0..{sim.cfg.pixels-1}")
        p = np.zeros((sim.cfg.pixels, 3), dtype=np.float32)
        for i in idxs:
            p[i] = (255, 255, 255)
        sim.add(HoldPattern(p))
        print(f"lit pixels {idxs}")

    elif cmd == "comet":
        # `comet 5,30,60` (default TXT_MSG) or `comet ADVERT 5,30,60` (typed).
        parts = arg.split(None, 1)
        ptype = None
        if len(parts) == 2:
            label = parts[0].upper()
            ptype = _payload_type_from_label(label)
            if ptype is None:
                raise ValueError(f"unknown payload type {label!r}; one of "
                                 f"{sorted(PAYLOAD_LABELS.values())}")
            node_str = parts[1]
        else:
            node_str = parts[0]
        nodes = [int(x) for x in node_str.split(",")]
        for n in nodes:
            if not (0 <= n < sim.cfg.pixels):
                raise ValueError(f"node {n} out of range 0..{sim.cfg.pixels-1}")
        color, head_color, label = _colors_for_type(ptype)
        sim.add(make_comet(sim.cfg, nodes, color=color, head_color=head_color))
        print(f"spawned {label} comet {nodes}")

    elif cmd == "randcomet":
        # `randcomet K` (K random pixel positions, random payload type) or
        # `randcomet K TYPE` (K random positions, fixed type).
        parts = arg.split(None, 1) if arg else []
        k = int(parts[0]) if parts else 3
        ptype = None
        if len(parts) == 2:
            label = parts[1].upper()
            ptype = _payload_type_from_label(label)
            if ptype is None:
                raise ValueError(f"unknown payload type {label!r}; one of "
                                 f"{sorted(PAYLOAD_LABELS.values())}")
        else:
            ptype = random.choice(list(PALETTE.keys()))
        nodes = [random.randint(0, sim.cfg.pixels - 1) for _ in range(k)]
        color, head_color, label = _colors_for_type(ptype)
        sim.add(make_comet(sim.cfg, nodes, color=color, head_color=head_color))
        print(f"spawned random {label} comet {nodes}")

    elif cmd == "walkup":
        sim.add(make_walkup(sim.cfg))
        print("spawned walkup")

    elif cmd == "dim":
        color = parse_color(arg) if arg else COLOR_ALIASES["cyan"]
        sim.add(make_dim_bloom(sim.cfg, color))
        print(f"spawned dim bloom {color}")

    elif cmd == "bright":
        b = float(arg)
        sim.set_brightness(b)
        print(f"brightness set to {b}")

    else:
        print(f"unknown command: {cmd!r}. type 'help' for the list.")

    return True


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Synthetic-input REPL for Meshlights animations.")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args()

    sim = Sim(args.config)
    print(f"sim ready: {sim.cfg.pixels} px, brightness={sim.cfg.brightness:.2f}")
    print("type 'help' for commands. Ctrl-D or 'q' to exit.")
    try:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            try:
                if not handle(sim, cmd, arg):
                    break
            except Exception as e:
                print(f"error: {e}")
    finally:
        sim.shutdown()
        print("bye.")


if __name__ == "__main__":
    main()
