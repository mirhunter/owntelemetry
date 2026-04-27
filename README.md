# OwnTelemetry

A self-hosted telemetry protocol for GPS, vehicle, weather, and other sensor data. Transport-agnostic — runs over IP, LoRa, high-bandwidth radio, or any medium that can carry bytes. Encoded compactly by default, optimised for constrained transports but suitable for any bandwidth budget.

Licensed under [CC0 1.0](LICENSE) (public domain). No cloud dependency. You own your data.

---

## Protocol Overview

Every packet is a CBOR-encoded map with a fixed 5-field header followed by profile-specific fields:

| Field | Key | Description |
|-------|-----|-------------|
| `profile` | 0 | Identifies the schema (0–255) |
| `id` | 1 | 8-byte endpoint identifier (SHA-256 of hardware serials) |
| `mid` | 2 | Rolling message ID (uint8); dedup key with `timestamp` |
| `ack` | 3 | Endpoint requests server acknowledgment |
| `type` | 4 | Packet type within the profile |

**CBOR** is the production wire format — integer keys replace field name strings, keeping packets compact. **JSON** is supported for development and debugging only.

Optional **per-endpoint encryption** (XOR keystream, 16-byte key) and optional **CRC-16** are available for profiles and deployments that need them.

See **[booklet.md](booklet.md)** for the full protocol specification, including encoding details, encryption, CRC, acknowledgments, deduplication, and batch packets.

---

## Profiles

### System (required)

| Profile | Description |
|---------|-------------|
| 0 | System — birth, will, ping/pong, command, provision |

All conformant endpoints implement Profile 0. It handles device lifecycle (birth/will announcements), server liveness probes (ping/pong), remote commands (`set_interval`, `reboot`, `request_birth`, `factory_reset`), and remote key provisioning.

### Standard (1–50)

| Profile | Description |
|---------|-------------|
| 1 | Basic GPS Telemetry |
| 2 | Advanced GPS Telemetry — Fleet Vehicle |
| 3 | Basic Weather Station |
| 4 | Advanced Weather Station |
| 5 | Asset Tracker |
| 6 | Air Quality |
| 7 | Power/Energy Monitor |
| 8 | Marine |
| 9 | Soil/Agricultural Sensor |

### Open (51–255)

User- and server-defined profiles. Two examples are included:

| Profile | Description |
|---------|-------------|
| 51 | Server Health Monitor (CPU, memory, disk, network) |
| 52 | Model Rocket (CRC enabled, microsecond timestamps) |

Profile definitions live in [`profiles/`](profiles/) as `<n>.json` (machine-readable schema) and `<n>.md` (human-readable field tables and examples).

---

## Quick Start — MQTT Example

The [`examples/`](examples/) directory contains a working proof-of-concept MQTT implementation using Profile 0 + Profile 51 (Server Health Monitor).

### Prerequisites

```bash
pip install -r examples/requirements.txt
```

### Configure

Copy the example config files and fill in your MQTT broker details:

```bash
cp examples/mqtt_client.conf.example examples/mqtt_client.conf
cp examples/mqtt_server.conf.example examples/mqtt_server.conf
# edit both files with your broker host, port, username, and password
```

### Run

In one terminal, start the server (logs all received packets to stdout and `owntelemetry.log`):

```bash
python3 examples/mqtt_server.py
```

In another terminal, start the client (publishes system metrics every 30 seconds):

```bash
python3 -u examples/mqtt_client.py
```

On startup the client sends a **birth** packet declaring its profiles and reporting interval. On clean exit (`Ctrl-C`) it sends a **will** packet with `reason=shutdown`. The broker delivers the MQTT last-will automatically on unexpected disconnect.

### Useful flags

| Flag | Effect |
|------|--------|
| `--debug` | Print JSON of each packet to the terminal before publishing |
| `--json` | Publish packets as JSON instead of CBOR — readable with `mosquitto_sub` |

```bash
# Watch raw traffic with mosquitto_sub while running the client in JSON mode:
python3 -u examples/mqtt_client.py --json --debug
mosquitto_sub -h <broker> -u <user> -P <pass> -t "ot/#" -v
```

### Topic structure

| Topic | Direction | Description |
|-------|-----------|-------------|
| `ot/<endpoint_id_hex>` | endpoint → server | Telemetry, alerts, lifecycle packets |
| `ot/<endpoint_id_hex>/cmd` | server → endpoint | Ping, commands, provisioning |

The base topic (`ot`) is configurable in the conf file.

---

## Repository Layout

```
booklet.md          Full protocol specification
profiles/
  0.json / 0.md     Profile 0 — System
  1.json / 1.md     Profile 1 — Basic GPS Telemetry
  ...               (all standard and example profiles)
examples/
  mqtt_client.py    Reference MQTT client (Profile 0 + 51)
  mqtt_server.py    Reference MQTT server (decodes all known profiles)
  *.conf.example    Config templates (copy and fill in credentials)
LICENSE             CC0 1.0 Universal
```

---

## Status

The core protocol is stable. Profile 51 and 52 are example user-defined profiles and may evolve. Profiles 10–50 are reserved for future standard profiles.

Planned examples: LoRa transport, LoRa-MQTT bridge.

Contributions and profile proposals welcome via issues and pull requests.
