# Meshlights

A generative LED art installation driven by live MeshCore mesh traffic. A
RAK4631 running MeshCore companion firmware is USB-tethered to a Raspberry Pi;
an APA102/DotStar strip on hardware SPI0 visualizes every received packet as
a path-trace "comet," with full-strip blooms for direct (zero-hop) arrivals
and a soft heartbeat when the mesh is quiet. Strip length is configurable
(see `strip.pixels` in `config.toml`).

## Hardware

- **Radio:** RAK4631 on `/dev/ttyACM0` running MeshCore companion firmware.
- **Strip:** APA102/DotStar on hardware SPI0 (LED count via `strip.pixels`).
- **Host:** Raspberry Pi 2 (development) / Pi Zero 2W (deployment).
- **PSU:** external 5V supply for the strip — see the power note below.

### OS image for the Pi Zero 2W

Use **Raspberry Pi OS (64-bit) Lite**. The Zero 2W is a 64-bit aarch64 SoC,
and on aarch64 essentially everything in `pyproject.toml` is a prebuilt
wheel (only `RPi.GPIO` still compiles from source, and it's tiny — seconds,
no RAM pressure). The 32-bit (armv7l) image, by contrast, has to compile a
couple of `adafruit-blinka` transitive deps (`sysv-ipc`, `dbus-fast`) from
source, and a single `gcc` invocation can blow past the Zero 2W's 512 MB
RAM into swap and lock the box up hard. Skip that whole class of problem
by starting on 64-bit Lite. "Lite" matters too — no desktop, more headroom
for the engine.

## Wiring

Four wires from the strip's **input end** (look for the arrow on the PCB —
data flows in the direction of the arrow; if you wire to the output end the
strip stays dark).

| DotStar (APA102) | Raspberry Pi |
|---|---|
| **DI** (Data In) | **GPIO 10 / MOSI** — physical pin 19 |
| **CI** (Clock In) | **GPIO 11 / SCLK** — physical pin 23 |
| **GND** | any Pi **GND** (e.g. physical pin 6) **and** PSU GND (common ground) |
| **5V / VCC** | external **5V PSU**, *not* the Pi 5V rail |

The Pi's hardware SPI0 pins are fixed in silicon — they have to be GPIO 10
and 11. Don't use other GPIOs unless you're willing to drop to the bit-banged
DotStar backend (much slower, more CPU on the hot path).

`MISO` (GPIO 9) and `CE0` (GPIO 8) are unused — APA102 is a one-way protocol
with no chip-select.

### Power note

APA102s pull ~60 mA per LED at full white — so a 144-LED strip can peak at
~8 A, a 70-LED strip at ~4 A, etc. Never power that off the Pi's 5V rail at
any meaningful length — it will brown-out the Pi instantly. Use a beefy 5V
PSU (size for your strip length with margin), and **tie the PSU ground to a
Pi GND pin** so the data/clock signals share a reference. For runs longer
than ~1 m, inject 5 V at both ends of the strip to avoid voltage droop
dimming the far end.

### 3.3 V → 5 V signalling

Pi GPIO outputs at 3.3 V; APA102 data inputs are spec'd to 5 V logic but
usually latch fine on 3.3 V at the strip's input end. If the first few LEDs
flicker or show wrong colors, drop in a 74AHCT125 level shifter on DI and CI
between the Pi and the strip — that's the canonical fix.

## Enable SPI

`sudo raspi-config` → **Interface Options** → **SPI** → **Enable**, then reboot.
Non-interactive equivalent (useful over SSH):

```
sudo raspi-config nonint do_spi 0      # 0 means "enable" in nonint, yes really
sudo reboot
```

Or add `dtparam=spi=on` to `/boot/firmware/config.txt` (note: it's
`/boot/firmware/config.txt` on current Raspberry Pi OS Bookworm; older images
used `/boot/config.txt`), then reboot.

**Verify it came up.** After reboot, `ls /dev/spidev*` should show
`/dev/spidev0.0` and `/dev/spidev0.1`. If those device nodes exist, the kernel
SPI driver is loaded and the bus is live. No nodes = it didn't take, recheck
`config.txt`.

## Permissions

Your user needs to be in the `spi` and `gpio` groups, or you run as root:

```
sudo usermod -aG spi,gpio $USER
```

then log out / in. If the engine throws a permission error opening
`/dev/spidev0.0`, that's this, not the code.

The RAK on `/dev/ttyACM0` also needs `dialout` group access:

```
sudo usermod -aG dialout $USER
```

## Install

Uses [uv](https://docs.astral.sh/uv/) for environment + dependency management.
Install uv once on the Pi:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

You'll also need the Python C headers — the `RPi.GPIO` package compiles
from source on every platform (no PyPI wheels), and the build needs
`Python.h`:

```
sudo apt install -y python3-dev
```

Then from the repo root:

```
uv sync
```

That creates `.venv/` and installs everything in `pyproject.toml`. Re-run
`uv sync` after pulling changes; `uv lock --upgrade` to refresh pinned
versions.

Requires Python ≥ 3.11 (uses stdlib `tomllib`); uv will fetch a matching
interpreter if the system Python is older. Pi OS Bookworm ships 3.11.

## Run

```
uv run python engine.py --port /dev/ttyACM0
```

Or activate the venv once per shell session and drop the prefix:

```
source .venv/bin/activate
python engine.py --port /dev/ttyACM0
```

Add `--debug` for a one-line log per received packet (handy when validating
the chain). `Ctrl-C` to stop — the strip blanks on the way out.

Other flags:

- `--config <path>` — TOML config file (default `config.toml`).
- `--baud <n>` — serial baud (default 115200).
- `--keepalive-sec <n>` — interval for the AGC-stick keepalive advert
  (default 300; see MeshCore issue #1209). `--no-keepalive` disables it,
  but the receiver may go dormant on an unattended run.

## Config

Edit `config.toml` and restart the engine. Two values
(`walkup.rssi_threshold`, `rssi_ramp.gamma`) are deliberate placeholders —
they're meant to be calibrated against an outdoor capture at the install site,
not against indoor / desk data.

### Strip length

`strip.pixels` in `config.toml` is the actual number of LEDs on the strip
you wired up. The original design target was 144, but real hardware varies —
use `utils/sim.py` (`pixel 0`, `pixel <n//2>`, `pixel <n-1>`) to map your
strip if you're not sure, then set `strip.pixels` to match. The heartbeat
centers itself based on this value, and the repeater-to-pixel hash uses it
as its modulus.

### Brightness / power tuning (battery operation)

Defaults are tuned for a **2× 18650 USB power bank** (~2 A output, ~22 Wh
energy) and **night-time art-piece viewing**, not room lighting. Three knobs:

| Knob | Default | What it does |
|---|---|---|
| `strip.brightness` | `0.25` | Maps to the APA102 per-LED 5-bit brightness byte (`int(31 * b)`). This is the primary current cap — per-LED current scales linearly with it. |
| `bloom.walkup_peak` | `0.6` | Intensity of the white walk-up bloom (the only thing that lights the entire strip at once). |
| `bloom.dim_peak` | `0.25` | Intensity of dim zero-hop blooms. |

Rough current envelope at defaults (`brightness=0.25`):

- **Idle heartbeat:** ~10 mA — basically free.
- **A comet active:** ~200–400 mA peak across the tail.
- **Walk-up bloom (worst case):** ~1.3 A peak, ~0.85 A averaged over the
  1.5 s sin envelope. Energy ≈ 0.4 mAh per bloom.

Expected runtime on a 2× 18650 bank (~3 Ah @ 5V usable): typically
**8–12 hours** in a quiet-to-moderately-busy mesh.

If you're on mains power, bump `strip.brightness` toward `1.0` for max
"loud." If the power bank's protection trips on walk-ups, drop
`bloom.walkup_peak` first (it's the only thing that exercises the worst-case
current), then `strip.brightness`.

## Files

- `engine.py` — entrypoint: MeshCore connect, RX subscription, keepalive
  worker, render loop, graceful shutdown.
- `animations.py` — Comet / Bloom dataclasses + idle heartbeat (vectorized NumPy).
- `config.py` — TOML loader, palette, repeater→pixel hash, RSSI ramp.
- `config.toml` — tunable parameters.
- `pyproject.toml` — uv project + dependency manifest.
- `docs/` — MeshCore protocol notes (RX event surface, path matching,
  AGC dormancy workaround) that informed the engine design.
- `utils/rx_logger.py` — standalone packet logger; useful for proving the
  chain works (`uv run python utils/rx_logger.py --port /dev/ttyACM0`)
  before running the engine.
- `utils/set_pnw_radio.py` — one-shot radio config helper (PNW region preset).
