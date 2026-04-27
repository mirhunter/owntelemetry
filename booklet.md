# OwnTelemetry Protocol

A self-hosted telemetry protocol for GPS, vehicle, weather, and other sensor data. Transport-agnostic and designed with low radio bandwidth budgets in mind — compact by default, but equally suitable for high-bandwidth links where that headroom simply goes unused. Licensed under CC0 1.0 (public domain).

---

## Overview

OwnTelemetry defines how endpoints (sensors, vehicles, devices) report telemetry data to a self-hosted server. Users own their data; there is no cloud dependency.

The protocol is transport-agnostic. Any medium capable of carrying a byte stream or datagram can be used:

- **IP** (Ethernet, Wi-Fi, cellular) — standard network connectivity
- **LoRa** — long-range, low-bandwidth radio; the primary constrained-transport design target
- **High-bandwidth radio** (e.g. dedicated 900 MHz or 2.4 GHz telemetry links) — ample headroom; compactness is a free bonus rather than a requirement
- **Other serial / RF links** — any transport an implementer chooses

The compact encoding is always used regardless of transport. On a high-bandwidth link this is inconsequential; on a constrained link it is essential.

---

## Communication Model

The protocol is bidirectional but asymmetric:

- **Endpoint → Server**: the primary flow — endpoints send telemetry packets plus lifecycle events (birth, will, pong, command_ack, provision_ack)
- **Server → Endpoint**: acknowledgments (when requested), liveness probes (ping), commands, and provisioning — defined in [Profile 0](profiles/0.md)

Endpoint-originated traffic dominates; server-to-endpoint traffic is kept minimal. This is especially critical on LoRa where downlink bandwidth is scarce.

---

## Encoding

Every packet can be encoded in one of two formats:

| Format | Use | Notes |
|--------|-----|-------|
| **CBOR** | Production | Compact binary; field names replaced with integer keys |
| **JSON** | Development / debug only | Human-readable; uses full field name strings |

In CBOR, each field name is replaced by a small unsigned integer key defined by the profile schema. A key ≤ 23 costs 1 byte in CBOR; keys 24–255 cost 2 bytes. This eliminates string overhead on every packet.

### Field Types

The following type names are used in profile definitions:

| Type | Width | Notes |
|------|-------|-------|
| `uint8` | 1 byte | Unsigned integer, 0–255 |
| `uint16` | 2 bytes | Unsigned integer, 0–65 535 |
| `uint32` | 4 bytes | Unsigned integer, 0–4 294 967 295 |
| `uint64` | 8 bytes | Unsigned integer; used for microsecond timestamps |
| `int8` | 1 byte | Signed integer, −128–127; used for signal strength (dBm) |
| `float32` | 4 bytes | IEEE 754 single precision; used for all sensor readings and coordinates |
| `bool` | 1 bit (CBOR) | Boolean true/false |
| `bytes` | variable | Raw byte string |

All floating-point fields use **float32** (IEEE 754 single precision) unless explicitly noted otherwise in the profile definition. Single precision gives approximately 7 significant digits — sufficient for GPS coordinates (~1 cm resolution at the equator) and all practical sensor readings.

### JSON Encoding of `id`

In JSON mode, the `id` field (raw bytes in CBOR) is encoded as a lowercase hexadecimal string. An 8-byte ID is represented as 16 hex characters, e.g. `"a3f1c2b4d5e6f7a8"`.

---

## CRC

CRC is **optional** and opt-in per profile. Profiles that require integrity checking declare `crc` as the last field (highest key number) in each of their packet type definitions. The schema is the source of truth — a decoder knows whether to expect a CRC by inspecting the profile definition.

| Property | Value |
|----------|-------|
| Algorithm | CRC-16/CCITT (polynomial `0x1021`, init `0xFFFF`) |
| Size | 2 bytes (uint16) |
| Coverage | All CBOR-encoded packet bytes, with the CRC field value set to `0x0000` during calculation |
| Position | Always the last field in the packet |

Most transports provide their own integrity layer (LoRa hardware CRC, IP/TCP checksums, 802.11 FCS). CRC at the protocol level is intended for transports that do not, such as raw serial or radio links without hardware error detection. Profile 52 (Model Rocket) is an example of a profile that enables CRC.

When a profile uses both CRC and encryption, the CRC is computed over the **ciphertext** (see [Encryption](#encryption) below). The receiver checks the CRC first — if it passes, the encrypted data arrived intact and decryption is guaranteed to yield correct plaintext.

---

## Encryption

Encryption is **optional** and configured per-endpoint — when an endpoint uses encryption, all of its packets across all profiles (including Profile 0 system packets) use the encrypted wire format. Different endpoints in the same deployment may use different settings; the server handles encrypted and unencrypted packets side by side. There is no per-profile toggle on an endpoint that uses encryption.

Each endpoint has its own unique key known only to that endpoint and the server — this means encryption also serves as authentication. A packet that decrypts correctly can only have been produced by the endpoint that holds that key.

### Key

Each endpoint is provisioned with a unique **16-byte (128-bit) key** at manufacture or registration. The key is not transmitted on the wire. It is stored in endpoint flash or device configuration; the server stores a mapping of `endpoint id → key`.

Keys are represented as a 32-character lowercase hex string, e.g. `"a3f1c2b4d5e6f7a81234567890abcdef"`.

### Wire Format

Encrypted packets use a different framing from standard CBOR packets. The `profile` and `id` fields are transmitted in plaintext so the server can identify which key to use before decrypting:

| Offset | Field        | Size     | Description                                                                                           |
|--------|--------------|----------|-------------------------------------------------------------------------------------------------------|
| 0      | `profile`    | 1 byte   | Raw profile number (uint8), plaintext                                                                 |
| 1      | `id`         | 8 bytes  | Raw endpoint identifier (bytes), plaintext                                                            |
| 9      | `nonce`      | 4 bytes  | Per-packet nonce (uint32, big-endian), plaintext                                                      |
| 13     | `ciphertext` | variable | CBOR-encoded packet, XOR-encrypted                                                                    |
| last 2 | `crc`        | 2 bytes  | CRC-16/CCITT over bytes 0 through end of ciphertext, if the profile also enables CRC; omit otherwise |

The `ciphertext` is the standard CBOR-encoded packet (identical to the normal unencrypted wire format) after XOR encryption. The `id` field inside the ciphertext is redundant with the plaintext `id` header — receivers should verify they match after decryption and discard the packet if they do not.

For **server-to-endpoint** packets (Profile 0 ping, command; server acknowledgment), `target_id` appears in the plaintext header in place of `id`. The server encrypts using the target endpoint's key, looked up by `target_id`.

### Nonce

The nonce is a 4-byte value that must differ between packets to prevent the same plaintext producing the same ciphertext. The recommended source is the packet's `timestamp` field (uint32). For packet types without a timestamp field, use a uint32 counter that increments with each transmission and wraps at 2^32.

The nonce is transmitted in plaintext so the receiver can derive the keystream before decrypting.

### Keystream

The keystream is generated by interleaving the key and nonce:

```
keystream[i] = key[i % 16] XOR nonce[i % 4]
```

This is repeated for as many bytes as the ciphertext requires.

### Encryption Process (Sender)

1. CBOR-encode the packet normally (standard wire format).
2. Choose a nonce (timestamp value or counter).
3. Generate keystream from the endpoint's key and nonce.
4. XOR each byte of the CBOR-encoded packet with the corresponding keystream byte → ciphertext.
5. If CRC is also enabled: compute CRC-16/CCITT over `[profile][id][nonce][ciphertext]` with CRC field set to `0x0000`.
6. Transmit: `[profile][id][nonce][ciphertext]`, appending `[crc]` if enabled.

### Decryption Process (Receiver)

1. Read byte 0: profile number — confirms the profile uses encryption.
2. Read bytes 1–8: endpoint `id` — look up the per-endpoint key. Discard if unknown.
3. If CRC is enabled: verify CRC over `[profile][id][nonce][ciphertext]` before proceeding. Discard on failure.
4. Read bytes 9–12: nonce.
5. Generate keystream from the endpoint's key and nonce.
6. XOR ciphertext bytes with keystream → recovers the CBOR-encoded packet.
7. Parse CBOR and verify the `id` field inside the packet matches the plaintext `id` header. Discard if they differ.

### Security Note

Per-endpoint keys mean that a valid ciphertext can only have been produced by the endpoint holding that key — encryption and authentication are provided by the same mechanism. Known-plaintext attacks on the keystream are still feasible due to predictable CBOR structure, and replay attacks are mitigated only by the 60-second deduplication window. For environments requiring cryptographic security, use a VPN or TLS at the transport layer.

---

## Profiles

Every packet includes a `profile` field that identifies its schema. The profile determines which fields are present and what CBOR integer key maps to each field name.

### Profile Ranges

| Range  | Ownership                                                       |
|--------|-----------------------------------------------------------------|
| 0      | System — global profile implemented by all conformant endpoints |
| 1–50   | Standard — profiles defined by this project                     |
| 51–255 | Open — defined by users or server operators                     |

Profile is a `uint8`.

### Common Header Fields

The following CBOR keys are reserved and must appear at the start of every profile in this order:

| CBOR Key | Field Name | Type | Description |
|----------|------------|------|-------------|
| 0 | `profile` | uint8 | Profile number |
| 1 | `id` | bytes (8) | Endpoint device identifier |
| 2 | `mid` | uint8 | Message ID — a small rolling counter; rolls over as needed. Combined with `timestamp` as the deduplication key: multipath duplicates of the same packet will share both `mid` and `timestamp`, distinguishing them from legitimately re-used `mid` values at a later time. Also used to reference a specific message in server responses. |
| 3 | `ack` | bool | `true` if the endpoint requests an acknowledgment |
| 4 | `type` | uint8 | Packet type within this profile (e.g. location, event). Types are defined per profile. |

Profile-specific data fields begin at CBOR key **5**.

Server-to-endpoint packets (e.g. profile 0 ping, server ack) use a different structure: `target_id` replaces `id` and the `ack` field is absent. See the [Server → Endpoint Messages](#server--endpoint-messages) section and [Profile 0](profiles/0.md) for their exact formats.

### Profile Versioning

Breaking changes to a profile (new required fields, removed fields, type changes) require assigning a new profile number — the old and new profiles coexist and decoders can support both by inspecting the `profile` field.

Non-breaking additions (new optional fields appended at higher key numbers) do not require a new profile number. The `version` field in the `.json` schema file is incremented to track such additions, but this version is not transmitted on the wire.

Implementers should treat unknown CBOR keys in a received packet as ignorable — this ensures forward compatibility when a decoder encounters a packet from a newer minor revision of a profile it knows.

### Profile Definitions

Each profile is defined by two files in the `profiles/` directory:

- `<n>.json` — machine-readable schema; contains `types` (type number → name), `event_codes` (code number → name, if the profile defines events), and `packets` (per-type field definitions including CBOR key → field name, field types, optional field list, and direction)
- `<n>.md` — human-readable field descriptions and examples per packet type

Each packet type definition includes a `direction` key — `"e->s"` (endpoint to server), `"s->e"` (server to endpoint), or `"both"`. This is documentation metadata only; it is not transmitted on the wire.

CBOR keys 0–4 (the common header) are identical across all packet types in all profiles. Keys 5+ are scoped to the specific packet type — the same key number can mean different fields in different types within the same profile.

### GPS Fix Convention

Profiles that transmit GPS position include a `fix` field (uint8) that indicates the quality of the current fix. `lat`, `lon`, and `alt` must be omitted when the fix state does not support them:

| Value | Name      | `lat`/`lon` | `alt`   |
|-------|-----------|-------------|---------|
| 0     | `no_fix`  | omit        | omit    |
| 1     | `fix_2d`  | present     | omit    |
| 2     | `fix_3d`  | present     | present |

`fix` is always transmitted, even when there is no fix, so the server knows the device is alive but position is unavailable.

### Standard Profiles

| Profile | Description | Files |
|---------|-------------|-------|
| 0 | System (birth, will, ping, pong, command, provision) | [0.json](profiles/0.json) · [0.md](profiles/0.md) |
| 1 | Basic GPS Telemetry | [1.json](profiles/1.json) · [1.md](profiles/1.md) |
| 2 | Advanced GPS Telemetry — Fleet Vehicle | [2.json](profiles/2.json) · [2.md](profiles/2.md) |
| 3 | Basic Weather Station | [3.json](profiles/3.json) · [3.md](profiles/3.md) |
| 4 | Advanced Weather Station | [4.json](profiles/4.json) · [4.md](profiles/4.md) |
| 5 | Asset Tracker | [5.json](profiles/5.json) · [5.md](profiles/5.md) |
| 6 | Air Quality | [6.json](profiles/6.json) · [6.md](profiles/6.md) |
| 7 | Power/Energy Monitor | [7.json](profiles/7.json) · [7.md](profiles/7.md) |
| 8 | Marine | [8.json](profiles/8.json) · [8.md](profiles/8.md) |
| 9 | Soil/Agricultural Sensor | [9.json](profiles/9.json) · [9.md](profiles/9.md) |

### Example User-Defined Profiles

| Profile | Description | Files |
|---------|-------------|-------|
| 51 | Server Health Monitor | [51.json](profiles/51.json) · [51.md](profiles/51.md) |
| 52 | Model Rocket (uses CRC, microsecond timestamps on flight/event packets) | [52.json](profiles/52.json) · [52.md](profiles/52.md) |

---

## Optional Fields

A field listed in the `optional` array of a packet type's schema definition may be omitted from a transmitted packet when the value is not available (e.g. GPS coordinates when no fix has been obtained).

**CBOR**: omit the key/value pair entirely. Do not encode a zero or null placeholder — a missing key and a zero are semantically different.

**JSON**: omit the key entirely, or set the value to `null`. Servers must treat a missing key and a `null` value equivalently.

Decoders must not error on a missing optional field. Non-optional fields must always be present.

The `optional` array in the schema file is the authoritative list of optional fields per packet type. Fields not listed are required.

---

## Endpoint ID

The `id` field uniquely identifies an endpoint device globally. It is computed as follows:

1. Collect all available hardware serial numbers for the device (e.g. MCU serial, GPS module serial, LoRa module serial, MAC address).
2. Concatenate them in a fixed, implementation-defined order.
3. Compute SHA-256 of the concatenated bytes.
4. Use the **first 8 bytes** (64 bits) of the digest as the `id`.

Combining multiple hardware serials before hashing minimises collision probability even when individual serials are not globally unique.

- The hardware-derived hash is the **default** ID.
- Can be **overridden** via device configuration.
- A **factory reset** reverts to the hardware-derived default.

---

## Acknowledgments and Deduplication

The `ack` field is set by the endpoint to `true` when it wants the server to confirm receipt. The server sends an acknowledgment only when requested, referencing the originating `mid` so the endpoint knows which message was received. Keeping acks optional reduces unnecessary downlink traffic.

### Deduplication

The server deduplicates incoming packets using the key `(id, mid, timestamp)`. A packet sharing all three values with a recently seen packet is a duplicate and is discarded without processing or re-acknowledging.

The deduplication window is **60 seconds** by default. After this window, a `mid` value may be reused without ambiguity — any legitimately re-used value will have a different timestamp, and the previous occurrence will have expired from the dedup cache.

**Mid rollover**: `mid` is uint8 and rolls from 255 back to 0. At 1 packet/second this occurs every ~4 minutes; at 10 packets/second every ~25 seconds. The dedup window handles this correctly: a newly rolled-over `mid` value will have a timestamp at least 256 seconds (at 1 Hz) or 25.6 seconds (at 10 Hz) later than its last use, which combined with the distinct timestamp makes it unambiguous. For profiles with microsecond timestamps (e.g. Profile 52), disambiguation is reliable at any realistic send rate.

---

## Server → Endpoint Messages

The server sends packets to an endpoint to acknowledge requests (`ack: true`), probe liveness (ping), issue commands, or push provisioning updates — all defined in [Profile 0](profiles/0.md). All server→endpoint packets use `target_id` in place of `id` and omit the `ack` field.

### Acknowledgment Packet

Sent by the server in response to a packet with `ack: true`. Uses CBOR encoding. Field order is fixed.

| CBOR Key | Field Name        | Type         | Description                                                                                     |
|----------|-------------------|--------------|-------------------------------------------------------------------------------------------------|
| 0        | `profile`         | uint8        | Profile number of the packet being acknowledged                                                 |
| 1        | `target_id`       | bytes (8)    | `id` of the endpoint being addressed                                                            |
| 2        | `ack_mid`         | uint8        | `mid` of the packet being acknowledged                                                          |
| 3        | `ack_timestamp`   | uint32/uint64 | `timestamp` from the acknowledged packet — matches the timestamp type used by the profile      |
| 4        | `status`          | uint8        | `1` = accepted; `2` = rejected (unknown profile, malformed packet, CRC failure)                |

The `profile` field always matches the profile of the endpoint's original packet. The endpoint matches an incoming ack by looking up `(ack_mid, ack_timestamp)` in its pending-ack table — this is unambiguous even when `mid` has rolled over, because the timestamp distinguishes re-used `mid` values.

`ack_timestamp` uses `uint32` for profiles with second-resolution timestamps, and `uint64` for profiles with microsecond-resolution timestamps (e.g. Profile 52 flight and event packets). The server determines the correct type from the profile schema.

### Command Packets

Commands are defined in [Profile 0](profiles/0.md) (types 5 and 6). The server sends a command using the Profile 0 type 5 packet; the endpoint responds with a Profile 0 type 6 command acknowledgment. See the Profile 0 documentation for command codes and field definitions.

---

## Batch Packets

A batch packet bundles multiple readings into a single transmission. This is used when an endpoint buffers data during a coverage gap and flushes on reconnection — avoiding N separate transmissions.

**Type 255 is reserved across all profiles as the batch type.**

| CBOR Key | Field Name | Type | Description |
|----------|------------|------|-------------|
| 0 | `profile` | uint8 | Profile number — all contained packets must share this profile |
| 1 | `id` | bytes (8) | Endpoint device identifier |
| 2 | `mid` | uint8 | Rolling message ID for the batch packet itself |
| 3 | `ack` | bool | `true` if endpoint requests acknowledgment for the batch |
| 4 | `type` | uint8 | Always `255` for batch |
| 5 | `count` | uint8 | Number of packets in the batch (1–255) |
| 6 | `packets` | array | CBOR array of byte strings; each element is a complete CBOR-encoded packet |

The `mid` and `ack` in each contained packet are preserved. Servers process and deduplicate each contained packet independently. The batch packet's own `mid` is used only to deduplicate the batch transmission itself.

---

## Server-Side Profile Schema

The server stores a mapping of profile number → packet type → field definitions, used to decode incoming CBOR packets. The schema format mirrors the profile `.json` files:

```json
{
  "1": {
    "version": 3,
    "types": { "1": "location" },
    "packets": {
      "1": {
        "direction": "e->s",
        "fields": {
          "0": "profile",
          "1": "id",
          "2": "mid",
          "3": "ack",
          "4": "type",
          "5": "timestamp",
          "6": "fix",
          "7": "lat",
          "8": "lon",
          "9": "alt",
          "10": "accuracy"
        },
        "field_types": {
          "profile": "uint8",
          "id": "bytes",
          "mid": "uint8",
          "ack": "bool",
          "type": "uint8",
          "timestamp": "uint32",
          "fix": "uint8",
          "lat": "float32",
          "lon": "float32",
          "alt": "float32",
          "accuracy": "float32"
        },
        "optional": ["lat", "lon", "alt", "accuracy"]
      }
    }
  }
}
```

When a packet arrives, the server reads `profile` (key 0) and `type` (key 4) first, then looks up the corresponding `fields` mapping to decode the remaining keys.

### Event Code Resolution

Profiles that define events include an `event_codes` map in their schema. The `event_code` uint8 from the packet is resolved server-side to a human-readable name and description using this map. **No human-readable string is transmitted on the wire** — the code alone is sufficient for all server-side processing and display.

---

## Open / TBD

These decisions are unresolved and should be addressed as the spec matures:

- **Standard profile gaps** — profiles 10–50 are unassigned; profiles 5–9 cover asset tracking, air quality, power monitoring, marine, and soil/agricultural sensing
- **Unencrypted endpoints are unauthenticated by design** — any party with access to the channel can read or forge plaintext packets. This is an accepted trade-off: if authentication matters, enable encryption. Encryption and authentication are provided by the same mechanism (per-endpoint keys).
