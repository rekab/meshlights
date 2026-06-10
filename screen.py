"""SSD1306 OLED helper — packet log + idle attract animation.

Wiring: VCC, GND, SCL1 (GPIO 3, pin 5), SDA1 (GPIO 2, pin 3). Pi i2c-1
must be enabled (see README "Enable i2c").

The Screen owns a background thread that redraws at ~10 fps. The display
arbitrates between three modes (highest priority wins):

  override:  show_lines() pinned an arbitrary text overlay (REPL / banners)
  log:       at least one live packet line — scrolling log dominates
  idle:      no packets; meshlights/mesh-graph attract animation runs

`push_packet(label, hops, rssi, duration_s)` appends a line to the log
("TYPE h=N -RR"). The line lives for duration_s seconds (= the strip
animation's own lifetime), then scrolls up off the top. Newest at the
bottom; each new arrival shifts older lines up by one slot at constant
pixel velocity.

The meshlights banner (white bar with black text, top or bottom) is owned
by Screen and shared across modes. The idle loop flips its side at the
start of each cycle; log mode keeps whatever side was last set.

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
DEFAULT_ADDR = 0x3C
FRAME_RATE = 10.0
BANNER_TEXT = "meshlights"
BANNER_PAD = 2
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_SIZE = 11
LINE_HEIGHT = 12          # vertical step per log line (px)
SCROLL_PX_PER_SEC = 32.0  # constant velocity — ~3.2 px/frame at 10 fps
                          # → one LINE_HEIGHT covered in ~4 frames


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


class _LogLine:
    __slots__ = ("text", "born_at", "expires_at", "y", "slot", "dying")

    def __init__(self, text, born_at, expires_at, y, slot):
        self.text = text
        self.born_at = born_at
        self.expires_at = expires_at
        self.y = float(y)
        self.slot = slot          # 0 = newest (bottom); increments as newer lines arrive
        self.dying = False


class _IdleAnim:
    """Mesh-graph attract loop. Owned by Screen; Screen draws the banner.

    Per cycle:
      1. Flip banner side, regen node layout + k-nearest adjacency
      2. Pick mode (routed | bfs), pick random source
      3. ROUTED: random walk from src, no revisits; one packet hops it
      4. BFS: src emits to all neighbours; each arrival re-emits ONLY on
         first-sight (MeshCore repeater rule — duplicates die at the node)
      5. Rest briefly (banner + constellation only), then restart
    """

    N_COLS = 4
    N_ROWS = 2
    K_NEIGHBORS = 3
    HOP_DURATION = 0.7
    REST = 1.0
    MAX_PACKETS = 32

    def __init__(self, screen):
        self.screen = screen
        self.mode = None
        self.nodes = []
        self.adj = []
        self.state = "rest"
        self.state_until = 0.0
        self.packets = []         # (from_idx, to_idx, hop_start_t)
        self.seen = set()
        self.path = []
        self.path_i = 0
        self._regen_layout()

    def _regen_layout(self):
        w, h = self.screen.width, self.screen.height
        margin_x = 8
        margin_y = 2
        log_top, log_bot = self.screen._log_area()
        top = log_top + margin_y
        bot = log_bot - margin_y
        cw = (w - 2 * margin_x) / self.N_COLS
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
                adj[j].add(i)
        self.nodes = nodes
        self.adj = [sorted(s) for s in adj]

    def _start_cycle(self, t):
        self.screen._flip_banner()
        self._regen_layout()
        self.mode = random.choice(("routed", "bfs"))
        self.seen = set()
        self.packets = []
        src = random.randrange(len(self.nodes))
        self.seen.add(src)
        if self.mode == "routed":
            self.path = self._random_walk(src)
            if len(self.path) < 2:
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

    def render(self, draw, t):
        for i, (x, y) in enumerate(self.nodes):
            if i in self.seen:
                draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=255)
            else:
                draw.point((x, y), fill=255)

        if self.state == "rest":
            if t >= self.state_until:
                self._start_cycle(t)
            return

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
                continue
            if self.mode == "routed":
                nxt = self.path_i + 2
                if nxt < len(self.path) and len(new_packets) < self.MAX_PACKETS:
                    self.path_i += 1
                    new_packets.append((to_i, self.path[nxt], t))
            else:
                for nbr in self.adj[to_i]:
                    if len(new_packets) >= self.MAX_PACKETS:
                        break
                    new_packets.append((to_i, nbr, t))

        self.packets = new_packets
        if not self.packets:
            self.state = "rest"
            self.state_until = t + self.REST


class Screen:
    def __init__(self, oled, width, height):
        self.oled = oled
        self.width = width
        self.height = height
        self.font = _load_font()
        self._img = Image.new("1", (width, height))
        self._draw = ImageDraw.Draw(self._img)
        # Banner state (shared across all modes)
        tmp = ImageDraw.Draw(Image.new("1", (width, height)))
        bbox = tmp.textbbox((0, 0), BANNER_TEXT, font=self.font)
        self._banner_bbox = bbox
        self._banner_h = (bbox[3] - bbox[1]) + 2 * BANNER_PAD
        self._banner_at_top = random.random() < 0.5
        # Log state
        self._log_lines = []
        # Override (REPL / startup banner)
        self._override = None
        self._override_until = 0.0
        # Thread + lock
        self._lock = threading.Lock()
        self._idle = _IdleAnim(self)
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---- banner helpers ----

    def _log_area(self):
        """Return (top, bottom) y bounds of the non-banner region."""
        if self._banner_at_top:
            return (self._banner_h, self.height)
        return (0, self.height - self._banner_h)

    def _flip_banner(self):
        self._banner_at_top = random.random() < 0.5

    def _draw_banner(self):
        y0 = 0 if self._banner_at_top else self.height - self._banner_h
        y1 = y0 + self._banner_h - 1
        self._draw.rectangle((0, y0, self.width - 1, y1), fill=255)
        bbox = self._banner_bbox
        tw = bbox[2] - bbox[0]
        tx = (self.width - tw) // 2
        ty = y0 + BANNER_PAD - bbox[1]
        self._draw.text((tx, ty), BANNER_TEXT, font=self.font, fill=0)

    # ---- public API ----

    def push_packet(self, label, hops, rssi, duration_s):
        """Append a packet line. `hops` and `rssi` are formatted as
        "TYPE h=N RR"; rssi=None renders as "--"."""
        rssi_str = "--" if rssi is None else str(rssi)
        text = f"{label} h={hops} {rssi_str}"
        with self._lock:
            born = time.monotonic()
            _, log_bot = self._log_area()
            # Existing lines: bump each alive line's slot up by one. Dying
            # lines already have a fixed off-top target — leave them be.
            for line in self._log_lines:
                if not line.dying:
                    line.slot += 1
            new_line = _LogLine(
                text=text,
                born_at=born,
                expires_at=born + duration_s,
                y=float(log_bot),         # spawns at the bottom edge, slides up
                slot=0,
            )
            self._log_lines.append(new_line)

    def show_lines(self, lines, hold=None):
        """Static text overlay. `hold=N` auto-dismisses back to idle/log
        after N seconds; `hold=None` is persistent until `clear()`."""
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

    # ---- render loop ----

    def _loop(self):
        period = 1.0 / FRAME_RATE
        next_t = time.monotonic()
        while not self._stop:
            t = time.monotonic()
            try:
                with self._lock:
                    self._draw.rectangle((0, 0, self.width, self.height),
                                         outline=0, fill=0)
                    override_active = (
                        self._override is not None and
                        (self._override_until == 0.0 or t < self._override_until)
                    )
                    if override_active:
                        self._render_override(self._override)
                    elif self._log_lines:
                        self._tick_and_render_log(t)
                    else:
                        if self._override is not None:
                            self._override = None
                        self._idle.render(self._draw, t)
                    self._draw_banner()
                self.oled.image(self._img)
                self.oled.show()
            except Exception as e:
                print(f"screen render error: {e}", file=sys.stderr)
            next_t += period
            sleep_for = next_t - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.monotonic()

    def _render_override(self, lines):
        line_h = LINE_HEIGHT
        log_top, _ = self._log_area()
        y = log_top
        for line in lines:
            if y >= self.height:
                break
            self._draw.text((2, y), str(line), font=self.font, fill=255)
            y += line_h

    def _tick_and_render_log(self, t):
        log_top, log_bot = self._log_area()
        line_h = LINE_HEIGHT
        step = SCROLL_PX_PER_SEC / FRAME_RATE
        off_top = float(log_top - line_h - 1)   # one pixel past the top edge
        keep = []
        for line in self._log_lines:
            # Mark dead lines as dying — they get a "scroll off top" target.
            if not line.dying and t >= line.expires_at:
                line.dying = True
            # Compute this frame's target_y.
            if line.dying:
                target = off_top
            else:
                target = float(log_bot - (line.slot + 1) * line_h)
                # Pushed off the top by newer lines? Treat as dying.
                if target < log_top:
                    target = off_top
                    line.dying = True
            # Move at constant velocity toward target.
            diff = target - line.y
            if diff > step:
                line.y += step
            elif diff < -step:
                line.y -= step
            else:
                line.y = target
            # Cull when fully above the top edge.
            if line.y > off_top + 1:
                keep.append(line)
        self._log_lines = keep

        for line in self._log_lines:
            self._draw.text((2, int(line.y)), line.text,
                            font=self.font, fill=255)
