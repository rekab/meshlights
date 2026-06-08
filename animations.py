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
BASE_TAIL = 3.0
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
    tail_length: float       # pixels — max tail length when fully extended
    dwell: float
    transit: float
    head_brightness: float   # multiplier on head pixel only (not tail)
    start_time: float
    last_direction: float = 1.0
    sparks: list = field(default_factory=list)   # list[(pixel:int, t_spawn:float)]
    _spark_seeded: int = -1                       # highest seeded node index

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

        # Head position + visibility + current tail length. Explicit tri-branch
        # with the final dwell carved out so we never read nodes[k+1] past the
        # end. Tail length contracts during dwell (catches up to the head) and
        # extends during transit (matches head's smoothstep easing) — boundary
        # values are continuous (0 at dwell-end == transit-start, full at
        # transit-end == next dwell-start) so there's no visible snap.
        head_visible = True
        head_pos = 0.0
        direction = self.last_direction
        tail_length_now = 0.0

        if elapsed >= final_dwell_end:
            head_visible = False
            self._ensure_sparks_through(n - 1)
        elif elapsed >= final_dwell_start:
            # Final dwell — sit on last node; do NOT index nodes[k+1].
            head_pos = float(self.nodes[-1])
            dwell_progress = (elapsed - final_dwell_start) / self.dwell
            tail_length_now = self.tail_length * max(0.0, 1.0 - dwell_progress)
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
                dwell_progress = s / self.dwell
                tail_length_now = self.tail_length * (1.0 - dwell_progress)
            else:
                f = (s - self.dwell) / self.transit
                f_e = f * f * (3.0 - 2.0 * f)
                head_pos = float(self.nodes[k]) + d * f_e
                tail_length_now = self.tail_length * f_e

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

        if not head_visible:
            return

        n_px = _POSITIONS.shape[0]

        # Tail — from one pixel behind the head out to `tail_length_now`,
        # quadratic fade. Starts at dist=1 (not 0) so the head pixel stays
        # purely in head_color, preserving the head/tail color contrast.
        # Skipped entirely when the tail has contracted below one pixel.
        if tail_length_now > 1.0:
            dist = (head_pos - _POSITIONS) * direction
            mask = (dist >= 1.0) & (dist <= tail_length_now)
            if mask.any():
                span = max(tail_length_now - 1.0, 1e-6)
                fade = np.where(
                    mask,
                    (1.0 - (dist - 1.0) / span) ** 2,
                    0.0,
                ).astype(np.float32)
                fb += self.color * fade[:, None] * self.intensity

        # Head — anti-aliased single pixel at head_pos in head_color.
        # head_brightness multiplier applies HERE only (not on tail), so the
        # accent really pops.
        head_int = int(head_pos)
        head_frac = float(head_pos) - head_int
        head_amp = self.head_brightness * self.intensity
        if 0 <= head_int < n_px:
            fb[head_int] += self.head_color * ((1.0 - head_frac) * head_amp)
        if head_frac > 0.0 and 0 <= head_int + 1 < n_px:
            fb[head_int + 1] += self.head_color * (head_frac * head_amp)

    def is_done(self, t):
        elapsed = t - self.start_time
        n = len(self.nodes)
        final_dwell_end = (n - 1) * (self.dwell + self.transit) + self.dwell
        if elapsed < final_dwell_end:
            return False
        if not self.sparks:
            return True
        return all((t - ts) >= SPARK_DURATION for _, ts in self.sparks)
