"""Vectorized NumPy animation objects: Comet, Bloom, plus the idle heartbeat.

No per-pixel Python loops on the render path. All accumulation is additive
into an (N_PIXELS, 3) float32 framebuffer — caller clips once after composition.
"""

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np


# Default strip length. Override at startup with configure(n_pixels) — see
# below — so all derived constants (_POSITIONS, _HEARTBEAT_PROFILE,
# HEARTBEAT_CENTER) get rebuilt to match the actual hardware.
N_PIXELS = 144

# Base timings (config multipliers scale these)
BASE_DWELL = 0.15
BASE_TRANSIT = 0.25
BASE_TAIL_DURATION = 0.2     # seconds — base time a visited pixel stays lit before fading
SPARK_DURATION = 1.1
WALKUP_BLOOM_DURATION = 1.5
DIM_BLOOM_DURATION = 1.0

# --- Heartbeat ---
# A single dim-red pixel sweeps end-to-end once per HEARTBEAT_CYCLE,
# always pixel 0 → pixel n-1, then the strip rests until the next cycle.
# Suspended entirely while comets/sparks are rendering: any in-flight
# traversal completes its current pass, but no new traversals start until
# `active` is empty. See the Heartbeat class below for the state machine.
HEARTBEAT_COLOR = np.array((50, 0, 0), dtype=np.float32)   # very dim red
HEARTBEAT_TRAVERSAL_TIME = 0.5     # seconds per end-to-end pass (~comet speed)
HEARTBEAT_CYCLE = 5.0              # seconds between traversal starts; also the
                                   # minimum delay after a comet sequence ends
                                   # before the next traversal starts

# These are rebuilt by configure(); leave the default-N_PIXELS values here so
# import-time code (tests, smoke checks) keeps working without configure().
HEARTBEAT_CENTER = N_PIXELS // 2
_POSITIONS = np.arange(N_PIXELS, dtype=np.float32)


def configure(n_pixels):
    """Rebuild module state for a strip of n_pixels LEDs. Call ONCE at startup
    after loading config and before spawning any animations. Reassigns the
    module globals (N_PIXELS, HEARTBEAT_CENTER, _POSITIONS) — Comet.render
    and render_heartbeat resolve these via the module namespace at call time,
    so they pick up the new values."""
    global N_PIXELS, HEARTBEAT_CENTER, _POSITIONS
    N_PIXELS = int(n_pixels)
    HEARTBEAT_CENTER = N_PIXELS // 2
    _POSITIONS = np.arange(N_PIXELS, dtype=np.float32)


class Heartbeat:
    """Stateful heartbeat sweeper. Engine and sim each own one and call
    `render(fb, t, busy)` every frame. busy=True (active animations are
    rendering) suppresses NEW traversal starts; an in-flight traversal
    still completes (so the dot doesn't disappear mid-sweep when a comet
    arrives). After all comets/sparks finish, the heartbeat waits
    HEARTBEAT_CYCLE seconds (5s) from the time busy last cleared before
    starting another traversal."""

    def __init__(self, on_traversal_start=None):
        # Monotonic time when the current traversal began. None when no
        # traversal is in flight.
        self._traversal_start = None
        # Earliest monotonic time the next traversal may start.
        self._next_allowed = 0.0
        # Previous frame's head_pos — used to span-mark every integer pixel
        # the head passed through this frame. Without this, the head moving
        # ~2.3 px/frame at 60fps skips pixels in between consecutive frames.
        self._prev_pos = None
        # Most recent monotonic time that busy=True. Used to enforce the
        # post-comet 5s delay (next_allowed >= last_busy + HEARTBEAT_CYCLE).
        self._last_busy = None
        # Optional callback invoked with (t,) the instant a new traversal
        # begins. Used by the OLED Screen to render a synced sweep mirror.
        self.on_traversal_start = on_traversal_start

    def render(self, fb, t, busy):
        # Track when the strip was last busy (comets/sparks rendering).
        if busy:
            self._last_busy = t

        # If a busy period just happened (or is ongoing), defer the next
        # traversal to last_busy + HEARTBEAT_CYCLE. Whichever is later
        # (regular cycle interval or post-busy delay) wins.
        if self._last_busy is not None:
            post_busy = self._last_busy + HEARTBEAT_CYCLE
            if post_busy > self._next_allowed:
                self._next_allowed = post_busy

        # Can we start a new traversal? Needs: not currently traversing,
        # nothing else rendering, and we're past the gating timestamp.
        if (self._traversal_start is None
                and not busy
                and t >= self._next_allowed):
            self._traversal_start = t
            if self.on_traversal_start is not None:
                try:
                    self.on_traversal_start(t)
                except Exception:
                    pass    # observer errors must never disrupt the heartbeat

        # Render + check completion. Order matters: we have to render the
        # frame where elapsed crosses HEARTBEAT_TRAVERSAL_TIME so the head
        # actually reaches the last pixel (progress clamped to 1.0) — only
        # THEN do we mark the traversal done. Reverse order would skip the
        # final pixel because completion would reset state before render.
        if self._traversal_start is None:
            return
        n = _POSITIONS.shape[0]
        if n < 2:
            return
        elapsed = t - self._traversal_start
        if elapsed < 0.0:
            return
        progress = min(1.0, elapsed / HEARTBEAT_TRAVERSAL_TIME)
        head_pos = progress * (n - 1)

        # Span-mark every integer pixel the head crossed between the prev
        # frame and this frame. At 2.3 px/frame, single-pixel rendering
        # would skip every other pixel — span-marking guarantees every
        # pixel is lit briefly as the head passes through it.
        prev = self._prev_pos if self._prev_pos is not None else head_pos
        lo = min(prev, head_pos)
        hi = max(prev, head_pos)
        p_start = max(0, int(math.floor(lo)))
        p_end = min(n - 1, int(math.floor(hi)))
        if p_end >= p_start:
            fb[p_start:p_end + 1] += HEARTBEAT_COLOR
        self._prev_pos = head_pos

        # Did this frame's render carry us to the end? Mark complete.
        if elapsed >= HEARTBEAT_TRAVERSAL_TIME:
            self._next_allowed = self._traversal_start + HEARTBEAT_CYCLE
            self._traversal_start = None
            self._prev_pos = None


@dataclass(eq=False)
class Bloom:
    """Dim full-strip pulse for hop-0 packets that don't meet the walk-up
    threshold. Solid color, sin-envelope rise-and-fall. The dramatic
    walk-up uses Walkup (below), not this class."""
    color: np.ndarray
    peak: float
    duration: float
    start_time: float

    def render(self, fb, t):
        age = t - self.start_time
        if age < 0.0 or age >= self.duration:
            return
        env = math.sin(math.pi * age / self.duration)
        if env <= 0.0:
            return
        fb += self.color * (env * self.peak)

    def is_done(self, t):
        return (t - self.start_time) >= self.duration

    def total_duration(self):
        return self.duration


@dataclass(eq=False)
class Walkup:
    """Walk-up showpiece: two white pulses traverse the strip from opposite
    ends, sum constructively where they overlap (peak amplification at the
    center as they cross), then continue past each other to the opposite
    ends. Rendered as (wave1 + wave2) * sin(pi*tn) * peak — additive, so
    the meeting moment is a bright flash, not a blackout. Reads as two
    waves passing through each other and amplifying as they cross."""
    color: np.ndarray            # typically WALKUP_COLOR (white)
    peak: float                  # amplitude multiplier
    duration: float
    start_time: float

    def render(self, fb, t):
        age = t - self.start_time
        if age < 0.0 or age >= self.duration:
            return
        n_px = fb.shape[0]
        tn = age / self.duration            # 0..1 across the bloom

        positions = np.arange(n_px, dtype=np.float32)
        # Each pulse traverses the full strip over `duration`, crossing at
        # tn = 0.5. Width scales with strip length so the visual reads the
        # same on a 71-px or 144-px strip (~1/5 of the strip wide).
        pos1 = tn * (n_px - 1)              # left → right
        pos2 = (1.0 - tn) * (n_px - 1)      # right → left
        sigma = max(2.0, n_px / 14.0)
        two_sigma_sq = 2.0 * sigma * sigma
        wave1 = np.exp(-((positions - pos1) ** 2) / two_sigma_sq)
        wave2 = np.exp(-((positions - pos2) ** 2) / two_sigma_sq)

        # Constructive interference: at the meeting point both gaussians
        # overlap fully, sum to 2× peak amplitude — bright flash where
        # they cross. Outer sin envelope adds graceful fade-in / fade-out
        # so the pulses don't snap on at full intensity at tn=0 or
        # suddenly cut off at tn=1.
        combined = wave1 + wave2
        env = math.sin(math.pi * tn)
        fb += self.color * (combined * env * self.peak)[:, None]

    def is_done(self, t):
        return (t - self.start_time) >= self.duration

    def total_duration(self):
        return self.duration


@dataclass(eq=False)
class Comet:
    nodes: np.ndarray        # int64 array of pixel positions, length n
    color: np.ndarray        # float32 RGB — tail / payload-type color
    head_color: np.ndarray   # float32 RGB — accent color at the head pixel
    intensity: float
    tail_duration: float     # seconds — how long a visited pixel stays lit before fading
    dwell: float
    transit: float
    head_brightness: float   # multiplier on head pixel only (not tail)
    start_time: float
    last_direction: float = 1.0
    sparks: list = field(default_factory=list)   # list[(pixel:int, t_spawn:float)]
    _spark_seeded: int = -1                       # highest seeded node index
    # Per-pixel last-visit timestamps (init lazy on first render to match
    # animations.configure()-set strip length). The tail is a TEMPORAL trail
    # — each pixel records when the head last touched it, and fades over
    # tail_duration seconds. Slow / oscillating heads produce short trails;
    # fast / wide-ranging heads produce long ones. Means the tail can never
    # extend past where the head has actually been.
    _trail_times: np.ndarray = None
    # Previous frame's head_pos — used to mark the SWEPT span (not just the
    # current pixel) into the trail each frame, so fast heads (head moving
    # > 1 px/frame) don't leave gaps in the trail.
    _prev_head_pos: float = None

    def __post_init__(self):
        if len(self.nodes) == 0:
            return
        # Seed the first-node spark at start; head arrives there at t=0.
        self.sparks.append((int(self.nodes[0]), self.start_time))
        self._spark_seeded = 0
        if len(self.nodes) >= 2:
            d = int(self.nodes[1]) - int(self.nodes[0])
            self.last_direction = float(d / abs(d)) if d != 0 else 1.0

    def _ensure_sparks_through(self, k_idx):
        # Lazily seed sparks for every node we've arrived at, up to k_idx.
        cycle = self.dwell + self.transit
        while self._spark_seeded < k_idx and self._spark_seeded < len(self.nodes) - 1:
            self._spark_seeded += 1
            self.sparks.append((
                int(self.nodes[self._spark_seeded]),
                self.start_time + self._spark_seeded * cycle,
            ))

    def render(self, fb, t):
        elapsed = t - self.start_time
        n = len(self.nodes)
        cycle = self.dwell + self.transit
        final_dwell_start = (n - 1) * cycle
        final_dwell_end = final_dwell_start + self.dwell

        # Head position + visibility. Tri-branch with the final dwell carved
        # out so we never read nodes[k+1] past the end.
        head_visible = True
        head_pos = 0.0
        direction = self.last_direction

        if elapsed >= final_dwell_end:
            head_visible = False
            self._ensure_sparks_through(n - 1)
        elif elapsed >= final_dwell_start:
            head_pos = float(self.nodes[-1])
            self._ensure_sparks_through(n - 1)
        else:
            k = int(elapsed // cycle)            # guarded: k <= n - 2 here
            s = elapsed - k * cycle
            self._ensure_sparks_through(k)
            d = int(self.nodes[k + 1]) - int(self.nodes[k])
            direction = float(d / abs(d)) if d != 0 else self.last_direction
            self.last_direction = direction
            if s < self.dwell:
                head_pos = float(self.nodes[k])
            else:
                f = (s - self.dwell) / self.transit
                f_e = f * f * (3.0 - 2.0 * f)
                head_pos = float(self.nodes[k]) + d * f_e

        # Sparks (vectorized fade + scatter add) render in HEAD color at
        # head's brightness — semantically the spark is the memory of the
        # head having rested at that node, so it carries the head accent,
        # not the tail/payload color. This also fixes muddy-dwell: at a
        # dwell node both the head and its just-emitted spark land on the
        # same pixel; if the spark were in tail color, head + spark sum
        # would mix the two complementary palette colors and wash out
        # (e.g. GRP_DATA head pink + spark teal → cyan-white). With the
        # spark in head color, the sum is just brighter head color.
        if self.sparks:
            n_sp = len(self.sparks)
            pix = np.empty(n_sp, dtype=np.int64)
            ages = np.empty(n_sp, dtype=np.float64)
            for i, (p, ts) in enumerate(self.sparks):
                pix[i] = p
                ages[i] = t - ts
            alive = (ages >= 0.0) & (ages < SPARK_DURATION)
            if alive.any():
                ap = pix[alive]
                aa = ages[alive].astype(np.float32)
                fade = (1.0 - aa / SPARK_DURATION) ** 2
                amp = self.head_brightness * self.intensity
                np.add.at(fb, ap, self.head_color * (fade * amp)[:, None])
            # prune dead sparks (cheap; n is bounded by hop count)
            if not alive.all():
                self.sparks = [s for s, ok in zip(self.sparks, alive) if ok]

        n_px = _POSITIONS.shape[0]

        # Lazy-init the per-pixel last-visit array to match current strip
        # length (animations.configure() may have rebuilt N_PIXELS).
        if self._trail_times is None or self._trail_times.shape[0] != n_px:
            self._trail_times = np.full(n_px, -1e9, dtype=np.float64)

        # Snap the head to the NEAREST integer pixel. Previously we
        # anti-aliased it across floor/ceil pixels and rendered the trail
        # at each of those with weight (1 - head_w) so brightness stayed
        # monotonic during the head's traversal. That fix worked for the
        # "pop" on slow short hops but it BLENDED the trail color into
        # the head pixel whenever the head was mid-pixel — for complementary
        # tail/head pairs (e.g. GRP_DATA: teal tail + pink head) the
        # blend dominates the un-head channels (the teal's G channel
        # swamps the pink's R/B), so the head reads as muddy purple-blue
        # instead of pink. Fast comets (head moves > 1 px/frame) are
        # mid-pixel almost every frame, so the head accent essentially
        # never showed.
        #
        # Snap-to-nearest fixes the color: head renders pure at one pixel,
        # trail excludes only that pixel. Cost is that slow head motion
        # produces a single-frame pixel→pixel "jump" at the frac=0.5
        # boundary (head pixel flips from N to N+1 in one frame, with the
        # vacated pixel switching to trail color). For our typical comet
        # speeds (~2 px/frame at peak transit), sub-pixel anti-aliasing
        # was already moot — the head jumps between pixels each frame
        # regardless.
        head_pix = -1
        if head_visible:
            head_int = int(head_pos)
            head_frac = float(head_pos) - head_int
            head_pix = head_int + 1 if head_frac >= 0.5 else head_int
            head_pix = max(0, min(n_px - 1, head_pix))

            # Mark every integer pixel SWEPT between the previous and current
            # head_pos with the current time — not just the head pixel — so
            # fast heads (movement > 1 px/frame) don't leave gaps. A 70-px
            # sweep over a 0.5s transit covers ~2.3 px per frame at 60fps;
            # without span-marking, every-other pixel gets skipped and the
            # tail looks like a discrete 2-px blob. Both endpoints use FLOOR
            # so we only mark pixels the head has actually been inside —
            # using ceil(max) was over-marking by one pixel in the direction
            # of motion, painting trail color one pixel AHEAD of where the
            # head had reached (visually: trail "running over" the head).
            prev = self._prev_head_pos
            lo = head_pos if prev is None else min(prev, head_pos)
            hi = head_pos if prev is None else max(prev, head_pos)
            p_start = max(0, int(math.floor(lo)))
            p_end = min(n_px - 1, int(math.floor(hi)))
            if p_end >= p_start:
                self._trail_times[p_start:p_end + 1] = t
            self._prev_head_pos = head_pos

        # Render trail with temporal quadratic fade. Exclude only the
        # single head pixel — head renders pure on top.
        trail_age = t - self._trail_times
        trail_alive = trail_age < self.tail_duration
        if head_visible:
            trail_alive[head_pix] = False
        if trail_alive.any():
            trail_fade = np.where(
                trail_alive,
                (1.0 - trail_age / self.tail_duration) ** 2,
                0.0,
            ).astype(np.float32)
            fb += self.color * trail_fade[:, None] * self.intensity

        if not head_visible:
            return

        # Render head: pure head_color at the snap-to-nearest pixel.
        # head_brightness multiplier applies HERE only (not on the trail).
        head_amp = self.head_brightness * self.intensity
        fb[head_pix] += self.head_color * head_amp

    def is_done(self, t):
        elapsed = t - self.start_time
        n = len(self.nodes)
        final_dwell_end = (n - 1) * (self.dwell + self.transit) + self.dwell
        # Wait for the trail to fade after the head's last update
        # (the head stops marking pixels once elapsed >= final_dwell_end)
        if elapsed < final_dwell_end + self.tail_duration:
            return False
        # And for any sparks still fading
        if not self.sparks:
            return True
        return all((t - ts) >= SPARK_DURATION for _, ts in self.sparks)

    def total_duration(self):
        # Strip animation lifetime from start_time. The head finishes at
        # final_dwell_end, then the trail fades over tail_duration; sparks
        # dominate when SPARK_DURATION > dwell + tail_duration (which is
        # ~always at default timings).
        n = len(self.nodes)
        if n == 0:
            return 0.0
        cycle = self.dwell + self.transit
        return (n - 1) * cycle + max(self.dwell + self.tail_duration,
                                      SPARK_DURATION)


@dataclass(eq=False)
class Waterfall:
    """Channel-occupancy spectrogram. The strip represents the last
    window_seconds of LoRa air, scrolling — newest packet at the right
    edge (pixel n-1), oldest scrolling off the left. Each record renders
    as an additive horizontal bar whose width is the packet's airtime and
    whose color is its payload-type hue.

    Airtime model:
        airtime_sec = overhead_sec + payload_bytes / bytes_per_sec

    `payload_bytes` here is the full on-air MeshCore frame size (MeshCore
    `payload_length`), which already includes the per-hop-growing path
    bytes. overhead_sec captures the LoRa PHY preamble + explicit-header
    cost that MeshCore doesn't surface.

    Heartbeat is suppressed in waterfall mode — the channel state itself
    indicates liveness. Engine drives this via `add()` on RX and
    `render(fb, t)` once per frame; there's exactly one Waterfall per
    Engine in waterfall mode."""
    n_pixels: int
    window_seconds: float
    bytes_per_sec: float
    overhead_sec: float
    exaggeration: float = 1.0    # visual width multiplier
    intensity: float = 1.0
    # records: deque[(t_arrival: float, airtime_sec: float, color: np.ndarray)]
    # deque (not list) so on_rx and render_loop are correct even if
    # meshcore ever dispatches off the asyncio loop — append (tail) and
    # popleft (head) are atomic single-bytecode ops in CPython under the
    # GIL. Today, dispatch is awaited from the serial reader's asyncio
    # task, so on_rx and render_loop are already serialized by
    # cooperative scheduling — this is defensive against future changes.
    records: deque = field(default_factory=deque)

    def add(self, t, payload_bytes, color):
        airtime = self.overhead_sec + max(payload_bytes, 0) / self.bytes_per_sec
        self.records.append((t, airtime, color))

    def render(self, fb, t):
        n_px = self.n_pixels
        if n_px <= 0 or self.window_seconds <= 0.0:
            return
        sec_per_px = self.window_seconds / n_px

        # Pop expired records off the head. Records are append-only and
        # arrival-time ordered, so a head past the cutoff means everything
        # before it is too. The +2s slack lets bars whose right edge has
        # scrolled past pixel 0 finish fading off cleanly instead of
        # snapping when their left edge hits the cull threshold.
        cutoff_age = self.window_seconds + 2.0
        while self.records and (t - self.records[0][0]) >= cutoff_age:
            self.records.popleft()

        # Snapshot for iteration. A concurrent append (if dispatch is
        # ever threaded) lands after the snapshot view and shows up next
        # frame — preferable to mutating-during-iterate.
        snapshot = list(self.records)
        for t_rx, airtime, color in snapshot:
            age = t - t_rx
            right_px = (n_px - 1) - age / sec_per_px
            # Position uses real airtime → time axis stays linear (a bar's
            # right edge IS where it arrived in time). Width is multiplied
            # by `exaggeration` only, so a bar reaches further into the
            # past visually than it actually occupied on air. This makes
            # collisions read truthfully: two packets that arrived close
            # enough in time to collide in real RF will visibly overlap on
            # the strip, and the strip "fills up" at roughly the real
            # channel's saturation point (real LoRa collapses around
            # 20–30% airtime utilization, so exaggeration ≈ 4–5× maps
            # "strip visually full" → "channel actually saturated").
            width_px = (airtime / sec_per_px) * self.exaggeration
            left_px = right_px - width_px

            # Sub-pixel anti-aliased horizontal bar. Each integer pixel
            # gets `color * coverage_fraction` so a 0.3-px-wide bar still
            # lights one pixel at 30% — keeps silence vs traffic readable
            # even when individual packets are narrower than a pixel
            # (typical at 60s window: a 40-byte packet's ~150 ms airtime
            # spans ~0.18 px at 71 px / 60 s).
            i_lo = max(0, int(math.floor(left_px)))
            i_hi = min(n_px - 1, int(math.floor(right_px)))
            if i_hi < i_lo:
                continue
            for i in range(i_lo, i_hi + 1):
                cov = min(i + 1, right_px) - max(i, left_px)
                if cov > 0.0:
                    fb[i] += color * (cov * self.intensity)
