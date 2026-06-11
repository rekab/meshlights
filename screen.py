"""SSD1306 OLED helper — packet log + idle attract animation.

Driven via luma.oled's native SSD1306 driver.

Wiring: VCC, GND, SCL1 (GPIO 3, pin 5), SDA1 (GPIO 2, pin 3). Pi i2c-1
must be enabled (see README "Enable i2c").

The Screen owns a background thread that redraws at ~10 fps. The display
arbitrates between three modes (highest priority wins):

  override:  show_lines() pinned an arbitrary text overlay (REPL / banners)
  log:       at least one live packet line — scrolling log dominates
  idle:      no packets; meshlights/mesh-graph attract animation runs

`push_packet(label, n_bytes, duration_s)` appends a line to the log
("TYPE NNB"). Lines obey simple 1D physics: each falls from above
the top edge under constant gravity, lands on whatever is already
stacked below it (or the screen floor), and rests there. When the
packet dies (`duration_s` elapsed) the line slides off the right edge
at constant horizontal velocity; lines that were resting on top lose
support and fall under gravity to the next floor down.

The "waiting for mesh packets" marquee banner is pinned to the bottom
of the panel and shown ONLY during the idle attract animation — it's
hidden while any packet is on the log so the lines have the full panel
height.

connect() returns a Screen on success or None if the OLED can't be reached.
Callers should treat the screen as optional so the engine/sim still runs
headless when nothing is plugged in.
"""

import random
import sys
import threading
import time

from animations import HEARTBEAT_TRAVERSAL_TIME

try:
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.oled.device import ssd1306
    from PIL import Image, ImageDraw, ImageFont
    _LUMA_AVAILABLE = True
except ImportError as e:
    print(f"screen deps missing: {e}", file=sys.stderr)
    _LUMA_AVAILABLE = False


DEFAULT_W = 128
DEFAULT_H = 64
DEFAULT_ADDR = 0x3C
FRAME_RATE = 10.0
BANNER_BASE_TEXT = "waiting for mesh packets"
N_BANNER_DOTS = 3
BANNER_DOT_SLOT_W = 8         # px per dot slot in the marquee
MARQUEE_PX_PER_SEC = 30.0     # right-to-left scroll velocity
DOT_PULSE_STEP_S = 0.35       # seconds each pulse-state holds (4 states/cycle)
NONBOLD_DOT_RADIUS = 1        # 3 px diameter — bigger than font's '.'
BOLD_DOT_RADIUS = 2           # 5 px diameter — smaller than the old 7
BANNER_BLANK_SECONDS = 1.0    # solid blank gap between marquee repeats
BANNER_PAD = 2
HEARTBEAT_TRAIL_PX = 28       # length of the comet trail behind the head pixel
                              # (≥ 1 frame of motion = ~26 px so no gaps frame-
                              # to-frame at 10 fps × ~256 px/s sweep)
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_SIZE = 11
LINE_HEIGHT = 12              # vertical step per log line (px)
GRAVITY_PX_PER_SEC2 = 280.0   # downward acceleration for falling lines
DIE_SLIDE_PX_PER_SEC = 180.0  # horizontal velocity when a line dies + scrolls off


def _load_font():
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, FONT_SIZE)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def connect(width=DEFAULT_W, height=DEFAULT_H, addr=DEFAULT_ADDR):
    """Bring up the OLED via luma.oled's native SSD1306 driver. Returns a
    Screen on success or None if the panel can't be reached."""
    if not _LUMA_AVAILABLE:
        return None
    try:
        serial = luma_i2c(port=1, address=addr)
    except Exception as e:
        print(f"i2c bus open failed: {e}  "
              f"(is i2c enabled? `sudo raspi-config nonint do_i2c 0 && sudo reboot`)",
              file=sys.stderr)
        return None
    try:
        oled = ssd1306(serial, width=width, height=height)
    except Exception as e:
        print(f"SSD1306 init failed at 0x{addr:02X}: {e}  "
              f"(check wiring; run `i2cdetect -y 1` to scan)", file=sys.stderr)
        return None
    return Screen(oled, width, height)


class _LogLine:
    __slots__ = ("text", "born_at", "expires_at", "y", "vy", "x", "dying")

    def __init__(self, text, born_at, expires_at, y):
        self.text = text
        self.born_at = born_at
        self.expires_at = expires_at
        self.y = float(y)
        self.vy = 0.0
        self.x = 0.0
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
        bbox = tmp.textbbox((0, 0), BANNER_BASE_TEXT, font=self.font)
        self._banner_bbox = bbox
        self._dot_bbox = tmp.textbbox((0, 0), ".", font=self.font)
        self._banner_h = (bbox[3] - bbox[1]) + 2 * BANNER_PAD
        self._banner_at_top = False    # banner is pinned to the bottom
        # Log state
        self._log_lines = []
        # Heartbeat-sweep state — monotonic time the sweep started, or None.
        # Engine/Sim call notify_heartbeat() to trigger this.
        self._heartbeat_start = None
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

    def _draw_heartbeat_sweep(self, t):
        """Single-pixel sweep with a short comet trail, synced to the strip
        heartbeat. Head x maps linearly to the panel width; trail extends
        HEARTBEAT_TRAIL_PX behind. Renders on the row just above the bottom
        banner and stops rendering when the traversal duration is up."""
        if self._heartbeat_start is None:
            return
        elapsed = t - self._heartbeat_start
        if elapsed < 0.0 or elapsed >= HEARTBEAT_TRAVERSAL_TIME:
            return
        progress = elapsed / HEARTBEAT_TRAVERSAL_TIME
        head_x = int(round(progress * (self.width - 1)))
        y = self.height - self._banner_h - 2     # 2 px gap above marquee
        trail_start = max(0, head_x - HEARTBEAT_TRAIL_PX)
        # 1 px tall comet trail.
        self._draw.line((trail_start, y, head_x, y), fill=255)
        # 2×2 head block so it reads as a distinct dot at the leading edge.
        self._draw.rectangle(
            (max(0, head_x - 1), y - 1, head_x, y), fill=255,
        )

    def _draw_banner(self, t):
        """White-on-black marquee pinned to the bottom of the panel:
        'waiting for mesh packets' + 3 dots, scrolling right-to-left with
        a 1 s solid-blank pause between repeats. Dots cycle a 4-state pulse
        (none → 1st → 2nd → 3rd → none); non-bold dots are small filled
        circles, the bold dot is a slightly larger filled circle."""
        # No background fill — the panel default (off/black) IS the banner
        # background. White-text-on-black matches the rest of the idle/log
        # content visually.

        bbox = self._banner_bbox
        tw = bbox[2] - bbox[0]
        y0 = 0 if self._banner_at_top else self.height - self._banner_h
        text_y = y0 + BANNER_PAD - bbox[1]

        pulse_state = int(t / DOT_PULSE_STEP_S) % (N_BANNER_DOTS + 1)
        bold_idx = pulse_state - 1   # -1 → none bold this frame

        # Vertical anchor: center on the font's natural "." baseline so the
        # bold/non-bold shapes sit on the same line, not floating.
        d = self._dot_bbox
        dot_center_y = text_y + (d[1] + d[3]) // 2

        seg_w = tw + N_BANNER_DOTS * BANNER_DOT_SLOT_W
        # Gap sized so the segment is fully off-screen for BANNER_BLANK_SECONDS:
        #   blank_time = (gap - panel_width) / velocity   →   gap = W + v * blank
        gap_w = int(self.width + MARQUEE_PX_PER_SEC * BANNER_BLANK_SECONDS)
        pattern_w = seg_w + gap_w
        offset = (t * MARQUEE_PX_PER_SEC) % pattern_w

        n_copies = (self.width // max(pattern_w, 1)) + 2
        for i in range(n_copies):
            base_x = -offset + i * pattern_w
            if base_x > self.width:
                break
            if base_x + seg_w < 0:
                continue
            self._draw.text((int(base_x), int(text_y)),
                            BANNER_BASE_TEXT, font=self.font, fill=255)
            for di in range(N_BANNER_DOTS):
                slot_x = base_x + tw + di * BANNER_DOT_SLOT_W
                cx = int(slot_x + BANNER_DOT_SLOT_W / 2)
                r = BOLD_DOT_RADIUS if di == bold_idx else NONBOLD_DOT_RADIUS
                self._draw.ellipse(
                    (cx - r, dot_center_y - r, cx + r, dot_center_y + r),
                    fill=255,
                )

    # ---- public API ----

    def push_packet(self, label, n_bytes, duration_s):
        """Append a packet line formatted as "TYPE NNB" (n_bytes = payload
        size on the wire). The line spawns just above the top of the panel
        (above any in-flight new lines) and falls under gravity onto the
        stack."""
        text = f"{label} {int(n_bytes)}B"
        with self._lock:
            born = time.monotonic()
            # Spawn at -LINE_HEIGHT, or above the highest in-flight line if
            # one is still falling. Keeps rapid-fire pushes stacking cleanly
            # instead of all spawning at the same y.
            spawn_y = float(-LINE_HEIGHT)
            for line in self._log_lines:
                if line.dying:
                    continue
                if line.y - LINE_HEIGHT < spawn_y:
                    spawn_y = line.y - LINE_HEIGHT
            self._log_lines.append(_LogLine(
                text=text,
                born_at=born,
                expires_at=born + duration_s,
                y=spawn_y,
            ))

    def notify_heartbeat(self):
        """Called by Engine/Sim when the strip's red heartbeat begins a
        traversal. The OLED renders a synced sweep dot+trail across the
        full width over HEARTBEAT_TRAVERSAL_TIME seconds, mirroring the
        strip in 1:1 timing."""
        with self._lock:
            self._heartbeat_start = time.monotonic()

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
            self.oled.clear()
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
                        # Heartbeat sweep mirrors the strip's red dot when one
                        # is in flight — drawn between constellation and banner.
                        self._draw_heartbeat_sweep(t)
                        # Banner only during idle — log gets the full panel,
                        # override owns its layout.
                        self._draw_banner(t)
                self.oled.display(self._img)
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
        y = 2          # slight top padding; banner is not drawn in this mode
        for line in lines:
            if y >= self.height:
                break
            self._draw.text((2, y), str(line), font=self.font, fill=255)
            y += line_h

    def _tick_and_render_log(self, t):
        """Physics step + render. Bottom-up processing so an upper line's
        floor calc sees the post-update y of the line directly below it,
        which keeps stacks glued together as the lower line falls. A
        resting line inherits its support's vy so the whole tower drops
        in lockstep when the bottom block tumbles."""
        line_h = LINE_HEIGHT
        dt = 1.0 / FRAME_RATE

        # Flag any line that just hit expires_at.
        for line in self._log_lines:
            if not line.dying and t >= line.expires_at:
                line.dying = True

        # Dying lines slide right at constant velocity, ignore gravity.
        # Frozen y means they don't fall through the panel — they just exit
        # via the right edge.
        for line in self._log_lines:
            if line.dying:
                line.x += DIE_SLIDE_PX_PER_SEC * dt

        # Alive lines: gravity + floor collision, bottom-up so each line
        # sees the up-to-date position of whatever's below it.
        alive_sorted = sorted(
            (l for l in self._log_lines if not l.dying),
            key=lambda l: -l.y,    # bottom-most first (largest y)
        )
        for line in alive_sorted:
            floor = float(self.height - line_h)
            support = None
            for other in self._log_lines:
                if other is line or other.dying:
                    continue
                if other.y > line.y:
                    cand = other.y - line_h
                    if cand < floor:
                        floor = cand
                        support = other
            line.vy += GRAVITY_PX_PER_SEC2 * dt
            new_y = line.y + line.vy * dt
            if new_y >= floor:
                line.y = floor
                # Inherit support's vy so stacks fall together; 0 when
                # resting on the screen floor.
                line.vy = support.vy if support is not None else 0.0
            else:
                line.y = new_y

        # Cull lines that have left the panel.
        self._log_lines = [
            l for l in self._log_lines
            if l.x < self.width and l.y < self.height
        ]

        for line in self._log_lines:
            self._draw.text((int(line.x), int(line.y)), line.text,
                            font=self.font, fill=255)
