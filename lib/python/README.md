# OwnTelemetry — Python Library

Schema-driven codec for the OwnTelemetry protocol. Encodes and decodes CBOR (production) and JSON (debug) wire-format packets, with optional per-endpoint encryption and CRC-16.

```bash
pip install lib/python        # from repo root
```

**Requires:** Python 3.9+, `cbor2>=5.4`

---

## Quick Start

```python
import time
from owntelemetry import OwnTelemetry

ot = OwnTelemetry("./profiles", [0, 51])

endpoint_id = bytes.fromhex("0cff64593f149b39")

payload, used = ot.encode(51, 1, {
    "id": endpoint_id,
    "timestamp": int(time.time()),
    "cpu_load": 45.2,
    "cpu_temp": 72.0,
    "memory_used": 60.0,
    ...
})

packet = ot.decode(payload)
# {
#   "profile": 51, "id": "0cff64593f149b39", "mid": 0, ...
#   "cpu_load": 45.2, "_profile_name": "Server Health Monitor",
#   "_type_name": "status"
# }
```

---

## API

### `OwnTelemetry(profiles_dir, profiles)`

Load profile schemas from `profiles_dir` (path to the `profiles/` directory). Only the listed profile numbers are loaded — pass every profile number you intend to encode or decode.

```python
ot = OwnTelemetry("./profiles", [0, 51, 52])
```

---

### `encode(profile, type, fields, *, key=None, crc=None, json_wire=False)`

Encode a packet to bytes.

| Parameter | Description |
|-----------|-------------|
| `profile` | Profile number (filled automatically into the packet) |
| `type` | Packet type within the profile |
| `fields` | Named field values — see the profile `.md` for field names |
| `key` | 16-byte encryption key. When provided the packet is wrapped in the encrypted wire frame |
| `crc` | Override CRC behaviour. Defaults to the profile schema setting (`uses_crc`) |
| `json_wire` | `True` to emit JSON instead of CBOR — for development and debugging only |

**Auto-generated fields** (can be overridden by including them in `fields`):

| Field | Behaviour |
|-------|-----------|
| `profile` | Set from the `profile` argument |
| `type` | Set from the `type` argument |
| `mid` | Rolling uint8 counter, tracked per endpoint ID |
| `ack` | Defaults to `False` for e→s packets |

`id` (or `target_id` for s→e packets) accepts either `bytes` or a hex string — the library normalises it.

**Returns** `(payload_bytes, used_fields)` where `used_fields` is a dict of every field written to the wire, including auto-generated values. Bytes fields such as `id` are hex strings.

```python
# Cleartext CBOR
payload, used = ot.encode(51, 1, {"id": endpoint_id, "cpu_load": 45.2, ...})
print(used["mid"])   # 0

# JSON wire format (readable with mosquitto_sub)
payload, used = ot.encode(51, 1, fields, json_wire=True)

# Encrypted
key = bytes.fromhex("a3f1c2b4d5e6f7a81234567890abcdef")
payload, used = ot.encode(51, 1, fields, key=key)

# Encrypted + CRC (auto for profiles that declare it; override with crc=True)
payload, used = ot.encode(52, 1, fields, key=key)   # Profile 52 uses CRC by schema
```

---

### `decode(data, *, get_key=None)`

Decode a packet from bytes.

Tries **cleartext CBOR** first, then **JSON**, then **encrypted**. When `get_key` is provided it is called for all three attempts, so a server receiving mixed encrypted/cleartext traffic can always pass it.

| Parameter | Description |
|-----------|-------------|
| `get_key` | `(endpoint_id: bytes) -> bytes \| None` — return the 16-byte key for the endpoint, or `None` to signal unknown (raises `KeyError`) |

**Returns** a named-field dict annotated with:

| Key | Present when |
|-----|-------------|
| `_profile_name` | Always |
| `_type_name` | Always |
| `_encrypted` | Packet was decrypted |
| `_wire_format` | `"json"` — JSON packet |
| `_warning` | JSON packet — reminds you to switch to CBOR in production |
| `_event_name` | Profile defines event codes (e.g. Profile 51 alerts) |
| `_reason_name` | Profile 0 will packet |
| `_command_name` | Profile 0 command packet |
| `_command_status` | Profile 0 command_ack packet |
| `_provision_code_name` | Profile 0 provision packet |
| `_provision_status` | Profile 0 provision_ack packet |

```python
# Cleartext
packet = ot.decode(payload)

# With key lookup — handles encrypted and cleartext transparently
key_store = {endpoint_id: key}
packet = ot.decode(payload, get_key=lambda eid: key_store.get(eid))

# Encrypted packets include _encrypted: True
assert packet["_encrypted"] == True
```

**Raises:**
- `ValueError` — packet is not CBOR/JSON and `get_key` was not provided, decryption failed, or CRC mismatch
- `KeyError` — `get_key` returned `None` for the endpoint

---

## Encryption

The wire format for encrypted packets:

```
[profile: 1 byte][id: 8 bytes][nonce: 4 bytes][ciphertext][crc: 2 bytes if enabled]
```

`profile` and `id` are plaintext so the server can look up the per-endpoint key before decrypting.

**Keystream:** `key[i % 16] XOR nonce[i % 4]`

**Nonce:** taken from the `timestamp` field when present; otherwise an internal per-endpoint counter is used.

**CRC:** verified before decryption — a corrupt packet is detected without attempting to decrypt. CRC is auto-applied for profiles that declare it in their schema (e.g. Profile 52). The `crc=True` override adds CRC for any profile.

Per-endpoint keys mean that a valid decryption can only have been produced by the endpoint holding that key — encryption and authentication are provided by the same mechanism.

---

## Profile 0 — System Packets

Profile 0 is implemented by all conformant endpoints. The library encodes and decodes all eight packet types:

| Type | Name | Direction |
|------|------|-----------|
| 1 | `birth` | e→s |
| 2 | `will` | e→s |
| 3 | `ping` | s→e |
| 4 | `pong` | e→s |
| 5 | `command` | s→e |
| 6 | `command_ack` | e→s |
| 7 | `provision` | s→e |
| 8 | `provision_ack` | e→s |

```python
# Birth packet
payload, used = ot.encode(0, 1, {
    "id": endpoint_id,
    "ack": True,
    "profiles": [0, 51],
    "interval": 30,
})

# Decode a will packet
packet = ot.decode(payload)
# → {..., "_type_name": "will", "_reason_name": "shutdown"}
```

---

## License

CC0 1.0 Universal — public domain. See [LICENSE](../../LICENSE).
