#!/usr/bin/env python3
"""
set_pnw_radio.py — set the RAK to the Seattle/PNW MeshCore radio preset.

PNW preset (per project protocol reference, late 2025):
    freq = 910.525 MHz, bw = 62.5 kHz, sf = 7, cr = 5

Run once. Verifies by re-reading self_info after the change.

    python set_pnw_radio.py --port /dev/ttyACM0
"""

import argparse
import asyncio
import sys

from meshcore import MeshCore, EventType

FREQ_MHZ = 910.525
BW_KHZ = 62.5
SF = 7
CR = 5


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    mc = await MeshCore.create_serial(args.port, args.baud)

    before = getattr(mc, "self_info", {}) or {}
    print("before:", {k: before.get(k) for k in
                       ("radio_freq", "radio_bw", "radio_sf", "radio_cr")})

    print(f"setting radio -> freq={FREQ_MHZ} bw={BW_KHZ} sf={SF} cr={CR}")
    result = await mc.commands.set_radio(FREQ_MHZ, BW_KHZ, SF, CR)
    if result.type == EventType.ERROR:
        print(f"set_radio FAILED: {result.payload}", file=sys.stderr)
        await mc.disconnect()
        sys.exit(1)

    # self_info after set isn't always authoritative until appstart re-reads it
    await mc.commands.send_appstart()
    after = getattr(mc, "self_info", {}) or {}
    print("after: ", {k: after.get(k) for k in
                      ("radio_freq", "radio_bw", "radio_sf", "radio_cr")})

    ok = (
        abs(float(after.get("radio_freq", 0)) - FREQ_MHZ) < 0.001
        and abs(float(after.get("radio_bw", 0)) - BW_KHZ) < 0.001
        and int(after.get("radio_sf", 0)) == SF
        and int(after.get("radio_cr", 0)) == CR
    )
    print("VERIFIED OK" if ok else "WARNING: readback does not match — re-check")

    await mc.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
