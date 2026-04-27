import json
import os
from dataclasses import dataclass, field


@dataclass
class PacketType:
    direction: str
    fields: dict        # int key  → field name
    fields_rev: dict    # field name → int key
    optional: set


@dataclass
class Profile:
    number: int
    name: str
    types: dict                              # int → name
    packets: dict                            # int → PacketType
    uses_crc: bool              = False      # any packet type declares a "crc" field
    reason_codes: dict          = field(default_factory=dict)
    command_codes: dict         = field(default_factory=dict)
    command_status_codes: dict  = field(default_factory=dict)
    event_codes: dict           = field(default_factory=dict)
    provision_codes: dict       = field(default_factory=dict)
    provision_status_codes: dict = field(default_factory=dict)


def _int_keys(d: dict) -> dict:
    return {int(k): v for k, v in d.items()}


def load_profile(profiles_dir: str, number: int) -> Profile:
    path = os.path.join(profiles_dir, f"{number}.json")
    with open(path) as f:
        data = json.load(f)

    packets = {}
    uses_crc = False
    for type_str, pkt in data.get("packets", {}).items():
        type_num = int(type_str)
        fields = _int_keys(pkt.get("fields", {}))
        if "crc" in fields.values():
            uses_crc = True
        packets[type_num] = PacketType(
            direction=pkt.get("direction", "e->s"),
            fields=fields,
            fields_rev={v: k for k, v in fields.items()},
            optional=set(pkt.get("optional", [])),
        )

    return Profile(
        number=int(data["profile"]),
        name=data["name"],
        types=_int_keys(data.get("types", {})),
        packets=packets,
        uses_crc=uses_crc,
        reason_codes=_int_keys(data.get("reason_codes", {})),
        command_codes=_int_keys(data.get("command_codes", {})),
        command_status_codes=_int_keys(data.get("command_status_codes", {})),
        event_codes=_int_keys(data.get("event_codes", {})),
        provision_codes=_int_keys(data.get("provision_codes", {})),
        provision_status_codes=_int_keys(data.get("provision_status_codes", {})),
    )


def load_profiles(profiles_dir: str, numbers: list) -> dict:
    return {n: load_profile(profiles_dir, n) for n in numbers}
