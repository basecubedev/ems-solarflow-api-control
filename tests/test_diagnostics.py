# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import struct
import subprocess
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import emsctl
from ems.diagnostics import diagnose_config_plausibility
from ems import diagnostics
from ems.state_store import BatteryFullChargeStateStore

from _emsctl_test_helpers import (
    DEFAULT_LIVE_DEVICE,
    assert_diagnose_help_discovery,
    diagnose_args,
    run_emsctl,
    write_config,
    write_control_runtime,
    write_live_control_status,
    write_two_device_config,
)

pytestmark = [
    pytest.mark.integration,
]


def test_emsctl_diagnose_service_entry_points(tmp_path):
    args = diagnose_args(tmp_path)

    service_functions = [
        (emsctl.run_install_diagnosis, None),
        (emsctl.run_deep_diagnosis, "deep"),
        (emsctl.run_hardware_diagnosis, "hardware"),
        (emsctl.run_control_diagnosis, "control"),
        (emsctl.run_control_quality_diagnosis, "control_quality"),
    ]

    for service_function, enabled_option in service_functions:
        report = service_function(args)

        assert report["schema_version"] == 1
        # No hardcoded release version: a local/dev diagnose reports None, and
        # build identity is surfaced under a dedicated "build" block.
        assert report["ems_version"] is None
        assert "build" in report
        assert "build_label" in report["build"]
        assert report["diagnosis"]["version"] == 1
        if enabled_option:
            assert report["options"][enabled_option] is True
        if enabled_option == "control":
            assert report["control"] is not None
        if enabled_option == "control_quality":
            assert report["control_quality"] is not None


def test_emsctl_diagnose_help_lists_all_diagnose_options(tmp_path):
    result = run_emsctl(tmp_path, "diagnose", "--help")

    assert result.returncode == 0, result.stderr
    assert_diagnose_help_discovery(result.stdout)
    for expected in (
        "--deep",
        "--hardware",
        "--control",
        "--control-quality",
        "--quality",
        "--sample-seconds",
        "--support-bundle",
        "--json",
        "--output",
    ):
        assert expected in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_diagnose_text_is_read_only_and_human_readable(tmp_path):
    result = run_emsctl(tmp_path, "diagnose")

    assert result.returncode == 0, result.stderr
    assert "EMS Diagnose" in result.stdout
    assert "Mode:" in result.stdout
    assert "[OK] Python version:" in result.stdout
    assert "[OK] config.json is valid JSON" in result.stdout
    assert "[WARN] Missing config key:" in result.stdout
    assert "Result: warning" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_diagnose_json_is_read_only_and_hides_sensitive_values(tmp_path):
    secret = "secret-token-that-must-not-appear"
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["ha"]["token"] = secret
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert payload["mode"] in ("native", "container")
    assert payload["summary"]["warning"] >= 1
    assert any(
        check["code"] == "missing_config_key"
        for check in payload["checks"]
    )
    assert secret not in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_diagnose_redact_report_for_http_masks_structured_and_text_secrets():
    secret = "super-secret-token"
    report = {
        "schema_version": 1,
        "diagnosis": {
            "status": "warning",
            "metrics": {
                "token": secret,
                "ok_count": 1,
            },
            "warnings": [f"request failed with token={secret}"],
            "errors": [f"http://user:{secret}@example.test/properties/report"],
            "sections": [
                {
                    "id": "hardware",
                    "checks": [
                        {
                            "code": "zendure_device_config_incomplete",
                            "missing": ["ip", "sn"],
                            "message": f"URL contains token={secret}",
                        }
                    ],
                }
            ],
            "root_causes": [],
        },
    }

    redacted = diagnostics.diagnose_redact_report_for_http(report)
    encoded = json.dumps(redacted, sort_keys=True)

    assert secret not in encoded
    assert redacted["diagnosis"]["metrics"]["token"] == "<redacted>"
    assert redacted["diagnosis"]["metrics"]["ok_count"] == 1
    assert "<redacted>" in redacted["diagnosis"]["warnings"][0]
    assert "http://<redacted>:<redacted>@example.test" in redacted["diagnosis"]["errors"][0]
    check = redacted["diagnosis"]["sections"][0]["checks"][0]
    assert check["missing"] == ["ip", "sn"]
    assert secret not in check["message"]
    json.dumps(redacted, sort_keys=True)


def test_diagnose_http_redaction_masks_cloud_route_in_name_and_arbitrary_topic():
    route = "ACCOUNT_ROUTE_1234"
    product = "PRODUCT_ACCOUNT_A"
    topic = f"iot/{product}/{route}/properties/write"
    report = {
        "zendure_mqtt": {
            "brokers": [
                {
                    "broker_ref": "cloud_a",
                    "source": "zendure_cloud_mqtt",
                    "password": "BROKER_PASSWORD",
                }
            ],
            "devices": [
                {
                    "broker_ref": "cloud_a",
                    "source": "zendure_cloud_mqtt",
                    "device_id": route,
                    "product_key": product,
                    "name": f"WR {route}",
                    "reason": f"publish {topic} pending",
                    "authorization_code": "AUTH_CODE_SECRET",
                }
            ],
        }
    }

    flattened = json.dumps(
        diagnostics.diagnose_redact_report_for_http(report)
    )

    for raw in (route, product, topic, "BROKER_PASSWORD", "AUTH_CODE_SECRET"):
        assert raw not in flattened


def test_diagnose_docker_deep_warns_when_docker_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)
    checks = []

    diagnostics.diagnose_docker_deep(checks)

    assert [check["code"] for check in checks] == ["docker_cli_missing"]
    assert checks[0]["level"] == "warning"


def test_diagnose_docker_deep_warns_when_compose_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/bin/docker")
    checks = []

    diagnostics.diagnose_docker_deep(checks)

    assert [check["code"] for check in checks] == [
        "docker_cli_found",
        "compose_file_missing",
    ]
    assert checks[-1]["level"] == "warning"


def test_diagnose_docker_deep_reports_compose_ps_success(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(diagnostics, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/bin/docker")
    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='[{"Name":"ems"}]\n', stderr="")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    checks = []

    diagnostics.diagnose_docker_deep(checks)

    assert [check["code"] for check in checks] == [
        "docker_cli_found",
        "compose_file_found",
        "docker_compose_ps",
    ]
    args, kwargs = run_calls[0]
    assert args[0] == ["/usr/bin/docker", "compose", "ps", "--format", "json"]
    assert kwargs == {
        "cwd": str(tmp_path),
        "text": True,
        "capture_output": True,
        "timeout": 5,
        "check": False,
    }
    assert checks[-1]["details"]["output_preview"] == '[{"Name":"ems"}]'


def test_diagnose_docker_deep_reports_compose_ps_nonzero(tmp_path, monkeypatch):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setattr(diagnostics, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="compose failed",
        ),
    )
    checks = []

    diagnostics.diagnose_docker_deep(checks)

    assert checks[-1]["code"] == "docker_compose_ps_failed"
    assert checks[-1]["level"] == "warning"
    assert checks[-1]["details"]["stderr_preview"] == "compose failed"


@pytest.mark.parametrize(
    "exception",
    [
        subprocess.TimeoutExpired(["docker", "compose", "ps"], 5),
        subprocess.SubprocessError("subprocess failed"),
    ],
)
def test_diagnose_docker_deep_reports_subprocess_errors(tmp_path, monkeypatch, exception):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(diagnostics, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(*args, **kwargs):
        raise exception

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    checks = []

    diagnostics.diagnose_docker_deep(checks)

    assert checks[-1]["code"] == "docker_compose_ps_failed"
    assert checks[-1]["level"] == "warning"
    assert exception.__class__.__name__ in checks[-1]["message"]


def test_emsctl_diagnose_json_contains_v2_structure(tmp_path):
    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["diagnosis"]["version"] == 1
    assert payload["status"] in ("ok", "warning", "error")
    assert payload["mode"] in ("native", "container")
    assert "mode_sources" in payload
    assert payload["battery_full_charge_assist"]["enabled"] is True
    assert payload["battery_full_charge_assist"]["interval_days"] == 28
    assert not (tmp_path / "ems_state.sqlite").exists()


def test_emsctl_diagnose_reports_battery_full_charge_assist_state(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    database_path = tmp_path / "ems_state.sqlite"
    config["battery_full_charge_assist"] = {
        "enabled": True,
        "interval_days": 28,
        "assist_window_days": 7,
        "assist_start_soc": 80,
        "force_time": "14:00",
        "ac_charge_power": 200,
        "enable_ac_charge_mode": True,
        "state_database_path": str(database_path),
    }
    config_path.write_text(json.dumps(config))

    store = BatteryFullChargeStateStore(str(database_path))
    now = datetime.now(timezone.utc)
    store.record_observation(
        "WR1",
        SimpleNamespace(
            soc=85,
            max_soc=90,
            soc_limit=0,
            ac_mode=2,
            ac_status=1,
            pack_num=1,
            soc_status=0,
            battery_calibration_time=1234,
        ),
        True,
        now,
        interval_days=28,
    )
    store.update_device_state(
        "WR1",
        now,
        next_due_at=(now + timedelta(days=3)).isoformat(),
    )

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assist = payload["battery_full_charge_assist"]
    assert assist["enabled"] is True
    assert assist["state_database_path"] == str(database_path)
    assert assist["devices"][0]["device"] == "WR1"
    assert assist["devices"][0]["battery"] is True
    assert assist["devices"][0]["packNum"] == 1
    assert assist["devices"][0]["batCalTime"] == 1234
    assert assist["devices"][0]["status"] == "due soon"
    assert "summary" in payload
    assert isinstance(payload["sections"], list)
    assert isinstance(payload["metrics"], dict)
    assert isinstance(payload["root_causes"], list)
    assert isinstance(payload["warnings"], list)
    assert isinstance(payload["errors"], list)
    assert payload["options"]["deep"] is False
    assert payload["options"]["hardware"] is False
    assert payload["options"]["support_bundle"] is False
    assert payload["options"]["control"] is False
    assert payload["options"]["sample_seconds"] == 0
    assert "generated_at" in payload
    assert payload["project"]["base_dir"]
    assert payload["project"]["config_path"].endswith("config.json")
    assert payload["project"]["runtime_state_path"].endswith("runtime-state.json")
    assert isinstance(payload["checks"], list)


def test_emsctl_diagnose_output_without_support_bundle_is_usage_error(tmp_path):
    result = run_emsctl(tmp_path, "diagnose", "--output", str(tmp_path / "bundle.zip"))

    assert result.returncode == 2
    assert "--output is only valid together with --support-bundle" in result.stderr


def test_emsctl_diagnose_duplicate_device_names_produce_error(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["devices"].append(dict(config["devices"][0]))
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        check["code"] == "device_name_duplicate"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_duplicate_mqtt_device_names_produce_error(tmp_path):
    # Zendure MQTT entries take part in the same name-uniqueness gate as API
    # devices; the check must not skip them.
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["devices"] = [
        _mqtt_device("Zendure MQTT SolarFlow 800 Pro2", "DEV1"),
        _mqtt_device("Zendure MQTT SolarFlow 800 Pro2", "DEV2"),
    ]
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        check["code"] == "device_name_duplicate"
        for check in payload["checks"]
    )


def _diagnose_codes(tmp_path, config):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    result = run_emsctl(tmp_path, "diagnose", "--json")
    payload = json.loads(result.stdout)
    return result, {check["code"] for check in payload["checks"]}


def _base_diagnose_config(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    return json.loads(config_path.read_text())


def _mqtt_device(name, device_id, **extra):
    device = {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": name,
        "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": device_id},
    }
    device.update(extra)
    return device


def test_emsctl_diagnose_duplicate_device_sn_produces_error(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SHARED"},
        {"name": "WR2", "max_power": 800, "sn": "shared"},
    ]
    result, codes = _diagnose_codes(tmp_path, config)
    assert result.returncode == 1, result.stderr
    assert "zendure_device_identity_duplicate" in codes


def test_emsctl_diagnose_local_sn_matches_mqtt_serial_number(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SHARED"},
        _mqtt_device("MQTT1", "DEV1", serial_number="shared"),
    ]
    result, codes = _diagnose_codes(tmp_path, config)
    assert result.returncode == 1, result.stderr
    assert "zendure_device_identity_duplicate" in codes


def test_emsctl_diagnose_duplicate_mqtt_device_id_produces_error(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        _mqtt_device("MQTT1", "DEV1"),
        _mqtt_device("MQTT2", "DEV1"),
    ]
    result, codes = _diagnose_codes(tmp_path, config)
    assert result.returncode == 1, result.stderr
    assert "zendure_device_identity_duplicate" in codes


def test_emsctl_diagnose_case_distinct_mqtt_device_ids_are_not_duplicates(tmp_path):
    # Defect 2: MQTT device ids are case-sensitive route segments, so "DEV1" and
    # "dev1" are two distinct devices, not a duplicate.
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        _mqtt_device("MQTT1", "DEV1"),
        _mqtt_device("MQTT2", "dev1"),
    ]
    _, codes = _diagnose_codes(tmp_path, config)
    assert "zendure_device_identity_duplicate" not in codes


def test_emsctl_diagnose_unique_mqtt_device_ids_pass(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        _mqtt_device("MQTT1", "DEV1"),
        _mqtt_device("MQTT2", "DEV2"),
    ]
    _, codes = _diagnose_codes(tmp_path, config)
    assert "zendure_device_identity_duplicate" not in codes


def test_emsctl_diagnose_reports_telemetry_only_device_root_cause(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [_mqtt_device("MQTT1", "DEV1")]
    result, codes = _diagnose_codes(tmp_path, config)

    assert result.returncode == 1
    assert "no_control_devices" in codes


def test_emsctl_diagnose_disabled_duplicate_does_not_error(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        {"name": "WR2", "max_power": 800, "sn": "SER1", "enabled": False},
    ]
    _, codes = _diagnose_codes(tmp_path, config)
    assert "zendure_device_identity_duplicate" not in codes


def test_emsctl_diagnose_disabled_broker_ref_produces_error(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["zendure_mqtt"] = {
        "enabled": True,
        "brokers": {
            "local_mqtt": {
                "enabled": False,
                "source": "local_mqtt",
                "host": "broker.local",
                "port": 1883,
            }
        },
    }
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        _mqtt_device("MQTT1", "DEV1", mqtt={
            "broker_ref": "local_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "device_id": "DEV1",
        }),
    ]
    result, codes = _diagnose_codes(tmp_path, config)
    assert result.returncode == 1, result.stderr
    assert "zendure_mqtt_broker_ref_disabled" in codes


def test_emsctl_diagnose_broker_issue_leaks_no_identifiers(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["zendure_mqtt"] = {"enabled": True, "brokers": {}}
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        _mqtt_device("MQTT1", "SECRETDEV", mqtt={
            "broker_ref": "ghost",
            "topic_family": "zensdk_ha_scalar",
            "device_id": "SECRETDEV",
        }),
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    result = run_emsctl(tmp_path, "diagnose", "--json")
    payload = json.loads(result.stdout)
    messages = " ".join(
        check.get("message", "")
        for check in payload["checks"]
        if check["code"] == "zendure_mqtt_broker_ref_unknown"
    )
    assert messages
    assert "SECRETDEV" not in messages


def test_emsctl_diagnose_invalid_dashboard_port_produces_error(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["dashboard"] = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 70000,
    }
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        check["code"] == "dashboard_port_invalid"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_invalid_loop_interval_produces_error(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["system"]["loop_interval"] = 0
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        check["code"] == "system_loop_interval_invalid"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_ha_control_without_ha_is_warning(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["ha"]["enabled"] = False
    config["ha"]["control_enabled"] = True
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert any(
        check["code"] == "ha_control_without_ha"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_reports_invalid_config_without_traceback(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{invalid json")

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert any(
        check["code"] == "config_invalid_json"
        for check in payload["checks"]
    )
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_diagnose_invalid_runtime_json_produces_error(tmp_path):
    (tmp_path / "runtime-state.json").write_text("{invalid runtime")

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        check["code"] == "runtime_state_invalid_json"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_runtime_unknown_device_produces_warning(tmp_path):
    (tmp_path / "runtime-state.json").write_text(json.dumps({
        "system": {"enabled": True},
        "devices": {
            "WR1": {"enabled": True, "max_power": 800},
            "WRX": {"enabled": True, "max_power": 100},
        },
    }))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert any(
        check["code"] == "runtime_device_unknown"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_does_not_flag_telemetry_only_device_as_missing(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1", "ip": "10.0.0.1"},
        _mqtt_device("INV_2", "DEV2"),
    ]
    (tmp_path / "runtime-state.json").write_text(json.dumps({
        "system": {"enabled": True},
        "devices": {"WR1": {"enabled": True, "max_power": 800}},
    }))

    _, codes = _diagnose_codes(tmp_path, config)

    assert "runtime_device_missing" not in codes


def test_diagnose_controllable_config_device_names_excludes_telemetry_only():
    config = {
        "devices": [
            {"name": "WR1", "sn": "S1", "ip": "10.0.0.1"},
            {
                "type": "zendure_mqtt",
                "name": "TEL",
                "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "D1"},
            },
            {
                "type": "zendure_mqtt",
                "name": "CTRL",
                "hardware_profile": "solarflow_800_pro_2",
                "mqtt": {"topic_family": "legacy_zendure_json_alt", "device_id": "D2", "product_key": "P2"},
                "capabilities": {"write_output_limit": True},
            },
        ]
    }

    names = set(diagnostics.diagnose_controllable_config_device_names(config))

    assert "WR1" in names
    assert "CTRL" in names
    assert "TEL" not in names


def test_emsctl_diagnose_support_bundle_redacts_secrets(tmp_path):
    secret = "bundle-super-secret-token"
    serial = "SERIAL-SECRET-123456"
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["ha"]["token"] = secret
    config["devices"][0]["sn"] = serial
    config_path.write_text(json.dumps(config))
    output_path = tmp_path / "ems-support.zip"

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--support-bundle",
        "--output",
        str(output_path),
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()
    assert str(output_path) in result.stdout

    combined = ""
    with zipfile.ZipFile(output_path) as bundle:
        names = set(bundle.namelist())
        assert names == {
            "diagnosis.txt",
            "diagnosis.json",
            "control-diagnostics.json",
            "control-diagnostics.txt",
            "control-quality.json",
            "control-quality.txt",
            "redacted-config.json",
            "runtime-state.json",
            "bundle-metadata.json",
        }
        for name in names:
            combined += bundle.read(name).decode("utf-8", errors="replace")

    assert secret not in combined
    assert serial not in combined
    assert "<redacted>" in combined


def test_emsctl_diagnose_control_json_output(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["options"]["control"] is True
    assert payload["control"]["snapshot"]["grid_power_w"] == 142
    assert payload["control"]["snapshot"]["filtered_grid_power_w"] == 131
    assert payload["control"]["snapshot"]["target_output_w"] == 130
    assert payload["control"]["control_path"]
    assert "root_causes" in payload["control"]


def test_emsctl_diagnose_control_text_explains_decision(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control")

    assert result.returncode == 0, result.stderr
    assert "Control Snapshot" in result.stdout
    assert "Grid Power:" in result.stdout
    assert "Decision Explanation" in result.stdout
    assert "Target output calculated" in result.stdout


def test_emsctl_diagnose_control_deadband_detection(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(4,), filtered_load_w=3)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["deadband"]["active"] is True
    assert any(check["code"] == "deadband_active" for check in payload["checks"])


def test_emsctl_diagnose_control_noisy_meter_detection(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(-45, 52, -40, 49, -35, 45, -30, 41))

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["noisy"] is True
    assert payload["control"]["meter_quality"]["sign_changes"] >= 7
    assert any(check["code"] == "meter_signal_noisy" for check in payload["checks"])


def test_emsctl_diagnose_control_repeated_meter_values_are_not_stale(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(18, 18, 18, 18))

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["samples"] == 4
    assert payload["control"]["meter_quality"]["stale"] is False
    assert not any(check["code"] == "meter_signal_stale" for check in payload["checks"])
    assert not any(
        cause["code"] == "grid_meter_values_are_stale"
        for cause in payload["control"]["root_causes"]
    )


def test_emsctl_diagnose_control_repeated_meter_values_with_changing_timestamps_are_not_stale(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(18, 18, 18), measured_at="per_cycle")

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["samples"] == 3
    assert payload["control"]["meter_quality"]["stale"] is False
    assert not any(check["code"] == "meter_signal_stale" for check in payload["checks"])


def test_emsctl_diagnose_control_repeated_meter_values_with_unchanged_timestamp_are_stale(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(18, 18, 18), measured_at="frozen")

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["stale"] is True
    assert payload["control"]["meter_quality"]["stale_reason"] == "unchanged_value_and_timestamp"
    assert any(check["code"] == "meter_signal_stale" for check in payload["checks"])
    assert any(
        cause["code"] == "grid_meter_values_are_stale"
        for cause in payload["control"]["root_causes"]
    )


def test_emsctl_diagnose_control_meter_read_failures_are_stale(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(
        tmp_path, grid=(18, 19, 18), measured_at="per_cycle", read_failures=3
    )

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["stale"] is True
    assert payload["control"]["meter_quality"]["stale_reason"] == "read_failures"
    assert any(check["code"] == "meter_signal_stale" for check in payload["checks"])


def test_emsctl_diagnose_control_without_a_live_snapshot_is_a_warning(tmp_path):
    """No running EMS means nothing to explain, and diagnose says so."""

    write_control_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    assert payload["control"]["runtime_state"]["stale"] is False
    assert payload["control"]["runtime_state"]["checked"] is False
    assert payload["control"]["runtime_state"]["snapshot_status"] == "missing"
    check = next(
        check
        for check in payload["checks"]
        if check["code"] == "control_live_snapshot_missing"
    )
    assert check["level"] == "warning"
    assert not any(
        check["code"] in ("control_staleness_skipped", "control_runtime_state_stale")
        for check in payload["checks"]
    )
    assert payload["control"]["snapshot"]["grid_power_w"] is None


def test_emsctl_diagnose_control_fresh_live_snapshot_is_not_stale(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["runtime_state"]["stale"] is False
    assert payload["control"]["runtime_state"]["checked"] is True
    assert not any(
        check["code"] == "control_runtime_state_stale"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_control_stale_live_control_timestamp_is_warning(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, age_seconds=600)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["runtime_state"]["stale"] is True
    assert payload["control"]["runtime_state"]["stale_source"] == "live_control_timestamp"
    stale_check = next(
        check
        for check in payload["checks"]
        if check["code"] == "control_runtime_state_stale"
    )
    assert stale_check["message"] == "Live control timestamp older than expected"
    assert stale_check["level"] == "warning"
    assert not any(check["level"] == "error" for check in payload["checks"])
    assert payload["control"]["snapshot"]["grid_power_w"] is None
    assert payload["control"]["soc_analysis"]["devices"] == []


def test_emsctl_diagnose_control_soc_imbalance_detection(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["devices"].append({
        "name": "WR2",
        "ip": "192.0.2.21",
        "sn": "SN-TEST-0002",
        "max_power": 800,
        "pv_priority_factor": 1.0,
        "min_soc": 15,
    })
    config_path.write_text(json.dumps(config))
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={
        "WR1": {"soc": 92, "min_soc": 15, "output_w": 130, "allocated_target_w": 130},
        "WR2": {"soc": 55, "min_soc": 15, "output_w": 0, "allocated_target_w": 0},
    })

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["soc_analysis"]["soc_imbalance_percent"] == 37
    assert any(check["code"] == "soc_imbalance_high" for check in payload["checks"])


def test_emsctl_diagnose_control_disabled_and_dry_run_detection(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["system"]["dry_run"] = True
    config_path.write_text(json.dumps(config))
    runtime = write_control_runtime(tmp_path)
    runtime["system"]["enabled"] = False
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "Control disabled" in payload["control"]["write_path"]
    assert "Dry run enabled" in payload["control"]["write_path"]
    assert any(check["code"] == "control_disabled" for check in payload["checks"])
    assert any(check["code"] == "dry_run_enabled" for check in payload["checks"])
    assert any(cause["title"] == "Control disabled" for cause in payload["control"]["root_causes"])
    assert any(cause["title"] == "Dry run enabled" for cause in payload["control"]["root_causes"])


def write_placeholder_config(path):
    write_config(path)
    config = json.loads(path.read_text())
    config["devices"][0].update({"ip": "198.51.100.100", "sn": "YOUR_SN"})
    path.write_text(json.dumps(config))


def test_emsctl_diagnose_names_the_placeholders_that_keep_ems_in_safe_mode(tmp_path):
    """A config EMS refuses to write with must not read as a healthy install.

    A warning rather than an error: safe mode is a state EMS chose, and a
    diagnose that exits non-zero fails the Guided Upgrade health gate of an
    upgrade that worked.
    """

    write_placeholder_config(tmp_path / "config.json")

    result = run_emsctl(tmp_path, "diagnose", "--json")

    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    check = next(
        check for check in payload["checks"]
        if check["code"] == "template_placeholders_safe_mode"
    )
    assert check["level"] == "warning"
    assert check["details"]["paths"] == ["devices[0].ip", "devices[0].sn"]
    assert "WR1: devices[0].ip, WR1: devices[0].sn" in check["message"]
    assert check["message"] in payload["diagnosis"]["warnings"]


def test_emsctl_diagnose_judges_the_config_as_ems_loads_it(tmp_path):
    """A legacy shelly block becomes the grid meter at load, placeholder and all."""

    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    del config["grid_meter"]
    config["shelly"] = {"ip": "198.51.100.50"}
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "diagnose", "--json")

    payload = json.loads(result.stdout)
    check = next(
        check for check in payload["checks"]
        if check["code"] == "template_placeholders_safe_mode"
    )
    assert check["details"]["paths"] == ["grid_meter.ip"]


def test_emsctl_diagnose_control_reports_the_safe_mode_ems_runs_in(tmp_path):
    """The snapshot showed the stored dry_run while EMS ran dry on placeholders."""

    write_placeholder_config(tmp_path / "config.json")
    write_control_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    payload = json.loads(result.stdout)
    assert payload["control"]["snapshot"]["dry_run"] is True
    causes = {cause["code"]: cause for cause in payload["control"]["root_causes"]}
    assert causes["template_placeholders_safe_mode"]["severity"] == "warning"
    assert "WR1: devices[0].sn" in causes["template_placeholders_safe_mode"]["message"]
    assert result.returncode == 0, result.stderr


def test_emsctl_diagnose_control_claims_no_dry_run_for_a_config_it_could_not_read(tmp_path):
    """EMS does not start on such a file; a dry run would be a reassurance."""

    (tmp_path / "config.json").write_text("{not json")
    write_control_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    payload = json.loads(result.stdout)
    assert payload["control"]["snapshot"]["dry_run"] is False
    assert "Dry run enabled" not in payload["control"]["write_path"]


@pytest.mark.parametrize("stored", [None, 0])
def test_emsctl_diagnose_control_reads_dry_run_as_ems_loads_it(tmp_path, stored):
    """A value that is not a real boolean runs dry, so diagnose must say so."""

    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["devices"][0].update({"ip": "192.0.2.20", "sn": "SN-DIAGNOSE-1"})
    config["system"]["dry_run"] = stored
    config_path.write_text(json.dumps(config))
    write_control_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    payload = json.loads(result.stdout)
    assert payload["control"]["snapshot"]["dry_run"] is True


def test_emsctl_diagnose_control_root_cause_min_soc(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": {"soc": 14, "min_soc": 15}})

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        cause["title"] == "Minimum SOC protection active"
        for cause in payload["control"]["root_causes"]
    )
    assert payload["control"]["soc_analysis"]["min_soc_protected_devices"] == ["WR1"]


def test_emsctl_diagnose_control_support_bundle_export(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path)
    output_path = tmp_path / "control-support.zip"

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--control",
        "--support-bundle",
        "--output",
        str(output_path),
    )

    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output_path) as bundle:
        assert "control-diagnostics.txt" in bundle.namelist()
        text = bundle.read("control-diagnostics.txt").decode()
    assert "Control Snapshot" in text
    assert "Decision Explanation" in text


def test_emsctl_diagnose_control_quality_json_structure(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(0, 10, -10, 5))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    quality = payload["control_quality"]
    assert payload["options"]["control_quality"] is True
    assert set(quality) == {
        "status",
        "sample_seconds",
        "export_import",
        "quality_score",
        "pv_diagnostics",
        "soc_balancing",
        "root_causes",
    }


def test_emsctl_diagnose_quality_alias(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(0, 10, -10, 5))

    result = run_emsctl(tmp_path, "diagnose", "--quality", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["options"]["control_quality"] is True


def test_emsctl_diagnose_control_quality_no_export_stable_import(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(10, 20, 25, 15))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["control_quality"]["export_import"]
    assert metrics["status"] == "ok"
    assert metrics["max_export_peak_w"] == 10
    assert metrics["near_zero_duration_percent"] == 100


def test_emsctl_diagnose_control_quality_small_export_peaks_only(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(20, -50, 15, 10))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["control_quality"]["export_import"]
    assert metrics["status"] == "warning"
    assert metrics["max_export_peak_w"] == -50


def test_emsctl_diagnose_control_quality_large_export_peaks(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(10, -300, -260, 20))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 1, result.stderr
    quality = json.loads(result.stdout)["control_quality"]
    assert quality["export_import"]["status"] == "error"
    assert any(cause["code"] == "export_peaks_detected" for cause in quality["root_causes"])


def test_emsctl_diagnose_control_quality_long_export_duration(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(-40, -45, -35, 10))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["control_quality"]["export_import"]
    assert metrics["status"] == "warning"
    assert metrics["export_duration_percent"] == 75


def test_emsctl_diagnose_control_quality_missing_samples(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(None,))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    quality = json.loads(result.stdout)["control_quality"]
    assert quality["export_import"]["samples"] == 0
    assert quality["export_import"]["status"] == "warning"
    assert not any(cause["code"] == "export_peaks_detected" for cause in quality["root_causes"])


def test_emsctl_diagnose_control_quality_score_classes(tmp_path):
    cases = [
        ([0, 10, -10, 5], "excellent"),
        ([25, 25, 25, 25], "good"),
        ([55, 55, 55, 55], "acceptable"),
        ([90, 90, 90, 90], "poor"),
        ([-300, -300, -300, -300], "critical"),
    ]

    for index, (samples, expected) in enumerate(cases):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        write_control_runtime(case_dir)
        write_live_control_status(case_dir, grid=tuple(samples))
        result = run_emsctl(case_dir, "diagnose", "--control-quality", "--json")
        payload = json.loads(result.stdout)
        assert payload["control_quality"]["quality_score"]["classification"] == expected


def test_emsctl_diagnose_control_quality_pv_available_and_used(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": {**DEFAULT_LIVE_DEVICE, "pv_input_w": 920, "output_w": 700, "battery_charge_w": 180}})

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    pv = json.loads(result.stdout)["control_quality"]["pv_diagnostics"]
    assert pv["available"] is True
    assert pv["status"] == "ok"
    assert "PV usage looks plausible." in pv["messages"]


def test_emsctl_diagnose_control_quality_pv_limited_by_system_limit(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": {**DEFAULT_LIVE_DEVICE, "pv_input_w": 1200, "output_w": 880}})

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "pv_limited_by_system_limit" for cause in causes)


def test_emsctl_diagnose_control_quality_pv_limited_by_device_limit(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["system"]["max_total_power"] = 2000
    config["devices"][0]["max_power"] = 500
    config_path.write_text(json.dumps(config))
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": {**DEFAULT_LIVE_DEVICE, "pv_input_w": 900, "output_w": 490}})

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "pv_limited_by_device_limit" for cause in causes)


def test_emsctl_diagnose_control_quality_pv_available_but_unused(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": {**DEFAULT_LIVE_DEVICE, "pv_input_w": 900, "output_w": 50, "battery_charge_w": 20}})

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    pv = json.loads(result.stdout)["control_quality"]["pv_diagnostics"]
    assert pv["status"] == "warning"
    assert any(cause["code"] == "pv_available_but_not_used" for cause in pv["root_causes"])


def test_emsctl_diagnose_control_quality_missing_pv_telemetry(tmp_path):
    """Without a running EMS there is no PV telemetry to judge."""

    write_control_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    pv = json.loads(result.stdout)["control_quality"]["pv_diagnostics"]
    assert pv["available"] is False
    assert pv["status"] == "info"


def test_emsctl_diagnose_control_quality_soc_balanced_devices(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={
        "WR1": dict(DEFAULT_LIVE_DEVICE, soc=62),
        "WR2": dict(DEFAULT_LIVE_DEVICE, soc=64, output_w=120),
    })

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "ok"
    assert soc["soc_spread"] == 2


def test_emsctl_diagnose_control_quality_soc_warning_spread(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={
        "WR1": dict(DEFAULT_LIVE_DEVICE, soc=80),
        "WR2": dict(DEFAULT_LIVE_DEVICE, soc=60, output_w=100),
    })

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "warning"
    assert soc["soc_spread"] == 20


def test_emsctl_diagnose_control_quality_soc_error_spread(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={
        "WR1": dict(DEFAULT_LIVE_DEVICE, soc=90),
        "WR2": dict(DEFAULT_LIVE_DEVICE, soc=55, output_w=100),
    })

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 1, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "error"
    assert soc["soc_spread"] == 35


def test_emsctl_diagnose_control_quality_lower_soc_device_overused(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={
        "WR1": dict(DEFAULT_LIVE_DEVICE, soc=80, output_w=100),
        "WR2": dict(DEFAULT_LIVE_DEVICE, soc=55, output_w=520),
    })

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "lower_soc_device_overused" for cause in causes)


def test_emsctl_diagnose_control_quality_min_soc_protected_device(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": dict(DEFAULT_LIVE_DEVICE, soc=17, min_soc=15)})

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "min_soc_protected_device" for cause in causes)


def test_emsctl_diagnose_control_quality_missing_soc_data(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, devices={"WR1": None})

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "info"
    assert soc["devices"] == []


def test_emsctl_diagnose_control_quality_support_bundle_export(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(tmp_path, grid=(0, 10, -10, 5))
    output_path = tmp_path / "quality-support.zip"

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--control-quality",
        "--support-bundle",
        "--output",
        str(output_path),
    )

    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output_path) as bundle:
        assert "control-quality.txt" in bundle.namelist()
        assert "control-quality.json" in bundle.namelist()
        text = bundle.read("control-quality.txt").decode()
    assert "Export / Import Quality" in text
    assert "Regulation Quality" in text


def _levels_by_code(checks):
    return {check["code"]: check["level"] for check in checks}


def test_diagnose_grid_meter_config_accepts_shelly_3em_gen1_with_ip():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {"grid_meter": {"type": "shelly_3em_gen1", "ip": "192.0.2.50"}},
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_type") == "ok"
    assert levels.get("grid_meter_ip_present") == "ok"
    assert "grid_meter_ip_missing" not in levels


def test_diagnose_grid_meter_config_errors_when_shelly_3em_gen1_ip_missing():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {"grid_meter": {"type": "shelly_3em_gen1"}},
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_ip_missing") == "error"


def test_diagnose_grid_meter_config_accepts_mqtt_number_payload():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {
            "grid_meter": {
                "type": "mqtt",
                "host": "mqtt.local",
                "topic": "Zendure/sensor/SN/totalPower",
                "payload_format": "number",
            }
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_mqtt_host_present") == "ok"
    assert levels.get("grid_meter_mqtt_topic_present") == "ok"
    assert levels.get("grid_meter_mqtt_payload_format") == "ok"


def test_diagnose_grid_meter_config_accepts_zendure_smartmeter_d0():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {
            "grid_meter": {
                "type": "zendure_smartmeter_d0",
                "mqtt": {
                    "host": "mqtt.local",
                    "topic": "Zendure/sensor/SN/totalPower",
                    "payload_format": "number",
                },
            }
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_type") == "ok"
    assert levels.get("grid_meter_mqtt_host_present") == "ok"
    assert levels.get("grid_meter_mqtt_topic_present") == "ok"
    assert levels.get("grid_meter_mqtt_payload_format") == "ok"


def test_diagnose_grid_meter_config_accepts_zendure_grid_meter_http():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {"grid_meter": {"type": "zendure_grid_meter_http", "ip": "192.0.2.80"}},
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_type") == "ok"
    assert levels.get("grid_meter_ip_present") == "ok"


def _grid_meter_type_check(checks):
    return next(check for check in checks if check.get("code") == "grid_meter_type")


def test_diagnose_grid_meter_config_accepts_zendure_smartmeter_d0_http():
    import json

    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {"grid_meter": {"type": "zendure_smartmeter_d0_http", "ip": "192.0.2.84"}},
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_type") == "ok"
    assert levels.get("grid_meter_ip_present") == "ok"

    # Diagnose must name the correct model and transport, and never call a D0 a 3CT.
    details = _grid_meter_type_check(checks)["details"]
    assert details.get("model") == "Zendure Smart Meter D0"
    assert "HTTP" in str(details.get("transport"))
    assert "3CT" not in json.dumps(checks)


def test_diagnose_grid_meter_config_d0_mqtt_names_mqtt_transport():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {
            "grid_meter": {
                "type": "zendure_smartmeter_d0",
                "mqtt": {"host": "10.0.0.5", "topic": "Zendure/sensor/SN/totalPower"},
            }
        },
    )
    details = _grid_meter_type_check(checks)["details"]
    assert details.get("model") == "Zendure Smart Meter D0"
    assert "MQTT" in str(details.get("transport"))


def test_diagnose_grid_meter_config_resolves_broker_ref_and_reports_tls():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {
            "grid_meter": {
                "type": "zendure_smartmeter_d0",
                "mqtt": {
                    "broker_ref": "local_mqtt",
                    "topic": "Zendure/sensor/SN/totalPower",
                    "payload_format": "number",
                },
            },
            "zendure_mqtt": {
                "enabled": True,
                "brokers": {
                    "local_mqtt": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": "10.0.0.9",
                        "port": 8883,
                        "tls": True,
                        "password": "secret",
                    }
                },
            },
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_broker_ref") == "ok"
    assert levels.get("grid_meter_mqtt_host_present") == "ok"
    assert levels.get("grid_meter_mqtt_tls") == "ok"
    # No credential ever appears in a diagnostic message.
    import json

    assert "secret" not in json.dumps(checks)


def test_diagnose_grid_meter_config_flags_unknown_broker_ref():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {
            "grid_meter": {
                "type": "zendure_smartmeter_d0",
                "mqtt": {
                    "broker_ref": "missing",
                    "topic": "Zendure/sensor/SN/totalPower",
                },
            },
            "zendure_mqtt": {"enabled": True, "brokers": {}},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_broker_ref_invalid") == "error"


def test_diagnose_grid_meter_config_requires_mqtt_json_value_path():
    checks = []
    diagnostics.diagnose_grid_meter_config(
        checks,
        {
            "grid_meter": {
                "type": "mqtt",
                "host": "mqtt.local",
                "topic": "meter/grid",
                "payload_format": "json",
            }
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("grid_meter_mqtt_value_path_missing") == "error"
    check = next(item for item in checks if item["code"] == "grid_meter_mqtt_value_path_missing")
    assert check["message"] == "MQTT JSON grid meter requires grid_meter.mqtt.value_path"


def test_diagnose_zendure_mqtt_runtime_silent_when_feature_unused():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(checks, {"devices": []})
    codes = {check["code"] for check in checks}
    assert not any(code.startswith("zendure_mqtt_") for code in codes)


def test_diagnose_zendure_mqtt_runtime_reports_inactive_with_devices():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Zendure Battery",
                    "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV1"},
                }
            ],
            "zendure_mqtt": {"enabled": False},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_telemetry_device_count") == "ok"
    # The feature is always on; without a broker host it is inactive, not
    # disabled, and never a config error.
    assert levels.get("zendure_mqtt_runtime_inactive") == "info"
    assert "zendure_mqtt_runtime_disabled" not in levels


def _check_by_code(checks, code):
    return next(check for check in checks if check["code"] == code)


def _iter_strings(value):
    """Yield every string reachable in a nested check structure (keys included)."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _assert_no_secret(checks, *secrets):
    # Recursively inspect structured keys and values rather than a serialized
    # blob, so a secret nested anywhere in a check fails the assertion.
    strings = list(_iter_strings(checks))
    for secret in secrets:
        offenders = [text for text in strings if secret in text]
        assert offenders == [], f"secret {secret!r} leaked into diagnostics: {offenders}"


def test_diagnose_zendure_mqtt_runtime_reports_configured_endpoint_without_secrets():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "devices": [],
            "zendure_mqtt": {
                "enabled": True,
                "host": "broker.local",
                "port": 8883,
                "username": "secretuser",
                "password": "sup3r-secret-pw",
                "app_key": "secretAppKey",
            },
        },
    )
    configured = _check_by_code(checks, "zendure_mqtt_runtime_configured")
    assert configured["level"] == "ok"
    # Assert the actual structured endpoint field, not a substring of a blob.
    assert configured["details"]["endpoint"] == "broker.local:8883"
    _assert_no_secret(checks, "sup3r-secret-pw", "secretuser", "secretAppKey")


def test_diagnose_zendure_mqtt_runtime_hostless_config_is_inactive_without_secrets():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "devices": [],
            "zendure_mqtt": {"enabled": True, "password": "sup3r-secret-pw"},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_runtime_inactive") == "info"
    assert "zendure_mqtt_runtime_config_invalid" not in levels
    assert "sup3r-secret-pw" not in json.dumps(checks)


def test_diagnose_zendure_mqtt_runtime_reports_invalid_config_without_secrets():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "devices": [],
            "zendure_mqtt": {
                "host": "broker.local",
                "port": "not-a-port",
                "password": "sup3r-secret-pw",
            },
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_runtime_config_invalid") == "error"
    assert "sup3r-secret-pw" not in json.dumps(checks)


def test_diagnose_zendure_mqtt_broker_profiles_report_missing_host_before_disabled():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "devices": [],
            "zendure_mqtt": {
                "brokers": {
                    "hostless": {"enabled": True, "source": "local_mqtt"},
                    "switched_off": {
                        "enabled": False,
                        "source": "local_mqtt",
                        "host": "broker.local",
                    },
                }
            },
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_broker_endpoint_missing") == "warning"
    assert levels.get("zendure_mqtt_broker_disabled") == "info"


def test_diagnose_zendure_mqtt_control_device_without_write_target_is_flagged():
    checks = []
    diagnostics.diagnose_zendure_mqtt_device_config(
        checks,
        0,
        {
            "type": "zendure_mqtt",
            "name": "Zendure Battery",
            "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV1"},
            "capabilities": {"write_output_limit": True},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_write_target_missing") == "error"


def test_diagnose_zendure_mqtt_control_device_is_control_capable():
    checks = []
    diagnostics.diagnose_zendure_mqtt_device_config(
        checks,
        0,
        {
            "type": "zendure_mqtt",
            "name": "Zendure Battery",
            "hardware_profile": "solarflow_800_pro_2",
            "mqtt": {
                "source": "local_mqtt",
                "topic_family": "legacy_zendure_json",
                "device_id": "DEV1",
                "product_key": "PK1",
            },
            "capabilities": {"write_output_limit": True},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_control_capable") == "ok"


def test_diagnose_zendure_mqtt_scalar_control_reports_protocol_unsupported():
    checks = []
    diagnostics.diagnose_zendure_mqtt_device_config(
        checks,
        0,
        {
            "type": "zendure_mqtt",
            "name": "Zendure Battery",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "device_id": "DEV1",
                "product_key": "PK1",
            },
            "capabilities": {"write_output_limit": True},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_write_protocol_unsupported") == "error"


def test_diagnose_hardware_checks_mqtt_broker_tcp(monkeypatch):
    captured = {}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_create_connection(address, timeout=0):
        captured["address"] = address
        captured["timeout"] = timeout
        return FakeSocket()

    monkeypatch.setattr(diagnostics.socket, "create_connection", fake_create_connection)

    checks = []
    health = diagnostics.diagnose_hardware(
        checks,
        {
            "grid_meter": {
                "type": "mqtt",
                "host": "mqtt.local",
                "port": 1883,
            },
            "devices": [],
        },
    )

    assert captured == {"address": ("mqtt.local", 1883), "timeout": 2}
    levels = _levels_by_code(checks)
    assert levels.get("mqtt_broker_connect_ok") == "ok"
    assert health["grid_meter"]["provider"] == "MQTT"


def test_diagnose_hardware_checks_zendure_smartmeter_d0_broker_tcp(monkeypatch):
    captured = {}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_create_connection(address, timeout=0):
        captured["address"] = address
        captured["timeout"] = timeout
        return FakeSocket()

    monkeypatch.setattr(diagnostics.socket, "create_connection", fake_create_connection)

    checks = []
    health = diagnostics.diagnose_hardware(
        checks,
        {
            "grid_meter": {
                "type": "zendure_smartmeter_d0",
                "mqtt": {
                    "host": "mqtt.local",
                    "port": 1883,
                    "topic": "Zendure/sensor/SN/totalPower",
                },
            },
            "devices": [],
        },
    )

    assert captured == {"address": ("mqtt.local", 1883), "timeout": 2}
    levels = _levels_by_code(checks)
    assert levels.get("mqtt_broker_connect_ok") == "ok"
    assert health["grid_meter"]["provider"] == "Zendure SmartMeter D0"


def test_diagnose_hardware_probes_shelly_3em_gen1_status_endpoint(monkeypatch):
    captured = {}

    def fake_http_json(url, headers=None, timeout=2):
        captured["url"] = url
        return 200, {"total_power": 123.4}

    monkeypatch.setattr(diagnostics, "diagnose_http_json", fake_http_json)

    checks = []
    diagnostics.diagnose_hardware(
        checks,
        {"grid_meter": {"type": "shelly_3em_gen1", "ip": "192.0.2.50"}, "devices": []},
    )

    assert captured["url"] == "http://192.0.2.50/status"
    levels = _levels_by_code(checks)
    assert levels.get("shelly_3em_gen1_read_ok") == "ok"


def test_diagnose_hardware_passes_channels_to_shelly_3em_gen1_parser(monkeypatch):
    def fake_http_json(url, headers=None, timeout=2):
        return 200, {
            "total_power": 999.0,
            "emeters": [
                {"power": 100.0},
                {"power": 20.0},
                {"power": -5.0},
            ],
        }

    monkeypatch.setattr(diagnostics, "diagnose_http_json", fake_http_json)

    checks = []
    diagnostics.diagnose_hardware(
        checks,
        {
            "grid_meter": {
                "type": "shelly_3em_gen1",
                "ip": "192.0.2.50",
                "channels": ["a", "c"],
            },
            "devices": [],
        },
    )

    ok = next(c for c in checks if c["code"] == "shelly_3em_gen1_read_ok")
    # total_power (999) is ignored; only phases a + c are summed.
    assert ok["details"]["power_w"] == 95.0


def test_diagnose_hardware_probes_shelly_pro_rpc_endpoint(monkeypatch):
    captured = {}

    def fake_http_json(url, headers=None, timeout=2):
        captured["url"] = url
        return 200, {"em:0": {"total_act_power": 50.0}}

    monkeypatch.setattr(diagnostics, "diagnose_http_json", fake_http_json)

    checks = []
    diagnostics.diagnose_hardware(
        checks,
        {"grid_meter": {"type": "shelly", "ip": "192.0.2.50"}, "devices": []},
    )

    assert captured["url"] == "http://192.0.2.50/rpc/Shelly.GetStatus"
    levels = _levels_by_code(checks)
    assert levels.get("shelly_read_ok") == "ok"


def test_diagnose_hardware_returns_health_and_renders_sections(monkeypatch):
    def fake_http_json(url, headers=None, timeout=2):
        if "Shelly.GetStatus" in url:
            raise TimeoutError("Read timed out")
        return 200, {"properties": {"electricLevel": 50}}

    monkeypatch.setattr(diagnostics, "diagnose_http_json", fake_http_json)

    checks = []
    health = diagnostics.diagnose_hardware(
        checks,
        {
            "grid_meter": {"type": "shelly", "ip": "192.0.2.50"},
            "devices": [{"name": "WR1", "ip": "192.0.2.60", "sn": "SN1"}],
        },
    )

    assert health["grid_meter"]["provider"] == "Shelly"
    assert health["grid_meter"]["status"] == "failed"
    assert health["devices"][0]["name"] == "WR1"
    assert health["devices"][0]["read"]["status"] == "ok"
    assert health["devices"][0]["write"] is None

    text = diagnostics.diagnose_hardware_health_text(health)
    assert "Grid meter health:" in text
    assert "provider: Shelly" in text
    assert "Device health:" in text
    assert "WR1:" in text
    assert "write: not attempted" in text


def test_diagnose_report_includes_hardware_health_section(monkeypatch, tmp_path):
    monkeypatch.setattr(
        diagnostics,
        "diagnose_http_json",
        lambda url, headers=None, timeout=2: (200, {"em:0": {"total_act_power": 12.0}}),
    )
    write_config(tmp_path / "config.json")

    report = emsctl.run_hardware_diagnosis(diagnose_args(tmp_path))

    assert report["hardware_health"] is not None
    text = diagnostics.diagnose_text(report)
    assert "Grid meter health:" in text


def test_diagnose_install_includes_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("EMS_IN_CONTAINER", raising=False)
    report = diagnostics.run_install_diagnosis(diagnose_args(tmp_path))

    codes = {
        check["code"]
        for check in report["checks"]
        if check["section"] == "runtime_paths"
    }
    assert {"container_mode", "config_path", "data_path", "backup_default"} <= codes

    text = diagnostics.diagnose_text(report)
    assert "Runtime paths" in text
    assert "backup default:" in text


def test_diagnose_runtime_paths_native_even_when_runner_in_container(
    tmp_path, monkeypatch
):
    # A containerized test runner has /.dockerenv but is not the official /app
    # layout: diagnostics must still report native mode and the data backup dir.
    from ems import backup as backup_mod

    monkeypatch.delenv("EMS_IN_CONTAINER", raising=False)
    real_exists = backup_mod.os.path.exists
    monkeypatch.setattr(
        backup_mod.os.path,
        "exists",
        lambda path: True if path == "/.dockerenv" else real_exists(path),
    )
    report = diagnostics.run_install_diagnosis(diagnose_args(tmp_path))

    container_check = next(
        check
        for check in report["checks"]
        if check["section"] == "runtime_paths" and check["code"] == "container_mode"
    )
    assert container_check["details"]["container_mode"] is False
    backup_check = next(
        check
        for check in report["checks"]
        if check["section"] == "runtime_paths" and check["code"] == "backup_default"
    )
    assert backup_check["details"]["path"].endswith("data/backups")


def test_diagnose_runtime_paths_warns_when_container_backup_not_persistent(
    tmp_path, monkeypatch
):
    from ems import backup as backup_mod

    monkeypatch.setenv("EMS_IN_CONTAINER", "1")
    monkeypatch.setattr(
        backup_mod, "CONTAINER_BACKUP_DIR", str(tmp_path / "loose-backups")
    )
    report = diagnostics.run_install_diagnosis(diagnose_args(tmp_path))

    codes = {
        check["code"]
        for check in report["checks"]
        if check["section"] == "runtime_paths"
    }
    assert "container_backup_not_persistent" in codes
    assert any("not under /app/data" in message for message in report["warnings"])


def test_diagnose_runtime_paths_no_warning_for_persistent_container_backup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EMS_IN_CONTAINER", "1")
    report = diagnostics.run_install_diagnosis(diagnose_args(tmp_path))

    codes = {
        check["code"]
        for check in report["checks"]
        if check["section"] == "runtime_paths"
    }
    host_path = next(
        check
        for check in report["checks"]
        if check["section"] == "runtime_paths" and check["code"] == "backup_host_path"
    )
    assert "backup_persistent" in codes
    assert "container_backup_not_persistent" not in codes
    assert host_path["details"]["path"] == "data/backups"


def test_diagnose_zendure_mqtt_runtime_reports_named_brokers_without_secrets():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "zendure_mqtt": {
                "enabled": True,
                "brokers": {
                    "zendure_cloud": {
                        "enabled": True,
                        "source": "zendure_cloud_mqtt",
                        "host": "mqtteu.zen-iot.com",
                        "port": 8883,
                        "username": "secretuser",
                        "password": "sup3r-secret-pw",
                    },
                    "local_mqtt": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": "192.168.20.10",
                        "port": 1883,
                    },
                },
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Cloud",
                    "mqtt": {
                        "broker_ref": "zendure_cloud",
                        "topic_family": "zensdk_ha_scalar",
                        "device_id": "CLOUDDEV",
                    },
                }
            ],
        },
    )
    endpoints = {
        check["details"].get("broker_ref"): check["details"].get("endpoint")
        for check in checks
        if check["code"] == "zendure_mqtt_broker_configured"
    }
    assert endpoints["zendure_cloud"] == "mqtteu.zen-iot.com:8883"
    assert endpoints["local_mqtt"] == "192.168.20.10:1883"
    _assert_no_secret(checks, "secretuser", "sup3r-secret-pw")


@pytest.mark.parametrize(
    "host",
    [
        "attacker:sup3r-secret@broker.local",  # userinfo
        "mqtt://broker.local",  # scheme
        "broker.local/path",  # path
        "broker.local?token=sup3r-secret",  # query
        "broker.local#sup3r-secret",  # fragment
        "broker .local",  # embedded whitespace
        "broker.local\x01",  # control character
    ],
)
def test_diagnose_zendure_mqtt_runtime_rejects_credential_bearing_host(host):
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {"devices": [], "zendure_mqtt": {"enabled": True, "host": host, "port": 8883}},
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_runtime_host_invalid") == "error"
    assert "zendure_mqtt_runtime_configured" not in levels
    # The raw host — which may embed a credential — is never echoed anywhere.
    _assert_no_secret(checks, host, "sup3r-secret")


def test_diagnose_zendure_mqtt_named_broker_rejects_credential_bearing_host():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "X",
                    "mqtt": {
                        "broker_ref": "evil",
                        "topic_family": "zensdk_ha_scalar",
                        "device_id": "D",
                    },
                }
            ],
            "zendure_mqtt": {
                "enabled": True,
                "brokers": {
                    "evil": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": "user:sup3r-secret@broker.local",
                        "port": 1883,
                    }
                },
            },
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_broker_host_invalid") == "error"
    assert "zendure_mqtt_broker_configured" not in levels
    _assert_no_secret(checks, "sup3r-secret")


def test_diagnose_zendure_mqtt_runtime_accepts_hostname_ipv4_and_ipv6():
    # Valid bare hosts (hostname, IPv4, bracketed IPv6) stay fully supported.
    for host, port, expected in (
        ("mqtteu.zen-iot.com", 8883, "mqtteu.zen-iot.com:8883"),
        ("192.168.20.10", 1883, "192.168.20.10:1883"),
        ("::1", 1883, "[::1]:1883"),
    ):
        checks = []
        diagnostics.diagnose_zendure_mqtt_runtime(
            checks,
            {"devices": [], "zendure_mqtt": {"enabled": True, "host": host, "port": port}},
        )
        configured = _check_by_code(checks, "zendure_mqtt_runtime_configured")
        assert configured["details"]["endpoint"] == expected


def test_diagnose_zendure_mqtt_runtime_flags_unknown_broker_ref():
    checks = []
    diagnostics.diagnose_zendure_mqtt_runtime(
        checks,
        {
            "zendure_mqtt": {
                "enabled": True,
                "brokers": {"local_mqtt": {"source": "local_mqtt", "host": "10.0.0.2"}},
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Ghost",
                    "mqtt": {
                        "broker_ref": "nope",
                        "topic_family": "zensdk_ha_scalar",
                        "device_id": "DEVX",
                    },
                }
            ],
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_broker_ref_unknown") == "error"


def _control_ready_telemetry_only_device(**overrides):
    device = {
        "type": "zendure_mqtt",
        "name": "INV_2",
        "hardware_profile": "solarflow_800_pro_2",
        "mqtt": {
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json_alt",
            "device_id": "DEV1",
            "product_key": "PK1",
        },
        "capabilities": {"read_power": True, "write_output_limit": False},
    }
    device.update(overrides)
    return device


def test_diagnose_flags_a_control_ready_device_saved_telemetry_only():
    checks = []
    diagnostics.diagnose_zendure_mqtt_device_config(
        checks, 0, _control_ready_telemetry_only_device()
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_control_ready_but_telemetry_only") == "warning"
    assert "zendure_mqtt_telemetry_only" not in levels


def test_diagnose_keeps_an_unwritable_telemetry_only_device_ok():
    checks = []
    diagnostics.diagnose_zendure_mqtt_device_config(
        checks,
        0,
        {
            "type": "zendure_mqtt",
            "name": "Zendure Battery",
            "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV1"},
        },
    )
    levels = _levels_by_code(checks)
    assert levels.get("zendure_mqtt_telemetry_only") == "ok"
    assert "zendure_mqtt_control_ready_but_telemetry_only" not in levels


def test_diagnose_does_not_flag_a_disabled_control_ready_device():
    checks = []
    diagnostics.diagnose_zendure_mqtt_device_config(
        checks, 0, _control_ready_telemetry_only_device(enabled=False)
    )
    levels = _levels_by_code(checks)
    assert "zendure_mqtt_control_ready_but_telemetry_only" not in levels


def test_emsctl_diagnose_reports_a_disabled_api_device(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        {"name": "WR2", "max_power": 800, "sn": "SER2", "enabled": False},
    ]
    _, codes = _diagnose_codes(tmp_path, config)
    assert "device_disabled" in codes


def test_emsctl_diagnose_reports_a_disabled_mqtt_device(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "sn": "SER1"},
        _mqtt_device("MQTT1", "DEV1", enabled=False),
    ]
    _, codes = _diagnose_codes(tmp_path, config)
    assert "device_disabled" in codes


def test_emsctl_diagnose_stays_silent_for_enabled_devices(tmp_path):
    config = _base_diagnose_config(tmp_path)
    config["devices"] = [{"name": "WR1", "max_power": 800, "sn": "SER1"}]
    _, codes = _diagnose_codes(tmp_path, config)
    assert "device_disabled" not in codes


def test_a_sample_window_below_the_write_interval_is_reported():
    """That pair silently voids every energy figure.

    Each interval between two stored samples is then longer than the window the
    statistics accept, so nothing is integrated and the board shows zeros with
    no reason given.
    """

    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {},
            "dashboard": {"write_interval_seconds": 60},
            "energy_savings": {"enabled": True, "max_sample_delta_seconds": 20},
        },
    )

    codes = {check["code"]: check for check in checks}
    assert codes["energy_sample_window_below_write_interval"]["level"] == "warning"


def test_a_sample_window_equal_to_the_write_interval_is_reported():
    """At equality, loop jitter decides every single sample."""

    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {},
            "dashboard": {"write_interval_seconds": 20},
            "energy_savings": {"enabled": True, "max_sample_delta_seconds": 20},
        },
    )

    codes = {check["code"] for check in checks}
    assert "energy_sample_window_below_write_interval" in codes


def test_the_sample_window_check_reads_enabled_the_way_the_store_does():
    """``1`` enables the statistics, and ``false`` disables them."""

    def codes_for(enabled):
        checks = []
        diagnose_config_plausibility(
            checks,
            None,
            {
                "system": {},
                "dashboard": {"write_interval_seconds": 60},
                "energy_savings": {
                    "enabled": enabled,
                    "max_sample_delta_seconds": 20,
                },
            },
        )
        return {check["code"] for check in checks}

    assert "energy_sample_window_below_write_interval" in codes_for(1)
    assert "energy_sample_window_below_write_interval" in codes_for("true")
    # Disabled statistics cannot be misconfigured into silence.
    assert "energy_sample_window_below_write_interval" not in codes_for(False)
    assert "energy_sample_window_valid" not in codes_for(False)


def test_an_unreadable_sample_window_is_an_error_not_an_ok():
    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {},
            "dashboard": {"write_interval_seconds": "xyz"},
            "energy_savings": {"enabled": True, "max_sample_delta_seconds": 20},
        },
    )

    codes = {check["code"]: check for check in checks}
    assert codes["energy_sample_window_invalid"]["level"] == "error"
    assert "energy_sample_window_valid" not in codes


def test_a_sample_window_above_the_write_interval_is_fine():
    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {},
            "dashboard": {"write_interval_seconds": 5},
            "energy_savings": {"enabled": True, "max_sample_delta_seconds": 20},
        },
    )

    codes = {check["code"] for check in checks}
    assert "energy_sample_window_valid" in codes
    assert "energy_sample_window_below_write_interval" not in codes


def test_the_sample_window_check_counts_the_loop_quantisation():
    """The controller can only write on a loop tick.

    A 16-second write interval on a 5-second loop stores a sample every 20
    seconds, which a bare comparison against the 20-second window calls fine.
    """

    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {"loop_interval": 5},
            "dashboard": {"write_interval_seconds": 16},
            "energy_savings": {"enabled": True, "max_sample_delta_seconds": 20},
        },
    )

    codes = {check["code"] for check in checks}
    assert "energy_sample_window_below_write_interval" in codes


def test_a_write_interval_of_zero_is_read_as_one_loop_tick():
    """Zero means "write every loop", which is a tick apart, not nothing."""

    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {"loop_interval": 30},
            "dashboard": {"write_interval_seconds": 0},
            "energy_savings": {"enabled": True, "max_sample_delta_seconds": 20},
        },
    )

    codes = {check["code"] for check in checks}
    assert "energy_sample_window_below_write_interval" in codes


def _telemetry_window_codes(output_control, *, loop_interval=5, enabled=True):
    checks = []
    diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {
                "loop_interval": loop_interval,
                "output_control": output_control,
            },
            "dashboard": {"write_interval_seconds": 5},
            "energy_savings": {"enabled": enabled, "max_sample_delta_seconds": 20},
        },
    )
    return {check["code"]: check for check in checks}


def test_a_telemetry_window_of_zero_is_reported():
    """The control loop reads zero as 'always stale', so nothing is measured.

    Every energy figure then stays at zero without saying why -- the same class
    of silent misconfiguration as a sample window below the write interval.
    """

    codes = _telemetry_window_codes({"telemetry_max_age_seconds": 0})

    assert codes["energy_telemetry_window_disabled"]["level"] == "warning"


def test_a_negative_telemetry_window_is_reported_too():
    """``safe_float`` clamps it to zero, which is the disabling value."""

    codes = _telemetry_window_codes({"telemetry_max_age_seconds": -5})

    assert "energy_telemetry_window_disabled" in codes


def test_a_telemetry_window_below_the_loop_interval_is_reported():
    """A single missed read then ages out before the next one arrives."""

    codes = _telemetry_window_codes({"telemetry_max_age_seconds": 3})

    assert codes["energy_telemetry_window_below_loop_interval"]["level"] == "warning"
    details = codes["energy_telemetry_window_below_loop_interval"]["details"]
    assert details["loop_interval"] == 5
    assert details["telemetry_max_age_seconds"] == 3


def test_the_default_telemetry_window_covers_the_loop_interval():
    codes = _telemetry_window_codes({})

    assert "energy_telemetry_window_valid" in codes
    assert "energy_telemetry_window_disabled" not in codes


def test_an_unreadable_telemetry_window_falls_back_like_the_controller_does():
    """``safe_float`` returns the default for text, so diagnose must not warn."""

    codes = _telemetry_window_codes({"telemetry_max_age_seconds": "ten"})

    assert "energy_telemetry_window_valid" in codes


def test_disabled_statistics_cannot_be_misconfigured_into_silence():
    codes = _telemetry_window_codes(
        {"telemetry_max_age_seconds": 0}, enabled=False
    )

    assert "energy_telemetry_window_disabled" not in codes
    assert "energy_telemetry_window_valid" not in codes


def test_a_telemetry_window_far_above_the_loop_interval_is_reported():
    """Too large invents energy, the way too small interrupts it.

    Inside the window a silent device keeps contributing its last reading. At
    ten seconds that is noise; at an hour it is a device that has been gone for
    an hour and still counted.
    """

    codes = _telemetry_window_codes({"telemetry_max_age_seconds": 3600})

    check = codes["energy_telemetry_window_far_above_loop_interval"]
    assert check["level"] == "warning"
    assert check["details"]["telemetry_max_age_seconds"] == 3600


def test_a_generous_but_plausible_telemetry_window_is_not_reported():
    codes = _telemetry_window_codes({"telemetry_max_age_seconds": 60})

    assert "energy_telemetry_window_valid" in codes
    assert "energy_telemetry_window_far_above_loop_interval" not in codes


def test_a_long_loop_interval_earns_a_wider_telemetry_window():
    """The plausible window is a number of loop intervals, not a fixed second.

    On a two-minute loop a 200 s window is under two intervals -- ordinary. A
    fixed 60 s ceiling called that implausible and warned about a healthy setup.
    """

    codes = _telemetry_window_codes(
        {"telemetry_max_age_seconds": 200}, loop_interval=120
    )

    assert "energy_telemetry_window_far_above_loop_interval" not in codes
    assert "energy_telemetry_window_valid" in codes


def test_a_long_loop_interval_does_not_excuse_any_window():
    codes = _telemetry_window_codes(
        {"telemetry_max_age_seconds": 7200}, loop_interval=120
    )

    assert "energy_telemetry_window_far_above_loop_interval" in codes


def test_a_short_loop_interval_keeps_the_absolute_floor():
    """A one-second loop must not turn a 30 s window into a warning."""

    codes = _telemetry_window_codes(
        {"telemetry_max_age_seconds": 30}, loop_interval=1
    )

    assert "energy_telemetry_window_far_above_loop_interval" not in codes


def test_an_invalid_loop_interval_does_not_silence_the_telemetry_check():
    """One unreadable field may not suppress an unrelated warning."""

    codes = _telemetry_window_codes(
        {"telemetry_max_age_seconds": 3600}, loop_interval="soon"
    )

    assert "energy_telemetry_window_far_above_loop_interval" in codes


def test_diagnose_hardware_resolves_a_grid_meter_broker_ref(monkeypatch):
    """The Admin layout names the broker by reference; the probe must follow it."""

    captured = {}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_create_connection(address, timeout=0):
        captured["address"] = address
        return FakeSocket()

    monkeypatch.setattr(diagnostics.socket, "create_connection", fake_create_connection)

    checks = []
    diagnostics.diagnose_hardware(
        checks,
        {
            "grid_meter": {
                "type": "mqtt",
                "mqtt": {"broker_ref": "home", "topic": "meter/power"},
            },
            "zendure_mqtt": {
                "brokers": {
                    "home": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": "broker.local",
                        "port": 1883,
                    }
                }
            },
            "devices": [],
        },
    )

    assert captured["address"] == ("broker.local", 1883)
    assert _levels_by_code(checks).get("mqtt_broker_connect_ok") == "ok"


def test_diagnose_hardware_probes_a_tasmota_meter(monkeypatch):
    captured = {}

    def fake_http_json(url, headers=None, timeout=2):
        captured["url"] = url
        return 200, {"StatusSNS": {"SML": {"Power": 321}}}

    monkeypatch.setattr(diagnostics, "diagnose_http_json", fake_http_json)

    checks = []
    diagnostics.diagnose_hardware(
        checks,
        {
            "grid_meter": {
                "type": "tasmota_http",
                "ip": "192.0.2.60",
                "power_path": "StatusSNS.SML.Power",
            },
            "devices": [],
        },
    )

    assert captured["url"] == "http://192.0.2.60/cm?cmnd=Status%2010"
    assert _levels_by_code(checks).get("tasmota_read_ok") == "ok"


def test_support_bundle_json_stays_parseable_and_redacted(tmp_path, monkeypatch):
    for renderer in (
        "diagnose_text",
        "diagnose_control_text",
        "diagnose_control_quality_text",
    ):
        monkeypatch.setattr(diagnostics, renderer, lambda report: "")
    report = {
        "schema_version": 1,
        "diagnosis": {
            "warnings": ["Dashboard binds without configured auth"],
            "session": {"id": "abc"},
            "auth": ["x"],
            "note": "token=SECRET123 and http://u:pw@host/x",
        },
        "control": {"device_id": "DEV-SECRET", "warnings": ["no auth"]},
        "control_quality": {"api_key": "KEY-SECRET"},
    }
    output = tmp_path / "bundle.zip"
    diagnostics.diagnose_write_support_bundle(
        report, SimpleNamespace(output=str(output)), {}, None
    )
    with zipfile.ZipFile(output) as bundle:
        payloads = {
            name: bundle.read(name).decode()
            for name in (
                "diagnosis.json",
                "control-diagnostics.json",
                "control-quality.json",
            )
        }
    for raw in payloads.values():
        json.loads(raw)
        for secret in ("SECRET123", "DEV-SECRET", "KEY-SECRET", "pw@"):
            assert secret not in raw
    diagnosis = json.loads(payloads["diagnosis.json"])["diagnosis"]
    assert diagnosis["warnings"] == ["Dashboard binds without configured auth"]


def _dashboard_exposure_codes(tmp_path, dashboard):
    checks = []
    diagnose_config_plausibility(
        checks,
        SimpleNamespace(dashboard_auth=str(tmp_path / "no-auth.json")),
        {"system": {}, "dashboard": dashboard},
    )
    return {check["code"] for check in checks}


@pytest.mark.parametrize(
    "dashboard",
    [
        {},
        {"enabled": "true", "ssl_enabled": "false"},
        {"enabled": True, "host": "0.0.0.0", "ssl_enabled": "off"},
    ],
)
def test_open_dashboard_is_reported_as_the_runtime_would_start_it(tmp_path, dashboard):
    codes = _dashboard_exposure_codes(tmp_path, dashboard)
    assert "dashboard_open_without_https_auth" in codes


@pytest.mark.parametrize(
    "dashboard",
    [
        {"enabled": "false"},
        {"enabled": True, "ssl_enabled": "true"},
        {"enabled": True, "host": "127.0.0.1"},
    ],
)
def test_closed_dashboard_is_not_reported_as_open(tmp_path, dashboard):
    codes = _dashboard_exposure_codes(tmp_path, dashboard)
    assert "dashboard_open_without_https_auth" not in codes


def _tzif(offset):
    """A minimal zone file with one fixed UTC offset, as glibc and zoneinfo read it."""

    chars = b"ZZZ\0"
    block = b"TZif2" + b"\0" * 15 + struct.pack(">6l", 0, 0, 0, 0, 1, len(chars))
    block += struct.pack(">lBB", offset, 0, 0) + chars
    return block + block + b"\n<ZZZ>%d\n" % (-offset // 3600)


BERLIN = 3600


def _timezone_checks(mode, environ, *, zoneinfo_dir="/none", localtime="/none"):
    checks = []
    diagnostics.diagnose_timezone(
        checks, mode, environ=environ, zoneinfo_dir=str(zoneinfo_dir), localtime=str(localtime)
    )
    assert [check["code"] for check in checks] == ["timezone"]
    return checks[0]


@pytest.mark.parametrize(
    ("mode", "environ", "known", "level"),
    [
        ("container", {}, (), "warning"),
        ("native", {}, (), "ok"),
        ("container", {"TZ": "Europe/Berlin"}, ("Europe/Berlin",), "ok"),
        ("container", {"TZ": "Europe/Berln"}, ("Europe/Berlin",), "warning"),
        ("container", {"TZ": "US/Eastern"}, ("America/New_York",), "warning"),
        ("container", {"TZ": "UTC"}, ("UTC",), "ok"),
        ("container", {"TZ": ":Europe/Berlin"}, ("Europe/Berlin",), "ok"),
        ("container", {"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}, (), "ok"),
        ("container", {"TZ": "UTC0"}, (), "ok"),
        ("container", {"TZ": "CET"}, (), "warning"),
        ("container", {"TZ": "Japan"}, (), "warning"),
    ],
)
def test_diagnose_says_which_zone_the_local_hour_windows_open_in(
    tmp_path, mode, environ, known, level
):
    """A zone the image has no file for runs on UTC without a word from glibc."""

    for name in known:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(_tzif(0 if name == "UTC" else BERLIN))

    assert _timezone_checks(mode, environ, zoneinfo_dir=tmp_path)["level"] == level


@pytest.mark.parametrize(
    ("mode", "content", "level"),
    [
        ("native", _tzif(BERLIN), "ok"),
        ("native", None, "warning"),
        ("container", _tzif(0), "warning"),
        ("container", _tzif(BERLIN), "ok"),
    ],
)
def test_diagnose_follows_a_tz_that_names_a_file(tmp_path, mode, content, level):
    """A file on UTC hours is, in a container, the image's own zone rather than the host's.

    A real zone named by its path, or the host's `/etc/localtime` mounted into the
    container, is judged by what the file holds and not by the name.
    """

    zone_file = tmp_path / "localtime"
    if content is not None:
        zone_file.write_bytes(content)

    assert _timezone_checks(mode, {"TZ": f":{zone_file}"})["level"] == level


@pytest.mark.parametrize("zone", ["GMT+1", "UTC+2", "UTC-3", "gmt+1", "utc-01"])
def test_diagnose_warns_that_posix_offsets_count_the_other_way(zone):
    check = _timezone_checks("container", {"TZ": zone})

    assert check["level"] == "warning"
    assert "Etc/GMT" in check["hint"]


@pytest.mark.parametrize("zone", ["GMT+0", "GMT-0", "UTC+0", "UTC-00", "EST+5EDT,M3.2.0/2,M11.1.0/2", "PST+8PDT"])
def test_a_zero_offset_has_no_sign_to_get_wrong(zone):
    """Nor does a western POSIX string that writes its offset with a plus, as glibc's manual does."""

    assert _timezone_checks("container", {"TZ": zone})["level"] == "ok"


@pytest.mark.parametrize("content", [b"TZif", b"# not a zone\n"])
def test_diagnose_does_not_take_any_file_for_a_zone(tmp_path, content):
    (tmp_path / "zone.tab").write_bytes(content)

    check = _timezone_checks("container", {"TZ": "zone.tab"}, zoneinfo_dir=tmp_path)

    assert check["level"] == "warning"


@pytest.mark.parametrize(("content", "level"), [(_tzif(BERLIN), "ok"), (_tzif(0), "warning"), (None, "warning")])
def test_a_container_without_tz_is_judged_by_its_localtime(tmp_path, content, level):
    """An older Compose file passes no TZ, but the host's /etc/localtime may be mounted."""

    localtime = tmp_path / "localtime"
    if content is not None:
        localtime.write_bytes(content)

    assert _timezone_checks("container", {}, localtime=localtime)["level"] == level


def test_tz_on_utc_hours_over_a_mounted_host_zone_says_which_wins(tmp_path):
    """A UTC zone file of its own, such as Etc/GMT, wins over a mounted /etc/localtime."""

    (tmp_path / "UTC").write_bytes(_tzif(0))
    localtime = tmp_path / "localtime"
    localtime.write_bytes(_tzif(BERLIN))

    check = _timezone_checks("container", {"TZ": "UTC"}, zoneinfo_dir=tmp_path, localtime=localtime)

    assert check["level"] == "warning"
    assert "localtime" in check["message"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
@pytest.mark.parametrize("prefix", [":", ""])
def test_a_tz_naming_a_fifo_does_not_stall_diagnose(tmp_path, prefix):
    """/api/diagnose runs this in a dashboard request thread; open() on a FIFO waits for a writer."""

    fifo = tmp_path / "zone.fifo"
    os.mkfifo(fifo)
    environ = {"TZ": f"{prefix}{fifo}"} if prefix else {"TZ": "zone.fifo"}
    result = []
    worker = threading.Thread(
        target=lambda: result.append(_timezone_checks("container", environ, zoneinfo_dir=tmp_path)),
        daemon=True,
    )

    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "diagnose blocked on the FIFO"
    assert result[0]["level"] == "warning"


@pytest.mark.parametrize("mode", ["container", "native"])
def test_an_empty_tz_is_utc_whatever_localtime_holds(tmp_path, mode):
    """glibc reads TZ= as UTC; it is not the absence of TZ."""

    localtime = tmp_path / "localtime"
    localtime.write_bytes(_tzif(BERLIN))

    check = _timezone_checks(mode, {"TZ": ""}, localtime=localtime)

    assert check["level"] == "warning"
    assert "empty" in check["message"]


@pytest.mark.parametrize("zone", ["UTC", "Etc/UTC", ":UTC"])
def test_a_utc_name_that_reads_another_zone_says_so(tmp_path, zone):
    """In the image /etc/localtime points at Etc/UTC, so a mounted host file
    lands on the UTC zone file itself: TZ=UTC then runs on the host's hours."""

    for name in ("UTC", "Etc/UTC"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(_tzif(BERLIN))

    check = _timezone_checks("container", {"TZ": zone}, zoneinfo_dir=tmp_path)

    assert check["level"] == "warning"
    assert "UTC" in check["message"]


@pytest.mark.parametrize("cut", [1, 2, 8, 9, 10, 11, 20])
def test_a_zone_file_cut_short_is_no_zone_and_never_stalls(tmp_path, cut):
    """A zone file cut anywhere in its footer is no zone file to diagnose.

    glibc may still run such a file on its transitions, so this errs towards a
    warning, never towards a zone glibc does not read. zoneinfo looked through
    it for a newline forever before Python 3.14, and a newline added behind it
    had it read a footer glibc drops.
    """

    zone_file = tmp_path / "cut"
    zone_file.write_bytes(_tzif(BERLIN)[:-cut])
    result = []
    worker = threading.Thread(
        target=lambda: result.append(diagnostics.diagnose_zone_offsets(str(zone_file))), daemon=True
    )

    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "reading the zone file never returned"
    assert result == [None]


def test_a_utc_path_that_reads_another_zone_says_so(tmp_path):
    zone_file = tmp_path / "Etc" / "UTC"
    zone_file.parent.mkdir()
    zone_file.write_bytes(_tzif(BERLIN))

    check = _timezone_checks("container", {"TZ": f":{zone_file}"})

    assert check["level"] == "warning"
    assert "UTC" in check["message"]


def test_the_zone_in_effect_is_named_with_its_offset(tmp_path):
    """Etc/GMT+1 is one hour behind UTC; the report says so rather than leave the sign to a guess."""

    (tmp_path / "Etc").mkdir()
    (tmp_path / "Etc" / "GMT+1").write_bytes(_tzif(-3600))

    check = _timezone_checks("container", {"TZ": "Etc/GMT+1"}, zoneinfo_dir=tmp_path)

    assert check["level"] == "ok"
    assert "UTC-01:00" in check["message"]


@pytest.mark.parametrize(
    "key",
    ["control_snapshot", "authority", "api_family", "snapshot_status", "monkey_patch", "keepalive_seconds"],
)
def test_a_key_that_only_contains_a_redaction_word_is_not_redacted(key):
    assert diagnostics.diagnose_redact_key(key) is False


@pytest.mark.parametrize(
    "key",
    [
        "password", "password_hash", "token", "api_key", "apiKey", "app_key", "sn",
        "serial", "serialNumber", "device_id", "deviceId", "auth_file", "session_cookie",
        "credentials_ref", "Authorization", "bearer",
    ],
)
def test_a_key_that_names_a_secret_or_identity_is_redacted(key):
    assert diagnostics.diagnose_redact_key(key) is True


def test_the_http_report_keeps_the_control_snapshot_readable():
    report = {"control": {"control_snapshot": {"commanded_total_w": 420, "sn": "ABC123"}}}

    redacted = diagnostics.diagnose_redact_report_for_http(report)

    assert redacted["control"]["control_snapshot"]["commanded_total_w"] == 420
    assert redacted["control"]["control_snapshot"]["sn"] == "<redacted>"


def test_emsctl_diagnose_control_names_the_limit_the_cycle_named(tmp_path):
    write_control_runtime(tmp_path)
    write_live_control_status(
        tmp_path, devices={"WR1": {**DEFAULT_LIVE_DEVICE, "limiting_reason": "deadband"}}
    )

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    reasons = {
        item["device"]: item["reason"]
        for item in payload["control"]["device_distribution"]["devices"]
    }
    assert reasons["WR1"] == "limited by deadband"
