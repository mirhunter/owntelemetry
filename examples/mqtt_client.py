#!/usr/bin/env python3
"""
OwnTelemetry MQTT client — Profile 0 (System) + Profile 51 (Server Health Monitor).

Publishes to:       <base_topic>/<endpoint_id_hex>
Subscribes to:      <base_topic>/<endpoint_id_hex>/cmd  (server→endpoint commands)

Config: mqtt_client.conf (INI format, gitignored)
Requires: paho-mqtt, cbor2, psutil

Flags:
    --debug     Print JSON of each packet before publishing/receiving
    --json      Publish packets as JSON instead of CBOR (development/debug wire format)
"""

import argparse
import configparser
import hashlib
import json
import os
import socket
import sys
import time

import cbor2
import paho.mqtt.client as mqtt
import psutil

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "mqtt_client.conf")

# ── Profile 0 (System) ────────────────────────────────────────────────────────

P0               = 0
P0_BIRTH         = 1
P0_WILL          = 2
P0_PING          = 3
P0_PONG          = 4
P0_COMMAND       = 5
P0_COMMAND_ACK   = 6
P0_PROVISION     = 7
P0_PROVISION_ACK = 8

REASON_SHUTDOWN = 1
REASON_ERROR    = 4

CMD_SET_INTERVAL  = 1
CMD_REBOOT        = 2
CMD_REQUEST_BIRTH = 3
CMD_FACTORY_RESET = 4

CMD_OK       = 1
CMD_REJECTED = 2
CMD_UNKNOWN  = 3

PROV_SET_KEY  = 1
PROV_READY    = 1
PROV_REJECTED = 2

# ── Profile 51 (Server Health Monitor) ───────────────────────────────────────

PROFILE = 51

EVENT_HIGH_CPU_LOAD     = 1
EVENT_HIGH_CPU_TEMP     = 2
EVENT_HIGH_MEMORY_USAGE = 3
EVENT_HIGH_DISK_USAGE   = 4
EVENT_HIGH_SWAP_USAGE   = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONFIG_FILE):
        sys.exit(f"Config file not found: {CONFIG_FILE}")
    return cfg


def make_endpoint_id():
    hostname = socket.gethostname().encode()
    return hashlib.sha256(hostname).digest()[:8]


def encode(wire, named, json_wire):
    if json_wire:
        return json.dumps(named).encode()
    return cbor2.dumps(wire)


def debug_print(label, named_packet):
    print(f"DEBUG {label}:")
    print(json.dumps(named_packet, indent=2))


# ── Profile 0 packet builders (e→s) ──────────────────────────────────────────

def build_birth(endpoint_id, mid, profiles, interval, json_wire=False):
    named = {
        "profile": P0, "id": endpoint_id.hex(), "mid": mid, "ack": False,
        "type": P0_BIRTH, "profiles": profiles, "interval": interval,
    }
    wire = {0: P0, 1: endpoint_id, 2: mid, 3: False, 4: P0_BIRTH, 5: profiles, 7: interval}
    return encode(wire, named, json_wire), named


def build_will_packet(endpoint_id, mid, reason, json_wire=False):
    ts = int(time.time())
    named = {
        "profile": P0, "id": endpoint_id.hex(), "mid": mid, "ack": False,
        "type": P0_WILL, "reason": reason, "timestamp": ts,
    }
    wire = {0: P0, 1: endpoint_id, 2: mid, 3: False, 4: P0_WILL, 5: reason, 6: ts}
    return encode(wire, named, json_wire), named


def build_pong(endpoint_id, mid, ping_mid, json_wire=False):
    named = {
        "profile": P0, "id": endpoint_id.hex(), "mid": mid, "ack": False,
        "type": P0_PONG, "ping_mid": ping_mid,
    }
    wire = {0: P0, 1: endpoint_id, 2: mid, 3: False, 4: P0_PONG, 5: ping_mid}
    return encode(wire, named, json_wire), named


def build_command_ack(endpoint_id, mid, command_mid, status, json_wire=False):
    named = {
        "profile": P0, "id": endpoint_id.hex(), "mid": mid, "ack": False,
        "type": P0_COMMAND_ACK, "command_mid": command_mid, "status": status,
    }
    wire = {0: P0, 1: endpoint_id, 2: mid, 3: False, 4: P0_COMMAND_ACK, 5: command_mid, 6: status}
    return encode(wire, named, json_wire), named


def build_provision_ack(endpoint_id, mid, provision_mid, status, json_wire=False):
    named = {
        "profile": P0, "id": endpoint_id.hex(), "mid": mid, "ack": False,
        "type": P0_PROVISION_ACK, "provision_mid": provision_mid, "status": status,
    }
    wire = {0: P0, 1: endpoint_id, 2: mid, 3: False, 4: P0_PROVISION_ACK, 5: provision_mid, 6: status}
    return encode(wire, named, json_wire), named


# ── Profile 51 packet builders ────────────────────────────────────────────────

def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            if key in temps and temps[key]:
                return temps[key][0].current
    except (AttributeError, NotImplementedError):
        pass
    return 0.0


def get_net_rate(prev_counters, elapsed):
    counters = psutil.net_io_counters()
    if prev_counters is None or elapsed <= 0:
        return counters, 0, 0
    rx_kb = max(0, (counters.bytes_recv - prev_counters.bytes_recv) / elapsed / 1024)
    tx_kb = max(0, (counters.bytes_sent - prev_counters.bytes_sent) / elapsed / 1024)
    return counters, int(rx_kb), int(tx_kb)


def build_status(endpoint_id, mid, net_rx, net_tx, json_wire=False):
    cpu_load = psutil.cpu_percent(interval=1)
    cpu_temp = get_cpu_temp()
    load_1, load_5, load_15 = os.getloadavg()
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    uptime = int(time.time() - psutil.boot_time())
    procs  = len(psutil.pids())
    ts     = int(time.time())

    named = {
        "profile": PROFILE, "id": endpoint_id.hex(), "mid": mid, "ack": False,
        "type": 1, "timestamp": ts,
        "cpu_load": round(cpu_load, 2), "cpu_temp": round(cpu_temp, 2),
        "load_1": round(load_1, 3), "load_5": round(load_5, 3), "load_15": round(load_15, 3),
        "memory_used": round(mem.percent, 2), "swap_used": round(swap.percent, 2),
        "disk_used": round(disk.percent, 2),
        "net_rx": net_rx, "net_tx": net_tx,
        "uptime": uptime, "processes": procs,
    }
    wire = {
        0: PROFILE, 1: endpoint_id, 2: mid, 3: False, 4: 1, 5: ts,
        6: float(cpu_load), 7: float(cpu_temp),
        8: float(load_1), 9: float(load_5), 10: float(load_15),
        11: float(mem.percent), 12: float(swap.percent), 13: float(disk.percent),
        14: net_rx, 15: net_tx, 16: uptime, 17: procs,
    }
    metrics = {
        "cpu_load": cpu_load, "cpu_temp": cpu_temp,
        "memory_used": mem.percent, "disk_used": disk.percent, "swap_used": swap.percent,
    }
    return encode(wire, named, json_wire), metrics, named


def build_alert(endpoint_id, mid, event_code, value, json_wire=False):
    ts = int(time.time())
    named = {
        "profile": PROFILE, "id": endpoint_id.hex(), "mid": mid, "ack": True,
        "type": 2, "event_code": event_code, "timestamp": ts, "value": round(value, 2),
    }
    wire = {
        0: PROFILE, 1: endpoint_id, 2: mid, 3: True,
        4: 2, 5: event_code, 6: ts, 7: float(value),
    }
    return encode(wire, named, json_wire), named


# ── Profile 0 incoming packet handling (s→e) ─────────────────────────────────

def handle_command(command_code, command_data, state):
    if command_code == CMD_SET_INTERVAL:
        try:
            if isinstance(command_data, (bytes, bytearray)):
                new_interval = int.from_bytes(command_data, "big")
            else:
                new_interval = int(command_data)
            state["interval"] = max(1, new_interval)
            print(f"CMD    set_interval → {state['interval']}s")
            return CMD_OK
        except Exception:
            return CMD_REJECTED

    elif command_code == CMD_REBOOT:
        print("CMD    reboot — shutting down")
        state["shutdown"] = True
        return CMD_OK

    elif command_code == CMD_REQUEST_BIRTH:
        state["request_birth"] = True
        print("CMD    request_birth")
        return CMD_OK

    elif command_code == CMD_FACTORY_RESET:
        print("CMD    factory_reset rejected (not supported)")
        return CMD_REJECTED

    else:
        print(f"CMD    unknown command code {command_code}")
        return CMD_UNKNOWN


def handle_server_packet(data, endpoint_id, mid_counter, topic, client, state, debug, json_wire):
    try:
        raw = cbor2.loads(data)
        is_json = False
        profile  = raw.get(0)
        pkt_type = raw.get(3)   # s→e: no ack field, type is at key 3
        pkt_mid  = raw.get(2)
    except Exception:
        try:
            raw = json.loads(data.decode())
            is_json = True
            profile  = raw.get("profile")
            pkt_type = raw.get("type")
            pkt_mid  = raw.get("mid")
        except Exception:
            print("RECV   could not decode server packet")
            return

    if profile != P0:
        print(f"RECV   unexpected profile {profile} on cmd topic")
        return

    if pkt_type == P0_PING:
        mid = mid_counter[0] % 256
        mid_counter[0] += 1
        payload, named = build_pong(endpoint_id, mid, pkt_mid, json_wire)
        if debug:
            debug_print("pong", named)
        client.publish(topic, payload, qos=1)
        print(f"PONG   ping_mid={pkt_mid}")

    elif pkt_type == P0_COMMAND:
        command_code = raw.get("command_code") if is_json else raw.get(4)
        command_data = raw.get("command_data") if is_json else raw.get(5)
        status = handle_command(command_code, command_data, state)
        mid = mid_counter[0] % 256
        mid_counter[0] += 1
        payload, named = build_command_ack(endpoint_id, mid, pkt_mid, status, json_wire)
        if debug:
            debug_print("command_ack", named)
        client.publish(topic, payload, qos=1)
        print(f"CMD_ACK command_code={command_code} status={status}")

    elif pkt_type == P0_PROVISION:
        prov_code = raw.get("provision_code") if is_json else raw.get(4)
        mid = mid_counter[0] % 256
        mid_counter[0] += 1
        payload, named = build_provision_ack(endpoint_id, mid, pkt_mid, PROV_REJECTED, json_wire)
        if debug:
            debug_print("provision_ack", named)
        client.publish(topic, payload, qos=1)
        print(f"PROV_ACK provision_code={prov_code} status=rejected (not implemented)")

    else:
        print(f"RECV   unknown Profile 0 type {pkt_type}")


# ── Check alerts (Profile 51) ─────────────────────────────────────────────────

def check_alerts(metrics, thresholds, endpoint_id, mid_counter, topic, client, active_alerts, debug, json_wire=False):
    checks = [
        (EVENT_HIGH_CPU_LOAD,     "cpu_load",    thresholds.getfloat("cpu_load")),
        (EVENT_HIGH_CPU_TEMP,     "cpu_temp",    thresholds.getfloat("cpu_temp")),
        (EVENT_HIGH_MEMORY_USAGE, "memory_used", thresholds.getfloat("memory_used")),
        (EVENT_HIGH_DISK_USAGE,   "disk_used",   thresholds.getfloat("disk_used")),
        (EVENT_HIGH_SWAP_USAGE,   "swap_used",   thresholds.getfloat("swap_used")),
    ]
    for event_code, key, threshold in checks:
        value = metrics[key]
        was_active = active_alerts.get(event_code, False)
        if value >= threshold and not was_active:
            mid = mid_counter[0] % 256
            mid_counter[0] += 1
            payload, named = build_alert(endpoint_id, mid, event_code, value, json_wire)
            if debug:
                debug_print("alert", named)
            client.publish(topic, payload, qos=1)
            print(f"ALERT  event_code={event_code} {key}={value:.1f} (threshold={threshold})")
            active_alerts[event_code] = True
        elif value < threshold:
            active_alerts[event_code] = False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OwnTelemetry MQTT client — Profile 0 + 51")
    parser.add_argument("--debug", action="store_true", help="Print JSON of each packet before publishing")
    parser.add_argument("--json", action="store_true", help="Publish packets as JSON instead of CBOR (development/debug wire format)")
    args = parser.parse_args()

    cfg = load_config()

    host       = cfg.get("mqtt", "server")
    port       = cfg.getint("mqtt", "port")
    username   = cfg.get("mqtt", "username")
    password   = cfg.get("mqtt", "password")
    base_topic = cfg.get("mqtt", "base_topic", fallback="ot")
    interval   = cfg.getint("client", "interval", fallback=30)
    thresholds = cfg["thresholds"]
    debug      = args.debug or cfg.getboolean("client", "debug", fallback=False)
    json_wire  = args.json  or cfg.getboolean("client", "json",  fallback=False)

    endpoint_id     = make_endpoint_id()
    endpoint_id_hex = endpoint_id.hex()
    topic           = f"{base_topic}/{endpoint_id_hex}"
    cmd_topic       = f"{base_topic}/{endpoint_id_hex}/cmd"

    print(f"Endpoint ID : {endpoint_id_hex}")
    print(f"Topic       : {topic}")
    print(f"Cmd topic   : {cmd_topic}")
    print(f"Interval    : {interval}s")
    if debug:
        print("Debug       : on")
    if json_wire:
        print("Wire format : JSON")

    mid_counter   = [0]
    active_alerts = {}
    state         = {"interval": interval, "request_birth": False, "shutdown": False}

    # Last will fires on unexpected disconnect (reason=error); timestamp is connection time
    will_payload, _ = build_will_packet(endpoint_id, 255, REASON_ERROR, json_wire)

    client = mqtt.Client()
    client.username_pw_set(username, password)
    client.will_set(topic, will_payload, qos=1, retain=False)

    def on_connect(mqttc, userdata, flags, rc):
        if rc != 0:
            print(f"MQTT connect failed rc={rc}")
            return
        mqttc.subscribe(cmd_topic, qos=1)
        mid = mid_counter[0] % 256
        mid_counter[0] += 1
        payload, named = build_birth(endpoint_id, mid, [P0, PROFILE], state["interval"], json_wire)
        if debug:
            debug_print("birth", named)
        mqttc.publish(topic, payload, qos=1)
        print(f"BIRTH  profiles=[{P0}, {PROFILE}] interval={state['interval']}s")

    def on_message(mqttc, userdata, msg):
        handle_server_packet(msg.payload, endpoint_id, mid_counter, topic, mqttc, state, debug, json_wire)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port)
    client.loop_start()

    prev_counters = None
    prev_time     = time.monotonic()

    try:
        while not state["shutdown"]:
            now     = time.monotonic()
            elapsed = now - prev_time
            prev_time = now

            prev_counters, net_rx, net_tx = get_net_rate(prev_counters, elapsed)

            if state["request_birth"]:
                state["request_birth"] = False
                mid = mid_counter[0] % 256
                mid_counter[0] += 1
                payload, named = build_birth(endpoint_id, mid, [P0, PROFILE], state["interval"], json_wire)
                if debug:
                    debug_print("birth", named)
                client.publish(topic, payload, qos=1)
                print(f"BIRTH  profiles=[{P0}, {PROFILE}] interval={state['interval']}s")

            mid = mid_counter[0] % 256
            mid_counter[0] += 1

            payload, metrics, named = build_status(endpoint_id, mid, net_rx, net_tx, json_wire)
            if debug:
                debug_print("status", named)
            client.publish(topic, payload, qos=0)
            print(
                f"STATUS mid={mid} cpu={metrics['cpu_load']:.1f}% "
                f"mem={metrics['memory_used']:.1f}% disk={metrics['disk_used']:.1f}%"
            )

            check_alerts(metrics, thresholds, endpoint_id, mid_counter, topic, client, active_alerts, debug, json_wire)

            # Sleep in 1s increments so interval/shutdown changes take effect promptly
            deadline = time.monotonic() + state["interval"]
            while not state["shutdown"] and time.monotonic() < deadline:
                time.sleep(1)

    except KeyboardInterrupt:
        pass

    # Graceful shutdown: send will with reason=shutdown before disconnecting
    payload, named = build_will_packet(endpoint_id, mid_counter[0] % 256, REASON_SHUTDOWN, json_wire)
    if debug:
        debug_print("will", named)
    client.publish(topic, payload, qos=1)
    print("WILL   reason=shutdown")
    time.sleep(0.5)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
