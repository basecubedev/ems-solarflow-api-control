# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-HTTP Maintenance parity for direct MQTT grid-meter credentials.

A direct MQTT grid meter (``grid_meter.mqtt.credentials_ref``) is a local MQTT
credential consumer, so the Maintenance apply endpoint stages, validates and
rolls its credential back exactly like a Zendure broker profile — through the
one shared credential-staging path both Setup and Maintenance use. These drive
the real ``/api/admin/maintenance/config/apply`` endpoint and assert the
decision, the config-file result and the credential-filesystem result.
"""

import json

import pytest

from ems.mqtt_credentials import FileMqttCredentialResolver
from tests.test_admin_maintenance_mqtt_apply import (
    _CloudFetch,
    _paths,
    _request,
    _serve,
    _write_config,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]

SECRET = "grid-meter-password"


def _base_with_grid_meter(credentials_ref="grid-meter", devices=None, brokers=None):
    config = {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            *(devices or []),
        ],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"host": "broker.local", "port": 1883, "topic": "meter/power"},
        },
    }
    if credentials_ref is not None:
        config["grid_meter"]["mqtt"]["credentials_ref"] = credentials_ref
    if brokers is not None:
        config["zendure_mqtt"] = {"brokers": brokers}
    return config


def _strip_field(secrets_dir, ref, field):
    path = secrets_dir / f"mqtt-{ref}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop(field, None)
    record.pop(f"{field}_encrypted", None)
    path.write_text(json.dumps(record), encoding="utf-8")


def _reapply(base):
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True},
    )


def test_maintenance_anonymous_direct_grid_meter_applies(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _base_with_grid_meter(credentials_ref=None))
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        status, payload = _reapply(base)
        assert status == 200 and payload.get("ok") is True, payload
        _, secrets = _paths(tmp_path)
        assert not secrets.exists() or list(secrets.glob("mqtt-*.json")) == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_authenticated_grid_meter_valid_record_applies(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _base_with_grid_meter("grid-meter"))
    srv, base = _serve(tmp_path, _CloudFetch())
    srv.credential_store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    config_path, secrets = _paths(tmp_path)
    record_before = (secrets / "mqtt-grid-meter.json").read_bytes()
    try:
        status, payload = _reapply(base)
        assert status == 200 and payload.get("ok") is True, payload
        # A valid record is reused untouched and reconstructs through Core.
        assert (secrets / "mqtt-grid-meter.json").read_bytes() == record_before
        resolved = FileMqttCredentialResolver(secrets).resolve("grid-meter")
        assert (resolved.username, resolved.password) == ("ems", SECRET)
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_grid_meter_invalid_ref_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _base_with_grid_meter("Bad Ref"))
    srv, base = _serve(tmp_path, _CloudFetch())
    config_path, secrets = _paths(tmp_path)
    original = config_path.read_bytes()
    try:
        status, payload = _reapply(base)
        assert status == 400, payload
        assert payload.get("code") == "mqtt_credentials_ref_invalid"
        assert payload.get("credentials_ref") == "Bad Ref"
        assert SECRET not in json.dumps(payload)
        assert config_path.read_bytes() == original
        assert not (secrets / "mqtt-bad-ref.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_grid_meter_missing_credential_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _base_with_grid_meter("grid-meter"))
    srv, base = _serve(tmp_path, _CloudFetch())
    config_path, secrets = _paths(tmp_path)
    original = config_path.read_bytes()
    try:
        status, payload = _reapply(base)
        assert status == 400, payload
        assert payload.get("ok") is False
        assert config_path.read_bytes() == original
        assert not (secrets / "mqtt-grid-meter.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_grid_meter_incomplete_credential_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _base_with_grid_meter("grid-meter"))
    srv, base = _serve(tmp_path, _CloudFetch())
    srv.credential_store.save_mqtt_broker_secret("grid-meter", "ems", SECRET)
    config_path, secrets = _paths(tmp_path)
    _strip_field(secrets, "grid-meter", "password")
    record_before = (secrets / "mqtt-grid-meter.json").read_bytes()
    original = config_path.read_bytes()
    try:
        status, payload = _reapply(base)
        assert status == 400, payload
        assert payload.get("ok") is False
        assert config_path.read_bytes() == original
        assert (secrets / "mqtt-grid-meter.json").read_bytes() == record_before
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_grid_meter_wrong_source_credential_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _base_with_grid_meter("grid-meter"))
    srv, base = _serve(tmp_path, _CloudFetch())
    srv.credential_store.save_mqtt_cloud_runtime_secret(
        "grid-meter",
        username="cloud-user",
        password=SECRET,
        client_id="cloud-client-1",
        app_key="cloud-app-key-1",
    )
    config_path, secrets = _paths(tmp_path)
    record_before = (secrets / "mqtt-grid-meter.json").read_bytes()
    original = config_path.read_bytes()
    try:
        status, payload = _reapply(base)
        assert status == 400, payload
        assert payload.get("ok") is False
        assert config_path.read_bytes() == original
        assert (secrets / "mqtt-grid-meter.json").read_bytes() == record_before
    finally:
        srv.shutdown()
        srv.server_close()


def _cloud_mqtt_device():
    return {
        "type": "zendure_mqtt",
        "name": "Cloud inverter",
        "enabled": True,
        "serial_number": "SN-CLOUD1",
        "mqtt": {
            "broker_ref": "cloud-main",
            "topic_family": "zensdk_ha_scalar",
            "device_id": "SN-CLOUD1",
        },
        "capabilities": {"write_output_limit": False},
    }


def test_maintenance_grid_meter_cloud_conflict_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(
        tmp_path,
        _base_with_grid_meter(
            "shared",
            devices=[_cloud_mqtt_device()],
            brokers={
                "cloud-main": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "credentials_ref": "shared",
                }
            },
        ),
    )
    srv, base = _serve(tmp_path, _CloudFetch())
    srv.credential_store.save_mqtt_broker_secret("shared", "ems", SECRET)
    config_path, secrets = _paths(tmp_path)
    original = config_path.read_bytes()
    record_before = (secrets / "mqtt-shared.json").read_bytes()
    try:
        status, payload = _reapply(base)
        assert status == 400, payload
        assert payload.get("code") == "mqtt_credential_source_conflict"
        assert payload.get("credentials_ref") == "shared"
        assert sorted(payload.get("sources") or []) == [
            "local_mqtt",
            "zendure_cloud_mqtt",
        ]
        assert sorted(payload.get("consumers") or []) == [
            "grid_meter",
            "zendure_mqtt_broker",
        ]
        # No mutation: config and the local record are byte-identical.
        assert config_path.read_bytes() == original
        assert (secrets / "mqtt-shared.json").read_bytes() == record_before
    finally:
        srv.shutdown()
        srv.server_close()
