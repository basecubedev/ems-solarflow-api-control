# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Zendure MQTT mock telemetry publisher (no broker required)."""

import io
import json

import pytest

from admin.mqtt_topic_discovery import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    classify_topic,
    parse_report_payload,
)
from tools import zendure_mqtt_mock_service as mock

pytestmark = pytest.mark.simulation

SECRET = "super-secret-pw"


def _args(argv):
    parser = mock.build_arg_parser()
    args = parser.parse_args(argv)
    mock._finalize_args(args)
    return args


def _dry_run_records(argv):
    args = _args(argv)
    return [json.loads(line) for line in mock.render_dry_run(args)]


def test_zensdk_scalar_emits_expected_topics():
    records = _dry_run_records(["--schema", "zensdk-scalar", "--device-sn", "SN1"])
    topics = {rec["topic"] for rec in records}
    for metric in mock.ZENSDK_METRICS:
        assert f"Zendure/sensor/SN1/{metric}" in topics
    for rec in records:
        assert classify_topic(rec["topic"]).family == FAMILY_ZENSDK_HA_SCALAR


def test_legacy_iot_json_topic_and_payload():
    records = _dry_run_records(
        ["--schema", "legacy-iot-json", "--product-key", "PK", "--device-id", "DEV"]
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["topic"] == "iot/PK/DEV/properties/report"
    assert classify_topic(rec["topic"]).family == FAMILY_LEGACY_JSON
    payload = json.loads(rec["payload"])
    assert "properties" in payload
    assert isinstance(payload["packData"], list) and payload["packData"]


def test_legacy_slash_json_topic():
    records = _dry_run_records(
        ["--schema", "legacy-slash-json", "--product-key", "PK", "--device-id", "DEV"]
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["topic"] == "/PK/DEV/properties/report"
    assert classify_topic(rec["topic"]).family == FAMILY_LEGACY_JSON_ALT
    payload = json.loads(rec["payload"])
    assert "properties" in payload and "packData" in payload


def test_cloud_scalar_topics_classify():
    records = _dry_run_records(["--schema", "cloud-scalar", "--app-key", "AK", "--device-sn", "SN1"])
    topics = {rec["topic"] for rec in records}
    assert "AK/sensor/SN1/electricLevel" in topics
    for rec in records:
        assert classify_topic(rec["topic"]).family == FAMILY_ZENDURE_CLOUD_SCALAR


def test_schema_all_includes_every_family():
    records = _dry_run_records(["--schema", "all"])
    families = {classify_topic(rec["topic"]).family for rec in records}
    assert families == {
        FAMILY_ZENSDK_HA_SCALAR,
        FAMILY_LEGACY_JSON,
        FAMILY_LEGACY_JSON_ALT,
        FAMILY_ZENDURE_CLOUD_SCALAR,
    }


def test_dry_run_does_not_leak_password():
    out = io.StringIO()
    rc = mock.run(["--schema", "all", "--password", SECRET, "--dry-run"], out=out)
    assert rc == 0
    assert SECRET not in out.getvalue()


def test_generated_legacy_payload_parses_with_discovery_helper():
    records = _dry_run_records(["--schema", "legacy-iot-json", "--product", "solarFlow800Pro"])
    parsed = parse_report_payload(records[0]["payload"])
    assert parsed["pack_data"] is True
    assert "electricLevel" in parsed["metrics"]
    assert parsed.get("model_hint") == "solarFlow800Pro"


def test_dry_run_records_carry_qos_and_retain():
    records = _dry_run_records(["--schema", "zensdk-scalar", "--qos", "1", "--retain"])
    assert records
    for rec in records:
        assert rec["qos"] == 1
        assert rec["retain"] is True
        assert set(rec) == {"topic", "payload", "qos", "retain"}


def test_tls_insecure_requires_tls():
    with pytest.raises(SystemExit):
        mock.run(["--tls-insecure", "--dry-run"])


def test_default_client_id_is_stable():
    args = _args(["--device-sn", "SN9"])
    assert args.client_id == "zendure-mqtt-mock-SN9"


def test_power_values_vary_between_ticks_but_deterministic():
    args = _args(["--schema", "zensdk-scalar"])
    batch0 = mock.build_batch(args, 0)
    batch1 = mock.build_batch(args, 1)
    assert batch0 != batch1
    assert mock.build_batch(args, 0) == batch0
