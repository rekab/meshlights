"""Vectorized NumPy animation objects: Comet, Bloom, plus the idle heartbeat.

No per-pixel Python loops on the render path. All accumulation is additive
into an (N_PIXELS, 3) float32 framebuffer — caller clips once after composition.
"""

import math
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
# A single dim-red dot continuously ping-pongs end-to-end at roughly comet
# speed (one direction per HEARTBEAT_TRAVERSAL_TIME seconds, then bounces).
# Always rendered first into the framebuffer; active animations composite
# on top via additive blending, so during a comet you see the comet, and
# between comets the dot's still tracking back and forth.
HEARTBEAT_COLOR = np.array((50, 0, 0), dtype=np.float32)   # very dim red
HEARTBEAT_TRAVERSAL_TIME = 0.5     # seconds for one end-to-end pass (~comet speed)

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


def render_heartbeat(fb, t):
    """Render the heartbeat dot into fb. Single dim-red pixel that bounces
    between pixel 0 and the last pixel, taking HEARTBEAT_TRAVERSAL_TIME
    seconds in each direction. Runs continuously — caller composites
    everything else on top."""
    n = _POSITIONS.shape[0]
    if n < 2:
        return
    span = float(n - 1)
    # Total distance the dot has traveled since t=0, in pixel units.
    total_dist = t * (span / HEARTBEAT_TRAVERSAL_TIME)
    # Triangle wave on [0, 2*span] for ping-pong motion.
    pos_in_cycle = total_dist % (2.0 * span)
    head_pos = pos_in_cycle if pos_in_cycle <= span else (2.0 * span - pos_in_cycle)

    # Anti-aliased single pixel — sub-pixel motion stays smooth.
    head_int = int(head_pos)
    head_frac = head_pos - head_int
    if 0 <= head_int < n:
        fb[head_int] += HEARTBEAT_COLOR * (1.0 - head_frac)
    if head_frac > 0.0 and 0 <= head_int + 1 < n:
        fb[head_int + 1] += HEARTBEAT_COLOR * head_frac


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
