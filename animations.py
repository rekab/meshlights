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
HEARTBEAT_PERIOD = 4.0
HEARTBEAT_WIDTH = 9
HEARTBEAT_COLOR = np.array((20, 30, 40), dtype=np.float32)

# These are rebuilt by configure(); leave the default-N_PIXELS values here so
# import-time code (tests, smoke checks) keeps working without configure().
HEARTBEAT_CENTER = N_PIXELS // 2
_POSITIONS = np.arange(N_PIXELS, dtype=np.float32)


def _make_heartbeat_profile(n, center, width):
    pos = np.arange(n, dtype=np.float32) - center
    half = width / 2.0
    return np.where(
        np.abs(pos) <= half,
        0.5 * (1.0 + np.cos(np.pi * pos / half)),
        0.0,
    ).astype(np.float32)


_HEARTBEAT_PROFILE = _make_heartbeat_profile(N_PIXELS, HEARTBEAT_CENTER, HEARTBEAT_WIDTH)


def configure(n_pixels):
    """Rebuild module state for a strip of n_pixels LEDs. Call ONCE at startup
    after loading config and before spawning any animations. Reassigns the
    module globals (N_PIXELS, HEARTBEAT_CENTER, _POSITIONS, _HEARTBEAT_PROFILE)
    — Comet.render and render_heartbeat resolve these via the module namespace
    at call time, so they pick up the new values."""
    global N_PIXELS, HEARTBEAT_CENTER, _POSITIONS, _HEARTBEAT_PROFILE
    N_PIXELS = int(n_pixels)
    HEARTBEAT_CENTER = N_PIXELS // 2
    _POSITIONS = np.arange(N_PIXELS, dtype=np.float32)
    _HEARTBEAT_PROFILE = _make_heartbeat_profile(N_PIXELS, HEARTBEAT_CENTER, HEARTBEAT_WIDTH)


def render_heartbeat(fb, t):
    breath = (math.sin(2.0 * math.pi * t / HEARTBEAT_PERIOD) + 1.0) * 0.5
    fb += HEARTBEAT_COLOR * _HEARTBEAT_PROFILE[:, None] * breath


@dataclass
class Bloom:
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


@dataclass
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

        # Sparks (vectorized fade + scatter add) — persistent route trail
        # stays in the tail/payload color, not the head accent.
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
                np.add.at(fb, ap, self.color * fade[:, None] * self.intensity)
            # prune dead sparks (cheap; n is bounded by hop count)
            if not alive.all():
                self.sparks = [s for s, ok in zip(self.sparks, alive) if ok]

        n_px = _POSITIONS.shape[0]

        # Lazy-init the per-pixel last-visit array to match current strip
        # length (animations.configure() may have rebuilt N_PIXELS).
        if self._trail_times is None or self._trail_times.shape[0] != n_px:
            self._trail_times = np.full(n_px, -1e9, dtype=np.float64)

        # Compute head positioning + anti-aliased weights up front. head_w0
        # and head_w1 are how much of the head color goes to head_int and
        # head_int+1 respectively; they sum to 1.0. Trail renders at the
        # COMPLEMENT of these weights at the same pixels — so as the head
        # moves off a pixel, head's contribution fades out while trail's
        # contribution fades in, keeping total brightness monotonic across
        # the boundary. Without this, slow head motion produces a visible
        # "pop": pixel dims to almost 0 as head_w → 0, then snaps back to
        # full trail brightness the moment head_int increments.
        head_int = -1
        head_frac = 0.0
        head_w0 = 0.0
        head_w1 = 0.0
        if head_visible:
            head_int = int(head_pos)
            head_frac = float(head_pos) - head_int
            head_w0 = 1.0 - head_frac
            head_w1 = head_frac

            # Mark every integer pixel SWEPT between the previous and current
            # head_pos with the current time — not just the head pixel — so
            # fast heads (movement > 1 px/frame) don't leave gaps. A 70-px
            # sweep over a 0.5s transit covers ~2.3 px per frame at 60fps;
            # without span-marking, every-other pixel gets skipped and the
            # tail looks like a discrete 2-px blob.
            prev = self._prev_head_pos
            lo = head_pos if prev is None else min(prev, head_pos)
            hi = head_pos if prev is None else max(prev, head_pos)
            p_start = max(0, int(math.floor(lo)))
            p_end = min(n_px - 1, int(math.ceil(hi)))
            if p_end >= p_start:
                self._trail_times[p_start:p_end + 1] = t
            self._prev_head_pos = head_pos

        # Render trail with temporal quadratic fade. At the head pixel(s),
        # scale by the complement of head's anti-aliased weight so the head
        # accent stays pure where the head is, and the trail "fills in" as
        # the head moves off — net brightness change at any pixel is
        # monotonic across the head's traversal.
        trail_age = t - self._trail_times
        trail_alive = trail_age < self.tail_duration
        if trail_alive.any():
            trail_fade = np.where(
                trail_alive,
                (1.0 - trail_age / self.tail_duration) ** 2,
                0.0,
            ).astype(np.float32)
            if head_visible:
                if 0 <= head_int < n_px:
                    trail_fade[head_int] *= (1.0 - head_w0)
                if head_w1 > 0.0 and 0 <= head_int + 1 < n_px:
                    trail_fade[head_int + 1] *= (1.0 - head_w1)
            fb += self.color * trail_fade[:, None] * self.intensity

        if not head_visible:
            return

        # Render head with anti-aliased weights. head_brightness multiplier
        # applies HERE only (not on the trail).
        head_amp = self.head_brightness * self.intensity
        if 0 <= head_int < n_px:
            fb[head_int] += self.head_color * (head_w0 * head_amp)
        if head_w1 > 0.0 and 0 <= head_int + 1 < n_px:
            fb[head_int + 1] += self.head_color * (head_w1 * head_amp)

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
