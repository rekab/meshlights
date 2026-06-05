# MeshCore path field — matching a known repeater hash

How the `path` field on received packets is constructed, how `meshcore_py` surfaces it, and how to membership-test a known repeater's hash against it (and determine its position in the journey).

All citations are to files within this working directory.

---

## 1. How the per-hop hash is computed

**The hash is the first N bytes of the repeater's 32-byte Ed25519 public key, with no hashing function applied — just a prefix.**

`Identity::copyHashTo()` at `MeshCore/src/Identity.h:23-26`:

```cpp
int copyHashTo(uint8_t* dest, uint8_t len) const {
  memcpy(dest, pub_key, len);    // hash is just prefix of pub_key
  return len;
}
```

This is the function the firmware calls when a repeater appends its identifier on retransmit (`MeshCore/src/Mesh.cpp:333`):

```cpp
self_id.copyHashTo(&packet->path[n * packet->getPathHashSize()], packet->getPathHashSize());
```

So if you know a repeater's 32-byte pubkey (e.g. from its advert), the hash it stamps into a path with `path_hash_size = 1` is just `pubkey[0]`. With size 2 it's `pubkey[0:2]`, with size 3 it's `pubkey[0:3]`.

Relevant constants in `MeshCore/src/MeshCore.h`:
- `PUB_KEY_SIZE = 32` (line 7)
- `PATH_HASH_SIZE = 1` (line 17 — default size; overridable per-packet)
- `MAX_PATH_SIZE = 64` (line 21 — total bytes of path storage)

No SHA, no salt, no XOR. Your matching code is just `repeater_pubkey[:path_hash_size]`.

---

## 2. How meshcore_py surfaces the path

`RX_LOG_DATA` events expose path data in two places.

**(a)** On `event.payload` (the `log_data` dict) — fields set in `meshcore_py/src/meshcore/meshcore_parser.py:111-113`:

| Field | Type | Meaning |
|---|---|---|
| `log_data["path_len"]` | int | **Number of hops** (not bytes) |
| `log_data["path_hash_size"]` | int | 1, 2, or 3 |
| `log_data["path"]` | str | **Hex string** of length `path_len * path_hash_size * 2` |

**(b)** Mirrored onto `event.attributes` — `meshcore_py/src/meshcore/reader.py:641-644`:

```python
attributes['route_type'] = log_data['route_type']
attributes['payload_type'] = log_data['payload_type']
attributes['path_len'] = log_data['path_len']
attributes['path'] = log_data['path']
```

Note `path_hash_size` is **NOT** mirrored to `attributes`, only to `payload`. So if you subscribe with attribute filters you can match on `path_len`/`path`/`route_type`/`payload_type`, but you'll need to crack open `event.payload` to get `path_hash_size`.

The path field is a flat hex string. To split into per-hop hashes you must use `path_hash_size`:

```python
hashes = [path[i*2*sz : (i+1)*2*sz] for i in range(path_len)]
```

where `sz = log_data["path_hash_size"]`.

---

## 3. path_hash_size — varies per packet, encoded in the wire path byte

**It varies per packet.** The wire format packs both size and count into a single byte (`MeshCore/src/Packet.cpp:65-78`, parsed in `meshcore_py/src/meshcore/meshcore_parser.py:78-80`):

```python
path_byte = pbuf.read(1)[0]
path_hash_size = ((path_byte & 0xC0) >> 6) + 1   # top 2 bits, +1
path_len = (path_byte & 0x3F)                    # bottom 6 bits = count
```

Encoded values: 0→size 1, 1→size 2, 2→size 3. Value 3 (would be size 4) is **reserved/invalid** and the firmware rejects it (`MeshCore/src/Packet.cpp:13-17`):

```cpp
bool Packet::isValidPathLen(uint8_t path_len) {
  uint8_t hash_count = path_len & 63;
  uint8_t hash_size = (path_len >> 6) + 1;
  if (hash_size == 4) return false;  // Reserved for future
  return hash_count*hash_size <= MAX_PATH_SIZE;
}
```

The **originator** picks the size when constructing the flood (`MeshCore/src/Mesh.cpp:623-635`), defaulting to the originator's local `_prefs.path_hash_mode + 1` (e.g. `MeshCore/examples/companion_radio/MyMesh.cpp:487`). It does NOT change as the packet traverses — every repeater along the way uses the size the originator chose. So within one packet, all hop entries are the same width.

But across two consecutive packets you receive, the size can differ — they may have come from different originators with different `path_hash_mode` settings. **Always read `path_hash_size` fresh on each event.**

---

## 4. Ordering — earliest hop first; sender NOT in path

For **flood packets** (`ROUTE_TYPE_FLOOD` / `ROUTE_TYPE_TRANSPORT_FLOOD`), each repeater **appends at the end and increments count** (`MeshCore/src/Mesh.cpp:328-340`):

```cpp
uint8_t n = packet->getPathHashCount();
if (packet->isRouteFlood() && ...) {
  // append this node's hash to 'path'
  self_id.copyHashTo(&packet->path[n * packet->getPathHashSize()], packet->getPathHashSize());
  packet->setPathHashCount(n + 1);
  ...
}
```

So journey-order = path-order:

- `path[0 : sz]` — **first** repeater that rebroadcast (closest to source)
- `path[(n-1)*sz : n*sz]` — **last** repeater before the packet hit your antenna
- The originating sender is **NOT** in the path. A fresh flood leaves the source with `count = 0` (set in `sendFlood()` at `MeshCore/src/Mesh.cpp:635` and `:664`). The first entry is the first relayer, not the originator.

Crucially, `logRxRaw` is called **before** the receiver appends itself — `MeshCore/src/Dispatcher.cpp:190-218` shows `logRxRaw` fires on the raw bytes immediately after `recvRaw()` returns, while the path-append in `routeRecvPacket` happens later when `processRecvPacket → onRecvPacket` runs. So your companion device sees the path **as it was on the wire**, not including itself.

### Important: flood vs direct have OPPOSITE semantics

For `ROUTE_TYPE_DIRECT` packets, the path field is the **pre-computed remaining route**, not the journey trace. Each hop calls `removeSelfFromPath()` (`MeshCore/src/Mesh.cpp:318-326`) which **shifts the front entry off and decrements count** — so as a direct packet travels, the path SHRINKS from the front and represents future hops, not past.

Always check `event.attributes['route_type']` before interpreting position semantics:

| Value | Route type | Path semantics |
|---|---|---|
| 0 | `ROUTE_TYPE_TRANSPORT_FLOOD` | Journey trace (earliest hop first), with 4-byte transport codes |
| 1 | `ROUTE_TYPE_FLOOD` | Journey trace (earliest hop first) |
| 2 | `ROUTE_TYPE_DIRECT` | Remaining route (next hop first, last entry = final dest) |
| 3 | `ROUTE_TYPE_TRANSPORT_DIRECT` | Remaining route, with 4-byte transport codes |

For LED comet visualization you almost certainly want flood packets — adverts (payload_type 4), channel msgs (5), and most public mesh traffic are flood.

---

## 5. Collisions

**At size=1, collisions are not just possible — they're statistically inevitable on any non-trivial mesh.** 256 buckets means the birthday-collision threshold is ~20 nodes hearing each other.

Disambiguation strategies, ordered by reliability:

1. **Use ADVERT events to seed a hash→pubkey table.** Adverts carry the full 32-byte pubkey (`meshcore_py/src/meshcore/meshcore_parser.py:175-176` reads it as `adv_key`). For repeaters you care about, you already know the full pubkey, so the table is trustworthy a priori. If a `path` hop matches `your_repeater_pubkey[:size]` AND you don't know of any other repeater you can hear with the same prefix, you have a confident match.

2. **Bump path_hash_size to 2 or 3 globally.** This is a sender-side setting (`path_hash_mode` in prefs), so it only affects packets YOU originate. You can't force other nodes to use a wider hash. For passive listening, you have to accept whatever size the originator chose.

3. **Cross-reference with RSSI/SNR.** A flood path passing through "your" nearby repeater should show a recognizable RSSI floor (close, strong) compared to a different repeater that happens to share the same first byte but lives 5km away. Not reliable alone, but a useful disambiguator.

4. **Adjacency model.** Build a graph from observed (hop_a, hop_b) bigrams over time. Your nearby repeater will appear adjacent to known neighbors in many flood paths. A collision twin would have a different adjacency signature.

5. **Position heuristic.** For very local floods originated by nearby nodes, your repeater (if it relayed) will often appear at `path[0]` or `path[1]` — early in the journey. A distant collision twin would more often appear in the middle/end of paths originated by distant nodes. Combine with RSSI.

For a single-installation art piece where you can pre-survey your immediate radio neighborhood, strategies 1 + 3 are usually enough. If you ever need to disambiguate "is this MY repeater" with high confidence, the right design move is to **deploy your repeater with a pubkey whose first byte is unique among nodes you hear** — generate keys until you get a desirable prefix (vanity-key style) and ship that one. Firmware doesn't constrain key choice.

---

## Quick reference (default 1-byte hash, flood packet)

```
RX_LOG_DATA event for a flood packet
  attributes.route_type      = 1            (ROUTE_TYPE_FLOOD)
  attributes.path_len        = 3            (3 hops have relayed it so far)
  payload["path_hash_size"]  = 1            (1 byte each)
  attributes.path            = "ab23cd"     (hex)

  → hop[0] = "ab"  (first relayer, closest to originator)
  → hop[1] = "23"
  → hop[2] = "cd"  (last relayer before your antenna)

  Your repeater's pubkey starts with 0xab → it relayed this packet first.
```

## Membership test, in pseudocode

```python
def repeater_in_path(event, repeater_pubkey: bytes) -> int | None:
    """Return the 0-based hop index where repeater_pubkey appears in the
    flood path, or None if not present. Only meaningful for flood routes."""
    if event.attributes['route_type'] not in (0, 1):
        return None  # direct routes have inverted semantics

    sz = event.payload['path_hash_size']
    n  = event.attributes['path_len']
    path_hex = event.attributes['path']

    my_hash_hex = repeater_pubkey[:sz].hex()
    hops = [path_hex[i*2*sz : (i+1)*2*sz] for i in range(n)]

    return hops.index(my_hash_hex) if my_hash_hex in hops else None
```

Position interpretation for flood:
- `0` = your repeater was the **first** to rebroadcast (you're 1 hop from originator)
- `n-1` = your repeater was the **last** before the packet reached your companion's antenna
- `None` = your repeater either didn't hear/relay it, OR did relay but the packet reached your companion via a different branch of the flood
