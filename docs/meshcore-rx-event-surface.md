# MeshCore RX event surface — notes for passive-listener art install

Findings against the current `meshcore-src/` tree (firmware: `MeshCore/`, python lib: `meshcore_py/`). All citations are file:line.

Use case context: Heltec V3 companion node USB-tethered to a Raspberry Pi, driving APA102 LED strips on 4 axes radiating outward, visualizing live mesh traffic. Companion role (passive listener, no rebroadcast).

---

## 1. Per-received-packet fields at the host

Two relevant event types in `meshcore_py`:

### `EventType.RX_LOG_DATA` — every demodulated packet

Fires for every packet that passes preamble + CRC, regardless of addressing or decryptability.

- Emission: `MeshCore/examples/companion_radio/MyMesh.cpp:285-288`
- Parser: `meshcore_py/src/meshcore/reader.py:607-649`

Fields in `event.payload`:

| Field | Wire | Decoded meaning |
|---|---|---|
| `snr` | 1 signed byte ×4 → `/4.0` | dB, 0.25 dB precision |
| `rssi` | 1 signed byte | dBm |
| `payload_length` | 1 byte | length of remaining raw frame |
| `payload` | raw hex | the encrypted packet body |
| `raw_hex` | hex | entire frame after the type byte |
| `recv_time` | host clock | `int(time.time())` set when host parses the frame (NOT from device) |

The first byte of the raw packet is parsed into header bits (`meshcore_py/src/meshcore/meshcore_parser.py:35-116`), surfaced in `event.attributes`:

- `route_type` (bits 0-1): `0=TC_FLOOD, 1=FLOOD, 2=DIRECT, 3=TC_DIRECT`
- `payload_type` (bits 2-5): see §4
- `payload_ver` (bits 6-7)
- `path_len` (number of hops, see §2)
- `path_hash_size` (1-4 bytes per hop)
- `path` (concatenated hop hashes, hex)

### Higher-level decoded events

`CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, `ADVERTISEMENT`, `ACK`, `TRACE_DATA` — fire only when the firmware can decrypt / route the packet. See per-event sections.

---

## 2. Hop count

`MeshCore/src/Packet.h:47-80` — `Packet::path_len` packs:

- 6 LSBs = **hop count, i.e. hops-traveled** (the path is built up as the packet is repeated)
- 2 MSBs = path hash size (1-4 bytes per hop entry)

`path_len & 0x3F` is the number of repeaters that have touched the packet so far. Not a TTL — there's no decrement-on-forward. The receiving node sees `path[]` filled with one hash per repeater that touched it.

Surfaced on the host in `reader.py:269` (channel msgs), embedded in `attributes` for RX_LOG_DATA, and in `CONTACT_MSG_RECV` at `reader.py:213-214`.

---

## 3. RSSI / SNR

Captured by the SX1262 driver and emitted at `MyMesh.cpp:771-772`:

```cpp
out_frame[i++] = (int8_t)(_radio->getLastSNR() * 4);
out_frame[i++] = (int8_t)(_radio->getLastRSSI());
```

- **RSSI:** signed dBm (typical −110 to 0)
- **SNR:** signed, pre-scaled ×4 on wire → host divides by 4 for dB

Available in `RX_LOG_DATA` always. Also in `CONTACT_MSG_RECV_V3` (`reader.py:234`) and `CHANNEL_MSG_RECV_V3` (`reader.py:299`) — but v3 variants require host-side `app_target_ver >= 3`.

---

## 4. Payload types (`MeshCore/src/Packet.h:19-32`)

| Value | Name | Host event |
|---|---|---|
| 0x00 | REQ | (telemetry, content-specific) |
| 0x01 | RESPONSE | (telemetry/path response) |
| 0x02 | TXT_MSG | `CONTACT_MSG_RECV` (if decryptable) |
| 0x03 | ACK | `ACK` (`reader.py:530-539`) |
| 0x04 | ADVERT | `ADVERTISEMENT` + may produce `NEW_CONTACT`/`NEXT_CONTACT` |
| 0x05 | GRP_TXT | `CHANNEL_MSG_RECV` (if you have the channel key) |
| 0x06 | GRP_DATA | `CHANNEL_DATA_RECV` |
| 0x07 | ANON_REQ | — |
| 0x08 | PATH | — |
| 0x09 | TRACE | `TRACE_DATA` (`reader.py:651-699`) |
| 0x0A | MULTIPART | — |
| 0x0B | CONTROL | — |
| 0x0F | RAW_CUSTOM | — |

All produce `RX_LOG_DATA` regardless of whether they fire a higher-level event.

---

## 5. Sender / origin identifier

| Event | What the host sees |
|---|---|
| `ADVERTISEMENT` | **Full 32-byte pubkey** (`reader.py:518-522`) |
| `CONTACT_MSG_RECV` (DM) | **6-byte pubkey_prefix only** (`reader.py:207`) |
| `CHANNEL_MSG_RECV` | **Nothing** — wire packet doesn't carry an authenticated sender for channel msgs |
| `RX_LOG_DATA` | **Nothing decoded** — sender info is inside the encrypted payload |

The on-wire `src_hash` is a 1-byte truncation of pubkey. To know who sent a DM you maintain a `pubkey_prefix → pubkey` map locally (which adverts populate).

**Stability:** 32-byte Ed25519 pubkey, derived at first boot from RNG and persisted to flash (`Identity.cpp:45-49`). Stable across reboots; only factory-reset or explicit key import changes it. No automatic rotation.

**Collision risk:** 6-byte prefix = 48 bits → birthday collision around ~16M contacts, fine. 1-byte path hash = 8 bits → collisions are common in dense meshes but only matter for routing; contact lookup uses the 6-byte prefix.

---

## 6. Destination identifier

The wire packet has a 1-byte `dst_hash`, but the host doesn't get an explicit "this DM was for you" field — receipt of `CONTACT_MSG_RECV` itself implies it was decryptable, which means it was for you. No broadcast-DM-seen-but-not-mine event is surfaced (the firmware filters by dst_hash before attempting decryption — `Mesh.cpp:160-170`).

---

## 7. Channels

Wire format for `PAYLOAD_TYPE_GRP_TXT`: `[1-byte channel hash][2-byte HMAC][AES-128-ECB ciphertext]`. The channel hash is the first byte of `SHA256(channel_secret)`.

Companion stores up to 40 channels indexed 0-39 (`GroupChannel` struct, `Mesh.h:7-11`). Host enumerates via `CHANNEL_INFO` (`reader.py:496-515`) — `channel_idx`, `channel_name` (32 chars), `channel_secret` (16 bytes).

`CHANNEL_MSG_RECV` (`reader.py:260-294`) gives `channel_idx` and decrypted `text` but **no sender** — channels are group-encrypted, not signed. Strangers' channel chatter for channels you don't have keys for shows up only as opaque `RX_LOG_DATA` (`payload_type=0x05`, cannot decrypt).

---

## 8. Timestamps

Two distinct concepts:

- **Sender timestamp** (sender's RTC): 4-byte LE uint32 in adverts (`last_advert`), DMs (`sender_timestamp` at `reader.py:217`), channel msgs (`sender_timestamp` at `reader.py:272`). Not all nodes have a synced clock — treat as "approximate, sender-local."
- **Local receive time:** `RX_LOG_DATA` sets `recv_time = int(time.time())` on the host (`reader.py:614-615`). The device itself does not stamp.

For an installation, use host time as the authoritative axis; sender clocks drift wildly.

---

## 9. Advertisement contents

Packet format inside the encrypted advert payload (`Mesh.cpp:242-249`):

```
[32-byte pubkey][4-byte timestamp][64-byte Ed25519 sig over (pubkey||timestamp||app_data)][app_data]
```

`app_data` format (`AdvertDataHelpers.h:19-66`, also `BaseChatMesh.cpp:92-104`):

```
byte 0: flags
  bits 0-3: ADV_TYPE  (1=CHAT, 2=REPEATER, 3=ROOM, 4=SENSOR)
  bit  4:   ADV_LATLON_MASK   ← lat/lon present iff this is set
  bit  5:   feature bit 1
  bit  6:   feature bit 2
  bit  7:   ADV_NAME_MASK     ← name present iff this is set

if LATLON: int32 lat_µdeg, int32 lon_µdeg  (6-decimal precision, ~10cm at equator)
if NAME:   up to 32 chars, null-terminated
```

**Lat/lon is optional** (bit 4). Privacy-conscious users / sensors / repeaters may omit it. Don't assume every advert has position.

Surfaced as `EventType.ADVERTISEMENT` (`reader.py:518-522`) and parsed into a contact-shaped dict (`reader.py:100-129`):

- `public_key` (hex)
- `adv_name` (string, trimmed)
- `adv_lat`, `adv_lon` (floats in degrees; default 0.0 if absent — ambiguous with "actually at 0,0")
- `last_advert` (sender's uint32)
- `type` (1/2/3/4)
- `flags` (raw)

---

## 10. Advertising interval

`CommonCLI.h:30`, `CommonCLI.cpp:188-192,511`. Stored in `NodePrefs.advert_interval` as **minutes/2** (uint8). Minimum 60 seconds (`MIN_LOCAL_ADVERT_INTERVAL=60`), can be disabled (0), max ~8 hours. Separate `flood_advert_interval` in hours for flood-advert refresh. **No hardcoded default** — depends on firmware build / user config / role.

Typical phone-companion default: ~60 min. Repeaters: 2-4 hours.

**Don't expect a tight cadence.** Adverts arrive sparsely (minutes to hours per node).

---

## 11. Other location-carrying packets

- **Telemetry response** (`MyMesh.cpp:617+`, `REQ_TYPE_GET_TELEMETRY_DATA`): can carry a LOCATION block, gated by permission bits in `ContactInfo.flags`. Explicit request/response, not pushed unsolicited.
- No standalone "position beacon" packet type.

**For passive collection: adverts are the only realistic location source** unless you explicitly query telemetry on authorized contacts.

---

## 12. Companion vs repeater for passive listening

A companion CAN passively surface a lot, with filters:

| Traffic | Visible at host? |
|---|---|
| Every demodulated packet (encrypted blob + RSSI/SNR/header) | ✅ via `RX_LOG_DATA` always (`MyMesh.cpp:285-288`, fires after `onRecvPacket` unconditionally) |
| Adverts from anyone | ✅ `ADVERTISEMENT` event, full pubkey, lat/lon if present |
| Channel msgs **for channels you have the secret for** | ✅ `CHANNEL_MSG_RECV` |
| Channel msgs for channels you don't have | ❌ decoded; only see `RX_LOG_DATA` with `payload_type=0x05` |
| DMs addressed to you | ✅ `CONTACT_MSG_RECV` |
| DMs addressed to someone else | ❌ filtered by 1-byte dst_hash before decrypt (`Mesh.cpp:160-170`) — but you still see `RX_LOG_DATA` for them |
| ACKs in flight | ✅ `ACK` event (you'll see ACKs for your own sent msgs; ACKs between other parties typically don't reach you decoded) |
| Trace packets | ✅ `TRACE_DATA` |

**Companion does NOT rebroadcast.** `allowPacketForward()` in `Mesh.cpp` returns false on the companion role; only Repeater role flood-forwards. You can listen without polluting the mesh.

**Verdict: companion is the right role for passive viz.** Every packet's RSSI/SNR/route_type/payload_type/path metadata via RX_LOG_DATA, plus decoded adverts and any channels you've joined. Flip to repeater only if you want to also propagate traffic (adds TX duty cycle, not needed for visualization).

---

## 13. Identity → location table feasibility

Right model. Strategy:

- Subscribe to `ADVERTISEMENT`. For each, upsert `pubkey → {lat, lon, name, last_seen}` in your table.
- For all other events that give a `pubkey_prefix` (DMs) or full key (acks/adverts/etc.), look up bearing via the table.
- For `RX_LOG_DATA` without sender info: render as anonymous flashes (use RSSI/SNR/route_type, no axis assignment).

Caveats:

- **Pubkey is stable.** No ID rotation under normal use. Factory reset = new identity (treat as new node).
- **Lat/lon is optional in adverts.** Many nodes won't have it.
- **`adv_lat=0.0, adv_lon=0.0` is ambiguous** — could mean "no location bit" OR "actually at 0,0." Current parser gives `adv_lat=0, adv_lon=0` for both. If you need to distinguish, parse the raw flags or use a sentinel.
- **First-time visibility:** you only learn a node's location after hearing its advert. If a DM arrives from a node whose advert you haven't yet heard, you have its 6-byte prefix but no position. Plan a "pending lookup" state.
- **Stale data:** advertising is sparse (minutes-to-hours). Pick a TTL — e.g. fade direction after 6h of silence.

---

## 14. meshcore_py event API

Async, single unified stream, with filtering.

```python
from meshcore import MeshCore, EventType

async def main():
    mc = await MeshCore.create_serial("/dev/ttyACM0")

    sub1 = mc.subscribe(EventType.ADVERTISEMENT, on_advert)
    sub2 = mc.subscribe(EventType.RX_LOG_DATA, on_rx)
    sub3 = mc.subscribe(EventType.CONTACT_MSG_RECV, on_dm)
    sub4 = mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel)
    sub5 = mc.subscribe(EventType.ACK, on_ack)

    # OR subscribe to ALL events (single firehose):
    sub_all = mc.subscribe(None, on_any_event)

    # Required for CONTACT_MSG_RECV to actually fire (offline-queue drain):
    await mc.start_auto_message_fetching()

    await asyncio.Event().wait()

def on_advert(ev):
    # ev.payload: dict with public_key, adv_name, adv_lat, adv_lon, last_advert, type, flags
    ...

def on_rx(ev):
    # ev.payload: snr, rssi, payload_length, payload (hex), raw_hex, recv_time
    # ev.attributes: route_type, payload_type, path_len, path
    ...
```

References:

- `EventType` enum: `meshcore_py/src/meshcore/events.py:12-66`
- `MeshCore.subscribe(...)`: `meshcore_py/src/meshcore/meshcore.py:222-247`
- Dispatcher accepts both sync and async callbacks (`events.py:235-240`)
- `event_type=None` matches all events (`events.py:217-219`)
- `Event` dataclass: `events.py:81-109` — `type`, `payload`, `attributes`
- Optional `attribute_filters={'payload_type': 0x04}` on subscribe for fine-grained routing

---

## Design implications

1. **`await mc.start_auto_message_fetching()` is mandatory after connect** — otherwise `CONTACT_MSG_RECV` events are queued in the device's offline queue and never surface. (Firmware sends only a 1-byte `PUSH_CODE_MSG_WAITING` (0x83) tickle; host must drain via `CMD_SYNC_NEXT_MESSAGE`. `start_auto_message_fetching` subscribes to `MESSAGES_WAITING` and auto-drains.)
2. **`RX_LOG_DATA` is your firehose for passive viz.** Decoded events (`ADVERTISEMENT`, `ACK`, channel/DM) are bonus where you have keys.
3. **Don't trust `sender_timestamp`;** use host receive time for animation timing.
4. **Bearing rendering needs a graceful fallback** for advert-less or no-location nodes — design the visual to handle "anonymous flash with RSSI ring but no axis assignment."
5. **Adverts are sparse;** the location table fills slowly. Early minutes will look quiet on axis-aligned visualizations even when `RX_LOG_DATA` is busy.
