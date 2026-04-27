"""
OwnTelemetry protocol codec.

Loads profile schemas from a directory and encodes/decodes CBOR (production)
and JSON (debug) wire-format packets. Optional per-endpoint encryption and
CRC-16 are supported.

Usage::

    from owntelemetry import OwnTelemetry

    ot = OwnTelemetry("/path/to/profiles", [0, 51])

    # Cleartext encode — mid is auto-generated and returned in used
    payload, used = ot.encode(51, 1, {"id": endpoint_id, "cpu_load": 45.2, ...})
    mid_used = used["mid"]

    # Encrypted encode — nonce taken from timestamp field if present
    payload, used = ot.encode(51, 1, fields, key=my_key)

    # Cleartext decode
    packet = ot.decode(payload)

    # Decode with key lookup — handles both encrypted and cleartext
    packet = ot.decode(payload, get_key=lambda endpoint_id: key_store.get(endpoint_id))
"""

import json as _json
import struct

import cbor2

from ._profile import Profile, load_profiles

__all__ = ["OwnTelemetry"]


# ── Crypto primitives ─────────────────────────────────────────────────────────

def _xor_crypt(data: bytes, key: bytes, nonce: int) -> bytes:
    nonce_bytes = struct.pack(">I", nonce & 0xFFFFFFFF)
    return bytes(b ^ key[i % 16] ^ nonce_bytes[i % 4] for i, b in enumerate(data))


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _to_bytes(val) -> bytes:
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    if isinstance(val, str):
        return bytes.fromhex(val)
    raise TypeError(f"id must be bytes or hex string, got {type(val).__name__}")


# ── Main class ────────────────────────────────────────────────────────────────

class OwnTelemetry:
    def __init__(self, profiles_dir: str, profiles: list) -> None:
        self._profiles: dict = load_profiles(profiles_dir, profiles)
        self._mids: dict   = {}   # bytes endpoint_id → next mid (int)
        self._nonces: dict = {}   # bytes endpoint_id → nonce counter (int)

    # ── Encode ────────────────────────────────────────────────────────────────

    def encode(
        self,
        profile: int,
        type: int,
        fields: dict,
        *,
        key: bytes = None,
        crc: bool = None,
        json_wire: bool = False,
    ) -> tuple:
        """
        Encode a packet to bytes.

        ``profile`` and ``type`` are set automatically.
        ``mid`` is auto-generated per endpoint (keyed by ``id``/``target_id``)
        and can be overridden by including it in ``fields``.
        ``ack`` defaults to ``False`` for e→s packets if not supplied.

        Encryption (``key``):
            When provided the packet is wrapped in the encrypted wire frame:
            ``[profile 1B][id 8B][nonce 4B][ciphertext][crc 2B if enabled]``
            The nonce is taken from the ``timestamp`` field when present;
            otherwise an internal per-endpoint counter is used.

        CRC (``crc``):
            Defaults to the profile schema setting (``uses_crc``). On decode,
            CRC is verified automatically for profiles that declare it — the
            decoder reads the profile number from the frame header before
            decrypting and knows whether to expect a CRC. Passing ``crc=True``
            for a profile whose schema does not declare CRC will add the bytes
            on encode but the decoder will not strip or verify them.

        Returns ``(payload_bytes, used_fields)`` where ``used_fields`` contains
        every field written to the wire including auto-generated values.
        Bytes fields such as ``id`` are represented as hex strings.
        """
        if profile not in self._profiles:
            raise KeyError(f"Profile {profile} not loaded")
        schema = self._profiles[profile]

        pkt = schema.packets.get(type)
        if pkt is None:
            raise KeyError(f"Profile {profile} type {type} not defined")

        fields = dict(fields)
        fields["profile"] = profile
        fields["type"] = type

        id_field = "target_id" if pkt.direction == "s->e" else "id"
        if id_field in fields:
            fields[id_field] = _to_bytes(fields[id_field])

        if "mid" not in fields:
            ep_key = fields.get(id_field, b"")
            if not isinstance(ep_key, bytes):
                ep_key = b""
            fields["mid"] = self._next_mid(ep_key)

        if pkt.direction == "e->s" and "ack" not in fields:
            fields["ack"] = False

        wire = {}
        for name, cbor_key in pkt.fields_rev.items():
            if name in fields:
                wire[cbor_key] = fields[name]

        used = {}
        for cbor_key, val in wire.items():
            name = pkt.fields.get(cbor_key, f"key_{cbor_key}")
            used[name] = val.hex() if isinstance(val, (bytes, bytearray)) else val

        if key is not None:
            return self._encrypt(profile, fields.get(id_field, b""), wire, used, key, crc, schema)

        if json_wire:
            return _json.dumps(used).encode(), used

        return cbor2.dumps(wire), used

    # ── Decode ────────────────────────────────────────────────────────────────

    def decode(self, data: bytes, *, get_key=None) -> dict:
        """
        Decode a packet from bytes.

        Tries cleartext CBOR first, then JSON, then encrypted (requires
        ``get_key``). When ``get_key`` is provided it is used for all three
        attempts so a server that receives mixed encrypted/cleartext traffic
        can always pass it.

        ``get_key`` must be a callable ``(endpoint_id: bytes) -> bytes | None``.
        Return ``None`` to signal an unknown endpoint (raises ``KeyError``).

        Decoded packets are annotated with ``_profile_name``, ``_type_name``,
        and applicable code-to-name fields such as ``_event_name``.
        Encrypted packets also include ``_encrypted: True``.
        JSON packets include ``_wire_format`` and ``_warning``.
        """
        # 1. Cleartext CBOR
        try:
            raw = cbor2.loads(data)
            if isinstance(raw, dict) and 0 in raw:
                return self._decode_cbor(raw)
        except Exception:
            pass

        # 2. JSON
        try:
            raw = _json.loads(data.decode())
            if isinstance(raw, dict) and "profile" in raw:
                return self._decode_json(raw)
        except Exception:
            pass

        # 3. Encrypted
        if get_key is None:
            raise ValueError(
                "Packet is not cleartext CBOR or JSON — provide get_key= to attempt decryption"
            )
        return self._decode_encrypted(data, get_key)

    # ── Internal encode helpers ───────────────────────────────────────────────

    def _encrypt(self, profile_num, ep_id_bytes, wire, used, key, crc, schema):
        nonce = int(used.get("timestamp") or self._next_nonce(ep_id_bytes)) & 0xFFFFFFFF
        plaintext = cbor2.dumps(wire)
        ciphertext = _xor_crypt(plaintext, key, nonce)
        frame = bytes([profile_num]) + ep_id_bytes + struct.pack(">I", nonce) + ciphertext
        use_crc = schema.uses_crc if crc is None else crc
        if use_crc:
            frame += struct.pack(">H", _crc16(frame))
        return frame, used

    def _next_mid(self, endpoint_id: bytes) -> int:
        current = self._mids.get(endpoint_id, 0)
        self._mids[endpoint_id] = (current + 1) % 256
        return current

    def _next_nonce(self, endpoint_id: bytes) -> int:
        current = self._nonces.get(endpoint_id, 0)
        self._nonces[endpoint_id] = (current + 1) & 0xFFFFFFFF
        return current

    # ── Internal decode helpers ───────────────────────────────────────────────

    def _decode_cbor(self, raw: dict) -> dict:
        profile_num = raw.get(0)
        type_num = raw[4] if 4 in raw else raw.get(3)
        schema = self._profiles.get(profile_num)

        if schema is None:
            return {"_raw": {str(k): v for k, v in raw.items()}, "_error": f"unknown profile {profile_num}"}

        pkt = schema.packets.get(type_num)
        field_map = pkt.fields if pkt is not None else {}

        packet = {}
        for k, v in raw.items():
            name = field_map.get(k, f"key_{k}")
            if name in ("id", "target_id") and isinstance(v, (bytes, bytearray)):
                v = v.hex()
            packet[name] = v

        self._annotate(packet, schema, type_num)
        return packet

    def _decode_json(self, raw: dict) -> dict:
        profile_num = raw.get("profile")
        type_num = raw.get("type")
        schema = self._profiles.get(profile_num)

        packet = dict(raw)
        if schema is not None:
            self._annotate(packet, schema, type_num)
        else:
            packet["_error"] = f"unknown profile {profile_num}"
        packet["_wire_format"] = "json"
        packet["_warning"] = "JSON wire format in use — switch to CBOR for production"
        return packet

    def _decode_encrypted(self, data: bytes, get_key) -> dict:
        if len(data) < 13:
            raise ValueError("Frame too short to be an encrypted packet (need at least 13 bytes)")

        profile_num = data[0]
        endpoint_id = data[1:9]
        nonce       = struct.unpack(">I", data[9:13])[0]

        key = get_key(endpoint_id)
        if key is None:
            raise KeyError(f"No key for endpoint {endpoint_id.hex()}")

        schema = self._profiles.get(profile_num)
        use_crc = schema.uses_crc if schema else False

        if use_crc:
            if len(data) < 15:
                raise ValueError("Frame too short to contain CRC")
            expected = struct.unpack(">H", data[-2:])[0]
            if _crc16(data[:-2]) != expected:
                raise ValueError("CRC check failed — packet may be corrupted or key is wrong")
            ciphertext = data[13:-2]
        else:
            ciphertext = data[13:]

        plaintext = _xor_crypt(ciphertext, key, nonce)

        try:
            raw = cbor2.loads(plaintext)
        except Exception as exc:
            raise ValueError(f"Decrypted payload is not valid CBOR — key may be wrong: {exc}")

        if not isinstance(raw, dict):
            raise ValueError("Decrypted payload is not a CBOR map — key may be wrong")

        inner_id = raw.get(1)
        if isinstance(inner_id, (bytes, bytearray)) and bytes(inner_id) != endpoint_id:
            raise ValueError("Inner packet id does not match frame id")

        packet = self._decode_cbor(raw)
        packet["_encrypted"] = True
        return packet

    def _annotate(self, packet: dict, schema: Profile, type_num) -> None:
        packet["_profile_name"] = schema.name
        packet["_type_name"] = schema.types.get(type_num, f"type_{type_num}")

        if "reason" in packet and schema.reason_codes:
            packet["_reason_name"] = schema.reason_codes.get(packet["reason"], "unknown")
        if "event_code" in packet and schema.event_codes:
            packet["_event_name"] = schema.event_codes.get(packet["event_code"], "unknown")
        if "command_code" in packet and schema.command_codes:
            packet["_command_name"] = schema.command_codes.get(packet["command_code"], "unknown")
        if "provision_code" in packet and schema.provision_codes:
            packet["_provision_code_name"] = schema.provision_codes.get(packet["provision_code"], "unknown")
        # type_num disambiguates "status" between command_ack (6) and provision_ack (8)
        if "status" in packet and type_num == 6 and schema.command_status_codes:
            packet["_command_status"] = schema.command_status_codes.get(packet["status"], "unknown")
        if "status" in packet and type_num == 8 and schema.provision_status_codes:
            packet["_provision_status"] = schema.provision_status_codes.get(packet["status"], "unknown")
