"""Config loading + small pure helpers: palette, repeater hash, RSSI ramp."""

import tomllib
from dataclasses import dataclass


PALETTE = {
    0x00: (255, 180, 70),    # REQ
    0x01: (180, 110, 255),   # RESPONSE
    0x02: (60, 150, 255),    # TXT_MSG
    0x03: (240, 240, 160),   # ACK
    0x04: (80, 255, 140),    # ADVERT
    0x05: (60, 150, 255),    # GRP_TXT
    0x07: (255, 120, 40),    # ANON_REQ
    0x08: (70, 220, 230),    # PATH
}
UNKNOWN_COLOR = (150, 150, 150)
WALKUP_COLOR = (255, 255, 255)   # reserved — never in PALETTE


@dataclass(frozen=True)
class Config:
    pixels: int                  # actual LED count on the strip
    tail_length: float
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
        tail_length=float(data["comet"]["tail_length"]),
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
