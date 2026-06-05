"""Vectorized NumPy animation objects: Comet, Bloom, plus the idle heartbeat.

No per-pixel Python loops on the render path. All accumulation is additive
into a (144, 3) float32 framebuffer — caller clips once after composition.
"""

import math
from dataclasses import dataclass, field

import numpy as np


N_PIXELS = 144

# Base timings (config multipliers scale these)
BASE_DWELL = 0.15
BASE_TRANSIT = 0.25
BASE_TAIL = 3.0
SPARK_DURATION = 1.1
WALKUP_BLOOM_DURATION = 1.5
DIM_BLOOM_DURATION = 1.0
HEARTBEAT_PERIOD = 4.0
HEARTBEAT_CENTER = N_PIXELS // 2
HEARTBEAT_WIDTH = 9
HEARTBEAT_COLOR = np.array((20, 30, 40), dtype=np.float32)

_POSITIONS = np.arange(N_PIXELS, dtype=np.float32)

# Precompute heartbeat spatial profile (raised-cosine bell, 9px wide)
_HB_OFFSET = _POSITIONS - HEARTBEAT_CENTER
_HB_HALF = HEARTBEAT_WIDTH / 2.0
_HEARTBEAT_PROFILE = np.where(
    np.abs(_HB_OFFSET) <= _HB_HALF,
    0.5 * (1.0 + np.cos(np.pi * _HB_OFFSET / _HB_HALF)),
    0.0,
).astype(np.float32)


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
    color: np.ndarray        # float32 RGB
    intensity: float
    tail_length: float       # pixels
    dwell: float
    transit: float
    head_brightness: float
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

        # Head position + visibility. Explicit tri-branch with the final dwell
        # carved out so we never read nodes[k+1] past the end.
        head_visible = True
        head_pos = 0.0
        direction = self.last_direction

        if elapsed >= final_dwell_end:
            head_visible = False
            self._ensure_sparks_through(n - 1)
        elif elapsed >= final_dwell_start:
            # Final dwell — sit on last node; do NOT index nodes[k+1].
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

        # Sparks (vectorized fade + scatter add)
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

        # Tail + head — only when head is visible
        if head_visible and self.tail_length > 0.0:
            dist = (head_pos - _POSITIONS) * direction
            mask = (dist >= 0.0) & (dist <= self.tail_length)
            if mask.any():
                brightness = np.where(
                    mask,
                    (1.0 - dist / self.tail_length) ** 2,
                    0.0,
                ).astype(np.float32)
                fb += self.color * brightness[:, None] * (self.head_brightness * self.intensity)

    def is_done(self, t):
        elapsed = t - self.start_time
        n = len(self.nodes)
        final_dwell_end = (n - 1) * (self.dwell + self.transit) + self.dwell
        if elapsed < final_dwell_end:
            return False
        if not self.sparks:
            return True
        return all((t - ts) >= SPARK_DURATION for _, ts in self.sparks)
