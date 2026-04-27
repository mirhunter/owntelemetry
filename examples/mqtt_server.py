#!/usr/bin/env python3
"""
OwnTelemetry MQTT server — subscribes to all endpoints and logs decoded packets.

Subscribes to: <base_topic>/#
Logs to: <log_file> (JSON Lines, one packet per line) and stdout.

Config: mqtt_server.conf (INI format, gitignored)
Requires: paho-mqtt, cbor2
"""

import configparser
import json
import logging
import os
import sys
import time

import cbor2
import paho.mqtt.client as mqtt

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "mqtt_server.conf")

# ── Schema ────────────────────────────────────────────────────────────────────

PROFILES = {
    0: {
        "name": "System",
        "types": {
            1: "birth", 2: "will", 3: "ping", 4: "pong",
            5: "command", 6: "command_ack", 7: "provision", 8: "provision_ack",
        },
        "reason_codes": {1: "shutdown", 2: "sleep", 3: "restart", 4: "error"},
        "command_codes": {1: "set_interval", 2: "reboot", 3: "request_birth", 4: "factory_reset"},
        "command_status_codes": {1: "ok", 2: "rejected", 3: "unknown_command", 4: "busy"},
        "provision_codes": {1: "set_key"},
        "provision_status_codes": {1: "ready", 2: "rejected"},
        # e→s packets: standard header (0=profile, 1=id, 2=mid, 3=ack, 4=type)
        # s→e packets: abbreviated header (0=profile, 1=target_id, 2=mid, 3=type)
        "fields": {
            1: {0: "profile", 1: "id",        2: "mid", 3: "ack", 4: "type", 5: "profiles", 6: "firmware", 7: "interval"},
            2: {0: "profile", 1: "id",        2: "mid", 3: "ack", 4: "type", 5: "reason",   6: "timestamp"},
            3: {0: "profile", 1: "target_id", 2: "mid", 3: "type"},
            4: {0: "profile", 1: "id",        2: "mid", 3: "ack", 4: "type", 5: "ping_mid"},
            5: {0: "profile", 1: "target_id", 2: "mid", 3: "type", 4: "command_code",   5: "command_data"},
            6: {0: "profile", 1: "id",        2: "mid", 3: "ack", 4: "type", 5: "command_mid",   6: "status", 7: "result"},
            7: {0: "profile", 1: "target_id", 2: "mid", 3: "type", 4: "provision_code", 5: "data"},
            8: {0: "profile", 1: "id",        2: "mid", 3: "ack", 4: "type", 5: "provision_mid", 6: "status"},
        },
    },
    51: {
        "name": "Server Health Monitor",
        "types": {1: "status", 2: "alert"},
        "event_codes": {
            1: "high_cpu_load",
            2: "high_cpu_temp",
            3: "high_memory_usage",
            4: "high_disk_usage",
            5: "high_swap_usage",
        },
        "fields": {
            1: {
                0: "profile", 1: "id", 2: "mid", 3: "ack", 4: "type",
                5: "timestamp", 6: "cpu_load", 7: "cpu_temp",
                8: "load_1", 9: "load_5", 10: "load_15",
                11: "memory_used", 12: "swap_used", 13: "disk_used",
                14: "net_rx", 15: "net_tx", 16: "uptime", 17: "processes",
            },
            2: {
                0: "profile", 1: "id", 2: "mid", 3: "ack", 4: "type",
                5: "event_code", 6: "timestamp", 7: "value",
            },
        },
    },
}


def decode_packet(data):
    # Try CBOR first (production wire format)
    json_wire = False
    try:
        raw = cbor2.loads(data)
        profile_num = raw.get(0)
        # s→e packets omit the ack field: type is at key 3 instead of key 4
        if 4 in raw:
            type_num = raw[4]   # e→s standard header
        else:
            type_num = raw.get(3)   # s→e abbreviated header
    except Exception:
        # Fall back to JSON (development/debug wire format)
        raw = json.loads(data.decode())
        json_wire = True
        profile_num = raw.get("profile")
        type_num    = raw.get("type")

    schema = PROFILES.get(profile_num)

    if json_wire:
        packet = dict(raw)
        if schema is None:
            packet["_error"] = f"unknown profile {profile_num}"
        else:
            _annotate(packet, schema, type_num, is_json=True)
        packet["_wire_format"] = "json"
        packet["_warning"]     = "JSON wire format in use — switch to CBOR for production"
        return packet

    if schema is None:
        return {"_raw": {str(k): v for k, v in raw.items()}, "_error": f"unknown profile {profile_num}"}

    field_map = schema["fields"].get(type_num, {})
    packet = {}

    for k, v in raw.items():
        name = field_map.get(k, f"key_{k}")
        if name in ("id", "target_id") and isinstance(v, bytes):
            v = v.hex()
        packet[name] = v

    _annotate(packet, schema, type_num, is_json=False)
    return packet


def _annotate(packet, schema, type_num, is_json):
    packet["_profile_name"] = schema["name"]
    packet["_type_name"]    = schema["types"].get(type_num, f"type_{type_num}")

    # Profile 0 specific annotations
    if "reason" in packet and "reason_codes" in schema:
        packet["_reason_name"] = schema["reason_codes"].get(packet["reason"], "unknown")

    if "command_code" in packet and "command_codes" in schema:
        packet["_command_name"] = schema["command_codes"].get(packet["command_code"], "unknown")

    if "status" in packet and "command_status_codes" in schema and type_num == 6:
        packet["_command_status"] = schema["command_status_codes"].get(packet["status"], "unknown")

    if "provision_code" in packet and "provision_codes" in schema:
        packet["_provision_code_name"] = schema["provision_codes"].get(packet["provision_code"], "unknown")

    if "status" in packet and "provision_status_codes" in schema and type_num == 8:
        packet["_provision_status"] = schema["provision_status_codes"].get(packet["status"], "unknown")

    # Profile 51 annotation
    if "event_code" in packet and "event_codes" in schema:
        packet["_event_name"] = schema["event_codes"].get(packet["event_code"], "unknown")


def setup_logging(log_file):
    logger = logging.getLogger("owntelemetry")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(message)s")

    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def load_config():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONFIG_FILE):
        sys.exit(f"Config file not found: {CONFIG_FILE}")
    return cfg


def make_callbacks(logger, base_topic):
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            sub = f"{base_topic}/#"
            client.subscribe(sub)
            logger.info(json.dumps({"_event": "connected", "_subscribed": sub, "_time": int(time.time())}))
        else:
            logger.error(json.dumps({"_event": "connect_failed", "_rc": rc, "_time": int(time.time())}))

    def on_message(client, userdata, msg):
        received_at = int(time.time())
        try:
            packet = decode_packet(msg.payload)
        except Exception as exc:
            packet = {"_error": str(exc), "_raw_bytes": msg.payload.hex()}

        packet["_topic"]       = msg.topic
        packet["_received_at"] = received_at
        logger.info(json.dumps(packet, default=str))

    def on_disconnect(client, userdata, rc):
        logger.info(json.dumps({"_event": "disconnected", "_rc": rc, "_time": int(time.time())}))

    return on_connect, on_message, on_disconnect


def main():
    cfg = load_config()

    host       = cfg.get("mqtt", "server")
    port       = cfg.getint("mqtt", "port")
    username   = cfg.get("mqtt", "username")
    password   = cfg.get("mqtt", "password")
    base_topic = cfg.get("mqtt", "base_topic", fallback="ot")
    log_file   = cfg.get("server", "log_file", fallback="owntelemetry.log")

    logger = setup_logging(log_file)
    logger.info(json.dumps({"_event": "starting", "_broker": f"{host}:{port}", "_time": int(time.time())}))

    on_connect, on_message, on_disconnect = make_callbacks(logger, base_topic)

    client = mqtt.Client()
    client.username_pw_set(username, password)
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    client.connect(host, port)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        logger.info(json.dumps({"_event": "shutdown", "_time": int(time.time())}))


if __name__ == "__main__":
    main()
