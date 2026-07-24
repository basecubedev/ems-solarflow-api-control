#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure MQTT mock telemetry publisher (dev/test only).

Publishes known-good Zendure telemetry schemas to an MQTT broker so EMS/Admin
MQTT work has reproducible data without real hardware. This is telemetry only:
it never publishes write/control topics, so nothing it emits can drive a device.

Schema families mirror the Admin discovery classifier in
``admin/mqtt_topic_discovery.py``.
"""

import argparse
import json
import sys
import time

SCHEMA_ZENSDK = "zensdk-scalar"
SCHEMA_LEGACY_IOT = "legacy-iot-json"
SCHEMA_LEGACY_SLASH = "legacy-slash-json"
SCHEMA_CLOUD = "cloud-scalar"
SCHEMA_ALL = "all"

SCHEMA_FAMILIES = (
    SCHEMA_ZENSDK,
    SCHEMA_LEGACY_IOT,
    SCHEMA_LEGACY_SLASH,
    SCHEMA_CLOUD,
)

# Scalar metrics published one topic each under the Zendure HA-scalar tree.
ZENSDK_METRICS = (
    "electricLevel",
    "solarInputPower",
    "packInputPower",
    "outputHomePower",
    "outputPackPower",
    "inputLimit",
    "outputLimit",
    "acMode",
    "packNum",
    "rssi",
)

# Cloud-prefixed scalar tree only carries a small display subset.
CLOUD_METRICS = ("electricLevel", "solarInputPower", "outputHomePower")

# Keys included in the legacy JSON `properties` object.
LEGACY_PROPERTY_KEYS = (
    "electricLevel",
    "solarInputPower",
    "outputHomePower",
    "packInputPower",
    "outputPackPower",
    "outputLimit",
    "inputLimit",
    "acMode",
    "packState",
    "rssi",
)


def _telemetry(tick):
    """Deterministic telemetry for a given tick.

    Values vary a little between ticks so consumers see movement, but the
    variation is a pure function of ``tick`` to keep tests reproducible.
    """

    solar = 620 + (tick % 5) * 12
    home = 180 + (tick % 3) * 5
    return {
        "electricLevel": 43,
        "solarInputPower": solar,
        "packInputPower": 0,
        "outputHomePower": home,
        "outputPackPower": home,
        "inputLimit": 0,
        "outputLimit": 301,
        "acMode": 2,
        "packNum": 1,
        "packState": 2,
        "rssi": -70,
    }


def _record(topic, payload, qos, retain):
    if not isinstance(payload, str):
        payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return {"topic": topic, "payload": payload, "qos": int(qos), "retain": bool(retain)}


def _legacy_payload(args, telem):
    properties = {key: telem[key] for key in LEGACY_PROPERTY_KEYS}
    pack = {
        "sn": f"{args.device_sn}-PACK1",
        "socLevel": telem["electricLevel"],
        "state": 2,
        "power": telem["outputHomePower"],
        "maxTemp": 2961,
        "totalVol": 4930,
        "maxVol": 329,
        "minVol": 328,
    }
    return {
        "sn": args.device_sn,
        "product": args.product,
        "properties": properties,
        "packData": [pack],
    }


def _zensdk_records(args, telem, qos, retain):
    return [
        _record(f"Zendure/sensor/{args.device_sn}/{metric}", str(telem[metric]), qos, retain)
        for metric in ZENSDK_METRICS
    ]


def _cloud_records(args, telem, qos, retain):
    return [
        _record(f"{args.app_key}/sensor/{args.device_sn}/{metric}", str(telem[metric]), qos, retain)
        for metric in CLOUD_METRICS
    ]


def selected_schemas(schema):
    return SCHEMA_FAMILIES if schema == SCHEMA_ALL else (schema,)


def build_batch(args, tick):
    """Return the publish records for one batch as ``{topic,payload,qos,retain}``."""

    telem = _telemetry(tick)
    records = []
    for schema in selected_schemas(args.schema):
        if schema == SCHEMA_ZENSDK:
            records.extend(_zensdk_records(args, telem, args.qos, args.retain))
        elif schema == SCHEMA_LEGACY_IOT:
            topic = f"iot/{args.product_key}/{args.device_id}/properties/report"
            records.append(_record(topic, _legacy_payload(args, telem), args.qos, args.retain))
        elif schema == SCHEMA_LEGACY_SLASH:
            topic = f"/{args.product_key}/{args.device_id}/properties/report"
            records.append(_record(topic, _legacy_payload(args, telem), args.qos, args.retain))
        elif schema == SCHEMA_CLOUD:
            records.extend(_cloud_records(args, telem, args.qos, args.retain))
    return records


def render_dry_run(args):
    """JSONL publish plan (single batch). Contains no credentials by design."""

    return [json.dumps(record, sort_keys=True) for record in build_batch(args, 0)]


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
    client.connect(args.host, args.port, keepalive=max(15, args.interval * 2))
    return client


def _publish(args, out):
    client = _connect(args)
    client.loop_start()
    try:
        tick = 0
        while True:
            for record in build_batch(args, tick):
                client.publish(
                    record["topic"],
                    record["payload"],
                    qos=record["qos"],
                    retain=record["retain"],
                )
            out.write(f"published batch tick={tick} to {args.host}:{args.port}\n")
            out.flush()
            if args.once:
                break
            tick += 1
            time.sleep(args.interval)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Publish mock Zendure telemetry to an MQTT broker (dev/test only). "
            "Telemetry only: never publishes write/control topics."
        )
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--tls", action="store_true", help="enable TLS transport")
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        help="allow self-signed local test certs (only valid with --tls)",
    )
    parser.add_argument("--username", default=None, help="optional MQTT username")
    parser.add_argument("--password", default=None, help="optional MQTT password (never logged)")
    parser.add_argument(
        "--client-id",
        default=None,
        help="optional client id (default: stable mock id from --device-sn)",
    )
    parser.add_argument(
        "--schema",
        choices=(SCHEMA_ALL,) + SCHEMA_FAMILIES,
        default=SCHEMA_ALL,
    )
    parser.add_argument("--device-sn", default="MOCK000SF800", help="anonymized mock serial")
    parser.add_argument("--product", default="solarFlow800Pro")
    parser.add_argument("--product-key", default="mockProductKey", help="legacy-topic product key")
    parser.add_argument("--device-id", default="mockDeviceId", help="legacy-topic device id")
    parser.add_argument("--app-key", default="mockAppKey", help="cloud-scalar app key")
    parser.add_argument("--interval", type=int, default=3, help="seconds between batches")
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    parser.add_argument("--retain", action="store_true", help="publish retained messages")
    parser.add_argument("--qos", type=int, choices=(0, 1), default=0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the JSONL publish plan without connecting",
    )
    return parser


def _finalize_args(args):
    if args.tls_insecure and not args.tls:
        raise ValueError("--tls-insecure requires --tls")
    if args.interval < 1:
        raise ValueError("--interval must be >= 1")
    if not args.client_id:
        args.client_id = f"zendure-mqtt-mock-{args.device_sn}"
    return args


def run(argv=None, out=None):
    out = out or sys.stdout
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        _finalize_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run:
        for line in render_dry_run(args):
            out.write(line + "\n")
        return 0

    try:
        _publish(args, out)
    except Exception as exc:
        # Credentials are never included in the message; only host/port are shown.
        print(
            f"error: failed to publish to {args.host}:{args.port}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
