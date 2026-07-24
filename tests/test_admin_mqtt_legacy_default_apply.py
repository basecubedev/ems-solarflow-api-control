# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real Maintenance HTTP apply for the legacy default broker and new gates.

These drive the actual ``/api/admin/maintenance/config`` + ``.../apply`` handlers
(not helpers) for configuration shapes the discovery flow never generates but an
existing install may carry: a legacy single-broker install whose broker lives in
the top-level ``zendure_mqtt`` block, an MQTT grid meter that ambiguously mixes
``broker_ref`` with inline connection fields, and a cloud runtime record whose
required field is only whitespace. Each must reject or accept exactly as the Core
contract says, and a rejected apply must leave ``config.json`` byte-identical
with no credential record created.
"""

import json

import pytest

from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.test_admin_maintenance_mqtt_apply import (
    API_KEY,
    _CloudFetch,
    _paths,
    _request,
    _serve,
    _write_config,
)

pytestmark = pytest.mark.simulation

CLOUD_REF = "zendure-cloud"


def _mqtt_device(broker_ref=None):
    mqtt = {"topic_family": "iot", "device_id": "SN-MQTT1"}
    if broker_ref is not None:
        mqtt["broker_ref"] = broker_ref
    return {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": "MQTT Inv",
        "serial_number": "SN-MQTT1",
        "mqtt": mqtt,
    }


def _base_config(zendure_mqtt, *, mqtt_device, grid_meter=None):
    devices = [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}]
    if mqtt_device is not None:
        devices.append(mqtt_device)
    return {
        "system": {"max_total_power": 1600},
        "devices": devices,
        "grid_meter": grid_meter or {"type": "shelly", "ip": "192.168.1.50"},
        "zendure_mqtt": zendure_mqtt,
    }


def _apply_current_draft(base):
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True},
    )


def test_maintenance_apply_blocks_missing_legacy_default_credential(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config = _base_config(
        {"host": "10.0.0.10", "port": 1883, "credentials_ref": "missing"},
        mqtt_device=_mqtt_device(),
    )
    config_path = _write_config(tmp_path, config)
    original = config_path.read_bytes()
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        status, payload = _apply_current_draft(base)
        assert status >= 400, payload
        assert payload.get("ok") is not True
        # Nothing committed: config byte-identical, no record forged.
        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        assert not (secrets_dir / "mqtt-missing.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_accepts_authenticated_legacy_default_broker(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config = _base_config(
        {"host": "10.0.0.10", "port": 1883, "credentials_ref": "legacy"},
        mqtt_device=_mqtt_device(),
    )
    config_path = _write_config(tmp_path, config)
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        srv.credential_store.save_mqtt_broker_secret("legacy", "user", "pw")
        status, payload = _apply_current_draft(base)
        assert status == 200 and payload.get("ok") is True, payload

        written = json.loads(config_path.read_text(encoding="utf-8"))
        # The legacy top-level broker round-trips: credentials_ref preserved,
        # never migrated into a brokers block on a plain read/apply.
        assert written["zendure_mqtt"]["credentials_ref"] == "legacy"
        assert "brokers" not in written["zendure_mqtt"]

        # A reconstructed EMS runtime resolves the default broker with no issue.
        _, secrets_dir = _paths(tmp_path)
        resolver = FileMqttCredentialResolver(secrets_dir)
        network = FakeMqttNetwork()
        runtime = build_zendure_mqtt_runtime(
            written,
            service_factory=network.telemetry_service_factory(),
            credential_resolver=resolver,
        )
        try:
            issues = {b["broker_ref"]: b["issue"] for b in runtime.status()["brokers"]}
            assert issues.get("default") is None, issues
        finally:
            runtime.stop()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_rejects_ambiguous_grid_meter_broker_reference(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    grid = {
        "type": "mqtt",
        "mqtt": {
            "broker_ref": "home",
            "topic": "meter/power",
            "credentials_ref": "inline-secret",
        },
    }
    zmqtt = {
        "brokers": {
            "home": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "10.0.0.10",
                "port": 1883,
                "credentials_ref": "homeref",
            }
        }
    }
    config = _base_config(zmqtt, mqtt_device=_mqtt_device("home"), grid_meter=grid)
    config_path = _write_config(tmp_path, config)
    original = config_path.read_bytes()
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        srv.credential_store.save_mqtt_broker_secret("homeref", "user", "pw")
        status, payload = _apply_current_draft(base)
        assert status >= 400, payload
        errors = payload.get("validation", {}).get("errors", [])
        ambiguous = [e for e in errors if e["code"] == "mqtt_broker_reference_ambiguous"]
        assert ambiguous, payload
        # The error message names the conflicting field, never a secret value.
        message = ambiguous[0]["message"]
        assert "credentials_ref" in message
        assert '"password"' not in message
        assert config_path.read_bytes() == original
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_rejects_named_brokers_default(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config = _base_config(
        {
            "host": "top.local",
            "port": 1883,
            "credentials_ref": "legacy",
            "brokers": {
                "default": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "collide.local",
                    "credentials_ref": "collideref",
                }
            },
        },
        mqtt_device=_mqtt_device(),
    )
    config_path = _write_config(tmp_path, config)
    original = config_path.read_bytes()
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        srv.credential_store.save_mqtt_broker_secret("legacy", "user", "pw")
        status, payload = _apply_current_draft(base)
        assert status >= 400, payload
        errors = payload.get("validation", {}).get("errors", [])
        reserved = [e for e in errors if e["code"] == "mqtt_broker_ref_reserved"]
        assert reserved, payload
        # The error message names only the reserved ref, never a host or secret
        # (the redacted preview may still echo the operator's own submitted host).
        assert "collide.local" not in reserved[0]["message"]
        assert "collideref" not in reserved[0]["message"]
        # Nothing committed and no record forged for the reserved profile.
        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        assert not (secrets_dir / "mqtt-collideref.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_rejects_whitespace_local_runtime_credential(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config = _base_config(
        {"host": "10.0.0.10", "port": 1883, "credentials_ref": "legacy"},
        mqtt_device=_mqtt_device(),
    )
    config_path = _write_config(tmp_path, config)
    original = config_path.read_bytes()
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        # A stored local record whose password is only whitespace is unusable, and
        # no complete discovery credential exists to replace it.
        srv.credential_store.save_mqtt_broker_secret("legacy", "user", "   ")
        status, payload = _apply_current_draft(base)
        assert status >= 400, payload
        assert payload.get("ok") is not True
        # Config untouched and the incomplete record is not silently replaced with
        # an anonymous one; it stays invalid rather than becoming usable.
        assert config_path.read_bytes() == original
        result = srv.credential_store.validate_runtime_credential(
            "legacy", expected_source="local_mqtt"
        )
        assert result.status == "invalid"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_accepts_effective_default_plus_secondary(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    secondary_device = {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": "MQTT Inv 2",
        "serial_number": "SN-MQTT2",
        "mqtt": {
            "topic_family": "iot",
            "device_id": "SN-MQTT2",
            "broker_ref": "secondary",
        },
    }
    config = {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            _mqtt_device(),
            secondary_device,
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "zendure_mqtt": {
            "host": "default.local",
            "port": 1883,
            "credentials_ref": "defaultref",
            "brokers": {
                "secondary": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "second.local",
                    "port": 1883,
                    "credentials_ref": "secondref",
                }
            },
        },
    }
    config_path = _write_config(tmp_path, config)
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        srv.credential_store.save_mqtt_broker_secret("defaultref", "user", "pw")
        srv.credential_store.save_mqtt_broker_secret("secondref", "user2", "pw2")
        status, payload = _apply_current_draft(base)
        assert status == 200 and payload.get("ok") is True, payload

        written = json.loads(config_path.read_text(encoding="utf-8"))
        # Top-level default and the named secondary both round-trip untouched.
        assert written["zendure_mqtt"]["credentials_ref"] == "defaultref"
        assert set(written["zendure_mqtt"]["brokers"]) == {"secondary"}

        _, secrets_dir = _paths(tmp_path)
        resolver = FileMqttCredentialResolver(secrets_dir)
        network = FakeMqttNetwork()
        runtime = build_zendure_mqtt_runtime(
            written,
            service_factory=network.telemetry_service_factory(),
            credential_resolver=resolver,
        )
        try:
            issues = {b["broker_ref"]: b["issue"] for b in runtime.status()["brokers"]}
            assert issues.get("default") is None, issues
            assert issues.get("secondary") is None, issues
        finally:
            runtime.stop()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_accepts_grid_meter_broker_ref_default(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    grid = {
        "type": "mqtt",
        "mqtt": {"broker_ref": "default", "topic": "meter/power"},
    }
    config = _base_config(
        {"host": "default.local", "port": 1883, "credentials_ref": "defaultref"},
        mqtt_device=_mqtt_device(),
        grid_meter=grid,
    )
    config_path = _write_config(tmp_path, config)
    srv, base = _serve(tmp_path, _CloudFetch())
    try:
        srv.credential_store.save_mqtt_broker_secret("defaultref", "user", "pw")
        status, payload = _apply_current_draft(base)
        assert status == 200 and payload.get("ok") is True, payload

        written = json.loads(config_path.read_text(encoding="utf-8"))
        # The grid meter keeps its broker_ref; resolution happens in memory only.
        assert written["grid_meter"]["mqtt"]["broker_ref"] == "default"
        from ems import config as cfg

        resolved = cfg.resolve_grid_meter_mqtt_settings(written)
        assert resolved["host"] == "default.local"
        assert resolved["credentials_ref"] == "defaultref"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_does_not_reuse_whitespace_cloud_record(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    zmqtt = {
        "brokers": {
            "zendure_cloud": {
                "enabled": True,
                "source": "zendure_cloud_mqtt",
                "host": "mqtteu.zen-iot.com",
                "port": 8883,
                "credentials_ref": CLOUD_REF,
            }
        }
    }
    config = _base_config(zmqtt, mqtt_device=_mqtt_device("zendure_cloud"))
    _write_config(tmp_path, config)
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        status, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            "POST",
            {"api_key": API_KEY},
        )
        assert status == 200 and payload["ok"] is True, payload
        # A stored cloud record whose app_key is only whitespace is unusable.
        srv.credential_store.save_mqtt_cloud_runtime_secret(
            CLOUD_REF,
            username="stale-user",
            password="stale-pass",
            client_id="stale-client",
            app_key="   ",
        )
        status, payload = _apply_current_draft(base)
        assert status == 200 and payload.get("ok") is True, payload

        # The whitespace record was not reused: it was reprovisioned from the
        # deviceList, so every field is now a real value.
        _, secrets_dir = _paths(tmp_path)
        resolved = FileMqttCredentialResolver(secrets_dir).resolve(CLOUD_REF)
        assert resolved.app_key == fetch.app_key
        assert resolved.app_key.strip()
    finally:
        srv.shutdown()
        srv.server_close()
