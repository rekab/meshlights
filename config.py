"""Config loading + small pure helpers: palette, repeater hash, RSSI ramp."""

import tomllib
from dataclasses import dataclass


# Tail palette. Each color is a fully saturated hue (one channel near 0)
# so the LED actually reads as that hue at low APA102 brightness — mid-mush
# values like (180,180,80) clip and desaturate toward white when the engine
# is run loud. Eight payload types → eight distinct positions around the
# hue wheel. Tails are NOT multiplied by head_brightness, so we can use
# the full 0..255 range here.
PALETTE = {
    0x00: (255, 100,   0),   # REQ      — pure orange    (30°)
    0x01: (160,   0, 255),   # RESPONSE — pure violet    (270°)
    0x02: (  0,  80, 255),   # TXT_MSG  — cobalt blue    (225°)
    0x03: (255, 220,   0),   # ACK      — pure amber     (52°)
    0x04: ( 40, 255,   0),   # ADVERT   — pure green     (130°)
    0x05: (255,   0, 180),   # GRP_TXT  — hot magenta    (318°)
    0x07: (255,   0,  30),   # ANON_REQ — pure red       (7°)
    0x08: (  0, 220, 255),   # PATH     — pure cyan      (188°)
}
UNKNOWN_COLOR = (110, 110, 110)        # neutral gray (no hue claim for unknown types)
WALKUP_COLOR = (255, 255, 255)         # reserved — never in PALETTE

# Comet HEAD accent colors. Each entry is the rough hue complement of its
# PALETTE counterpart (across the wheel) — picked so the head reads as a
# clearly different hue from the tail it's leading.
#
# IMPORTANT: every channel here is ≤ 127 so the head_brightness multiplier
# (currently 2.0) can boost without clipping a channel above 255 and
# desaturating toward white. At 2× they land at peak hue saturation; at
# 1× they're dim but still hue-correct.
HEAD_PALETTE = {
    0x00: (  0,  60, 127),   # REQ      head: blue       (complement of orange)
    0x01: ( 80, 127,   0),   # RESPONSE head: lime       (complement of violet)
    0x02: (127,  80,   0),   # TXT_MSG  head: amber      (complement of blue)
    0x03: ( 50,   0, 127),   # ACK      head: indigo     (complement of amber)
    0x04: (127,   0,  60),   # ADVERT   head: pink       (complement of green)
    0x05: (  0, 127,  50),   # GRP_TXT  head: green      (complement of magenta)
    0x07: (  0, 110, 127),   # ANON_REQ head: cyan       (complement of red)
    0x08: (127,  60,   0),   # PATH     head: orange     (complement of cyan)
}
UNKNOWN_HEAD_COLOR = (100, 100, 100)   # neutral gray (matches UNKNOWN_COLOR; avoid white which is reserved for walk-ups)


@dataclass(frozen=True)
class Config:
    pixels: int                  # actual LED count on the strip
    tail_duration: float         # multiplier on BASE_TAIL_DURATION (sec)
    speed: float
    head_brightness: float
    walkup_rssi_threshold: int
    rssi_ramp_gamma: float
    brightness: float            # 0.0..1.0 → APA102 per-LED 5-bit brightness byte
    walkup_peak: float           # peak intensity for the white walk-up bloom
    dim_bloom_peak: float        # peak intensity for dim zero-hop blooms


def load_config(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    strip = data.get("strip", {})
    bloom = data.get("bloom", {})
    return Config(
        pixels=int(strip.get("pixels", 144)),
        tail_duration=float(data["comet"]["tail_duration"]),
        speed=float(data["comet"]["speed"]),
        head_brightness=float(data["comet"]["head_brightness"]),
        walkup_rssi_threshold=int(data["walkup"]["rssi_threshold"]),
        rssi_ramp_gamma=float(data["rssi_ramp"]["gamma"]),
        brightness=float(strip.get("brightness", 0.25)),
        walkup_peak=float(bloom.get("walkup_peak", 0.6)),
        dim_bloom_peak=float(bloom.get("dim_peak", 0.25)),
    )


_PIX_CACHE = {}

def byte_to_pixel(byte_hex, n_pixels):
    key = (byte_hex, n_pixels)
    p = _PIX_CACHE.get(key)
    if p is None:
        p = ((int(byte_hex, 16) * 2654435761) & 0xFFFFFFFF) % n_pixels
        _PIX_CACHE[key] = p
    return p


def rssi_to_intensity(rssi, gamma):
    # Normalize dBm in [-110, -30] to [0, 1] then apply gamma curve.
    # gamma > 1 expands contrast — strong signals stay strong, weak get
    # weaker → more visual "drama" per the spec.
    if rssi is None:
        return 0.5
    norm = (rssi - (-110)) / ((-30) - (-110))
    if norm < 0.0:
        norm = 0.0
    elif norm > 1.0:
        norm = 1.0
    return norm ** gamma
