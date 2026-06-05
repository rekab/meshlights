#!/usr/bin/env python3
"""
rx_logger.py — baseline MeshCore RX logger for the meshlights art piece.

Two jobs:
  1. Prove the chain works: RAK enumerates, meshcore_py connects, events flow.
  2. Capture every RX_LOG_DATA packet to CSV so we can eyeball the real
     local traffic distribution (hop count, RSSI, SNR, route_type, payload_type)
     before designing the auto-ranging visuals.

Also logs ADVERTISEMENT events (name + lat/lon) to a second CSV, since those
are the only passive location source and we want to know how often they carry
coordinates in this environment.

Usage:
    python rx_logger.py --port /dev/ttyACM0
    python rx_logger.py --port /dev/ttyACM0 --csv-dir ./logs

Ctrl-C to stop; CSVs are flushed per-row so a kill -9 won't lose data.
"""

import argparse
import asyncio
import csv
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from meshcore import MeshCore, EventType
except ImportError:
    print("meshcore not installed. In your venv:  pip install meshcore", file=sys.stderr)
    sys.exit(1)


# route_type (bits 0-1 of header) -> human label, per the event-surface notes
ROUTE_TYPES = {0: "TC_FLOOD", 1: "FLOOD", 2: "DIRECT", 3: "TC_DIRECT"}

# payload_type (bits 2-5) -> human label, per Packet.h in the notes
PAYLOAD_TYPES = {
    0x00: "REQ", 0x01: "RESPONSE", 0x02: "TXT_MSG", 0x03: "ACK",
    0x04: "ADVERT", 0x05: "GRP_TXT", 0x06: "GRP_DATA", 0x07: "ANON_REQ",
    0x08: "PATH", 0x09: "TRACE", 0x0A: "MULTIPART", 0x0B: "CONTROL",
    0x0F: "RAW_CUSTOM",
}


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def path_bytes_count(path_hex):
    """Number of bytes in the path field (independent cross-check)."""
    if not path_hex:
        return 0
    try:
        return len(path_hex) // 2
    except TypeError:
        return 0


class Logger:
    def __init__(self, csv_dir: Path):
        csv_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.rx_path = csv_dir / f"rx_{stamp}.csv"
        self.adv_path = csv_dir / f"adverts_{stamp}.csv"

        self.rx_file = self.rx_path.open("w", newline="")
        self.adv_file = self.adv_path.open("w", newline="")

        self.rx_w = csv.writer(self.rx_file)
        self.adv_w = csv.writer(self.adv_file)

        self.rx_w.writerow([
            "recv_iso", "recv_time", "route_type", "route_label",
            "payload_type", "payload_label", "hops", "path_hash_size",
            "path_bytes", "hops_from_path", "rssi", "snr",
            "payload_length", "path", "raw_hex",
        ])
        self.adv_w.writerow([
            "recv_iso", "public_key", "adv_name", "adv_lat", "adv_lon",
            "has_latlon", "type", "flags", "last_advert",
        ])
        self.rx_file.flush()
        self.adv_file.flush()

        self.rx_count = 0
        self.adv_count = 0
        self.adv_with_loc = 0
        self.start = time.time()

    def on_rx(self, ev):
        p = ev.payload or {}
        a = ev.attributes or {}

        rt = a.get("route_type")
        pt = a.get("payload_type")
        hops = a.get("path_len")          # library already decodes this to hop count
        hash_size = a.get("path_hash_size")  # surfaced separately by meshcore_py
        path_hex = a.get("path")
        pbytes = path_bytes_count(path_hex)
        # independent estimate: bytes / hash_size (sanity vs. the library's hops)
        hops_from_path = (pbytes // hash_size) if hash_size else None
        rssi = p.get("rssi")
        snr = p.get("snr")

        self.rx_w.writerow([
            iso_now(), p.get("recv_time"), rt, ROUTE_TYPES.get(rt, "?"),
            pt, PAYLOAD_TYPES.get(pt, "?"), hops, hash_size,
            pbytes, hops_from_path, rssi, snr,
            p.get("payload_length"), path_hex, p.get("raw_hex"),
        ])
        self.rx_file.flush()

        self.rx_count += 1
        # flag only when library hops and path-derived hops disagree
        disagree = (hops is not None and hops_from_path is not None
                    and hops != hops_from_path)
        flag = " !HOPCHK" if disagree else ""
        print(
            f"RX  {ROUTE_TYPES.get(rt,'?'):8s} "
            f"{PAYLOAD_TYPES.get(pt,'?'):8s} "
            f"hops={hops if hops is not None else '?':<3} "
            f"hsz={hash_size if hash_size is not None else '?'} "
            f"pbytes={pbytes:<3} "
            f"rssi={rssi} snr={snr}{flag}  [#{self.rx_count}]"
        )

    def on_advert(self, ev):
        p = ev.payload or {}
        lat = p.get("adv_lat")
        lon = p.get("adv_lon")
        # 0.0/0.0 is ambiguous (no-location vs actually-null-island); flag it
        has_loc = bool((lat or lon))
        self.adv_w.writerow([
            iso_now(), p.get("public_key"), p.get("adv_name"),
            lat, lon, has_loc, p.get("type"), p.get("flags"),
            p.get("last_advert"),
        ])
        self.adv_file.flush()

        self.adv_count += 1
        if has_loc:
            self.adv_with_loc += 1
        name = p.get("adv_name") or "(unnamed)"
        loc = f"{lat:.5f},{lon:.5f}" if has_loc else "no-loc"
        print(f"ADV {name!r:24s} {loc}  [adv#{self.adv_count}, w/loc={self.adv_with_loc}]")

    def summary(self):
        dur = time.time() - self.start
        rate = self.rx_count / dur * 60 if dur > 0 else 0
        print("\n--- summary ---")
        print(f"duration:    {dur:.0f}s")
        print(f"rx packets:  {self.rx_count}  (~{rate:.1f}/min)")
        print(f"adverts:     {self.adv_count}  (with location: {self.adv_with_loc})")
        print(f"rx csv:      {self.rx_path}")
        print(f"adv csv:     {self.adv_path}")

    def close(self):
        self.rx_file.close()
        self.adv_file.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0", help="serial port of the RAK")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--csv-dir", default="./logs")
    ap.add_argument("--debug", action="store_true", help="meshcore library debug")
    ap.add_argument("--no-advert", action="store_true",
                    help="skip startup + keepalive adverts (RX may go dormant via AGC stick)")
    ap.add_argument("--keepalive-sec", type=int, default=300,
                    help="seconds between keepalive adverts to prevent AGC-stick RX dormancy "
                         "(MeshCore #1209); 0 to disable. Default 300.")
    args = ap.parse_args()

    logger = Logger(Path(args.csv_dir))

    print(f"connecting to {args.port} @ {args.baud} ...")
    try:
        mc = await MeshCore.create_serial(args.port, args.baud, debug=args.debug)
    except Exception as e:
        print(f"connect failed: {e}", file=sys.stderr)
        print("checks: is the RAK at this port? (ls /dev/ttyACM*) "
              "are you in the 'dialout' group? (groups)", file=sys.stderr)
        logger.close()
        sys.exit(1)

    print("connected. self_info:", getattr(mc, "self_info", None))
    print("subscribing to RX_LOG_DATA and ADVERTISEMENT. Ctrl-C to stop.\n")

    mc.subscribe(EventType.RX_LOG_DATA, logger.on_rx)
    mc.subscribe(EventType.ADVERTISEMENT, logger.on_advert)

    # Startup advert + periodic keepalive: WORKAROUND for the SX126x AGC-stick
    # bug (MeshCore #1209). On RAK4631 companion firmware (<= v1.15.0), the
    # receiver's analog AGC can latch into a desensitized state after a strong
    # nearby packet and never recover, so RX goes dormant until a strong signal
    # (or a TX cycle) dislodges it. The companion build never runs the periodic
    # resetAGC() that repeaters get. A TX (send_advert) cycles the PA/standby
    # enough to unstick it. We advert once at startup and then every
    # --keepalive-sec to keep RX alive on an unattended run.
    #
    # REAL FIX (not this): build companion firmware v1.14.1+ with
    #   int getAGCResetInterval() const override { return 4000; }
    # in examples/companion_radio/MyMesh.h — gives continuous immunity, zero TX.
    #
    # NOTE: these keepalive adverts are this node's OWN zero-hop adverts. Any
    # downstream consumer (e.g. the art piece's walk-up detector) MUST filter
    # out our own pubkey or it'll mistake the keepalive for a person walking up.
    async def send_keepalive_advert(reason):
        try:
            r = await mc.commands.send_advert(flood=False)
            if getattr(r, "type", None) == EventType.ERROR:
                print(f"{reason} advert error: {r.payload}", file=sys.stderr)
                return False
            return True
        except Exception as e:
            print(f"{reason} advert failed: {e}", file=sys.stderr)
            return False

    if not args.no_advert:
        if await send_keepalive_advert("startup"):
            print("startup advert sent (flood=False) — RX armed / AGC unstuck")

    # graceful shutdown on Ctrl-C
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # heartbeat so a quiet mesh doesn't look like a hang
    async def heartbeat():
        while not stop.is_set():
            await asyncio.sleep(30)
            if not stop.is_set():
                dur = time.time() - logger.start
                print(f"... alive {dur:.0f}s, rx={logger.rx_count} adv={logger.adv_count}")

    # periodic keepalive advert to keep the AGC unstuck (see comment above)
    async def keepalive():
        last_rx_seen = logger.rx_count
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.keepalive_sec)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            # diagnostic: did RX stall since last keepalive? (AGC-stick signature)
            stalled = (logger.rx_count == last_rx_seen)
            await send_keepalive_advert("keepalive")
            tag = " (rx was STALLED — likely AGC stick)" if stalled else ""
            print(f"... keepalive advert sent, rx={logger.rx_count}{tag}")
            last_rx_seen = logger.rx_count

    tasks = [asyncio.create_task(heartbeat())]
    if not args.no_advert and args.keepalive_sec > 0:
        tasks.append(asyncio.create_task(keepalive()))

    await stop.wait()
    for t in tasks:
        t.cancel()

    logger.summary()
    logger.close()
    try:
        await mc.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
