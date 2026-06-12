#!/usr/bin/env python3
"""sim.py — synthetic-input REPL for debugging Meshlights animations.

Drives the real animation engine and real strip with synthetic input,
no MeshCore connection. Honors `style` from config.toml (or --style on
the CLI), so you can drive either the comet or waterfall renderer.

Use this when you want to verify rendering behavior in isolation:
  - is the heartbeat actually at the strip's physical center?
  - which physical LED is index 0? which is 143?
  - what does a comet that ends at the strip's edge look like?
  - how long do sparks ACTUALLY linger after a comet passes?
  - does the waterfall saturation glow kick in around 20% utilization?

A background render thread runs the same composition pipeline as
engine.py (animations.py classes, additive NumPy framebuffer, direct
write into the DotStar _post_brightness_buffer, strip.show()). The
main thread reads commands from stdin and mutates the active list.

This grabs the SPI bus — you can't run sim.py and engine.py at the
same time.

Commands (type at the > prompt; 'help' or '?' for this list):
  pixel N [color]      light up LED N (default white) — orientation test
  pixels N,N,N[...]    light up multiple LEDs (default white)

  Comet-mode commands:
    comet N1,N2,...            spawn a comet, default TXT_MSG colors (blue/gold)
    comet TYPE N1,N2,...       spawn a comet with the given payload-type colors
    walkup                     spawn a white walkup bloom
    dim [color]                spawn a dim bloom (default cyan)

  Waterfall-mode commands:
    packet [TYPE] [BYTES]      single RX packet (defaults: TXT_MSG, 60 bytes)

  Mode-aware:
    randcomet K [TYPE]
      • in COMET mode: K-hop random comet
      • in WATERFALL mode: BURST of K random packets — spam this to
        push utilization past the saturation threshold and watch the
        red glow rise in the gaps

  Common:
    clear                kill all active animations (heartbeat resumes)
    bright X             set APA102 brightness (0..1) live
    screen [TEXT]        write TEXT to the OLED (use "|" for line breaks),
                         or no arg = clear
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
    Bloom, Comet, Heartbeat, Walkup, Waterfall,
)
from config import (
    HEAD_PALETTE, PALETTE, PAYLOAD_LABELS,
    UNKNOWN_HEAD_COLOR, WALKUP_COLOR, load_config,
)
_LABEL_TO_TYPE = {v: k for k, v in PAYLOAD_LABELS.items()}


def _payload_type_from_label(label):
    return _LABEL_TO_TYPE.get(label.upper())


def _hops_str(n):
    return "1 hop" if n == 1 else f"{n} hops"


def _bytes_str(n):
    return "1 byte" if n == 1 else f"{n} bytes"


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
import screen as oled_screen


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
    def __init__(self, cfg):
        self.cfg = cfg
        animations.configure(self.cfg.pixels)
        self.strip, self.pixel_view = setup_strip(self.cfg.brightness, self.cfg.pixels)
        self.screen = oled_screen.connect(driver=self.cfg.oled_driver,
                                          style=self.cfg.style)
        if self.screen is not None:
            # Show a startup banner for 2s, then auto-dismiss to the idle
            # attract animation built into screen.py.
            self.screen.show_lines(["meshlights",
                                    f"{self.cfg.pixels} px",
                                    f"{self.cfg.style}"], hold=2.0)
        self.active = []
        # on_traversal_start mirrors the sweep onto the OLED if connected.
        self.heartbeat = Heartbeat(on_traversal_start=self._on_heartbeat_start)
        # Waterfall is constructed only when cfg.style says so — matches
        # engine.py's pattern. In waterfall mode, packet commands push
        # records into self.waterfall and the render loop renders the
        # waterfall instead of the heartbeat. HoldPattern / active
        # objects still composite on top (orientation tests work in
        # either mode).
        self.waterfall = None
        if self.cfg.style == "waterfall":
            self.waterfall = Waterfall(
                n_pixels=self.cfg.pixels,
                window_seconds=self.cfg.waterfall_seconds,
                bytes_per_sec=self.cfg.waterfall_bytes_per_sec,
                overhead_sec=self.cfg.waterfall_overhead_sec,
                exaggeration=self.cfg.waterfall_exaggeration,
                intensity=self.cfg.waterfall_intensity,
                glow_threshold=self.cfg.waterfall_glow_threshold,
                glow_peak=self.cfg.waterfall_glow_peak,
                glow_color=self.cfg.waterfall_glow_color,
            )
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
            if self.waterfall is not None:
                # Waterfall renders the strip; orientation HoldPatterns
                # and any comet-mode objects (if someone spawned them
                # anyway) composite ON TOP, additively.
                try:
                    self.waterfall.render(fb, t)
                except Exception as e:
                    print(f"waterfall render error: {e}", file=sys.stderr)
            else:
                # Heartbeat renders first; comets composite on top. busy
                # gate suspends new traversal starts while comets are
                # active.
                self.heartbeat.render(fb, t, busy=bool(snapshot))
            for obj in snapshot:
                obj.render(fb, t)
                if obj.is_done(t):
                    with self.lock:
                        try:
                            self.active.remove(obj)
                        except ValueError:
                            pass
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

    def add_packet(self, n_bytes, color_arr, label):
        """Push a synthetic RX into the waterfall + OLED log. Waterfall-only;
        raises if called in comet mode."""
        if self.waterfall is None:
            raise RuntimeError("waterfall not enabled — start sim with "
                               "--style waterfall (or set style in config.toml)")
        # `add` is thread-safe via CPython's atomic deque ops; no need to
        # take self.lock (which guards self.active, not the waterfall).
        self.waterfall.add(time.monotonic(), n_bytes, color_arr)
        # OLED line tracks the bar's strip lifetime so it slides off the
        # right exactly when the bar exits the left edge of the strip.
        airtime = (self.cfg.waterfall_overhead_sec
                   + max(n_bytes, 0) / self.cfg.waterfall_bytes_per_sec)
        lifetime = self.cfg.waterfall_seconds + airtime
        # Header: "LABEL Nms/NB". Subline: synthetic RSSI + hops so the
        # display feels realistic during sim sessions.
        detail = f"{int(round(airtime * 1000))}ms/{n_bytes}B"
        synth_rssi = random.randint(-105, -45)
        synth_hops = random.choice((0, 0, 1, 1, 2, 2, 3, 4))  # bias toward few hops
        hops_str = "1 hop" if synth_hops == 1 else f"{synth_hops} hops"
        subline = f"  {synth_rssi}dBm  {hops_str}"
        self.log_packet(label, detail, lifetime, subline=subline)

    def log_packet(self, label, detail, duration_s, subline=None):
        """Mirror the spawn onto the OLED log (no-op when no screen).
        `detail` is a pre-formatted trailing token, e.g. "3 hops" or
        "60 bytes" — caller picks the most informative summary for its
        mode (matches engine.py). `subline` is the optional second line
        (waterfall mode only — ignored by comet)."""
        if self.screen is None:
            return
        self.screen.push_packet(label, detail, duration_s, subline=subline)

    def _on_heartbeat_start(self, t):
        if self.screen is not None:
            self.screen.notify_heartbeat()

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
            items = [(type(o).__name__, now - getattr(o, "start_time", now))
                     for o in self.active]
        if self.waterfall is not None:
            # Snapshot the deque count without holding the deque (deque
            # mutations are atomic in CPython, len() reads cleanly).
            items.append((f"Waterfall ({len(self.waterfall.records)} pkts)", 0.0))
        return items

    def shutdown(self):
        self.stop = True
        self.render_thread.join(timeout=1.0)
        self.pixel_view[:, 1:] = 0
        try:
            self.strip.show()
        except Exception:
            pass
        if self.screen is not None:
            self.screen.close()


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
        comet = make_comet(sim.cfg, nodes, color=color, head_color=head_color)
        sim.add(comet)
        sim.log_packet(label, _hops_str(len(nodes)), comet.total_duration())
        print(f"spawned {label} comet {nodes}")

    elif cmd == "randcomet":
        # `randcomet K [TYPE]`. Behavior depends on mode:
        #   COMET     — one K-hop random comet
        #   WATERFALL — burst of K random packets (1 per K), each
        #               with random byte size from 12..150 (ACK to
        #               near-MTU). Spam this to push utilization past
        #               the saturation threshold and watch the glow
        #               rise.
        parts = arg.split(None, 1) if arg else []
        k = int(parts[0]) if parts else 3
        ptype_fixed = None
        if len(parts) == 2:
            type_label = parts[1].upper()
            ptype_fixed = _payload_type_from_label(type_label)
            if ptype_fixed is None:
                raise ValueError(f"unknown payload type {type_label!r}; one of "
                                 f"{sorted(PAYLOAD_LABELS.values())}")

        if sim.waterfall is not None:
            total_bytes = 0
            for _ in range(k):
                ptype = ptype_fixed if ptype_fixed is not None else random.choice(list(PALETTE.keys()))
                n_bytes = random.randint(12, 150)
                color, _h, label = _colors_for_type(ptype)
                sim.add_packet(n_bytes, np.array(color, dtype=np.float32), label)
                total_bytes += n_bytes
            print(f"fired {k} random packets (total {total_bytes} bytes)")
        else:
            ptype = ptype_fixed if ptype_fixed is not None else random.choice(list(PALETTE.keys()))
            nodes = [random.randint(0, sim.cfg.pixels - 1) for _ in range(k)]
            color, head_color, label = _colors_for_type(ptype)
            comet = make_comet(sim.cfg, nodes, color=color, head_color=head_color)
            sim.add(comet)
            sim.log_packet(label, _hops_str(len(nodes)), comet.total_duration())
            print(f"spawned random {label} comet {nodes}")

    elif cmd == "walkup":
        wu = make_walkup(sim.cfg)
        sim.add(wu)
        # Walkup branch is triggered by hop-0 ADVERTs above the RSSI
        # threshold — represent honestly as such on the OLED log.
        sim.log_packet("ADVERT", _hops_str(0), wu.total_duration())
        print("spawned walkup")

    elif cmd == "dim":
        color = parse_color(arg) if arg else COLOR_ALIASES["cyan"]
        bloom = make_dim_bloom(sim.cfg, color)
        sim.add(bloom)
        sim.log_packet("DIM", _hops_str(0), bloom.total_duration())
        print(f"spawned dim bloom {color}")

    elif cmd == "packet":
        # `packet [TYPE] [BYTES]` — single waterfall RX. Both args
        # optional and order-insensitive (BYTES is the int, TYPE is the
        # label). Defaults: TXT_MSG, 60 bytes.
        if sim.waterfall is None:
            raise ValueError("`packet` is waterfall-only — start sim "
                             "with --style waterfall")
        ptype = 0x02      # TXT_MSG
        n_bytes = 60
        for tok in arg.split():
            if tok.isdigit():
                n_bytes = int(tok)
            else:
                pt = _payload_type_from_label(tok)
                if pt is None:
                    raise ValueError(f"unknown payload type {tok!r}; one of "
                                     f"{sorted(PAYLOAD_LABELS.values())}")
                ptype = pt
        color, _h, label = _colors_for_type(ptype)
        sim.add_packet(n_bytes, np.array(color, dtype=np.float32), label)
        print(f"fired {label} packet ({n_bytes} bytes)")

    elif cmd == "bright":
        b = float(arg)
        sim.set_brightness(b)
        print(f"brightness set to {b}")

    elif cmd == "screen":
        if sim.screen is None:
            print("screen: not connected (see screen.py for wiring + i2c setup)")
        elif not arg:
            sim.screen.clear()
            print("screen cleared")
        else:
            lines = arg.split("|")
            sim.screen.show_lines(lines)
            print(f"screen: {lines}")

    else:
        print(f"unknown command: {cmd!r}. type 'help' for the list.")

    return True


def main():
    import argparse
    import dataclasses
    ap = argparse.ArgumentParser(description="Synthetic-input REPL for Meshlights animations.")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--style", choices=("comet", "waterfall"),
                    help="override [style] from config.toml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.style is not None:
        cfg = dataclasses.replace(cfg, style=args.style)

    sim = Sim(cfg)
    print(f"sim ready: {sim.cfg.pixels} px, brightness={sim.cfg.brightness:.2f}, "
          f"style={sim.cfg.style}")
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
