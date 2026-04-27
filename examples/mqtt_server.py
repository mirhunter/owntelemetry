#!/usr/bin/env python3
"""
OwnTelemetry MQTT server — subscribes to all endpoints and logs decoded packets.

Subscribes to: <base_topic>/#
Logs to: <log_file> (JSON Lines, one packet per line) and stdout.

Config: mqtt_server.conf (INI format, gitignored)
Requires: paho-mqtt, cbor2, psutil  (pip install -r requirements.txt)
"""

import configparser
import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "python"))
from owntelemetry import OwnTelemetry

CONFIG_FILE  = os.path.join(os.path.dirname(__file__), "mqtt_server.conf")
PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiles")

ALL_PROFILES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 51, 52]


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


def make_callbacks(ot, logger, base_topic):
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
            packet = ot.decode(msg.payload)
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

    ot = OwnTelemetry(PROFILES_DIR, ALL_PROFILES)

    logger = setup_logging(log_file)
    logger.info(json.dumps({"_event": "starting", "_broker": f"{host}:{port}", "_time": int(time.time())}))

    on_connect, on_message, on_disconnect = make_callbacks(ot, logger, base_topic)

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
