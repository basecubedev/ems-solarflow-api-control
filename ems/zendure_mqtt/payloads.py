# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure MQTT payload parsing for EMS Core runtime telemetry.

Scalar payloads and JSON report payloads are parsed defensively: invalid input
returns an empty result rather than raising, and unknown keys are preserved.
"""

import json
import math
from dataclasses import dataclass, field

MAX_PAYLOAD_BYTES = 256 * 1024

_KNOWN_REPORT_KEYS = frozenset(
    {"sn", "serialNumber", "deviceSn", "product", "properties", "packData"}
)


def coerce_scalar(payload):
    """Coerce a scalar payload to ``int``/``float`` when safe, else ``str``.

    Bytes are decoded as UTF-8; non-finite floats and non-numeric text stay as
    strings so no telemetry value is silently lost.
    """

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return payload
    text = payload.strip()
    if not text:
        return payload
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return payload
    if not math.isfinite(value):
        return payload
    return value


@dataclass
class ParsedReport:
    serial_number: str | None = None
    product: str | None = None
    properties: dict = field(default_factory=dict)
    battery_packs: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _coerce_json(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_PAYLOAD_BYTES:
            return None
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD_BYTES:
        return None
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return None


def _extract_packs(data, properties):
    packs = data.get("packData")
    if packs is None:
        packs = properties.get("packData")
    if isinstance(packs, list):
        return [pack for pack in packs if isinstance(pack, dict)]
    if isinstance(packs, dict):
        return [packs]
    return []


def parse_report_payload(payload):
    """Best-effort parse of a JSON report payload; empty result when unusable."""

    data = _coerce_json(payload)
    if not isinstance(data, dict):
        return ParsedReport()

    report = ParsedReport()
    for key in ("sn", "serialNumber", "deviceSn"):
        value = data.get(key)
        if isinstance(value, str) and value:
            report.serial_number = value
            break
    product = data.get("product")
    if isinstance(product, str) and product:
        report.product = product
    properties = data.get("properties")
    if isinstance(properties, dict):
        report.properties = dict(properties)
    report.battery_packs = _extract_packs(data, report.properties)
    report.extra = {
        key: value for key, value in data.items() if key not in _KNOWN_REPORT_KEYS
    }
    return report
