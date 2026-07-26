# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance config draft, preview and explicit apply tests."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
    summarize_config_changes,
)
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import auth_headers, authenticate

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _config():
    return {
        "_comment": ["keep me"],
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800, "min_soc": 10},
            {"name": "WR2", "ip": "192.168.1.101", "sn": "BBB", "max_power": 600},
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "dashboard": {"enabled": True, "port": 8080},
        "influxdb": {"enabled": True, "mode": "bundled"},
        "winter": {"enabled": False},
        "custom_vendor_block": {"keep": True, "nested": [1, 2, 3]},
    }


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- load ---------------------------------------------------------------


def test_load_reads_standard_config(tmp_path):
    _write_config(tmp_path, _config())
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "ok"
    assert result["config_path"].endswith("config/config.json")
    assert result["summary"]["device_count"] == 2
    assert result["summary"]["grid_meter_type"] == "shelly"
    assert result["summary"]["influx_mode"] == "bundled"
    devices = result["draft"]["devices"]
    assert devices[0]["original_name"] == "WR1"
    assert devices[0]["ip"] == "192.168.1.100"
    assert result["draft"]["grid_meter"]["type"] == "shelly"
    assert len(result["revision"]) == 64
    assert "winter.enabled" in result["draft"]["features"]


def test_load_missing_config_is_clear_status(tmp_path):
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "missing"
    assert "message" in result


def test_load_invalid_json_is_clear_status(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{not json", encoding="utf-8")
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "invalid"


def test_load_never_exposes_secret_feature_values(tmp_path):
    data = _config()
    data["influxdb"]["token"] = "s3cret-token"
    _write_config(tmp_path, data)
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert "influxdb.token" not in result["draft"]["features"]
    for section in result["catalog"]["feature_sections"]:
        assert all(field["path"] != "influxdb.token" for field in section["fields"])


def test_load_surfaces_runtime_override_provenance(tmp_path):
    data = _config()
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    (tmp_path / "runtime-state.json").write_text(
        json.dumps({"system": {"loop_interval": 5}}), encoding="utf-8"
    )
    result = load_maintenance_config(base_dir=str(tmp_path))
    overrides = result["overrides"]
    assert overrides["system.loop_interval"]["config_value"] == 3
    assert overrides["system.loop_interval"]["effective_value"] == 5
    assert overrides["system.loop_interval"]["source"] == "dashboard_override"
    assert overrides["system.max_total_power"]["source"] == "config"
    assert result["draft"]["features"]["system.loop_interval"] == 3


def test_load_without_runtime_file_marks_all_config(tmp_path):
    _write_config(tmp_path, _config())
    result = load_maintenance_config(base_dir=str(tmp_path))
    overrides = result["overrides"]
    assert overrides["system.max_total_power"]["source"] == "config"


def test_preview_ignores_runtime_override(tmp_path):
    data = _config()
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    (tmp_path / "runtime-state.json").write_text(
        json.dumps({"system": {"loop_interval": 5}}), encoding="utf-8"
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["changed"] is False


# --- preview ------------------------------------------------------------


def test_preview_preserves_unknown_keys(tmp_path):
    _write_config(tmp_path, _config())
    result = load_maintenance_config(base_dir=str(tmp_path))
    draft = result["draft"]
    draft["devices"][0]["max_power"] = 600
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok"
    assert preview["preview"]["custom_vendor_block"] == {"keep": True, "nested": [1, 2, 3]}
    assert preview["preview"]["_comment"] == ["keep me"]


def test_preview_reports_changed_fields(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][0]["max_power"] = 600
    draft["grid_meter"]["type"] = "shelly_3em_gen1"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    diff = preview["diff"]
    assert preview["changed"] is True
    change_paths = {c["path"] for c in diff["changes"]}
    assert "devices[0].max_power" in change_paths
    assert "grid_meter.type" in change_paths


def test_preview_rejects_telemetry_only_mqtt_devices_as_non_bootable(tmp_path):
    data = _config()
    data["devices"] = [
        {
            "type": "zendure_mqtt",
            "name": "Telemetry only",
            "enabled": True,
            "serial_number": "MQTT1",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "device_id": "MQTT1",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        }
    ]
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["validation"]["ok"] is False
    assert "no_control_devices" in {
        issue["code"] for issue in preview["validation"]["errors"]
    }


def test_preview_reports_removed_device(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"] = [d for d in draft["devices"] if d.get("original_name") != "WR2"]

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["preview"]["devices"] == [preview["preview"]["devices"][0]]
    assert len(preview["preview"]["devices"]) == 1
    assert any(e["path"].startswith("devices[1]") for e in preview["diff"]["removed"])


def test_preview_reports_added_device(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"].append(
        {"original_name": None, "name": "WR3", "ip": "192.168.1.102", "sn": "CCC", "enabled": True}
    )
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert len(preview["preview"]["devices"]) == 3
    assert any(e["path"].startswith("devices[2]") for e in preview["diff"]["added"])


def test_preview_removes_grid_meter_from_draft_without_writing(tmp_path):
    path = _write_config(tmp_path, _config())
    original = path.read_text(encoding="utf-8")
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"]["present"] = False

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    assert "grid_meter" not in preview["preview"]
    assert any(e["path"].startswith("grid_meter") for e in preview["diff"]["removed"])
    assert path.read_text(encoding="utf-8") == original


def test_preview_validates_bad_host(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][0]["ip"] = "not a host!!"
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    assert any(e["code"] == "device_host_invalid" for e in preview["validation"]["errors"])


def test_preview_reports_duplicate_zendure_identity(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][1]["sn"] = draft["devices"][0]["sn"]

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    assert preview["validation"]["ok"] is False
    duplicate = [
        e
        for e in preview["validation"]["errors"]
        if e["code"] == "zendure_device_identity_duplicate"
    ]
    assert duplicate
    message = duplicate[0]["message"]
    assert draft["devices"][0]["sn"] not in message
    assert "AAA" not in message


def test_load_and_preview_zendure_3ct_http_grid_meter(tmp_path):
    data = _config()
    data["grid_meter"] = {"type": "zendure_smartmeter_3ct_http", "ip": "192.168.1.60"}
    _write_config(tmp_path, data)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["summary"]["grid_meter_type"] == "zendure_smartmeter_3ct_http"
    draft = loaded["draft"]
    assert draft["grid_meter"]["type"] == "zendure_smartmeter_3ct_http"
    assert draft["grid_meter"]["ip"] == "192.168.1.60"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True
    assert preview["preview"]["grid_meter"] == {
        "type": "zendure_smartmeter_3ct_http",
        "ip": "192.168.1.60",
    }


def test_load_and_preview_zendure_d0_http_grid_meter(tmp_path):
    data = _config()
    data["grid_meter"] = {"type": "zendure_smartmeter_d0_http", "ip": "192.168.1.60"}
    _write_config(tmp_path, data)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["summary"]["grid_meter_type"] == "zendure_smartmeter_d0_http"
    draft = loaded["draft"]
    assert draft["grid_meter"]["type"] == "zendure_smartmeter_d0_http"
    assert draft["grid_meter"]["ip"] == "192.168.1.60"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True
    assert preview["preview"]["grid_meter"] == {
        "type": "zendure_smartmeter_d0_http",
        "ip": "192.168.1.60",
    }


def test_preview_requires_ip_for_zendure_d0_http_grid_meter(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"]["type"] = "zendure_smartmeter_d0_http"
    draft["grid_meter"]["ip"] = ""

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    assert any(
        e["code"] == "grid_meter_host_invalid" for e in preview["validation"]["errors"]
    )


def test_switch_3ct_to_d0_http_changes_type_but_keeps_shared_http_logic(tmp_path):
    # Switching a 3CT local-API meter to a D0 local-API meter only changes the
    # type: the IP is kept and the meter stays a flat HTTP meter (no MQTT block
    # gets introduced), because both share the Zendure local-HTTP logic.
    data = _config()
    data["grid_meter"] = {"type": "zendure_smartmeter_3ct_http", "ip": "192.168.1.60"}
    _write_config(tmp_path, data)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"] = {
        "present": True,
        "type": "zendure_smartmeter_d0_http",
        "ip": "192.168.1.60",
    }

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    grid = preview["preview"]["grid_meter"]
    assert preview["validation"]["ok"] is True
    assert grid["type"] == "zendure_smartmeter_d0_http"
    assert grid["ip"] == "192.168.1.60"
    assert "mqtt" not in grid


def test_preview_requires_ip_for_zendure_3ct_http_grid_meter(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"]["type"] = "zendure_smartmeter_3ct_http"
    draft["grid_meter"]["ip"] = ""

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    assert any(
        e["code"] == "grid_meter_host_invalid" for e in preview["validation"]["errors"]
    )


def test_switch_to_zendure_3ct_http_drops_stale_tasmota_and_mqtt_keys(tmp_path):
    data = _config()
    data["grid_meter"] = {
        "type": "tasmota_http",
        "ip": "192.168.1.60",
        "url": "http://192.168.1.60/cm?cmnd=Status%2010",
        "power_path": "StatusSNS.SML.Power_curr",
        "mqtt": {"host": "mqtt.local", "topic": "meter/grid"},
        "topic": "meter/grid",
    }
    _write_config(tmp_path, data)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"] = {
        "present": True,
        "type": "zendure_smartmeter_3ct_http",
        "ip": "192.168.1.60",
    }

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    grid = preview["preview"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.168.1.60"
    assert "url" not in grid
    assert "power_path" not in grid
    assert "mqtt" not in grid
    assert "topic" not in grid


def test_preview_does_not_write_config(tmp_path):
    path = _write_config(tmp_path, _config())
    original = path.read_text(encoding="utf-8")
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["devices"][0]["max_power"] = 123
    draft["features"]["winter.enabled"] = True
    preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert path.read_text(encoding="utf-8") == original


def test_prepare_apply_rejects_stale_real_config(tmp_path):
    path = _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    changed = _config()
    changed["system"]["max_total_power"] = 999
    path.write_text(json.dumps(changed), encoding="utf-8")

    result = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )

    assert result["status"] == "conflict"
    assert json.loads(path.read_text(encoding="utf-8"))["system"]["max_total_power"] == 999


def test_prepare_apply_serializes_valid_reviewed_draft_without_writing(tmp_path):
    path = _write_config(tmp_path, _config())
    original = path.read_bytes()
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"].append(
        {"name": "WR3", "ip": "192.168.1.102", "sn": "CCC", "enabled": True}
    )

    result = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )

    assert result["status"] == "ok"
    assert json.loads(result["payload"])["devices"][-1]["sn"] == "CCC"
    assert path.read_bytes() == original


def test_preview_missing_config_is_clear_status(tmp_path):
    result = preview_maintenance_config({}, base_dir=str(tmp_path))
    assert result["status"] == "missing"


def test_feature_change_is_coerced_and_diffed(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["features"]["winter.enabled"] = True
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["preview"]["winter"]["enabled"] is True
    assert any(c["path"] == "winter.enabled" for c in preview["diff"]["changes"])


def test_zendure_mqtt_enabled_feature_path_is_ignored(tmp_path):
    # The always-on telemetry feature has no toggle anymore; a stale draft
    # carrying the removed path must neither diff nor write anything.
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["features"]["zendure_mqtt.enabled"] = False
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert not any(
        c["path"] == "zendure_mqtt.enabled" for c in preview["diff"]["changes"]
    )
    assert "enabled" not in preview["preview"].get("zendure_mqtt", {})


# --- Zendure MQTT devices + broker --------------------------------------


def _mqtt_config():
    data = _config()
    data["zendure_mqtt"] = {
        "enabled": True,
        "host": "192.168.1.20",
        "port": 1883,
        "tls": False,
        "username": "mqttuser",
        "password": "BROKER_SECRET",
    }
    data["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Zendure MQTT SolarFlow",
            "enabled": True,
            "serial_number": "DEVICE_SN",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "base_topic": "Zendure",
                "device_id": "DEVICE_SN",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        }
    )
    return data


def test_load_preserves_existing_zendure_mqtt_device(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    mqtt = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"]
    assert len(mqtt) == 1
    device = mqtt[0]
    assert device["serial_number"] == "DEVICE_SN"
    assert device["hardware_generation"] == "solarflow_zensdk"
    assert device["hardware_model"] == ""
    assert device["mqtt"]["topic_family"] == "zensdk_ha_scalar"
    assert device["capabilities"]["write_output_limit"] is False


def test_cloud_route_id_is_masked_in_browser_and_preserved_on_round_trip(tmp_path):
    data = _mqtt_config()
    data["zendure_mqtt"] = {
        "brokers": {
            "zendure_cloud": {
                "enabled": True,
                "source": "zendure_cloud_mqtt",
                "host": "mqtt.example.invalid",
                "port": 8883,
                "tls": True,
                "tls_insecure": True,
                "credentials_ref": "zendure-cloud",
            }
        }
    }
    device = data["devices"][-1]
    device.update(
        serial_number="DEVICE_SN",
        hardware_profile="solarflow_800_pro_2",
        power_write_profile="zensdk_properties_write",
    )
    device["mqtt"] = {
        "broker_ref": "zendure_cloud",
        "source": "zendure_cloud_mqtt",
        "topic_family": "legacy_zendure_json_alt",
        "base_topic": None,
        "device_id": "DEVICEKEY",
        "product_key": "PRODUCTKEY",
    }
    device["capabilities"]["write_output_limit"] = True
    _write_config(tmp_path, data)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    mqtt_draft = next(
        item for item in loaded["draft"]["devices"] if item.get("kind") == "zendure_mqtt"
    )
    assert mqtt_draft["serial_number"] == "DEVICE_SN"
    assert mqtt_draft["device_id"] == "••••"
    assert mqtt_draft["mqtt"]["device_id"] == "••••"
    # The canonical effective topic must mask the route id, never leak it.
    assert "DEVICEKEY" not in (mqtt_draft["mqtt"].get("effective_write_topic") or "")
    assert mqtt_draft["mqtt"].get("effective_write_topic") == (
        "iot/…/…/properties/write"
    )
    assert "DEVICEKEY" not in json.dumps(loaded)

    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    persisted = json.loads(prepared["payload"])
    mqtt_device = next(
        item for item in persisted["devices"] if item.get("type") == "zendure_mqtt"
    )
    assert mqtt_device["serial_number"] == "DEVICE_SN"
    assert mqtt_device["mqtt"]["device_id"] == "DEVICEKEY"


def test_preview_zendure_mqtt_device_does_not_require_ip_or_sn(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True
    mqtt = [d for d in preview["preview"]["devices"] if d.get("type") == "zendure_mqtt"]
    assert mqtt and "ip" not in mqtt[0] and "sn" not in mqtt[0]


def test_preview_validates_zendure_mqtt_device_shape(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    # A newly added telemetry device with no generation and no identifier is
    # rejected by the EMS-owned validator, not by a traceback.
    draft["devices"].append(
        {
            "kind": "zendure_mqtt",
            "original_name": None,
            "name": "Broken MQTT",
            "enabled": True,
            "serial_number": "",
            "device_id": "",
            "hardware_profile": "",
            "product_key": "",
        }
    )
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    codes = {e["code"] for e in preview["validation"]["errors"]}
    assert "topic_family_missing" in codes
    assert "device_identifier_missing" in codes


def test_preview_rejects_invalid_direct_grid_meter_credentials_ref(tmp_path):
    # Preview must enforce the same canonical credentials_ref contract Apply and
    # the Core resolver do, so a bad direct grid-meter ref never previews ready.
    config = _config()
    config["grid_meter"] = {
        "type": "mqtt",
        "mqtt": {
            "host": "broker.local",
            "port": 1883,
            "topic": "meter/power",
            "credentials_ref": "Bad Ref",
        },
    }
    _write_config(tmp_path, config)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    codes = {e["code"] for e in preview["validation"]["errors"]}
    assert "mqtt_credentials_ref_invalid" in codes


def test_preview_forces_zendure_mqtt_write_output_limit_false(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    device = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
    device["capabilities"]["write_output_limit"] = True
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    mqtt = [d for d in preview["preview"]["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert mqtt["capabilities"]["write_output_limit"] is False


def test_preview_blocks_local_and_mqtt_duplicate_identity(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    # Local inverter shares the physical serial of the MQTT telemetry device.
    draft["devices"][0]["sn"] = "DEVICE_SN"
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    assert any(
        e["code"] == "zendure_device_identity_duplicate"
        for e in preview["validation"]["errors"]
    )


def test_new_top_level_broker_is_written_without_enabled_key(tmp_path):
    _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["zendure_mqtt"] = {"present": True, "host": "192.168.1.20", "port": 1883}
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    written = json.loads(prepared["payload"])["zendure_mqtt"]
    assert written["host"] == "192.168.1.20"
    # The feature is always on; the removed top-level toggle is never written.
    assert "enabled" not in written


def test_legacy_enabled_key_roundtrips_unchanged_on_noop_apply(tmp_path):
    # An existing legacy key is neither rewritten nor stripped: an unchanged
    # draft round-trips the stored block as-is (the runtime ignores the key).
    _write_config(tmp_path, _mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    written = json.loads(prepared["payload"])["zendure_mqtt"]
    assert written["enabled"] is True


def test_broker_draft_enabled_is_derived_from_host_presence(tmp_path):
    data = _mqtt_config()
    data["zendure_mqtt"]["enabled"] = False  # legacy key, ignored by the runtime
    _write_config(tmp_path, data)
    broker = load_maintenance_config(base_dir=str(tmp_path))["draft"]["zendure_mqtt"]
    assert broker["managed"] == "legacy"
    assert broker["enabled"] is True


def test_named_broker_draft_reports_feature_always_on(tmp_path):
    data = _config()
    data["zendure_mqtt"] = {
        "enabled": False,
        "brokers": {
            "local_mqtt": {"enabled": True, "source": "local_mqtt", "host": "10.0.0.9"}
        },
    }
    _write_config(tmp_path, data)
    broker = load_maintenance_config(base_dir=str(tmp_path))["draft"]["zendure_mqtt"]
    assert broker["managed"] == "named"
    assert broker["enabled"] is True


def test_broker_password_not_returned_in_loaded_draft(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    broker = loaded["draft"]["zendure_mqtt"]
    assert broker["has_password"] is True
    assert "password" not in broker
    assert "BROKER_SECRET" not in json.dumps(loaded)


def test_empty_broker_password_keeps_existing(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["zendure_mqtt"]["password"] = ""
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    assert json.loads(prepared["payload"])["zendure_mqtt"]["password"] == "BROKER_SECRET"


def test_clear_broker_password_removes_it(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["zendure_mqtt"]["clear_password"] = True
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert "password" not in json.loads(prepared["payload"])["zendure_mqtt"]


def test_preview_and_diff_never_leak_broker_password(tmp_path):
    data = _mqtt_config()
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["zendure_mqtt"]["password"] = "NEW_SECRET"
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert "BROKER_SECRET" not in json.dumps(preview)
    assert "NEW_SECRET" not in json.dumps(preview)


# --- discovered MQTT proposals persist broker profiles --------------------


def _local_proposal(
    topic_family="zensdk_ha_scalar",
    serial="SN-NEW",
    host="10.0.0.10",
    credentials_ref=None,
):
    from admin.zendure_mqtt_config_proposals import build_proposals

    observation = {
        "source_type": "local_mqtt",
        "broker_host": host,
        "broker_port": 1883,
        "topic_family": topic_family,
        "serial_number": serial,
        "device_id": serial,
        "metrics_seen": ["electricLevel", "outputHomePower"],
    }
    if credentials_ref:
        observation["credentials_ref"] = credentials_ref
    return build_proposals([observation])[0]


def _draft_item_from_proposal(proposal, config_name="INV_2"):
    """Mirror the browser's ``mconfigAddZendureMqttProposal`` draft projection."""

    fragment = proposal["config_fragment"]
    mqtt = fragment.get("mqtt") or {}
    caps = fragment.get("capabilities") or {}
    output_control = caps.get("write_output_limit") is True
    identity = proposal.get("serial_number") or proposal.get("device_id") or ""
    return {
        "kind": "zendure_mqtt",
        "original_name": None,
        "proposal_id": proposal.get("id") or "",
        "proposal_broker_ref": proposal.get("broker_ref") or mqtt.get("broker_ref") or "",
        "name": config_name,
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": fragment.get("serial_number") or identity,
        "device_id": mqtt.get("device_id") or identity,
        "product_key": mqtt.get("product_key") or "",
        "hardware_profile": proposal.get("hardware_profile") or "",
        "power_hardware_profile": fragment.get("hardware_profile") or "",
        "power_write_profile": fragment.get("power_write_profile") or "",
        "alternative_layout": bool(proposal.get("alternative_layout")),
        "output_control": output_control,
        "supports_output_control": proposal.get("output_control_supported") is True,
        "trusted_write_target": (
            output_control and proposal.get("control_block_reason") != "write_target_missing"
        ),
        "mqtt": {
            "broker_ref": mqtt.get("broker_ref") or "",
            "source": mqtt.get("source") or "",
            "topic_family": mqtt.get("topic_family") or "",
            "base_topic": mqtt.get("base_topic"),
            "device_id": mqtt.get("device_id") or identity,
            "product_key": mqtt.get("product_key") or "",
            "write_protocol": mqtt.get("write_protocol") or "",
        },
        "capabilities": {
            "read_power": caps.get("read_power") is not False,
            "read_soc": caps.get("read_soc") is not False,
            "write_output_limit": output_control,
        },
        "broker": {
            "ref": proposal.get("broker_ref") or "",
            "host": proposal.get("broker_host") or "",
            "port": proposal.get("broker_port"),
            "tls": proposal.get("broker_tls") is True,
            "tls_insecure": proposal.get("broker_tls_insecure") is True,
            "tls_mode": proposal.get("broker_tls_mode") or "",
            "credentials_ref": proposal.get("credentials_ref") or "",
            "source": proposal.get("connection_source") or proposal.get("source") or "",
        },
    }


def test_added_mqtt_proposal_persists_local_broker_profile(tmp_path):
    _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal()
    ref = proposal["broker_ref"]
    draft["devices"].append(_draft_item_from_proposal(proposal))

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    brokers = preview["preview"]["zendure_mqtt"]["brokers"]
    assert brokers[ref] == {
        "enabled": True,
        "source": "local_mqtt",
        "host": "10.0.0.10",
        "port": 1883,
        "tls": False,
    }
    device = [
        d for d in preview["preview"]["devices"] if d.get("type") == "zendure_mqtt"
    ][0]
    assert device["mqtt"]["broker_ref"] == ref

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    written = json.loads(prepared["payload"])
    assert written["zendure_mqtt"]["brokers"][ref]["host"] == "10.0.0.10"
    assert [
        d for d in written["devices"] if d.get("type") == "zendure_mqtt"
    ][0]["mqtt"]["broker_ref"] == ref


def test_added_authenticated_mqtt_proposal_keeps_credentials_ref(tmp_path):
    _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    proposal = _local_proposal(credentials_ref="home")
    ref = proposal["broker_ref"]
    draft["devices"].append(_draft_item_from_proposal(proposal))

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    profile = preview["preview"]["zendure_mqtt"]["brokers"][ref]
    assert profile["credentials_ref"] == "home"
    # The profile carries only the non-secret reference, never the secret.
    assert "password" not in profile
    assert "username" not in profile


def test_added_mqtt_proposal_reuses_matching_existing_broker(tmp_path):
    data = _config()
    data["zendure_mqtt"] = {
        "brokers": {
            "home_broker": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "10.0.0.10",
                "port": 1883,
                "tls": False,
                "vendor_note": "keep me",
            }
        }
    }
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(_draft_item_from_proposal(_local_proposal()))

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    brokers = preview["preview"]["zendure_mqtt"]["brokers"]
    # The matching broker is reused: no duplicate profile, unrelated fields kept.
    assert set(brokers) == {"home_broker"}
    assert brokers["home_broker"]["vendor_note"] == "keep me"
    device = [
        d for d in preview["preview"]["devices"] if d.get("type") == "zendure_mqtt"
    ][0]
    assert device["mqtt"]["broker_ref"] == "home_broker"


def test_added_mqtt_proposal_conflicting_broker_ref_is_rejected(tmp_path):
    proposal = _local_proposal()
    ref = proposal["broker_ref"]
    data = _config()
    data["zendure_mqtt"] = {
        "brokers": {
            ref: {
                "enabled": True,
                "source": "local_mqtt",
                "host": "10.9.9.9",
                "port": 1883,
                "tls": False,
            }
        }
    }
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(_draft_item_from_proposal(proposal))

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    conflicts = [
        e
        for e in preview["validation"]["errors"]
        if e["code"] == "zendure_mqtt_broker_conflict"
    ]
    assert conflicts, preview["validation"]["errors"]
    assert ref in conflicts[0]["message"]
    # The existing profile is never silently replaced.
    assert preview["preview"]["zendure_mqtt"]["brokers"][ref]["host"] == "10.9.9.9"

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "invalid"
    assert "payload" not in prepared


def test_added_cloud_mqtt_proposal_provisions_cloud_broker_profile(tmp_path):
    from admin.credential_store import CredentialStore
    from admin.zendure_mqtt_config_proposals import build_proposals

    CredentialStore().zendure.save_token("account-api-key")
    proposal = build_proposals(
        [
            {
                "source_type": "zendure_cloud_mqtt",
                "topic_family": "legacy_zendure_json",
                "serial_number": "SN-CLOUD",
                "device_id": "DK-CLOUD",
                "product_key": "PK-CLOUD",
                "metrics_seen": ["electricLevel"],
            }
        ]
    )[0]
    assert proposal["broker_ref"] == "zendure_cloud"
    _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(_draft_item_from_proposal(proposal))

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    profile = preview["preview"]["zendure_mqtt"]["brokers"]["zendure_cloud"]
    assert profile["source"] == "zendure_cloud_mqtt"
    assert profile["credentials_ref"] == "zendure-cloud"
    assert profile["tls"] is True
    raw = json.dumps(preview)
    assert "account-api-key" not in raw


def test_added_cloud_mqtt_proposal_without_account_auth_is_rejected(tmp_path):
    from admin.zendure_mqtt_config_proposals import build_proposals

    proposal = build_proposals(
        [
            {
                "source_type": "zendure_cloud_mqtt",
                "topic_family": "legacy_zendure_json",
                "serial_number": "SN-CLOUD",
                "device_id": "DK-CLOUD",
                "product_key": "PK-CLOUD",
                "metrics_seen": ["electricLevel"],
            }
        ]
    )[0]
    _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(_draft_item_from_proposal(proposal))

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    codes = {e["code"] for e in preview["validation"]["errors"]}
    assert "zendure_mqtt_broker_auth_missing" in codes


def test_existing_mqtt_device_draft_without_broker_data_stays_valid(tmp_path):
    # Existing devices carry no endpoint data in their draft; their broker
    # profile already lives in config and must not be rewritten or duplicated.
    data = _config()
    data["zendure_mqtt"] = {
        "brokers": {
            "home_broker": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "10.0.0.10",
                "port": 1883,
            }
        }
    }
    data["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Existing MQTT",
            "enabled": True,
            "serial_number": "EXIST1",
            "mqtt": {
                "broker_ref": "home_broker",
                "topic_family": "zensdk_ha_scalar",
                "base_topic": "Zendure",
                "device_id": "EXIST1",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        }
    )
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is True, preview["validation"]["errors"]
    assert preview["changed"] is False, preview["diff"]


# --- catalog-driven device fields ----------------------------------------


def test_device_draft_covers_catalog_fields_beyond_legacy_numeric_list(tmp_path):
    """Every catalog device field is editable, not only a hand-picked subset.

    smart_mode is a catalog device field that was missing from the legacy
    hard-coded numeric list; it must round-trip draft -> preview like max_power.
    """

    data = _config()
    data["devices"][0]["smart_mode"] = 0
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["draft"]["devices"][0]["smart_mode"] == 0

    draft = loaded["draft"]
    draft["devices"][0]["smart_mode"] = "1"
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok"
    assert preview["preview"]["devices"][0]["smart_mode"] == 1
    change_paths = {c["path"] for c in preview["diff"]["changes"]}
    assert "devices[0].smart_mode" in change_paths


def test_device_draft_keeps_unknown_device_keys_out_but_preserved(tmp_path):
    """Non-catalog device keys never enter the draft yet survive an apply."""

    data = _config()
    data["devices"][0]["vendor_extra"] = {"keep": True}
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert "vendor_extra" not in loaded["draft"]["devices"][0]

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["preview"]["devices"][0]["vendor_extra"] == {"keep": True}


def test_unchanged_device_draft_with_smart_mode_is_a_noop(tmp_path):
    """Surfacing smart_mode in the draft must not turn a no-op into a change."""

    data = _config()
    data["devices"][0]["smart_mode"] = 1
    _write_config(tmp_path, data)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["changed"] is False


# --- diff ---------------------------------------------------------------


def test_summarize_ignores_comment_keys():
    before = {"_comment": ["a"], "x": 1}
    after = {"_comment": ["b"], "x": 2}
    diff = summarize_config_changes(before, after)
    assert diff["changes"] == [{"path": "x", "before": 1, "after": 2}]


def test_summarize_bounds_long_strings():
    before = {"x": "a"}
    after = {"x": "b" * 500}
    diff = summarize_config_changes(before, after)
    assert diff["changes"][0]["after"].endswith("…")
    assert len(diff["changes"][0]["after"]) <= 200


# --- endpoints ----------------------------------------------------------


def _server(config_apply=None):
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server(
        "127.0.0.1", 0, registry=registry, config_apply=config_apply
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _get(url):
    req = urllib.request.Request(url, headers=auth_headers(url, "GET"), method="GET")
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    headers = dict(auth_headers(url, "POST"))
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_config_endpoint_reads_current_config(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        status, payload = _get(f"{base}/api/admin/maintenance/config")
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["summary"]["device_count"] == 2


def test_preview_endpoint_returns_validation_and_diff(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        load_status, loaded = _get(f"{base}/api/admin/maintenance/config")
        draft = loaded["draft"]
        draft["devices"][0]["max_power"] = 555
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/preview", {"draft": draft}
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert load_status == 200
    assert status == 200
    assert payload["changed"] is True
    assert "validation" in payload


def test_preview_and_rejected_apply_mask_route_bearing_cloud_name_everywhere(
    tmp_path, monkeypatch
):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    topic = f"iot/{product}/{route}/properties/write"
    data = _config()
    data["grid_meter"]["type"] = "intentionally_invalid_meter"
    data["zendure_mqtt"] = {
        "brokers": {
            "cloud_a": {
                "enabled": True,
                "source": "zendure_cloud_mqtt",
                "host": "mqtt.example.invalid",
                "port": 8883,
                "tls": True,
                "credentials_ref": "zendure_cloud:cloud_a",
            }
        }
    }
    data["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": f"Cloud shed {route}",
            "enabled": True,
            "hardware_profile": "solarflow_800_pro_2",
            "power_write_profile": "zensdk_properties_write",
            "mqtt": {
                "broker_ref": "cloud_a",
                "topic_family": "legacy_zendure_json_alt",
                "device_id": route,
                "product_key": product,
                "write_topic": topic,
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        }
    )
    _write_config(tmp_path, data)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        _, loaded = _get(f"{base}/api/admin/maintenance/config")
        preview_status, preview = _post(
            f"{base}/api/admin/maintenance/config/preview",
            {"draft": loaded["draft"]},
        )
        apply_status, rejected = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert preview_status == 200
    assert preview["validation"]["ok"] is False
    assert any(
        issue["code"] == "device_common_values_missing"
        for issue in preview["validation"]["warnings"]
    )
    assert apply_status == 400
    assert rejected["status"] == "invalid"
    for response in (loaded, preview, rejected):
        flattened = json.dumps(response)
        assert route not in flattened
        assert product not in flattened
        assert topic not in flattened


def test_preview_endpoint_rejects_custom_path(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/preview",
            {"path": "/etc/evil.json", "draft": {}},
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 400


def test_preview_endpoint_accepts_zendure_mqtt_draft_devices(tmp_path, monkeypatch):
    _write_config(tmp_path, _mqtt_config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        _, loaded = _get(f"{base}/api/admin/maintenance/config")
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/preview",
            {"draft": loaded["draft"]},
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200
    assert payload["validation"]["ok"] is True
    assert "BROKER_SECRET" not in json.dumps(payload)


def test_preview_endpoint_malformed_mqtt_draft_returns_validation_not_traceback(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, _config())
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server()
    try:
        _, loaded = _get(f"{base}/api/admin/maintenance/config")
        draft = loaded["draft"]
        draft["devices"].append(
            {"kind": "zendure_mqtt", "name": "Broken", "enabled": True}
        )
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/preview", {"draft": draft}
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200
    assert payload["validation"]["ok"] is False
    assert payload["validation"]["errors"]


def test_apply_endpoint_requires_confirmation_and_leaves_config_unchanged(
    tmp_path, monkeypatch
):
    path = _write_config(tmp_path, _config())
    original = path.read_bytes()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    srv, base = _server()
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": False,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 400
    assert "confirmation" in payload["error"]
    assert path.read_bytes() == original


def test_apply_endpoint_writes_reviewed_draft_and_creates_backup(
    tmp_path, monkeypatch
):
    from admin.config_apply import ConfigApplyService
    from admin.install_context import detect_install_context

    path = _write_config(tmp_path, _config())
    original = path.read_bytes()
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"][0]["ip"] = "192.168.1.111"
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    service = ConfigApplyService(
        None,
        tmp_path / "admin-data",
        install_context_provider=lambda: detect_install_context(base_dir=str(tmp_path)),
    )
    srv, base = _server(config_apply=service)
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["devices"][0]["ip"] == "192.168.1.111"
    assert payload["backup_path"]
    assert Path(payload["backup_path"]).read_bytes() == original


def _seed_runtime_state(tmp_path, **system):
    base_system = {
        "enabled": True,
        "max_total_power": 1600,
        "loop_interval": 3,
        "min_output_limit": 35,
    }
    base_system.update(system)
    data = {
        "system": base_system,
        "winter": {"enabled": False},
        "devices": {
            "WR1": {
                "enabled": True,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            },
            "WR2": {
                "enabled": True,
                "max_power": 600,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            },
        },
    }
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _convergence_service(tmp_path):
    from admin.config_apply import ConfigApplyService
    from admin.install_context import detect_install_context

    return ConfigApplyService(
        None,
        tmp_path / "admin-data",
        install_context_provider=lambda: detect_install_context(base_dir=str(tmp_path)),
    )


def test_apply_mirrors_changed_overlapping_key_to_runtime(tmp_path, monkeypatch):
    data = _config()
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    runtime_path = _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.loop_interval"] = 7
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["ok"] is True
    assert payload["runtime_sync"]["applied"] == ["system.loop_interval"]
    assert json.loads(runtime_path.read_text())["system"]["loop_interval"] == 7
    assert (
        json.loads((tmp_path / "config" / "config.json").read_text())["system"][
            "loop_interval"
        ]
        == 7
    )


def test_apply_succeeds_when_runtime_file_absent(tmp_path, monkeypatch):
    data = _config()
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.loop_interval"] = 7
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {
                "draft": loaded["draft"],
                "revision": loaded["revision"],
                "confirm": True,
                "backup": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["ok"] is True
    assert payload["runtime_sync"]["applied"] == []
    assert payload["runtime_sync"]["skipped"][0]["reason"] == "runtime_state_absent"
    assert not (tmp_path / "runtime-state.json").exists()
    assert (
        json.loads((tmp_path / "config" / "config.json").read_text())["system"][
            "loop_interval"
        ]
        == 7
    )


def test_reset_runtime_endpoint_writes_config_value(tmp_path, monkeypatch):
    data = _config()
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    runtime_path = _seed_runtime_state(tmp_path, loop_interval=5)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/reset-runtime",
            {"targets": [{"scope": "system", "key": "loop_interval"}]},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["ok"] is True
    assert payload["runtime_sync"]["applied"] == ["system.loop_interval"]
    assert json.loads(runtime_path.read_text())["system"]["loop_interval"] == 3


def test_reset_runtime_resolves_masked_cloud_name_by_opaque_token(
    tmp_path, monkeypatch
):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    raw_name = f"Cloud shed {route}"
    data = _config()
    data["devices"] = [
        {
            "type": "zendure_mqtt",
            "name": raw_name,
            "enabled": True,
            "max_power": 700,
            "mqtt": {
                "broker_ref": "cloud_a",
                "topic_family": "legacy_zendure_json",
                "product_key": product,
                "device_id": route,
                "write_topic": f"iot/{product}/{route}/properties/write",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        }
    ]
    data["zendure_mqtt"] = {
        "brokers": {
            "cloud_a": {
                "host": "mqtt.example.invalid",
                "port": 8883,
                "tls": True,
                "source": "zendure_cloud_mqtt",
                "credentials_ref": "zendure_cloud:cloud_a",
            }
        }
    }
    _write_config(tmp_path, data)
    runtime_path = tmp_path / "runtime-state.json"
    runtime_path.write_text(
        json.dumps(
            {
                "system": {"max_total_power": 1600},
                "devices": {raw_name: {"max_power": 500}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        load_status, loaded = _get(f"{base}/api/admin/maintenance/config")
        safe_name, fields = next(iter(loaded["overrides"]["devices"].items()))
        token = fields["max_power"]["physical_identity_token"]
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/reset-runtime",
            {
                "targets": [
                    {
                        "scope": "device",
                        "name": safe_name,
                        "key": "max_power",
                        "physical_identity_token": token,
                    }
                ]
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert load_status == 200
    assert route not in safe_name
    assert token.startswith("opaque:v1:")
    assert status == 200
    assert payload["ok"] is True
    assert json.loads(runtime_path.read_text())["devices"][raw_name]["max_power"] == 700
    response_text = json.dumps(payload)
    assert route not in response_text
    assert product not in response_text
    assert f"iot/{product}/{route}/properties/write" not in response_text
