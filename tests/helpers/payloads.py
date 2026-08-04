# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical, anonymized MQTT payload fixtures per supported family.

Stable, credential-free identifiers only: no real serials, app keys or tokens.
Each helper returns the wire bytes so tests can inject them through the fake
broker exactly as a device would publish them.
"""

import json

# --- stable anonymized identifiers ------------------------------------------
SCALAR_SERIAL = "SCALARSN01"
CLOUD_SERIAL = "CLOUDSN01"
CLOUD_APP_KEY = "appkeyAAAA"  # anonymized account prefix; never a real secret
LEGACY_DEVICE_ID = "LEGACYDEV1"
LEGACY_PRODUCT_KEY = "PKLEGACY1"
LEGACY_ALT_DEVICE_ID = "LEGACYDEV2"
LEGACY_ALT_PRODUCT_KEY = "PKLEGACY2"
D0_SERIAL = "D0SERIAL01"


def _b(value) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value).encode("utf-8")


# --- ZenSDK HA scalar (local) : Zendure/sensor/<serial>/<metric> -------------
def scalar_topic(metric, serial=SCALAR_SERIAL) -> str:
    return f"Zendure/sensor/{serial}/{metric}"


SCALAR_METRICS = {
    "electricLevel": 82,
    "solarInputPower": 640,
    "outputHomePower": 300,
    "packInputPower": 0,
    "outputLimit": 800,
}


def scalar_messages(serial=SCALAR_SERIAL):
    """Return ``(topic, payload)`` pairs for a scalar telemetry-only device."""

    return [
        (scalar_topic(metric, serial), _b(str(value)))
        for metric, value in SCALAR_METRICS.items()
    ]


# --- Zendure cloud scalar : <appKey>/sensor/<serial>/<metric> ----------------
def cloud_scalar_topic(metric, serial=CLOUD_SERIAL, app_key=CLOUD_APP_KEY) -> str:
    return f"{app_key}/sensor/{serial}/{metric}"


def cloud_scalar_messages(serial=CLOUD_SERIAL, app_key=CLOUD_APP_KEY):
    return [
        (cloud_scalar_topic(metric, serial, app_key), _b(str(value)))
        for metric, value in SCALAR_METRICS.items()
    ]


# --- Legacy Zendure JSON : iot/<pk>/<dev>/properties/report ------------------
def legacy_json_topic(product_key=LEGACY_PRODUCT_KEY, device_id=LEGACY_DEVICE_ID) -> str:
    return f"iot/{product_key}/{device_id}/properties/report"


def legacy_json_report(
    device_id=LEGACY_DEVICE_ID,
    serial=LEGACY_DEVICE_ID,
    *,
    electric_level=74,
    solar_input=520,
    output_limit=600,
    packs=1,
) -> bytes:
    body = {
        "sn": serial,
        "product": "Hub 2000",
        "properties": {
            "electricLevel": electric_level,
            "solarInputPower": solar_input,
            "outputHomePower": 250,
            "packInputPower": 0,
            "outputLimit": output_limit,
            "acMode": 2,
        },
        "packData": [
            {"sn": f"{serial}P{n}", "socLevel": electric_level, "power": 0}
            for n in range(1, packs + 1)
        ],
    }
    return _b(body)


# --- Legacy Zendure JSON alt : /<pk>/<dev>/properties/report -----------------
def legacy_json_alt_topic(
    product_key=LEGACY_ALT_PRODUCT_KEY, device_id=LEGACY_ALT_DEVICE_ID
) -> str:
    return f"/{product_key}/{device_id}/properties/report"


def legacy_json_alt_report(
    device_id=LEGACY_ALT_DEVICE_ID, serial=LEGACY_ALT_DEVICE_ID, **kwargs
) -> bytes:
    return legacy_json_report(device_id=device_id, serial=serial, **kwargs)


# --- D0 grid meter : Zendure/sensor/<serial>/totalPower ----------------------
def d0_topic(serial=D0_SERIAL) -> str:
    return f"Zendure/sensor/{serial}/totalPower"


def d0_total_power(value=-43) -> bytes:
    return _b(str(value))


# --- Degenerate / hostile payloads ------------------------------------------
MALFORMED_JSON = b"{not valid json"
EMPTY_PAYLOAD = b""
PARTIAL_REPORT = _b({"sn": LEGACY_DEVICE_ID})  # no properties block
UNKNOWN_TOPIC = "totally/unrelated/topic"
WRITE_RESPONSE_TOPIC = f"iot/{LEGACY_PRODUCT_KEY}/{LEGACY_DEVICE_ID}/properties/write"
FOREIGN_WILDCARD_TOPIC = "#/not/a/real/topic"


def packdata_report(serial=LEGACY_DEVICE_ID, packs=3) -> bytes:
    """A report with multiple battery packs, to exercise pack aggregation."""

    return legacy_json_report(device_id=serial, serial=serial, packs=packs)
