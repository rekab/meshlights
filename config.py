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
    0x06: (  0, 240, 160),   # GRP_DATA — teal           (160°)
    0x07: (255,   0,  30),   # ANON_REQ — pure red       (7°)
    0x08: (  0, 220, 255),   # PATH     — pure cyan      (188°)
    0x09: ( 50, 180, 255),   # TRACE    — sky-blue       (207°)
    0x0A: ( 90,  50, 255),   # MULTIPART— indigo         (248°)
    0x0B: (180, 255,  50),   # CONTROL  — chartreuse     (89°)
    0x0F: (255,  80, 140),   # RAW_CUSTOM — rose         (341°)
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
    0x06: (127,   0,  80),   # GRP_DATA head: rose pink  (complement of teal)
    0x07: (  0, 110, 127),   # ANON_REQ head: cyan       (complement of red)
    0x08: (127,  60,   0),   # PATH     head: orange     (complement of cyan)
    0x09: (127,  80,  30),   # TRACE    head: warm coral (complement of sky-blue)
    0x0A: (110, 127,   0),   # MULTIPART head: lime-gold (complement of indigo)
    0x0B: ( 75,   0, 127),   # CONTROL  head: deep purple(complement of chartreuse)
    0x0F: ( 40, 127,  80),   # RAW_CUSTOM head: teal-green (complement of rose)
}
UNKNOWN_HEAD_COLOR = (100, 100, 100)   # neutral gray (matches UNKNOWN_COLOR; avoid white which is reserved for walk-ups)


STYLES = ("comet", "waterfall")


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
    style: str                       # "comet" (default) | "waterfall"
    waterfall_seconds: float         # window of LoRa air shown across the strip
    waterfall_bytes_per_sec: float   # marginal LoRa payload rate (B/s)
    waterfall_overhead_sec: float    # fixed LoRa PHY cost per TX (s)
    waterfall_intensity: float       # waterfall bar brightness multiplier


def load_config(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    strip = data.get("strip", {})
    bloom = data.get("bloom", {})
    wf = data.get("waterfall", {})
    style = str(data.get("style", "comet"))
    if style not in STYLES:
        raise ValueError(
            f"config: style={style!r} not recognized — must be one of "
            f"{STYLES}. Set top-level `style = \"comet\"` or "
            f"`style = \"waterfall\"` in {path}."
        )
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
        style=style,
        waterfall_seconds=float(wf.get("window_seconds", 60.0)),
        waterfall_bytes_per_sec=float(wf.get("bytes_per_sec", 340.0)),
        waterfall_overhead_sec=float(wf.get("overhead_sec", 0.030)),
        waterfall_intensity=float(wf.get("intensity", 1.0)),
    )


# payload_type → human label. Single source of truth, imported by both
# engine.py (for the debug RX log line) and utils/sim.py (for the
# `comet TYPE ...` / `randcomet K TYPE` parser). Keep in sync with
# PALETTE / HEAD_PALETTE — types listed here but missing from the
# palettes will render gray-white (UNKNOWN_COLOR fallback).
PAYLOAD_LABELS = {
    0x00: "REQ",      0x01: "RESPONSE",  0x02: "TXT_MSG",   0x03: "ACK",
    0x04: "ADVERT",   0x05: "GRP_TXT",   0x06: "GRP_DATA",  0x07: "ANON_REQ",
    0x08: "PATH",     0x09: "TRACE",     0x0A: "MULTIPART", 0x0B: "CONTROL",
    0x0F: "RAW_CUSTOM",
}


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
