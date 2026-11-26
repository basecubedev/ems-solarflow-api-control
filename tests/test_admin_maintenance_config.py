# SPDX-FileCopyrightText: 2026 basecubedev
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

pytestmark = [
    pytest.mark.admin,
    pytest.mark.config,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


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


@pytest.mark.parametrize("stored", ["true", 1, "on"])
def test_load_shows_a_device_switched_on_as_text_off_as_the_ems_reads_it(tmp_path, stored):
    """Backlog 59: the EMS builds a device only for ``"enabled": true``; the Admin showed text and 1 on."""

    data = _config()
    data["devices"] = [
        {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "enabled": stored},
        {"name": "WR2", "ip": "192.168.1.101", "sn": "BBB", "enabled": None},
        {"name": "TSUN OpenDTU", "type": "opendtu_http", "ip": "192.168.1.70", "serial": "116491234567", "enabled": stored},
        {
            "type": "zendure_mqtt",
            "name": "MQTT WR",
            "enabled": stored,
            "serial_number": "MQTT1",
            "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "MQTT1"},
            "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
        },
    ]
    _write_config(tmp_path, data)

    result = load_maintenance_config(base_dir=str(tmp_path))

    enabled = {device["original_name"]: device["enabled"] for device in result["draft"]["devices"]}
    assert enabled == {"WR1": False, "WR2": True, "TSUN OpenDTU": False, "MQTT WR": False}
    assert result["summary"]["enabled_device_count"] == 1


def _three_kinds(enabled):
    return [
        {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "enabled": enabled},
        {"name": "TSUN OpenDTU", "type": "opendtu_http", "ip": "192.168.1.70", "serial": "116491234567", "enabled": enabled},
        {
            "type": "zendure_mqtt",
            "name": "MQTT WR",
            "enabled": enabled,
            "serial_number": "MQTT1",
            "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "MQTT1"},
            "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
        },
    ]


@pytest.mark.parametrize("stored", [None, "true", 1])
def test_an_unchanged_draft_leaves_a_device_switch_as_written(tmp_path, stored):
    """The EMS runs ``null`` and never runs ``"true"``; a save that changes neither writes nothing."""

    data = _config()
    data["devices"] = _three_kinds(stored)
    _write_config(tmp_path, data)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["changed"] is False, preview["diff"]


def test_a_device_switched_on_in_the_draft_is_written_true_over_its_text(tmp_path):
    data = _config()
    data["devices"] = _three_kinds("true")
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    for item in loaded["draft"]["devices"]:
        item["enabled"] = True

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert [device["enabled"] for device in preview["preview"]["devices"]] == [True, True, True]


@pytest.mark.parametrize(
    ("dashboard", "expected"),
    [(None, True), ({"port": 8081}, True), ({"enabled": False}, False), ({"enabled": "off"}, False), ({"enabled": "yes"}, True)],
)
def test_summary_reads_the_dashboard_switch_as_the_ems_does(tmp_path, dashboard, expected):
    config = _config()
    if dashboard is None:
        del config["dashboard"]
    else:
        config["dashboard"] = dashboard
    _write_config(tmp_path, config)
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["summary"]["dashboard_enabled"] is expected


def test_summary_reads_only_the_dashboard_switch_so_an_odd_timeout_never_breaks_the_page(tmp_path):
    config = _config()
    config["dashboard"] = {"enabled": True, "session_idle_timeout_seconds": "inf", "session_absolute_max_seconds": 1e999}
    _write_config(tmp_path, config)
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "ok"
    assert result["summary"]["dashboard_enabled"] is True


def test_summary_names_every_template_value_ems_would_refuse_to_control_with(tmp_path):
    template = json.loads((Path(__file__).resolve().parents[1] / "config" / "config.template.json").read_text("utf-8"))
    _write_config(tmp_path, template)
    fields = load_maintenance_config(base_dir=str(tmp_path))["summary"]["control"]["placeholder_fields"]
    assert any("devices[0].ip" in field for field in fields)
    assert "grid_meter.ip" in fields
    _write_config(tmp_path, _config())
    assert load_maintenance_config(base_dir=str(tmp_path))["summary"]["control"]["placeholder_fields"] == []


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


def test_load_shows_operating_values_as_the_ems_runs_them(tmp_path):
    """An operating value has one value, the runtime one; a hard limit stays the
    config's, with the operating value below it named apart."""

    data = _config()
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    (tmp_path / "runtime-state.json").write_text(
        json.dumps({
            "system": {"loop_interval": 5, "max_total_power": 900},
            "devices": {"WR1": {"identity": "AAA", "max_power": 500, "enabled": False}},
        }),
        encoding="utf-8",
    )
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["draft"]["features"]["system.loop_interval"] == 5
    assert result["draft"]["features"]["system.max_total_power"] == data["system"]["max_total_power"]
    assert result["operating"]["system.max_total_power"] == 900
    assert result["operating"]["devices"]["WR1"] == {"max_power": 500, "control": False}
    assert result["operating"]["devices"]["WR2"]["control"] is None
    assert "overrides" not in result


@pytest.mark.parametrize(("config_enabled", "runtime_enabled", "status"), [
    (True, False, "disabled"),
    (False, True, "may_control"),
    (True, None, "may_control"),
    (False, None, "disabled"),
])
def test_the_control_summary_shows_control_as_the_ems_runs_it(tmp_path, config_enabled, runtime_enabled, status):
    """Control on or off is an operating value: the EMS takes the runtime value
    before the config's, and the summary shows what it takes."""

    data = _config()
    data["system"].update({"enabled": config_enabled, "dry_run": False})
    _write_config(tmp_path, data)
    if runtime_enabled is not None:
        (tmp_path / "runtime-state.json").write_text(
            json.dumps({"system": {"enabled": runtime_enabled}}), encoding="utf-8"
        )

    control = load_maintenance_config(base_dir=str(tmp_path))["summary"]["control"]

    assert control["enabled"] is (runtime_enabled if runtime_enabled is not None else config_enabled)
    assert control["status"] == status


def test_load_without_runtime_file_names_the_operating_values_the_ems_would_seed(tmp_path):
    data = _config()
    _write_config(tmp_path, data)
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["operating"]["system.max_total_power"] == data["system"]["max_total_power"]
    assert result["operating"]["devices"]["WR1"] == {"max_power": 800, "control": None}


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


def _adopted_mqtt_grid_meter_config():
    """A config with the local broker profile an adopted MQTT meter references."""

    data = _config()
    data["zendure_mqtt"] = {
        "brokers": {
            "local_bridge": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "192.168.1.10",
                "port": 1883,
                "tls": False,
            }
        }
    }
    return data


# Exactly the draft the Admin grid-meter adoption writes
# (frontend/admin-modern/src/lib/discovery.js meterFromProposal): meter type,
# broker reference, topic and payload defaults from the trusted proposal
# fragment, and no endpoint fields of its own.
_ADOPTED_MQTT_GRID_METER_DRAFT = {
    "present": True,
    "type": "zendure_smartmeter_d0",
    "mqtt": {
        "broker_ref": "local_bridge",
        "topic": "Zendure/sensor/D0SERIAL/totalPower",
        "payload_format": "number",
        "max_age_seconds": 15,
    },
}


def test_adopted_mqtt_grid_meter_draft_validates_and_replaces_the_http_meter(tmp_path):
    _write_config(tmp_path, _adopted_mqtt_grid_meter_config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["grid_meter"] = dict(_ADOPTED_MQTT_GRID_METER_DRAFT)

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    grid = preview["preview"]["grid_meter"]
    assert preview["validation"]["ok"] is True
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"] == {
        "broker_ref": "local_bridge",
        "topic": "Zendure/sensor/D0SERIAL/totalPower",
        "payload_format": "number",
        "max_age_seconds": 15,
    }
    # The replaced HTTP endpoint never lingers beside the MQTT settings.
    assert "ip" not in grid


def test_adopted_mqtt_grid_meter_reaches_disk_only_through_explicit_apply(tmp_path):
    path = _write_config(tmp_path, _adopted_mqtt_grid_meter_config())
    original = path.read_bytes()
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["grid_meter"] = dict(_ADOPTED_MQTT_GRID_METER_DRAFT)

    preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert path.read_bytes() == original

    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    assert json.loads(prepared["payload"])["grid_meter"]["mqtt"]["topic"] == (
        "Zendure/sensor/D0SERIAL/totalPower"
    )
    assert path.read_bytes() == original


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
    draft["features"]["winter.winter_min_soc"] = 35
    draft["features"]["winter.enabled"] = not draft["features"]["winter.enabled"]
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    rows = {c["path"]: c for bucket in ("changes", "added") for c in preview["diff"][bucket]}
    assert preview["preview"]["winter"]["winter_min_soc"] == 35
    assert rows["winter.winter_min_soc"]["applies_live"] is False
    assert rows["winter.enabled"]["applies_live"] is True
    assert preview["preview"]["winter"].get("enabled") == _config()["winter"].get("enabled")


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


@pytest.mark.parametrize(("tls", "port"), [(False, 1883), (True, 8883)])
def test_a_new_top_level_broker_without_a_port_takes_the_default_port_of_its_tls(tmp_path, tls, port):
    _write_config(tmp_path, _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["zendure_mqtt"] = {"present": True, "host": "192.168.1.20", "port": "", "tls": tls, "tls_explicit": True}
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    written = json.loads(prepared["payload"])["zendure_mqtt"]
    assert (written["port"], written.get("tls", False)) == (port, tls)


def test_a_tls_grid_meter_draft_says_so_and_an_unchanged_apply_writes_nothing(tmp_path):
    config = _config()
    config["grid_meter"] = {
        "type": "mqtt",
        "mqtt": {"host": "192.168.1.7", "port": 8883, "tls_mode": "tls", "topic": "meter/power"},
    }
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["draft"]["grid_meter"]["mqtt"]["tls"] is True
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok"
    assert json.loads(prepared["payload"])["grid_meter"] == config["grid_meter"]


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


def test_an_unchanged_draft_keeps_the_template_s_empty_broker_credentials(tmp_path):
    config = _config()
    config["zendure_mqtt"] = {"host": "", "port": 1883, "tls": False, "username": "", "password": "", "app_key": ""}
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["changed"] is False
    assert preview["diff"]["removed"] == []
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert json.loads(prepared["payload"])["zendure_mqtt"]["username"] == ""


def test_a_cleared_broker_username_is_removed(tmp_path):
    _write_config(tmp_path, _mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    assert draft["zendure_mqtt"]["username"]
    draft["zendure_mqtt"]["username"] = ""
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert "username" not in json.loads(prepared["payload"])["zendure_mqtt"]


def test_a_whitespace_username_is_dropped_and_a_draft_without_the_field_keeps_the_stored_one(tmp_path):
    config = _mqtt_config()
    config["zendure_mqtt"]["username"] = "   "
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert "username" not in json.loads(prepared["payload"])["zendure_mqtt"]

    _write_config(tmp_path, _mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["zendure_mqtt"].pop("username")
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert json.loads(prepared["payload"])["zendure_mqtt"]["username"] == "mqttuser"


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
    assert diff["changes"] == [
        {"path": "x", "before": 1, "after": 2, "applies_live": False}
    ]


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


def test_apply_writes_a_changed_operating_value_to_the_runtime_state_only(tmp_path, monkeypatch):
    """An operating value lives in runtime-state.json; the apply writes it there
    and leaves config.json as it is (docs/developer/settings-operation-store.md)."""

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
    assert payload["changed"] is True
    assert payload["operating_sync"]["applied"] == ["system.loop_interval"]
    assert json.loads(runtime_path.read_text())["system"]["loop_interval"] == 7
    assert json.loads((tmp_path / "config" / "config.json").read_text())["system"]["loop_interval"] == 3


def test_apply_writes_a_device_operating_value_to_its_runtime_entry_only(tmp_path, monkeypatch):
    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    item = next(device for device in loaded["draft"]["devices"] if device["name"] == "WR1")
    item["pv_priority_factor"] = 1.5
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True, "backup": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200 and payload["ok"] is True
    assert payload["operating_sync"]["applied"] == ["devices[0].pv_priority_factor"]
    assert json.loads(runtime_path.read_text())["devices"]["WR1"]["pv_priority_factor"] == 1.5
    saved = json.loads((tmp_path / "config" / "config.json").read_text())["devices"][0]
    assert "pv_priority_factor" not in saved


def test_an_operating_change_creates_the_runtime_state_where_the_ems_has_not(tmp_path, monkeypatch):
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
    assert payload["operating_sync"]["applied"] == ["system.loop_interval"]
    created = json.loads((tmp_path / "runtime-state.json").read_text())
    assert created["system"]["loop_interval"] == 7
    assert created["defaults_revision"] == 0
    assert json.loads((tmp_path / "config" / "config.json").read_text())["system"]["loop_interval"] == 3


def test_a_created_runtime_state_records_the_configs_defaults_revision(tmp_path, monkeypatch):
    """Its first EMS start then takes no value set here for an old default it would refresh."""

    from ems.config import DEFAULTS_REVISION

    data = _config()
    data["config_defaults_revision"] = DEFAULTS_REVISION
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.loop_interval"] = 3
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True, "backup": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200 and payload["operating_sync"]["applied"] == ["system.loop_interval"]
    assert json.loads((tmp_path / "runtime-state.json").read_text())["defaults_revision"] == DEFAULTS_REVISION


def test_a_page_loaded_before_a_change_elsewhere_puts_back_no_operating_value(tmp_path):
    """The dashboard, the scheduler or emsctl changed values after the page loaded; a save of another field keeps them."""

    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    changed = json.loads(runtime_path.read_text())
    changed["system"]["loop_interval"] = 9
    changed["winter"]["enabled"] = True
    changed["devices"]["WR1"]["ac_discharge_enabled"] = False
    runtime_path.write_text(json.dumps(changed), encoding="utf-8")
    loaded["draft"]["features"]["system.min_output_limit"] = 50

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    rows = {row["path"]: row for row in preview["diff"]["changes"]}
    assert rows["system.min_output_limit"]["after"] == 50
    assert not {"system.loop_interval", "winter.enabled", "devices[0].ac_discharge_enabled"} & set(rows)


def test_an_operating_value_of_a_device_the_ems_does_not_run_stays_in_its_entry(tmp_path):
    data = _config()
    data["devices"][1].update(enabled=False, pv_priority_factor=2.0)
    _write_config(tmp_path, data)
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    del seeded["devices"]["WR2"]
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    item = next(device for device in loaded["draft"]["devices"] if device["name"] == "WR2")
    item["pv_priority_factor"] = 2.5

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["preview"]["devices"][1]["pv_priority_factor"] == 2.5
    live = [row for row in preview["diff"]["changes"] if row.get("applies_live") and row["path"].startswith("devices[1]")]
    assert live == []


def test_a_new_device_keeps_the_operating_values_typed_for_it(tmp_path):
    _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    template = next(device for device in loaded["draft"]["devices"] if device["name"] == "WR2")
    new = {key: value for key, value in template.items() if key not in ("original_name", "operating_base")}
    new.update(name="WR3", ip="192.168.1.102", sn="CCC", ac_charge_enabled=False, ac_discharge_enabled=False, pv_priority_factor=2.0)
    loaded["draft"]["devices"].append(new)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    added = next(device for device in preview["preview"]["devices"] if device["name"] == "WR3")
    assert (added["ac_charge_enabled"], added["ac_discharge_enabled"], added["pv_priority_factor"]) == (False, False, 2.0)


def _apply_retention(tmp_path, monkeypatch, before, after, loop_interval=None):
    data = _config()
    data["influxdb"]["retention"] = {"raw_days": before}
    data["system"]["loop_interval"] = 3
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["influxdb.retention.raw_days"] = after
    if loop_interval is not None:
        loaded["draft"]["features"]["system.loop_interval"] = loop_interval
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        return _post(
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


def test_a_confirmed_apply_records_the_retention_for_the_ems(tmp_path, monkeypatch):
    """The operator saw the change and its data-loss warning and confirmed it;
    the EMS's schema keeper applies a retention recorded here over older data."""

    from ems.history.confirmed_retention import confirmations, confirmed_path

    status, payload = _apply_retention(tmp_path, monkeypatch, 14, 7)

    assert status == 200
    assert payload["ok"] is True
    assert payload["analytics_retention"] == {"confirmed": ["ems_raw"], "warnings": []}
    entries = confirmations(confirmed_path(tmp_path / "data"))
    assert {name: entry.retention_seconds for name, entry in entries.items()} == {"ems_raw": 7 * 86400}


def test_a_confirmed_longer_retention_is_recorded_too(tmp_path, monkeypatch):
    """InfluxDB's setup may have left the bucket keeping data forever; the
    longer retention confirmed here is still shorter than that."""

    from ems.history.confirmed_retention import confirmations, confirmed_path

    status, payload = _apply_retention(tmp_path, monkeypatch, 14, 30)

    assert status == 200
    assert payload["analytics_retention"] == {"confirmed": ["ems_raw"], "warnings": []}
    entries = confirmations(confirmed_path(tmp_path / "data"))
    assert {name: entry.retention_seconds for name, entry in entries.items()} == {"ems_raw": 30 * 86400}


def test_an_apply_that_changes_no_retention_records_nothing(tmp_path, monkeypatch):
    from ems.history.confirmed_retention import confirmed_path

    status, payload = _apply_retention(tmp_path, monkeypatch, 14, 14, loop_interval=7)

    assert status == 200
    assert payload["analytics_retention"] == {"confirmed": [], "warnings": []}
    assert not confirmed_path(tmp_path / "data").exists()


def test_a_confirmation_that_cannot_be_recorded_does_not_undo_the_apply(tmp_path, monkeypatch):
    (tmp_path / "data").write_text("not a directory", encoding="utf-8")

    status, payload = _apply_retention(tmp_path, monkeypatch, 14, 7)

    assert status == 200
    assert payload["ok"] is True
    assert payload["analytics_retention"]["confirmed"] == []
    assert payload["analytics_retention"]["warnings"]
    saved = json.loads((tmp_path / "config" / "config.json").read_text())
    assert saved["influxdb"]["retention"]["raw_days"] == 7


def test_a_broker_address_change_survives_the_feature_round_trip():
    """The browser sends the whole draft back; the broker card owns host and port."""

    from admin.maintenance_config import _merge_draft, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 1883, "username": "u", "password": "p"},
    }
    draft = build_maintenance_draft(config)
    draft["zendure_mqtt"]["host"] = "10.0.0.2"
    draft["zendure_mqtt"]["port"] = 8883
    issues = []

    merged = _merge_draft(config, draft, issues)

    assert issues == []
    assert (merged["zendure_mqtt"]["host"], merged["zendure_mqtt"]["port"]) == ("10.0.0.2", 8883)
    assert not {"zendure_mqtt.host", "zendure_mqtt.port", "zendure_mqtt.tls"} & set(draft["features"])


def test_the_other_zendure_mqtt_settings_stay_editable_in_maintenance():
    from admin.maintenance_config import _merge_draft, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {
            "host": "10.0.0.1",
            "port": 1883,
            "tls_insecure": False,
            "keepalive_seconds": 30,
            "connect_timeout_seconds": 10.0,
        },
    }
    draft = build_maintenance_draft(config)
    draft["features"]["zendure_mqtt.tls_insecure"] = True
    draft["features"]["zendure_mqtt.keepalive_seconds"] = 60
    draft["features"]["zendure_mqtt.connect_timeout_seconds"] = 20
    issues = []

    merged = _merge_draft(config, draft, issues)

    assert issues == []
    assert merged["zendure_mqtt"]["tls_insecure"] is True
    assert merged["zendure_mqtt"]["keepalive_seconds"] == 60
    assert merged["zendure_mqtt"]["connect_timeout_seconds"] == 20
    assert merged["zendure_mqtt"]["host"] == "10.0.0.1"


def test_an_apply_that_leaves_tls_off_with_verification_skipped_is_refused():
    """The EMS disables such a broker at start; Maintenance says so first."""

    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 8883, "tls": True, "tls_insecure": True},
    }
    draft = build_maintenance_draft(config)
    draft["zendure_mqtt"]["tls"] = False
    issues = []

    merged = _merge_draft(config, draft, issues)
    codes = [issue["code"] for issue in _validate(merged, issues)["errors"]]

    assert "zendure_mqtt_tls_invalid" in codes


def test_tls_with_verification_skipped_is_a_valid_maintenance_apply():
    from admin.maintenance_config import _validate

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 8883, "tls": True, "tls_insecure": True},
    }

    codes = [issue["code"] for issue in _validate(config)["errors"]]

    assert "zendure_mqtt_tls_invalid" not in codes


@pytest.mark.parametrize("tls", [False, "false"])
def test_top_level_tls_keys_beside_named_brokers_do_not_block_an_apply(tls):
    """Named brokers without a top-level host: the EMS runs no broker there."""

    from admin.maintenance_config import _validate

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {
            "tls": tls,
            "tls_insecure": True,
            "brokers": {"home": {"host": "192.168.50.10", "source": "local_mqtt", "port": 1883}},
        },
    }

    codes = [issue["code"] for issue in _validate(config)["errors"]]

    assert "zendure_mqtt_tls_invalid" not in codes


@pytest.mark.parametrize(
    "block, refused",
    [
        ({"tls": False, "tls_insecure": True}, True),
        ({"host": "   ", "tls": False, "tls_insecure": True}, True),
        ({"tls": "false"}, True),
        ({"tls": "false", "brokers": {}}, True),
        ({"host": "10.0.0.1", "tls": False, "tls_insecure": True}, True),
        ({"host": "10.0.0.1", "tls": True, "tls_insecure": True}, False),
        (
            {
                "host": 123,
                "tls": False,
                "tls_insecure": True,
                "brokers": {"home": {"host": "192.168.50.10", "source": "local_mqtt"}},
            },
            False,
        ),
    ],
)
def test_maintenance_refuses_exactly_the_top_level_tls_pairs_the_ems_rejects(block, refused):
    from admin.maintenance_config import _validate
    from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": block,
    }

    codes = [issue["code"] for issue in _validate(config)["errors"]]
    _brokers, runtime_errors, _timeout = load_zendure_mqtt_broker_configs(block)

    assert ("zendure_mqtt_tls_invalid" in codes) is refused
    assert bool(runtime_errors.get("default")) is refused


@pytest.mark.parametrize(
    "block, hint",
    [
        ({"host": "10.0.0.1", "tls": False, "tls_insecure": True}, "tls_insecure to false"),
        ({"host": "10.0.0.1", "tls_insecure": True}, "tls_insecure to false"),
        ({"host": "10.0.0.1", "tls": "false"}, "without quotes"),
        ({"host": "10.0.0.1", "tls": 0, "tls_insecure": True}, "without quotes"),
        ({"host": "10.0.0.1", "tls_mode": "  ", "tls": False, "tls_insecure": True}, "tls_insecure to false"),
        (
            {"host": "10.0.0.1", "tls_mode": "insecure_no_verify", "tls_insecure": False},
            "contradicts tls_mode",
        ),
    ],
)
def test_the_tls_refusal_names_the_fix_that_applies(block, hint):
    from admin.maintenance_config import _validate

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": block,
    }

    messages = [
        issue["message"]
        for issue in _validate(config)["errors"]
        if issue["code"] == "zendure_mqtt_tls_invalid"
    ]

    assert len(messages) == 1
    assert hint in messages[0]


@pytest.mark.parametrize(
    "stored",
    [
        {"tls": "false"},
        {"tls": False, "tls_insecure": True},
    ],
    ids=["quoted-false", "insecure-without-tls"],
)
def test_an_untouched_broker_card_never_rewrites_an_invalid_stored_tls(stored):
    """The view shows such a broker as TLS so it cannot invite a downgrade; a
    round trip must not turn that view into a silent `tls: true` either."""

    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 1883, **stored},
    }
    draft = build_maintenance_draft(config)
    issues = []

    merged = _merge_draft(config, draft, issues)
    codes = [issue["code"] for issue in _validate(merged, issues)["errors"]]

    assert draft["zendure_mqtt"]["tls_stored_invalid"] is True
    assert {k: merged["zendure_mqtt"].get(k) for k in stored} == stored
    assert "zendure_mqtt_tls_invalid" in codes


def test_an_explicit_security_choice_replaces_an_invalid_stored_tls():
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 8883, "tls": "false"},
    }
    draft = build_maintenance_draft(config)
    draft["zendure_mqtt"]["tls"] = True
    draft["zendure_mqtt"]["tls_explicit"] = True
    issues = []

    merged = _merge_draft(config, draft, issues)
    codes = [issue["code"] for issue in _validate(merged, issues)["errors"]]

    assert merged["zendure_mqtt"]["tls"] is True
    assert "zendure_mqtt_tls_invalid" not in codes


def test_a_valid_stored_tls_is_not_marked_invalid():
    from admin.maintenance_config import build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 8883, "tls": True},
    }

    assert build_maintenance_draft(config)["zendure_mqtt"]["tls_stored_invalid"] is False


@pytest.mark.parametrize("port", ["abc", 0, 70000, "1.5", True])
def test_maintenance_refuses_a_top_level_broker_port_the_ems_rejects(port):
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 1883},
    }
    draft = build_maintenance_draft(config)
    draft["zendure_mqtt"]["port"] = port
    issues = []

    merged = _merge_draft(config, draft, issues)
    codes = [issue["code"] for issue in _validate(merged, issues)["errors"]]

    assert "zendure_mqtt_port_invalid" in codes


@pytest.mark.parametrize("port", [1883, "8883", None])
def test_a_port_the_ems_accepts_passes(port):
    from admin.maintenance_config import _validate

    block = {"host": "10.0.0.1"}
    if port is not None:
        block["port"] = port
    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": block,
    }

    codes = [issue["code"] for issue in _validate(config)["errors"]]

    assert "zendure_mqtt_port_invalid" not in codes


def test_an_operating_value_the_runtime_service_refuses_blocks_the_apply(tmp_path):
    _write_config(tmp_path, _config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    draft["features"]["winter.enabled"] = "perhaps"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    codes = [e["code"] for e in preview["validation"]["errors"] if "winter.enabled" in e["message"]]
    assert codes == ["operating_value_invalid"]
    assert not (tmp_path / "runtime-state.json").exists()


@pytest.mark.parametrize(
    "path,value",
    [
        ("winter.adjust_hour", "noon"),
        ("winter.ramp_step_percent", 9),
    ],
)
def test_a_feature_value_its_type_cannot_hold_blocks_the_apply(path, value):
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "winter": {"enabled": True, "adjust_hour": 12, "ramp_step_percent": 3},
    }
    draft = build_maintenance_draft(config)
    draft["features"][path] = value
    issues = []

    merged = _merge_draft(config, draft, issues)
    errors = _validate(merged, issues)["errors"]

    assert merged["winter"] == {"enabled": True, "adjust_hour": 12, "ramp_step_percent": 3}
    assert [e["code"] for e in errors if path in e["message"]] == ["config_value_invalid"]


def test_a_device_value_its_type_cannot_hold_names_the_device():
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC", "max_power": 800}],
    }
    draft = build_maintenance_draft(config)
    draft["devices"][0]["max_power"] = "lots"
    issues = []

    merged = _merge_draft(config, draft, issues)
    errors = _validate(merged, issues)["errors"]

    assert merged["devices"][0]["max_power"] == 800
    assert any(
        e["code"] == "config_value_invalid" and e["message"].startswith("INV_1: max_power")
        for e in errors
    )


def test_a_stored_value_outside_the_catalog_range_does_not_block_other_edits():
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "winter": {"enabled": True, "ramp_step_percent": 5, "adjust_hour": 12},
    }
    draft = build_maintenance_draft(config)
    draft["features"]["winter.adjust_hour"] = 13
    issues = []

    merged = _merge_draft(config, draft, issues)
    codes = [e["code"] for e in _validate(merged, issues)["errors"]]

    assert "config_value_invalid" not in codes
    assert merged["winter"]["ramp_step_percent"] == 5
    assert merged["winter"]["adjust_hour"] == 13


def test_a_stored_out_of_range_value_sent_back_as_text_is_still_left_alone():
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "winter": {"enabled": True, "ramp_step_percent": 5},
    }
    draft = build_maintenance_draft(config)
    draft["features"]["winter.ramp_step_percent"] = "5"
    issues = []

    merged = _merge_draft(config, draft, issues)
    codes = [e["code"] for e in _validate(merged, issues)["errors"]]

    assert "config_value_invalid" not in codes
    assert merged["winter"]["ramp_step_percent"] == 5


def _config_with_an_mqtt_device_and_meter():
    return {
        "system": {"enabled": True},
        "devices": [
            {"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"},
            {
                "type": "zendure_mqtt",
                "name": "INV_2",
                "enabled": True,
                "serial_number": "MQTT1",
                "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "MQTT1"},
                "max_power": 800,
            },
        ],
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"host": "10.0.0.9", "topic": "t", "port": 1883},
        },
    }


def test_an_mqtt_device_value_its_type_cannot_hold_names_the_device():
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = _config_with_an_mqtt_device_and_meter()
    draft = build_maintenance_draft(config)
    mqtt = next(item for item in draft["devices"] if item["name"] == "INV_2")
    mqtt["max_power"] = "lots"
    issues = []

    merged = _merge_draft(config, draft, issues)
    errors = _validate(merged, issues)["errors"]

    assert merged["devices"][1]["max_power"] == 800
    assert any(
        e["code"] == "config_value_invalid" and e["message"].startswith("INV_2: max_power")
        for e in errors
    )


def test_a_grid_meter_value_its_type_cannot_hold_blocks_the_apply():
    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = _config_with_an_mqtt_device_and_meter()
    draft = build_maintenance_draft(config)
    draft["grid_meter"]["mqtt"]["port"] = "abc"
    issues = []

    merged = _merge_draft(config, draft, issues)
    errors = _validate(merged, issues)["errors"]

    assert merged["grid_meter"]["mqtt"]["port"] == 1883
    assert any(
        e["code"] == "config_value_invalid" and e["message"].startswith("Grid meter:")
        for e in errors
    )


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"tls": True},
        {"tls": False},
        {"tls": True, "tls_insecure": True},
        {"tls_mode": "tls"},
        {"tls_mode": "system_ca"},
        {"tls_mode": "insecure_no_verify"},
        {"tls_mode": "plaintext"},
        {"tls_mode": "tls", "tls": True},
        {"tls_mode": "bogus"},
        {"tls_mode": "tls", "tls": False},
        {"tls_mode": "tls", "tls_insecure": True},
        {"tls": "false"},
        {"tls": False, "tls_insecure": True},
    ],
)
def test_the_broker_card_reads_tls_as_the_ems_connects(stored):
    """The card's TLS view is the EMS's own reading of the stored keys,
    ``tls_mode`` included; a reading the EMS refuses is marked invalid and
    shown as TLS."""

    from admin.maintenance_config import build_maintenance_draft
    from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

    block = {"host": "10.0.0.1", **stored}
    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": block,
    }

    view = build_maintenance_draft(config)["zendure_mqtt"]
    brokers, runtime_errors, _timeout = load_zendure_mqtt_broker_configs(block)

    refused = bool(runtime_errors.get("default"))
    assert view["tls_stored_invalid"] is refused
    assert view["tls"] is (True if refused else brokers["default"].tls)


@pytest.mark.parametrize(
    "stored",
    [
        {"tls_mode": "tls"},
        {"tls_mode": "insecure_no_verify"},
        {"tls_mode": "plaintext"},
        {"tls_mode": "bogus"},
    ],
)
def test_an_untouched_broker_card_leaves_a_stored_tls_mode_alone(stored):
    from admin.maintenance_config import _merge_draft, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 8883, **stored},
    }
    before = json.dumps(config["zendure_mqtt"])
    draft = build_maintenance_draft(config)

    merged = _merge_draft(config, draft, [])

    assert json.dumps(merged["zendure_mqtt"]) == before


@pytest.mark.parametrize(
    "stored, choice",
    [
        ({"tls_mode": "tls"}, False),
        ({"tls_mode": "plaintext"}, True),
    ],
)
def test_a_security_choice_against_a_stored_tls_mode_is_refused_not_dropped(
    stored, choice
):
    """The card never writes ``tls_mode``; a choice that contradicts it is
    written as ``tls`` so the EMS's validation refuses it and names the fix,
    instead of an apply that reports success and changes nothing."""

    from admin.maintenance_config import _merge_draft, _validate, build_maintenance_draft

    config = {
        "system": {"enabled": True},
        "devices": [{"name": "INV_1", "ip": "192.0.2.5", "sn": "ABC"}],
        "zendure_mqtt": {"host": "10.0.0.1", "port": 8883, **stored},
    }
    draft = build_maintenance_draft(config)
    draft["zendure_mqtt"]["tls"] = choice
    draft["zendure_mqtt"]["tls_explicit"] = True
    issues = []

    merged = _merge_draft(config, draft, issues)
    errors = _validate(merged, issues)["errors"]

    assert merged["zendure_mqtt"]["tls_mode"] == stored["tls_mode"]
    assert merged["zendure_mqtt"]["tls"] is choice
    assert [e["code"] for e in errors if e["code"] == "zendure_mqtt_tls_invalid"] == [
        "zendure_mqtt_tls_invalid"
    ]
    assert "contradicts tls_mode" in next(
        e["message"] for e in errors if e["code"] == "zendure_mqtt_tls_invalid"
    )


def test_a_switched_off_device_shows_the_values_waiting_for_it(tmp_path):
    data = _config()
    data["devices"][1]["enabled"] = False
    _write_config(tmp_path, data)
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    del seeded["devices"]["WR2"]
    seeded["parked_devices"] = {"identity:BBB": {"pv_priority_factor": 2.5, "ac_discharge_enabled": False}}
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")

    loaded = load_maintenance_config(base_dir=str(tmp_path))

    item = next(device for device in loaded["draft"]["devices"] if device["name"] == "WR2")
    assert (item["pv_priority_factor"], item["ac_discharge_enabled"]) == (2.5, False)


def test_while_a_setup_reset_waits_an_operating_change_goes_into_the_config(tmp_path):
    """The next EMS start builds the runtime state from that config; a value written there now would be set aside."""

    from ems.runtime_state import RESET_REQUEST_SUFFIX, config_digest

    config_path = _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    (tmp_path / f"runtime-state.json{RESET_REQUEST_SUFFIX}").write_text(
        json.dumps({"config_sha256": config_digest(config_path.read_bytes())}), encoding="utf-8"
    )
    assert runtime_path == tmp_path / "runtime-state.json"
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.grid_target_discharge_w"] = 40

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["preview"]["system"]["grid_target_discharge_w"] == 40
    rows = (preview["diff"].get("changes") or []) + (preview["diff"].get("added") or [])
    named = [row for row in rows if row["path"] == "system.grid_target_discharge_w"]
    assert named and not [row for row in named if row.get("applies_live")]


def test_an_operating_value_is_checked_against_the_limit_the_save_sets(tmp_path):
    _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.max_total_power"] = 100
    loaded["draft"]["features"]["system.min_output_limit"] = 150

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["validation"]["ok"] is False
    assert any("system.min_output_limit" in error["message"] for error in preview["validation"]["errors"])


def test_an_operating_change_reaches_a_cloud_device_whose_name_the_browser_saw_masked(tmp_path):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    raw_name = f"Cloud shed {route}"
    data = _config()
    data["devices"] = [{
        "type": "zendure_mqtt", "name": raw_name, "enabled": True, "max_power": 700,
        "mqtt": {"broker_ref": "cloud_a", "topic_family": "legacy_zendure_json", "product_key": product,
                 "device_id": route, "write_topic": f"iot/{product}/{route}/properties/write"},
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
    }]
    data["zendure_mqtt"] = {"brokers": {"cloud_a": {"host": "mqtt.example.invalid", "port": 8883, "tls": True,
                                                   "source": "zendure_cloud_mqtt", "credentials_ref": "zendure_cloud:cloud_a"}}}
    _write_config(tmp_path, data)
    (tmp_path / "runtime-state.json").write_text(json.dumps({
        "system": {"max_total_power": 1600},
        "devices": {raw_name: {"identity": route, "max_power": 700, "pv_priority_factor": 1.0}},
    }), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path), identity_token_key=b"k" * 32)
    item = loaded["draft"]["devices"][0]
    assert route not in item["original_name"]
    item["pv_priority_factor"] = 2.5

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path), identity_token_key=b"k" * 32)

    assert [row["after"] for row in preview["diff"]["changes"] if row["path"] == "devices[0].pv_priority_factor"] == [2.5]


def _parked_wr2(tmp_path):
    data = _config()
    data["devices"][1]["enabled"] = False
    _write_config(tmp_path, data)
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    del seeded["devices"]["WR2"]
    seeded["parked_devices"] = {"identity:BBB": {"pv_priority_factor": 2.5, "ac_discharge_enabled": False}}
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    return runtime_path


def test_a_parked_devices_value_is_changed_where_it_is_parked(tmp_path, monkeypatch):
    runtime_path = _parked_wr2(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    item = next(device for device in loaded["draft"]["devices"] if device["name"] == "WR2")
    item["pv_priority_factor"] = 3.0
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _server(config_apply=_convergence_service(tmp_path))
    try:
        status, payload = _post(
            f"{base}/api/admin/maintenance/config/apply",
            {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True, "backup": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200 and payload["operating_sync"]["applied"] == ["devices[1].pv_priority_factor"]
    assert json.loads(runtime_path.read_text())["parked_devices"]["identity:BBB"]["pv_priority_factor"] == 3.0
    assert "pv_priority_factor" not in json.loads((tmp_path / "config" / "config.json").read_text())["devices"][1]


def test_a_new_device_under_a_removed_devices_name_touches_none_of_its_values(tmp_path):
    _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    old = loaded["draft"]["devices"].pop(1)
    new = {key: value for key, value in old.items() if key not in ("original_name", "operating_base")}
    new.update(sn="CCC", ip="192.168.1.109", ac_discharge_enabled=False, pv_priority_factor=2.0)
    loaded["draft"]["devices"].append(new)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert not [row for row in preview["diff"]["changes"] if row.get("applies_live") and row["path"].startswith("devices[1].")]
    added = next(device for device in preview["preview"]["devices"] if device.get("sn") == "CCC")
    assert (added["ac_discharge_enabled"], added["pv_priority_factor"]) == (False, 2.0)


def test_a_serial_change_keeps_the_devices_values_in_its_entry(tmp_path):
    """The next start finds no runtime entry for the new serial and seeds the device from its entry."""

    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["devices"]["WR1"].update(identity="AAA", pv_priority_factor=1.5, ac_discharge_enabled=False)
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"][0]["sn"] = "AAB"

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    device = preview["preview"]["devices"][0]
    assert (device["sn"], device["pv_priority_factor"], device["ac_discharge_enabled"]) == ("AAB", 1.5, False)


def test_a_save_after_the_switch_over_warns_of_no_missing_operating_value(tmp_path):
    _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.max_total_power"] = 1500

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    text = json.dumps(preview["validation"].get("warnings") or [])
    assert "pv_priority_factor" not in text and "ac_discharge_enabled" not in text


def test_while_a_setup_reset_waits_the_page_shows_the_values_the_config_gives(tmp_path):
    from ems.runtime_state import RESET_REQUEST_SUFFIX, config_digest

    config_path = _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path, loop_interval=9)
    (tmp_path / f"runtime-state.json{RESET_REQUEST_SUFFIX}").write_text(
        json.dumps({"config_sha256": config_digest(config_path.read_bytes())}), encoding="utf-8"
    )

    loaded = load_maintenance_config(base_dir=str(tmp_path))

    assert loaded["draft"]["features"]["system.loop_interval"] == 5


def test_a_device_the_ems_does_not_run_names_no_control_switch(tmp_path):
    """Its activation is off in the config; the dashboard switched nothing."""

    _parked_wr2(tmp_path)

    loaded = load_maintenance_config(base_dir=str(tmp_path))

    assert loaded["operating"]["devices"]["WR2"]["control"] is None
    assert loaded["operating"]["devices"]["WR1"]["control"] is True


def test_while_a_setup_reset_waits_the_control_status_follows_the_config(tmp_path):
    from ems.runtime_state import RESET_REQUEST_SUFFIX, config_digest

    data = _config()
    data["system"].update(enabled=True, dry_run=False)
    config_path = _write_config(tmp_path, data)
    _seed_runtime_state(tmp_path, enabled=False)
    (tmp_path / f"runtime-state.json{RESET_REQUEST_SUFFIX}").write_text(
        json.dumps({"config_sha256": config_digest(config_path.read_bytes())}), encoding="utf-8"
    )

    control = load_maintenance_config(base_dir=str(tmp_path))["summary"]["control"]

    assert control["enabled"] is True


def test_values_typed_for_a_device_added_back_before_a_restart_reach_the_entry_its_serial_owns(tmp_path):
    """The next start keeps that entry; without the write it would win over what was typed."""

    data = _config()
    removed = data["devices"].pop(1)
    _write_config(tmp_path, data)
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["devices"]["WR1"]["identity"] = "AAA"
    seeded["devices"]["WR2"].update(identity="BBB", ac_discharge_enabled=True)
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"].append({**removed, "pv_priority_factor": 2.0, "ac_discharge_enabled": False})

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    rows = {row["path"]: row for row in preview["diff"]["changes"] if row.get("applies_live")}
    assert rows["devices[1].pv_priority_factor"]["after"] == 2.0
    assert rows["devices[1].ac_discharge_enabled"]["after"] is False


def test_an_operating_row_names_the_device_where_the_save_places_it(tmp_path):
    _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    del loaded["draft"]["devices"][0]
    loaded["draft"]["devices"][0]["pv_priority_factor"] = 1.5

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert [row["path"] for row in preview["diff"]["changes"] if row.get("applies_live")] == ["devices[0].pv_priority_factor"]


def test_a_switched_off_copy_of_a_running_device_leaves_its_values_alone(tmp_path):
    """A spare entry for WR1's serial is not what the next start builds for it."""

    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["devices"]["WR1"].update(identity="AAA", pv_priority_factor=3.0, ac_discharge_enabled=False)
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    spare = {key: value for key, value in loaded["draft"]["devices"][0].items() if key not in ("original_name", "operating_base")}
    spare.update(name="WR1 spare", ip="192.168.1.120", enabled=False, pv_priority_factor=1.0, ac_discharge_enabled=True)
    loaded["draft"]["devices"].append(spare)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert not [row for row in preview["diff"]["changes"] if row.get("applies_live") and row["path"].startswith("devices[2].")]


def test_an_existing_device_sent_without_the_values_its_page_loaded_changes_none(tmp_path):
    """A transport switch built from an older page puts back no value set elsewhere since."""

    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["devices"]["WR1"].update(pv_priority_factor=3.0, ac_discharge_enabled=False)
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    item = loaded["draft"]["devices"][0]
    del item["operating_base"]
    item.update(pv_priority_factor=1.5, ac_discharge_enabled=True)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert not [row for row in preview["diff"]["changes"] if row.get("applies_live") and row["path"].startswith("devices[0].")]


def test_a_device_that_never_ran_removed_and_added_back_keeps_its_typed_values(tmp_path):
    data = _config()
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    old = loaded["draft"]["devices"].pop(1)
    again = {key: value for key, value in old.items() if key not in ("original_name", "operating_base")}
    again.update(pv_priority_factor=2.0, ac_discharge_enabled=False)
    loaded["draft"]["devices"].append(again)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    device = next(device for device in preview["preview"]["devices"] if device.get("sn") == "BBB")
    assert (device["pv_priority_factor"], device["ac_discharge_enabled"]) == (2.0, False)


def test_a_draft_without_the_values_its_page_loaded_changes_no_operating_setting(tmp_path):
    """A stale tab or an API caller would otherwise put config values back over the running ones."""

    _write_config(tmp_path, _config())
    _seed_runtime_state(tmp_path, loop_interval=9)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    del loaded["draft"]["operating_base"]
    loaded["draft"]["features"]["system.loop_interval"] = 3

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert not [row for row in preview["diff"]["changes"] if row["path"] == "system.loop_interval"]



def test_a_running_device_removed_and_added_back_in_one_draft_brings_its_typed_values(tmp_path):
    """The merge matches the added item to WR2 by its serial; what was typed for it is what it should run."""

    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["devices"]["WR2"].update(identity="BBB", ac_discharge_enabled=True)
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    old = loaded["draft"]["devices"].pop(1)
    again = {key: value for key, value in old.items() if key not in ("original_name", "operating_base")}
    again.update(ac_discharge_enabled=False)
    loaded["draft"]["devices"].append(again)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    rows = [row for row in preview["diff"]["changes"] if row.get("applies_live") and row["path"] == "devices[1].ac_discharge_enabled"]
    assert [row["after"] for row in rows] == [False]


def test_a_runtime_state_path_that_is_no_text_still_loads_the_page(tmp_path):
    """A hand-edited config is when the page is needed to repair it."""

    data = _config()
    data["system"]["runtime_state_path"] = 5
    _write_config(tmp_path, data)

    assert load_maintenance_config(base_dir=str(tmp_path))["status"] == "ok"


def test_a_value_that_takes_effect_at_once_is_listed_once(tmp_path):
    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["devices"]["WR1"]["identity"] = "AAA"
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["devices"][0].update(sn="AAB", pv_priority_factor=1.5)

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    rows = [row for bucket in ("changes", "added", "removed") for row in preview["diff"].get(bucket) or []]
    assert len([row for row in rows if row["path"] == "devices[0].pv_priority_factor"]) == 1


def test_while_the_runtime_state_comes_from_template_placeholders_a_change_goes_into_the_config(tmp_path):
    """The first start without placeholders builds the runtime state afresh from the config."""

    _write_config(tmp_path, _config())
    runtime_path = _seed_runtime_state(tmp_path)
    seeded = json.loads(runtime_path.read_text())
    seeded["seeded_from_placeholders"] = True
    runtime_path.write_text(json.dumps(seeded), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["winter.enabled"] = True

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["preview"]["winter"]["enabled"] is True
    rows = (preview["diff"].get("changes") or []) + (preview["diff"].get("added") or [])
    assert not [row for row in rows if row.get("applies_live") and row["path"] == "winter.enabled"]


def test_before_any_start_on_a_config_with_template_placeholders_a_change_goes_into_the_config(tmp_path):
    """The first EMS start would force control off into a runtime state the Admin created; the config is read instead."""

    from ems.config import template_placeholder_paths

    template = json.loads((Path(__file__).resolve().parents[1] / "config" / "config.template.json").read_text())
    assert template_placeholder_paths(template)
    _write_config(tmp_path, template)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.min_output_limit"] = 50

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["preview"]["system"]["min_output_limit"] == 50
    assert not (tmp_path / "runtime-state.json").exists()


def test_a_config_without_a_grid_meter_counts_as_still_holding_placeholders(tmp_path):
    """The EMS checks the config with its defaults applied, and so does the Admin."""

    data = _config()
    del data["grid_meter"]
    _write_config(tmp_path, data)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    loaded["draft"]["features"]["system.min_output_limit"] = 50

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert preview["preview"]["system"]["min_output_limit"] == 50
