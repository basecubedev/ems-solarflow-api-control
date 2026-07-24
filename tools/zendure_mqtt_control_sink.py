#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure MQTT control-sink mock (dev/test only).

Stands in for a device's write endpoint: subscribes to Zendure
``.../properties/write`` topics, records the commanded ``outputLimit`` per
device, and optionally echoes a telemetry report reflecting the new limit so a
local EMS<->broker loop can be exercised end to end without real hardware.

This is the write-side counterpart of ``zendure_mqtt_mock_service.py`` (which
only publishes telemetry). It never controls anything physical.
"""

import argparse
import json
import sys

# iot/<pk>/<dev>/properties/write and the leading-slash legacy variant.
WRITE_TOPIC_FILTERS = ("iot/+/+/properties/write", "/+/+/properties/write")


def parse_write(topic, payload):
    """Return ``{product_key, device_id, properties}`` for a write, else None."""

    if not isinstance(topic, str):
        return None
    segments = topic.split("/")
    if len(segments) != 5 or segments[3:] != ["properties", "write"]:
        return None
    product_key, device_id = segments[1], segments[2]
    if not product_key or not device_id:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, dict):
        return None
    return {
        "product_key": product_key,
        "device_id": device_id,
        "properties": properties,
    }


def _report_topic(product_key, device_id):
    return f"iot/{product_key}/{device_id}/properties/report"


class ControlSink:
    """Records received writes and optionally echoes telemetry back."""

    def __init__(self, *, echo=False, out=sys.stdout):
        self.echo = echo
        self.out = out
        self.received = []

    def on_write(self, client, topic, payload):
        parsed = parse_write(topic, payload)
        if parsed is None:
            return None
        self.received.append(parsed)
        self.out.write(json.dumps(parsed) + "\n")
        self.out.flush()
        if self.echo and client is not None and "outputLimit" in parsed["properties"]:
            report = {"properties": {"outputLimit": parsed["properties"]["outputLimit"]}}
            client.publish(
                _report_topic(parsed["product_key"], parsed["device_id"]),
                json.dumps(report),
                qos=0,
            )
        return parsed


def _connect(args):
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id)
    except (AttributeError, TypeError):  # paho < 2.0 has no versioned ctor
        client = mqtt.Client(client_id=args.client_id)
    if args.tls:
        client.tls_set()
        if args.tls_insecure:
            client.tls_insecure_set(True)
    if args.username is not None:
        client.username_pw_set(args.username, args.password)
    client.connect(args.host, args.port, keepalive=30)
    return client


def _run_loop(args, sink):
    client = _connect(args)

    def on_connect(c, *_a, **_k):
        for topic in WRITE_TOPIC_FILTERS:
            c.subscribe(topic, qos=0)

    def on_message(c, _userdata, message):
        sink.on_write(c, message.topic, message.payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_forever()


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Record Zendure MQTT outputLimit writes (dev/test only). "
            "Optionally echo telemetry reflecting the commanded value."
        )
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--tls-insecure", action="store_true")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None, help="never logged")
    parser.add_argument("--client-id", default="zendure-mqtt-control-sink")
    parser.add_argument(
        "--echo",
        action="store_true",
        help="publish a telemetry report reflecting each commanded outputLimit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the subscription plan without connecting",
    )
    return parser


def run(argv=None, out=None):
    out = out or sys.stdout
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.tls_insecure and not args.tls:
        parser.error("--tls-insecure requires --tls")

    if args.dry_run:
        for topic in WRITE_TOPIC_FILTERS:
            out.write(f"subscribe {topic}\n")
        return 0

    sink = ControlSink(echo=args.echo, out=out)
    try:
        _run_loop(args, sink)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"error: control sink failed on {args.host}:{args.port}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
