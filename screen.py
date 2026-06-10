"""SSD1306 OLED helper — idle attract animation + transient text banners.

Wiring: VCC, GND, SCL1 (GPIO 3, pin 5), SDA1 (GPIO 2, pin 3). Pi i2c-1
must be enabled (see README "Enable i2c").

The Screen object owns a background thread that redraws at ~10 fps. By
default it renders an idle attract loop:

  * A "meshlights" banner pinned to the top OR bottom of the panel
    (chosen per cycle).
  * A constellation of nodes connected to their k nearest neighbours.
  * Each cycle, a random source either ROUTES a single packet through
    a non-revisiting random walk to a dead end, or FLOODS in BFS style
    where every node re-emits to its neighbours the FIRST time it sees
    the packet (matches MeshCore repeater semantics: subsequent dupes
    arrive but aren't re-broadcast).

`show_lines(lines)` swaps to a static text overlay. `clear()` returns to
the idle animation. `close()` blanks the panel and stops the thread.

connect() returns a Screen on success or None if the OLED can't be reached.
Callers should treat the screen as optional so the engine/sim still runs
headless when nothing is plugged in.
"""

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
DEFAULT_ADDR = 0x3C      # 0x3D is the other common option
FRAME_RATE = 10.0        # OLED reads smoothly here; ~80 ms i2c TX/frame is fine
BANNER_TEXT = "meshlights"
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_SIZE = 11


def _load_font():
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, FONT_SIZE)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


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
    """Cycle structure (per animation):

        1. Pick banner side (top/bottom), regen node layout + adjacency.
        2. Pick mode (routed | bfs), pick random source node.
        3. ROUTED: precompute a random walk from source that never revisits
           a node; animate one packet hop-by-hop along it. Ends when path
           is exhausted (dead end or max length).
        4. BFS: emit packets from source to all its neighbours. Each
           arrival: if destination is unseen, mark seen + emit to its
           neighbours; if already seen, packet dies on arrival (mirrors
           MeshCore repeaters' "broadcast once on first sight" rule).
           Ends when no packets remain in flight.
        5. Rest for REST seconds (constellation only, banner stays), then
           start the next cycle.
    """

    N_COLS = 4
    N_ROWS = 2
    K_NEIGHBORS = 3
    HOP_DURATION = 0.7
    REST = 1.0
    BANNER_PAD = 2
    MAX_PACKETS = 32       # safety cap on simultaneous in-flight packets

    def __init__(self, w, h, font):
        self.w = w
        self.h = h
        self.font = font
        tmp = ImageDraw.Draw(Image.new("1", (w, h)))
        bbox = tmp.textbbox((0, 0), BANNER_TEXT, font=font)
        self.banner_h = (bbox[3] - bbox[1]) + 2 * self.BANNER_PAD
        self._banner_bbox = bbox
        self.banner_at_top = True
        self.mode = None
        self.nodes = []
        self.adj = []
        self.state = "rest"
        self.state_until = 0.0
        self.packets = []      # list of (from_idx, to_idx, hop_start_t)
        self.seen = set()
        self.path = []
        self.path_i = 0
        self._regen_layout()

    def _regen_layout(self):
        margin_x = 8
        margin_y = 2
        if self.banner_at_top:
            top = self.banner_h + margin_y
            bot = self.h - margin_y
        else:
            top = margin_y
            bot = self.h - self.banner_h - margin_y
        cw = (self.w - 2 * margin_x) / self.N_COLS
        ch = (bot - top) / self.N_ROWS
        nodes = []
        for r in range(self.N_ROWS):
            for c in range(self.N_COLS):
                cx = margin_x + cw * (c + 0.5)
                cy = top + ch * (r + 0.5)
                jx = random.uniform(-cw * 0.3, cw * 0.3)
                jy = random.uniform(-ch * 0.3, ch * 0.3)
                nodes.append((int(cx + jx), int(cy + jy)))
        adj = [set() for _ in nodes]
        for i, (x, y) in enumerate(nodes):
            dists = sorted(
                (((x - x2) ** 2 + (y - y2) ** 2), j)
                for j, (x2, y2) in enumerate(nodes) if j != i
            )
            for _, j in dists[:self.K_NEIGHBORS]:
                adj[i].add(j)
                adj[j].add(i)        # symmetric — A↔B
        self.nodes = nodes
        self.adj = [sorted(s) for s in adj]

    def _start_cycle(self, t):
        self.banner_at_top = random.random() < 0.5
        self._regen_layout()
        self.mode = random.choice(("routed", "bfs"))
        self.seen = set()
        self.packets = []
        src = random.randrange(len(self.nodes))
        self.seen.add(src)
        if self.mode == "routed":
            self.path = self._random_walk(src)
            if len(self.path) < 2:
                # No usable edges from src; brief rest and try again.
                self.state = "rest"
                self.state_until = t + 0.3
                return
            self.path_i = 0
            self.packets.append((self.path[0], self.path[1], t))
        else:
            for nbr in self.adj[src]:
                self.packets.append((src, nbr, t))
        self.state = "animating"

    def _random_walk(self, src, max_len=6):
        path = [src]
        while len(path) < max_len:
            unvisited = [n for n in self.adj[path[-1]] if n not in path]
            if not unvisited:
                break
            path.append(random.choice(unvisited))
        return path

    def render(self, draw, font, t):
        # Constellation: dim dot for unseen, 3x3 block for seen.
        for i, (x, y) in enumerate(self.nodes):
            if i in self.seen:
                draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=255)
            else:
                draw.point((x, y), fill=255)

        if self.state == "rest":
            self._draw_banner(draw)
            if t >= self.state_until:
                self._start_cycle(t)
            return

        # state == animating
        new_packets = []
        arrivals = []
        for (from_i, to_i, start_t) in self.packets:
            elapsed = t - start_t
            progress = min(1.0, elapsed / self.HOP_DURATION)
            n0 = self.nodes[from_i]
            n1 = self.nodes[to_i]
            x = int(n0[0] + (n1[0] - n0[0]) * progress)
            y = int(n0[1] + (n1[1] - n0[1]) * progress)
            draw.rectangle((x, y, x + 1, y + 1), fill=255)
            if progress < 1.0:
                new_packets.append((from_i, to_i, start_t))
            else:
                arrivals.append((from_i, to_i))

        for (from_i, to_i) in arrivals:
            first_time = to_i not in self.seen
            self.seen.add(to_i)
            if not first_time:
                continue            # MeshCore: re-broadcast only on first sight
            if self.mode == "routed":
                nxt = self.path_i + 2
                if nxt < len(self.path) and len(new_packets) < self.MAX_PACKETS:
                    self.path_i += 1
                    new_packets.append((to_i, self.path[nxt], t))
            else:  # bfs
                for nbr in self.adj[to_i]:
                    if len(new_packets) >= self.MAX_PACKETS:
                        break
                    new_packets.append((to_i, nbr, t))

        self.packets = new_packets
        self._draw_banner(draw)       # always drawn last so it sits on top

        if not self.packets:
            self.state = "rest"
            self.state_until = t + self.REST

    def _draw_banner(self, draw):
        if self.banner_at_top:
            y0 = 0
        else:
            y0 = self.h - self.banner_h
        y1 = y0 + self.banner_h - 1
        draw.rectangle((0, y0, self.w - 1, y1), fill=255)
        bbox = self._banner_bbox
        tw = bbox[2] - bbox[0]
        tx = (self.w - tw) // 2
        # bbox[1] is the font's top-side bearing — subtract it so the text's
        # visual top lands at y0 + BANNER_PAD.
        ty = y0 + self.BANNER_PAD - bbox[1]
        draw.text((tx, ty), BANNER_TEXT, font=self.font, fill=0)


class Screen:
    def __init__(self, oled, width, height):
        self.oled = oled
        self.width = width
        self.height = height
        self.font = _load_font()
        self._img = Image.new("1", (width, height))
        self._draw = ImageDraw.Draw(self._img)
        self._lock = threading.Lock()
        self._idle = _IdleAnim(width, height, self.font)
        self._override = None
        self._override_until = 0.0
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
        line_h = 12
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
