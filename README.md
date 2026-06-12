# Meshlights

A generative LED art installation driven by live MeshCore mesh traffic. A
RAK4631 running MeshCore companion firmware is USB-tethered to a Raspberry Pi;
an APA102/DotStar strip on hardware SPI0 visualizes the RX feed in one of
two styles:

- **`waterfall` (default)** — the strip is a scrolling spectrogram of the
  last ~15 s of LoRa air. Each packet renders as a horizontal bar whose
  width is its airtime and whose color is its payload type; gaps are real
  silence. Reads as channel state — a busy mesh looks dense, a quiet mesh
  looks sparse, a long text message visibly occupies more channel than
  an ACK. Isomorphic to channel occupancy.
- **`comet`** — each packet spawns a path-tracing comet that traces its
  hop-by-hop route across the strip, with full-strip blooms for direct
  (zero-hop) arrivals and a soft heartbeat when the mesh is quiet. Reads
  as individual packet events.

Style is set in `config.toml` or overridden with `--style`; strip length
is configurable via `strip.pixels`. Defaults assume the SF7 / BW62.5 / CR4-5
LoRa preset — see the **Visualization styles** section below to retune if
your mesh runs a different preset.

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

## Enable i2c (for the optional OLED status screen)

An SSD1309 (default — 2.42" panel) or SSD1306 (0.96" panel) OLED over i2c
can be wired to the Pi to display a scrolling log of received packets.
Wire VCC → 3.3 V (pin 1), GND → any GND, SDA → GPIO 2 / SDA1 (pin 3),
SCL → GPIO 3 / SCL1 (pin 5). Select the driver under `[oled] driver` in
`config.toml` (`"ssd1309"` / `"ssd1306"` / `"sh1106"`); see the inline
comment for what each is for. If a panel is dark or shows column
ghosting, dial it in with `utils/screen_debug.py --driver …`.

`sudo raspi-config` → **Interface Options** → **I2C** → **Enable**, then reboot.
Non-interactive equivalent:

```
sudo raspi-config nonint do_i2c 0      # 0 means "enable"
sudo reboot
```

Or add `dtparam=i2c_arm=on` to `/boot/firmware/config.txt` and reboot.

`i2cdetect` (the bus-scan tool used for verification below) isn't installed
by default — grab it once:

```
sudo apt install -y i2c-tools
```

**Verify it came up.** After reboot, `ls /dev/i2c-1` should show the bus
device, and `i2cdetect -y 1` should show the OLED's address in the grid
(usually `3c`, occasionally `3d`). If you see `3d`, pass `addr=0x3D` to
`screen.connect()` in `screen.py`.

The screen is optional — `screen.connect()` returns `None` and prints a
warning if i2c isn't enabled or no panel responds, and the engine/sim keep
running headless.

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

### Run at boot (systemd)

For an unattended install, set Meshlights up as a systemd service so it
starts on every boot and auto-restarts on crash:

```
sudo ./install-service.sh
sudo systemctl start meshlights
```

The install script reads your username, home directory, and the path to
`uv` at install time — nothing is hardcoded into the repo. The generated
unit file lives at `/etc/systemd/system/meshlights.service` (outside the
repo) and runs the engine as your user, from this checkout, with the same
SPI/i2c/dialout access you have interactively.

To use a non-default port: `sudo PORT=/dev/ttyACM1 ./install-service.sh`.

Day-to-day:

```
sudo systemctl status meshlights       # is it up?
journalctl -u meshlights -f            # tail logs (no sudo needed)
sudo systemctl restart meshlights      # pick up code changes after `git pull`
sudo systemctl stop meshlights         # free the SPI bus + RAK for manual debugging
```

To uninstall: `sudo systemctl disable --now meshlights && sudo rm /etc/systemd/system/meshlights.service && sudo systemctl daemon-reload`.

Other flags:

- `--style {waterfall,comet}` — override the animation style from
  `config.toml` for one run (handy for A/B'ing during install).
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

### Visualization styles

Top-level `style = "waterfall"` (default) or `"comet"` in `config.toml`,
overridable with `--style` on the CLI.

**Waterfall** turns the strip into a scrolling channel-occupancy spectrogram.
Each RX packet renders as an additive bar — right edge pinned to the live
pixel while the packet is "being received" (over its real airtime, in real
time), then locking at full width and scrolling left. Color = payload type,
width ∝ airtime. Bars overlap visually exactly when the underlying
transmissions overlapped on air — so collisions read truthfully. A red
"saturation glow" rises in the gaps when in-window utilization passes the
ALOHA-collapse threshold (~20–30%), so a busy channel visibly warms even
when individual packets are small. Knobs in `[waterfall]`:

| Knob | Default | What it does |
|---|---|---|
| `window_seconds` | `7.0` | How much LoRa air the full strip represents. At 7 s / 71 px the strip resolves at ~100 ms/px, so a typical TXT_MSG (~206 ms airtime) reads as ~2 px and an ACK (~65 ms) reads as ~0.7 px. Widen (15–60 s) for more channel history at smaller bars; shorten (3–5 s) to see packet detail. |
| `bytes_per_sec` | `340.0` | Marginal LoRa payload rate at SF7 / BW62.5 / CR4-5. Drop for slower presets (SF12/BW250 ≈ 22; SF10 ≈ 88), raise for faster ones (SF8/BW125 ≈ 1500). |
| `overhead_sec` | `0.030` | Fixed LoRa PHY cost (preamble + explicit header) per transmission. Without this, small packets (ACKs) hit the floor and disappear. |
| `exaggeration` | `1.0` | Visual width multiplier on real airtime. At `1.0` the strip is honest 1:1 — fraction of strip lit = fraction of channel time used, so 20% lit = real saturation. Crank to `4.0–5.0` to dramatize channel pressure (each bar 5× wider than real airtime) at the cost of breaking the 1 px = X ms reading. |
| `intensity` | `1.0` | Overall bar brightness multiplier on top of `strip.brightness`. |
| `edge_fade_px` | `1.5` | Linear taper at each bar's head/tail INSIDE its extent. Hides the pixel-snap feel of slow scroll; mildly dims narrow bars. Set `0.0` for strict honest edges. |
| `halo_depth` / `halo_peak` | `4.0` / `0.06` | Dim halo OUTSIDE each bar's nominal extent — `halo_peak` brightness at the bar edge ramping to 0 over `halo_depth` pixels. Smooths motion the most; least honest — narrow bars get long dim tails. Set `halo_peak = 0` to disable. |
| `reverse_flow` | `true` | Flow direction. `false` puts the live edge at pixel n-1 (far end) — bars scroll TOWARD the input wires. `true` puts it at pixel 0 — bars scroll AWAY from the input wires. |
| `glow_threshold` | `0.20` | Channel utilization fraction at which the saturation glow begins (default 20%, matching LoRa's ALOHA collapse threshold). |
| `glow_peak` | `0.15` | Peak brightness of the saturation glow at 2× threshold utilization (40% by default). Set `0` to disable. |
| `glow_color` | `[255, 0, 0]` | RGB (0..255) color of the saturation glow. Lives BEHIND the packet bars — bar pixels keep their honest payload color; only gap pixels show the glow. |

**Comet** spawns a per-packet animation that walks the route across the
strip: each hop is a dwell-and-transit step in the payload-type color,
with a contrasting head accent and lingering "spark" at each visited
pixel. Direct (zero-hop) arrivals trigger either a dim full-strip bloom
or a brighter "walk-up" bloom above the RSSI threshold; a soft red
heartbeat sweeps the strip when the mesh is idle. Knobs in `[comet]`,
`[bloom]`, `[walkup]`, `[rssi_ramp]` — see the comments in `config.toml`.

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
| `bloom.walkup_peak` | `0.6` | Intensity of the white walk-up bloom (comet mode only). |
| `bloom.dim_peak` | `0.25` | Intensity of dim zero-hop blooms (comet mode only). |

Rough current envelope at defaults (`brightness=0.25`):

- **Waterfall, quiet mesh:** near zero — gaps render as off pixels.
- **Waterfall, busy mesh:** scales with channel occupancy; the strip
  caps out around the same draw as a comet at the same brightness.
- **Comet, idle heartbeat:** ~10 mA — basically free.
- **Comet active:** ~200–400 mA peak across the tail.
- **Walk-up bloom (comet, worst case):** ~1.3 A peak, ~0.85 A averaged
  over the 1.5 s sin envelope. Energy ≈ 0.4 mAh per bloom.

Expected runtime on a 2× 18650 bank (~3 Ah @ 5V usable): typically
**8–12 hours** in a quiet-to-moderately-busy mesh.

If you're on mains power, bump `strip.brightness` toward `1.0` for max
"loud." If the power bank's protection trips on walk-ups, drop
`bloom.walkup_peak` first (it's the only thing that exercises the worst-case
current), then `strip.brightness`.

## Files

- `engine.py` — entrypoint: MeshCore connect, RX subscription, keepalive
  worker, render loop, graceful shutdown.
- `animations.py` — Waterfall / Comet / Bloom / Walkup dataclasses + idle heartbeat (vectorized NumPy).
- `config.py` — TOML loader, palette, repeater→pixel hash, RSSI ramp.
- `config.toml` — tunable parameters.
- `pyproject.toml` — uv project + dependency manifest.
- `docs/` — MeshCore protocol notes (RX event surface, path matching,
  AGC dormancy workaround) that informed the engine design.
- `utils/rx_logger.py` — standalone packet logger; useful for proving the
  chain works (`uv run python utils/rx_logger.py --port /dev/ttyACM0`)
  before running the engine.
- `utils/set_pnw_radio.py` — one-shot radio config helper (PNW region preset).
