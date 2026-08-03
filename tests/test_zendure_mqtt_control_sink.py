# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Zendure MQTT control-sink mock (no broker required)."""

import io
import json

import pytest

from tools import zendure_mqtt_control_sink as sink

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def test_parse_write_extracts_iot_command():
    parsed = sink.parse_write(
        "iot/PK/DEV/properties/write",
        json.dumps({"properties": {"outputLimit": 250}}),
    )
    assert parsed == {
        "product_key": "PK",
        "device_id": "DEV",
        "properties": {"outputLimit": 250},
    }


def test_parse_write_handles_leading_slash_variant():
    parsed = sink.parse_write(
        "/PK/DEV/properties/write",
        json.dumps({"properties": {"outputLimit": 100}}),
    )
    # Leading slash still exposes product_key/device_id in segments 1 and 2.
    assert parsed == {
        "product_key": "PK",
        "device_id": "DEV",
        "properties": {"outputLimit": 100},
    }


def test_parse_write_rejects_non_write_and_malformed():
    assert sink.parse_write("iot/PK/DEV/properties/report", "{}") is None
    assert sink.parse_write("iot/PK/DEV/properties/write", b"not json") is None
    assert sink.parse_write("iot/PK/DEV/properties/write", json.dumps({})) is None


def test_sink_records_and_echoes():
    class FakeClient:
        def __init__(self):
            self.published = []

        def publish(self, topic, payload, qos=0):
            self.published.append((topic, payload))

    out = io.StringIO()
    control_sink = sink.ControlSink(echo=True, out=out)
    client = FakeClient()
    control_sink.on_write(
        client,
        "iot/PK/DEV/properties/write",
        json.dumps({"properties": {"outputLimit": 321}}),
    )
    assert control_sink.received[0]["properties"]["outputLimit"] == 321
    assert json.loads(out.getvalue())["device_id"] == "DEV"
    # Echoed a telemetry report reflecting the commanded value.
    topic, payload = client.published[0]
    assert topic == "iot/PK/DEV/properties/report"
    assert json.loads(payload) == {"properties": {"outputLimit": 321}}


def test_dry_run_prints_subscription_plan():
    out = io.StringIO()
    rc = sink.run(["--dry-run"], out=out)
    assert rc == 0
    assert "iot/+/+/properties/write" in out.getvalue()
