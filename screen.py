"""SSD1306 OLED helper — idle attract animation + transient text banners.

Wiring: VCC, GND, SCL1 (GPIO 3, pin 5), SDA1 (GPIO 2, pin 3). Pi i2c-1
must be enabled (see README "Enable i2c").

The Screen object owns a background thread that redraws at ~10 fps. By
default it renders an "attract" loop: a constellation of dim nodes with a
packet that walks 1-3 hops between random pairs, revealing the meshlights
title centered on the panel when the packet arrives at its destination.

`show_lines(lines)` swaps to a static text overlay (used for the startup
banner and the `screen` REPL command). `clear()` returns to the idle
animation. `close()` blanks the panel and stops the thread.

connect() returns a Screen on success or None if the OLED can't be reached.
Callers should treat the screen as optional so the engine/sim still runs
headless when nothing is plugged in.
"""

import math
import random
import sys
import threading
import time

try:
    import board
    import busio
    import adafruit_ssd1306
    from PIL import Image, ImageDraw, ImageFont
except ImportError as e:
    print(f"screen deps missing: {e}", file=sys.stderr)
    board = None


DEFAULT_W = 128
DEFAULT_H = 64
DEFAULT_ADDR = 0x3C   # 0x3D is the other common option
FRAME_RATE = 10.0     # OLED reads smoothly here; ~80 ms i2c TX/frame is fine
IDLE_TEXT = ["meshlights", "listening..."]


def connect(width=DEFAULT_W, height=DEFAULT_H, addr=DEFAULT_ADDR):
    if board is None:
        return None
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
    except Exception as e:
        print(f"i2c bus open failed: {e}  "
              f"(is i2c enabled? `sudo raspi-config nonint do_i2c 0 && sudo reboot`)",
              file=sys.stderr)
        return None
    try:
        oled = adafruit_ssd1306.SSD1306_I2C(width, height, i2c, addr=addr)
    except Exception as e:
        print(f"SSD1306 init failed at 0x{addr:02X}: {e}  "
              f"(check wiring; run `i2cdetect -y 1` to scan)", file=sys.stderr)
        return None
    return Screen(oled, width, height)


class _IdleAnim:
    """Constellation of nodes; a packet walks 1-3 random hops; on arrival
    the title text appears centered with a black halo to stay readable
    over the constellation, then fades back to bare nodes.

    State machine:
      rest      → wait REST seconds, then start a new packet
      traveling → animate packet between current and next path node
      arrived   → hold title text for TEXT_HOLD seconds, then rest
    """

    N_COLS = 4
    N_ROWS = 2
    HOP_DURATION = 0.7    # seconds per hop; ~10 px/s wander rate
    TEXT_HOLD = 2.2       # title visible after arrival
    REST = 0.5            # gap between packets (constellation only)
    MARGIN = 8

    def __init__(self, w, h):
        self.w = w
        self.h = h
        # Grid the panel and jitter within each cell so nodes spread
        # evenly without clustering or grid-snap artifacts.
        cw = (w - 2 * self.MARGIN) / self.N_COLS
        ch = (h - 2 * self.MARGIN) / self.N_ROWS
        self.nodes = []
        for r in range(self.N_ROWS):
            for c in range(self.N_COLS):
                cx = self.MARGIN + cw * (c + 0.5)
                cy = self.MARGIN + ch * (r + 0.5)
                jx = random.uniform(-cw * 0.3, cw * 0.3)
                jy = random.uniform(-ch * 0.3, ch * 0.3)
                self.nodes.append((int(cx + jx), int(cy + jy)))
        self.state = "rest"
        self.state_until = 0.0
        self.packet = None

    def _start_packet(self, t):
        hops = random.randint(1, 3)
        path = [random.randrange(len(self.nodes))]
        while len(path) <= hops:
            n = random.randrange(len(self.nodes))
            if n != path[-1]:
                path.append(n)
        self.packet = {"path": path, "i": 0, "hop_start": t}
        self.state = "traveling"

    def render(self, draw, font, t):
        # Constellation: every node as a single dim pixel.
        for (x, y) in self.nodes:
            draw.point((x, y), fill=255)

        if self.state == "rest" and t >= self.state_until:
            self._start_packet(t)

        if self.state == "traveling":
            p = self.packet
            elapsed = t - p["hop_start"]
            progress = min(1.0, elapsed / self.HOP_DURATION)
            n0 = self.nodes[p["path"][p["i"]]]
            n1 = self.nodes[p["path"][p["i"] + 1]]
            x = int(n0[0] + (n1[0] - n0[0]) * progress)
            y = int(n0[1] + (n1[1] - n0[1]) * progress)
            # Packet as a 2x2 block — visually distinguishes from 1px nodes.
            draw.rectangle((x, y, x + 1, y + 1), fill=255)
            if progress >= 1.0:
                p["i"] += 1
                if p["i"] >= len(p["path"]) - 1:
                    self.state = "arrived"
                    self.state_until = t + self.TEXT_HOLD
                else:
                    p["hop_start"] = t

        if self.state == "arrived":
            # Destination node lit as 3x3 to mark the arrival point.
            dn = self.nodes[self.packet["path"][-1]]
            draw.rectangle((dn[0] - 1, dn[1] - 1, dn[0] + 1, dn[1] + 1), fill=255)
            text = "\n".join(IDLE_TEXT)
            bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=2)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = (self.w - tw) // 2
            ty = (self.h - th) // 2
            # Black halo so the text stays legible over any underlying nodes.
            draw.rectangle((tx - 2, ty - 1, tx + tw + 1, ty + th + 1), fill=0)
            draw.multiline_text((tx, ty), text, font=font, fill=255,
                                spacing=2, align="center")
            if t >= self.state_until:
                self.state = "rest"
                self.state_until = t + self.REST
                self.packet = None


class Screen:
    def __init__(self, oled, width, height):
        self.oled = oled
        self.width = width
        self.height = height
        self.font = ImageFont.load_default()
        self._img = Image.new("1", (width, height))
        self._draw = ImageDraw.Draw(self._img)
        self._lock = threading.Lock()
        self._idle = _IdleAnim(width, height)
        self._override = None        # list[str] or None
        self._override_until = 0.0   # 0 = persistent; >0 = auto-clears at t
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        period = 1.0 / FRAME_RATE
        next_t = time.monotonic()
        while not self._stop:
            t = time.monotonic()
            try:
                with self._lock:
                    self._draw.rectangle((0, 0, self.width, self.height),
                                         outline=0, fill=0)
                    show_override = (self._override is not None and
                                     (self._override_until == 0.0 or
                                      t < self._override_until))
                    if show_override:
                        self._render_text(self._override)
                    else:
                        self._override = None
                        self._idle.render(self._draw, self.font, t)
                self.oled.image(self._img)
                self.oled.show()
            except Exception as e:
                print(f"screen render error: {e}", file=sys.stderr)
            next_t += period
            sleep_for = next_t - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # Fell behind (e.g. blocked on i2c) — resync rather than busy-loop.
                next_t = time.monotonic()

    def _render_text(self, lines):
        line_h = 11
        y = 0
        for line in lines:
            if y >= self.height:
                break
            self._draw.text((0, y), str(line), font=self.font, fill=255)
            y += line_h

    def show_lines(self, lines, hold=None):
        """Show `lines` as a static text overlay. If `hold` is set, auto-
        dismisses back to the idle animation after that many seconds."""
        with self._lock:
            self._override = list(lines)
            self._override_until = (time.monotonic() + hold) if hold else 0.0

    def clear(self):
        with self._lock:
            self._override = None
            self._override_until = 0.0

    def close(self):
        self._stop = True
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.oled.fill(0)
            self.oled.show()
        except Exception:
            pass
