# RAK4631 RX dormancy (MeshCore issue #1209) — firmware investigation

Tracking the cause of the "RX goes dormant until a strong nearby packet wakes it" symptom reported against RAK4631 + SX1262 + companion firmware over USB serial.

Short version: there is no SX1262 duty-cycle / RX-power-save mode in the firmware at all. The dormancy is the SX126x **AGC getting stuck in a desensitized state**, and the firmware function that's supposed to recover from that was effectively a no-op until a fix landed in late Feb 2026 (after v1.11.0).

All file paths are relative to this working directory.

---

## Issue #1209 specifics

- Filed 2025-12-13 against **v1.11.0**, RAK4631 **repeater** (not companion — but companion shares the radio driver code).
- Open, no maintainer comments, no PR referenced from the issue itself.
- Reporter symptoms: needs a strong-signal packet to "wake up" weak-signal reception. Same as the symptom observed locally on a passive RAK4631 companion listener.

The maintainer hasn't tagged a fix in the thread, but tracing the source there is one: PR #1743 ("fixagcreset"), merged 2026-03-03, contains the relevant changes.

---

## 1. Is there an RX power-save / duty-cycled-RX / sleep / AGC in the companion build?

**RX is continuous, no duty cycle, no sleep.** Searched all of `src/` and `examples/` for `startReceiveDutyCycle`, `SX126X_CMD_SET_RX_DUTY_CYCLE`, `RxDuty`, etc. — zero hits. The single call site that starts reception is `RadioLibWrapper::startRecv()` at `MeshCore/src/helpers/radiolib/RadioLibWrappers.cpp:96-103`:

```cpp
void RadioLibWrapper::startRecv() {
  int err = _radio->startReceive();   // no args → RADIOLIB_SX126X_RX_TIMEOUT_INF
  if (err == RADIOLIB_ERR_NONE) state = STATE_RX;
}
```

The chip is parked in RX-continuous and never re-armed unless either (a) an IRQ fires and `recvRaw()` runs the re-arm path, (b) a TX cycles it, or (c) `resetAGC()` runs.

### What "power-saving" actually means in this code

There IS a setting called `rx_boosted_gain` (`MeshCore/examples/companion_radio/NodePrefs.h:31` — the comment literally reads `(0=power saving, 1=boosted)`), but it's a **static register write** to SX126x's `REG_RX_GAIN`, not a duty cycle:

- `setRxBoostedGainMode(true)` → `REG_RX_GAIN = 0x96` (higher RX current, +3 dB sensitivity)
- `setRxBoostedGainMode(false)` → `REG_RX_GAIN = 0x94` (lower RX current, lower sensitivity)

Companion defaults to **enabled / boosted** at `MeshCore/examples/companion_radio/MyMesh.cpp:872-876`:

```cpp
#ifdef SX126X_RX_BOOSTED_GAIN
  _prefs.rx_boosted_gain = SX126X_RX_BOOSTED_GAIN;
#else
  _prefs.rx_boosted_gain = 1; // enabled by default
#endif
```

So the radio is already in the more-sensitive mode. Toggling this would only hurt. The "power-saving" label is misleading — it doesn't cause dormancy.

### The actual mechanism: AGC

The SX126x has an analog automatic gain control that can latch in a low-sensitivity state after a strong nearby signal, and not recover (a documented Semtech behavior). The firmware-side mitigation is `RadioLibWrapper::resetAGC()` at `MeshCore/src/helpers/radiolib/RadioLibWrappers.cpp:60-74`, which is supposed to do a warm sleep + recalibrate to force a fresh gain pickup. It's invoked periodically from `Dispatcher::loop()` at `MeshCore/src/Dispatcher.cpp:132-135`:

```cpp
if (getAGCResetInterval() > 0 && millisHasNowPassed(next_agc_reset_time)) {
  _radio->resetAGC();
  next_agc_reset_time = futureMillis(getAGCResetInterval());
}
```

Entered: timer expires AND interval > 0. Exited: returns immediately.

### Two compounding bugs that produce the #1209 symptom

**Bug A — the periodic timer is disabled on companion firmware.** `getAGCResetInterval()` is a virtual at `MeshCore/src/Dispatcher.h:169` that returns **0 by default**. `examples/companion_radio/` does not override it. By contrast, `simple_repeater`, `simple_room_server`, and `simple_sensor` all override it (e.g. `MeshCore/examples/simple_repeater/MyMesh.h:153-155` returns `agc_reset_interval * 4000` ms, default 4 s). So on companion, `resetAGC()` never fires — there's no periodic recovery at all.

**Bug B — even when fired, `resetAGC()` was a no-op until late Feb 2026.** Commit `f81ec4b1` ("fix agc reset", 2026-02-19) shows what the function looked like before:

```cpp
// pre-fix (v1.11.0 era)
void RadioLibWrapper::resetAGC() {
  if ((state & STATE_INT_READY) != 0 || isReceivingPacket()) return;
  // NOTE: according to higher powers, just issuing RadioLib's startReceive() will reset the AGC.
  //      revisit this if a better impl is discovered.
  state = STATE_IDLE;   // trigger a startReceive()
}
```

The author's "higher powers" comment is the bug. The post-fix version actually calls `_radio->sleep()` (warm sleep), with the explanation:

> *Warm sleep powers down the entire analog frontend (including AGC), forcing a fresh gain calibration on the next startReceive(). A plain standby→startReceive cycle does NOT reset the AGC — the analog state can persist across STDBY_RC.*

So issue #1209 is **two bugs stacked**: repeaters configured AGC reset, but the reset function didn't actually reset anything; companion users don't even configure it.

The full SX126x AGC reset sequence — what `sx126xResetAGC()` does today at `MeshCore/src/helpers/radiolib/SX126xReset.h:8-37` — is warm sleep, standby_RC, `Calibrate(0x7F)` (all PLL/ADC/image), `calibrateImage(freqMHz)` for the actual band, then re-apply DIO2/RX-boost/0x8B5 register patch. That's the proper full-reset routine, only present after the Feb 2026 patches landed.

---

## 2. Build flag / config / serial command to force RX-CONTINUOUS or disable AGC?

**RX is already continuous; you can't make it more continuous.** There is no `setRxDutyCycle` call to disable. So the "force RX-CONTINUOUS" part of the question is already the state of the firmware.

**Disabling AGC is not a knob the firmware exposes.** The SX126x AGC can't be disabled cleanly without breaking RX entirely. What you'd want is the *opposite* — to **enable** periodic AGC reset on the companion build. That control exists in the codebase but is wired to a CLI command (`agc.reset.interval`) which only the repeater/sensor/room_server firmwares expose through their text CLI (`MeshCore/src/helpers/CommonCLI.cpp:488-490`). The companion serial protocol has **no equivalent CMD_** for it. Every command in `MeshCore/examples/companion_radio/MyMesh.cpp` was checked — `agc_reset_interval` is in `_prefs` (it's part of the shared `NodePrefs` struct via `MeshCore/src/helpers/CommonCLI.h:44`) but companion doesn't read/write it, so even if you set it, `getAGCResetInterval()` still returns 0 because that virtual isn't overridden.

**The one-line firmware fix** for a companion build is to add the override yourself in `MeshCore/examples/companion_radio/MyMesh.h` (mirroring `MeshCore/examples/simple_repeater/MyMesh.h:153-155`):

```cpp
int getAGCResetInterval() const override { return 4000; }   // every 4s
```

That, combined with the corrected `resetAGC()` from v1.14.1+, gives companion the same dormancy immunity that repeaters get.

---

## 3. Non-TX host command to keep RX armed?

**There isn't one.** Every command handler in `MeshCore/examples/companion_radio/MyMesh.cpp` was audited. No CMD_RESET_RX, no CMD_AGC_RESET, no CMD_RADIO_CALIBRATE. The internal `_radio->resetAGC()` and `_radio->triggerNoiseFloorCalibrate()` are not surfaced through the companion serial protocol.

The closest non-TX command that affects radio state is `CMD_SET_RADIO_PARAMS` (`MeshCore/examples/companion_radio/MyMesh.cpp:1351-1385`) — calling it forces `radio_set_params()` to re-init the chip. But it also calls `savePrefs()` on every invocation (`:1375`), which writes to flash. Doing that every few minutes is hostile to the flash and not a real solution.

A legitimate non-TX option that doesn't exist but would be trivial to add: surface `resetAGC()` as a new CMD_* opcode in MyMesh's command dispatch. Until that exists, the choices on stock firmware are:

- **TX-based**: `send_advert(flood=False)` every ~5 min from the host. The TX→`onSendFinished`→`startRecv` path at `MeshCore/src/helpers/radiolib/RadioLibWrappers.cpp:165-169` re-arms the radio cleanly, including via standby. Cheap on airtime (zero-hop, no flood). This is the practical workaround.
- **Firmware patch**: add the `getAGCResetInterval()` override and rebuild. No TX, no host commands, fully passive listener. This is the right fix.

Note about `send_advert`: standby→startReceive doesn't reset the AGC according to the fix commit's own comment. But a full TX cycle goes through `_radio->startTransmit` → `finishTransmit` → which involves PA shutdown and standby transition. Empirically the symptom describes this as a clean recovery, so it does enough to dislodge the AGC stick — likely because the PA enable/disable forces analog state changes that the plain standby doesn't. So `send_advert` is still effective even though it's not a "proper" AGC reset.

---

## 4. Version differences — which versions are affected?

Release dates (from git tags):

| Release | Date | AGC reset enabled on repeater? | `resetAGC()` actually works? | Companion AGC reset enabled? |
|---|---|---|---|---|
| v1.10.0 | 2025-11-13 | yes (4s default) | **no** (no-op) | no |
| **v1.11.0** | 2025-11-30 | yes | **no** | no |
| v1.12.0 | 2026-01-29 | yes | **no** | no |
| v1.14.1 | 2026-03-20 | yes | **yes** (PR #1743 merged 2026-03-03) | no |
| v1.15.0 | 2026-04-19 | yes | **yes** | no |
| dev (current tree) | — | yes | yes | no |

The AGC reset function was broken from whenever the periodic reset machinery was introduced (around v1.10) until commit `f81ec4b1` on 2026-02-19. PR #1743 ("fixagcreset", merged 2026-03-03) bundles:

- `f81ec4b1` — fix agc reset (the warm-sleep fix above)
- `a2dc2eb5` — when doing AGC reset, call `Calibrate(0x7F)`
- `9106ab46` — reset noise floor sampling after agc reset
- `85f764a1` — Calibrate configured frequency for AGC reset

All four are needed for the fix to be effective. **v1.14.1 is the first release that has all four.**

So:

- **Repeaters**: upgrading from v1.11.0 → v1.14.1 or later resolves it directly. That's the fix to recommend in the #1209 thread.
- **Companion (passive listener)**: upgrading does **not** resolve it. Companion never enables the periodic timer that drives `resetAGC()`, even in v1.15.0. You'd be running fixed code that's never invoked. The user-visible fix for companion is still either the `send_advert` keepalive workaround or the one-line firmware patch in §2.

### "Is there a version known not to have this behavior?"

For the specific hardware/role combination (RAK4631 companion, passive listener):

- **No stock version is immune.** v1.10 and earlier had broken `resetAGC` but also no callers on companion, so behavior was identical. v1.14.1+ fixed the function but companion still doesn't call it.
- A **custom build** of v1.14.1 or later with the one-line `getAGCResetInterval()` override on companion would be the first effectively-immune configuration.

The reason mesh_bot-style nodes don't observe this: they TX often enough (replies, telemetry, periodic adverts) that the AGC-stick window never accumulates. Repeater/sensor/room_server firmwares additionally have the (now working) periodic resetAGC. Companion as a pure passive listener is the configuration that exposes the gap.

---

## Recommended actions, prioritized

1. **Confirm the diagnosis**: poll `get_stats_packets()` during dormancy. If `recv` is frozen, that's the AGC stick; the radio is wedged at the analog level. (See `meshcore-rx-dormancy.md` for the full diagnostic procedure.)
2. **Workaround for stock firmware**: `send_advert(flood=False)` every 5 min. Negligible airtime.
3. **Real fix**: build companion firmware from v1.14.1+ (or dev) with this added to `MeshCore/examples/companion_radio/MyMesh.h`:

   ```cpp
   int getAGCResetInterval() const override { return 4000; }
   ```

   That single line, against the post-Feb-2026 codebase, gives continuous immunity without any TX traffic.
4. **For #1209 thread (the repeater case)**: PR #1743 / commits `f81ec4b1`, `a2dc2eb5`, `9106ab46`, `85f764a1` are the fix. First in v1.14.1. Worth posting if no one has linked it yet.
