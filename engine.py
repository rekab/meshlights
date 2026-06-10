#!/usr/bin/env python3
"""Meshlights — drives an APA102 LED strip from live MeshCore RX.

Subscribes to EventType.RX_LOG_DATA, spawns one animation per packet, composites
all live objects into a NumPy framebuffer, pushes frames to the strip via
hardware SPI0. Renders at ~60fps while animations are live and drops to ~10fps
during dormancy (idle heartbeat only) — battery installation budget.
"""

import argparse
import asyncio
import signal
import sys
import time
import traceback

import numpy as np

try:
    from meshcore import MeshCore, EventType
except ImportError:
    print("meshcore not installed:  pip install meshcore", file=sys.stderr)
    sys.exit(1)

try:
    import board
    import adafruit_dotstar
except ImportError as e:
    print(f"DotStar/blinka import failed: {e}", file=sys.stderr)
    print("  pip install adafruit-circuitpython-dotstar adafruit-blinka", file=sys.stderr)
    sys.exit(1)

import animations
from config import (
    HEAD_PALETTE, PALETTE, PAYLOAD_LABELS, UNKNOWN_COLOR, UNKNOWN_HEAD_COLOR,
    WALKUP_COLOR, byte_to_pixel, load_config, rssi_to_intensity,
)
from animations import (
    BASE_DWELL, BASE_TAIL_DURATION, BASE_TRANSIT,
    DIM_BLOOM_DURATION, WALKUP_BLOOM_DURATION,
    Bloom, Comet, Walkup, render_heartbeat,
)


PAYLOAD_ADVERT = 0x04


class Engine:
    def __init__(self, cfg, debug=False):
        self.cfg = cfg
        self.debug = debug
        self.active = []
        self.new_packet = asyncio.Event()
        self.shutdown = asyncio.Event()
        # Suppression window for own keepalive adverts (echo back as RX).
        self.keepalive_until = 0.0
        self.rx_count = 0
        # Liveness counters — surfaced by the alive-log task and used to
        # detect a frozen render loop (previously these silently died on an
        # animation exception, leaving the strip stuck on its last frame).
        self.render_count = 0

    def on_rx(self, ev):
        p = ev.payload or {}
        a = ev.attributes or {}
        payload_type = a.get("payload_type")
        hops = a.get("path_len")
        path_hex = a.get("path") or ""
        rssi = p.get("rssi")
        now = time.monotonic()

        # Drop our own keepalive adverts (zero-hop ADVERT inside the post-TX
        # window) — without this, the walk-up detector trips on our own echo.
        if payload_type == PAYLOAD_ADVERT and (hops == 0 or hops is None) \
                and now < self.keepalive_until:
            return

        color = PALETTE.get(payload_type, UNKNOWN_COLOR)
        head_color = HEAD_PALETTE.get(payload_type, UNKNOWN_HEAD_COLOR)
        color_arr = np.array(color, dtype=np.float32)
        head_color_arr = np.array(head_color, dtype=np.float32)
        intensity = rssi_to_intensity(rssi, self.cfg.rssi_ramp_gamma)
        kind = "?"

        if not hops:
            # Hop-0: no path to trace.
            if rssi is not None and rssi > self.cfg.walkup_rssi_threshold:
                self.active.append(Walkup(
                    color=np.array(WALKUP_COLOR, dtype=np.float32),
                    peak=self.cfg.walkup_peak,
                    duration=WALKUP_BLOOM_DURATION,
                    start_time=now,
                ))
                kind = "WALKUP"
            else:
                self.active.append(Bloom(
                    color=color_arr,
                    peak=self.cfg.dim_bloom_peak,
                    duration=DIM_BLOOM_DURATION,
                    start_time=now,
                ))
                kind = "DIM_BLOOM"
        else:
            # Hop-N: trace the path. Take the first `hops` single bytes of
            # path_hex (NOT chunked by path_hash_size — see plan rationale:
            # path_bytes != hops*hash_size in 122/433 captured packets, but
            # path_len is authoritative and the validated simulator uses
            # 1-byte nodes regardless of hash_size).
            need = hops * 2
            if len(path_hex) < need:
                if self.debug:
                    print(f"WARN short path: hops={hops} path_hex_len={len(path_hex)}")
                available = len(path_hex) // 2
                if available < 1:
                    return
                hops = available
            nodes = np.array(
                [byte_to_pixel(path_hex[2*i:2*i+2], self.cfg.pixels)
                 for i in range(hops)],
                dtype=np.int64,
            )
            self.active.append(Comet(
                nodes=nodes,
                color=color_arr,
                head_color=head_color_arr,
                intensity=intensity,
                tail_duration=BASE_TAIL_DURATION * self.cfg.tail_duration,
                dwell=BASE_DWELL / self.cfg.speed,
                transit=BASE_TRANSIT / self.cfg.speed,
                head_brightness=self.cfg.head_brightness,
                start_time=now,
            ))
            kind = f"COMET[{hops}]"

        self.rx_count += 1
        self.new_packet.set()
        if self.debug:
            label = PAYLOAD_LABELS.get(payload_type, "?")
            rssi_str = f"{rssi}" if rssi is not None else "?"
            print(f"RX  {label:8s} hops={hops if hops is not None else '?':<2} "
                  f"rssi={rssi_str:<5} → {kind} [#{self.rx_count}]")

    async def render_loop(self, strip, pixel_view):
        fb = np.zeros((self.cfg.pixels, 3), dtype=np.float32)
        while not self.shutdown.is_set():
            try:
                t = time.monotonic()
                fb.fill(0.0)
                # Heartbeat is always rendered FIRST, underneath everything
                # else. Active animations composite on top via additive blending.
                render_heartbeat(fb, t)
                for obj in list(self.active):
                    # Per-animation try/except: a broken Comet/Bloom/Walkup
                    # can't take down the loop. Log the traceback, drop the
                    # bad object from active so we don't keep crashing on it.
                    try:
                        obj.render(fb, t)
                        if obj.is_done(t):
                            self.active.remove(obj)
                    except Exception as e:
                        print(f"render error on {type(obj).__name__}: {e}",
                              file=sys.stderr)
                        traceback.print_exc()
                        try:
                            self.active.remove(obj)
                        except ValueError:
                            pass
                # Heartbeat moves continuously, so we always render at 60fps.
                # (Previously dropped to 10fps idle for battery, but a 2px/frame
                # moving dot at 10fps would strobe; 60fps keeps it smooth.)
                target_dt = 1.0 / 60.0

                np.clip(fb, 0.0, 255.0, out=fb)
                fb_u8 = fb.astype(np.uint8)
                # Vectorized write into the DotStar buffer (no per-pixel Python loop).
                pixel_view[:, 1] = fb_u8[:, 2]   # B
                pixel_view[:, 2] = fb_u8[:, 1]   # G
                pixel_view[:, 3] = fb_u8[:, 0]   # R
                try:
                    strip.show()
                except Exception as e:
                    print(f"strip.show() failed: {e}", file=sys.stderr)

                self.render_count += 1

                try:
                    await asyncio.wait_for(self.new_packet.wait(), timeout=target_dt)
                except asyncio.TimeoutError:
                    pass
                self.new_packet.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Loop-body backstop: anything not caught above gets logged and
                # we keep going. Without this, asyncio swallows the exception,
                # the task dies, and the strip freezes on its last frame while
                # on_rx keeps printing packets — exactly the symptom we hit.
                print(f"render loop body error: {e}", file=sys.stderr)
                traceback.print_exc()
                await asyncio.sleep(0.1)

    async def alive_log(self, period=30.0):
        """Periodic liveness log — if render_count stops advancing, the loop
        is frozen even though the event loop is still alive."""
        start = time.monotonic()
        last_render = -1
        while not self.shutdown.is_set():
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass
            if self.shutdown.is_set():
                break
            dur = time.monotonic() - start
            stalled = " (RENDER STALLED!)" if self.render_count == last_render else ""
            print(f"... alive {dur:.0f}s  rx={self.rx_count}  "
                  f"render={self.render_count}  active={len(self.active)}{stalled}",
                  file=sys.stderr)
            last_render = self.render_count


def setup_strip(brightness, n_pixels):
    strip = adafruit_dotstar.DotStar(
        board.SCK, board.MOSI, n_pixels,
        brightness=1.0, auto_write=False,
    )
    # Locate the internal wire-format buffer that show() transmits.
    # Current adafruit_pixelbuf (the parent class) uses
    # `_post_brightness_buffer`; older standalone adafruit_dotstar versions
    # used `_buffer` or `_buf`. We init with brightness=1.0, which hits the
    # setter's <0.001 early-return, so `_pre_brightness_buffer` stays None
    # and direct writes to `_post_brightness_buffer` go straight to SPI.
    buf_attr = next(
        (name for name in ("_post_brightness_buffer", "_buffer", "_buf")
         if hasattr(strip, name)),
        None,
    )
    if buf_attr is None:
        raise RuntimeError(
            "adafruit_dotstar/pixelbuf has no recognized wire-buffer "
            "attribute on this version — the engine writes directly to "
            "that buffer to avoid per-pixel Python loops on the hot path. "
            "Pin a known-good version in pyproject.toml and re-check."
        )
    raw = getattr(strip, buf_attr)
    offset = getattr(strip, "_offset", 4)   # bytes before the LED data area
    needed = offset + 4 * n_pixels
    if len(raw) < needed:
        raise RuntimeError(
            f"DotStar {buf_attr} too small: {len(raw)} < {needed}"
        )
    buf = np.frombuffer(raw, dtype=np.uint8)
    pixel_view = buf[offset:offset + 4 * n_pixels].reshape(n_pixels, 4)
    # APA102 per-LED brightness byte: top 3 bits MUST be 0b111, low 5 bits
    # are the 0–31 brightness. Set ONCE at init — this is the primary power
    # knob (per-LED current scales with it). 0 = LEDs off entirely.
    b5 = max(0, min(31, int(round(31 * brightness))))
    pixel_view[:, 0] = 0xE0 | b5
    return strip, pixel_view


async def send_keepalive(mc, engine, reason):
    # Set the suppression window BEFORE TX so the echo lands inside it.
    engine.keepalive_until = time.monotonic() + 3.0
    try:
        r = await mc.commands.send_advert(flood=False)
        if getattr(r, "type", None) == EventType.ERROR:
            print(f"{reason} advert error: {r.payload}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"{reason} advert failed: {e}", file=sys.stderr)
        return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-keepalive", action="store_true",
                    help="disable the AGC-stick keepalive advert "
                         "(RX may go dormant — see MeshCore #1209)")
    ap.add_argument("--keepalive-sec", type=int, default=300)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.debug:
        print("config:", cfg)

    animations.configure(cfg.pixels)
    strip, pixel_view = setup_strip(cfg.brightness, cfg.pixels)
    print(f"strip up ({cfg.pixels} px, hardware SPI0, brightness={cfg.brightness:.2f})")

    print(f"connecting to {args.port} @ {args.baud} ...")
    try:
        mc = await MeshCore.create_serial(args.port, args.baud, debug=args.debug)
    except Exception as e:
        print(f"connect failed: {e}", file=sys.stderr)
        print("checks: is the RAK at this port? (ls /dev/ttyACM*) "
              "in the 'dialout' group? (groups)", file=sys.stderr)
        sys.exit(1)
    print("connected.")

    engine = Engine(cfg, debug=args.debug)
    mc.subscribe(EventType.RX_LOG_DATA, engine.on_rx)
    print("subscribed to RX_LOG_DATA. Ctrl-C to stop.\n")

    if not args.no_keepalive:
        if await send_keepalive(mc, engine, "startup"):
            print("startup advert sent — RX armed / AGC unstuck")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.shutdown.set)

    async def keepalive_worker():
        while not engine.shutdown.is_set():
            try:
                await asyncio.wait_for(engine.shutdown.wait(),
                                       timeout=args.keepalive_sec)
            except asyncio.TimeoutError:
                pass
            if engine.shutdown.is_set():
                break
            await send_keepalive(mc, engine, "keepalive")

    def _render_died(task):
        # If the render task ever exits while we're not shutting down,
        # log loudly (asyncio normally swallows task exceptions) and trip
        # shutdown so the user notices instead of staring at a frozen strip.
        if engine.shutdown.is_set():
            return
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            print(f"\nFATAL: render loop crashed: {exc!r}", file=sys.stderr)
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        else:
            print("\nFATAL: render loop exited unexpectedly "
                  "(no exception)", file=sys.stderr)
        engine.shutdown.set()

    render_task = asyncio.create_task(engine.render_loop(strip, pixel_view))
    render_task.add_done_callback(_render_died)
    tasks = [render_task, asyncio.create_task(engine.alive_log())]
    if not args.no_keepalive and args.keepalive_sec > 0:
        tasks.append(asyncio.create_task(keepalive_worker()))

    await engine.shutdown.wait()
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    # Blank the strip on the way out.
    try:
        pixel_view[:, 1:] = 0
        strip.show()
    except Exception:
        pass

    try:
        await mc.disconnect()
    except Exception:
        pass
    print("\nshutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
