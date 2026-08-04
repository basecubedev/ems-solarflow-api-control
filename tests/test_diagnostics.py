# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import emsctl
from ems import diagnostics
from ems.state_store import BatteryFullChargeStateStore

from _emsctl_test_helpers import (
    assert_diagnose_help_discovery,
    diagnose_args,
    run_emsctl,
    write_config,
    write_control_runtime,
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

    result = run_emsctl(tmp_path, "diagnose", "--control")

    assert result.returncode == 0, result.stderr
    assert "Control Snapshot" in result.stdout
    assert "Grid Power:" in result.stdout
    assert "Decision Explanation" in result.stdout
    assert "Target output calculated" in result.stdout


def test_emsctl_diagnose_control_deadband_detection(tmp_path):
    write_control_runtime(
        tmp_path,
        grid_power_w=4,
        filtered_load_w=3,
        controller={
            "effective_target_total_w": 130,
            "commanded_total_w": 130,
            "filtered_load_w": 3,
        },
    )

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["deadband"]["active"] is True
    assert any(check["code"] == "deadband_active" for check in payload["checks"])


def test_emsctl_diagnose_control_noisy_meter_detection(tmp_path):
    write_control_runtime(
        tmp_path,
        control_samples=[-45, 52, -40, 49, -35, 45, -30, 41],
    )

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--control",
        "--sample-seconds",
        "30",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["noisy"] is True
    assert payload["control"]["meter_quality"]["sign_changes"] >= 7
    assert any(check["code"] == "meter_signal_noisy" for check in payload["checks"])


def test_emsctl_diagnose_control_repeated_meter_values_are_not_stale(tmp_path):
    write_control_runtime(tmp_path, control_samples=[18, 18, 18, 18])

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["stale"] is False
    assert not any(check["code"] == "meter_signal_stale" for check in payload["checks"])
    assert not any(
        cause["code"] == "grid_meter_values_are_stale"
        for cause in payload["control"]["root_causes"]
    )


def test_emsctl_diagnose_control_repeated_meter_values_with_changing_timestamps_are_not_stale(tmp_path):
    now = datetime.now(timezone.utc)
    write_control_runtime(
        tmp_path,
        control_samples=[
            {"grid_power_w": 18, "timestamp": (now - timedelta(seconds=3)).isoformat()},
            {"grid_power_w": 18, "timestamp": (now - timedelta(seconds=2)).isoformat()},
            {"grid_power_w": 18, "timestamp": (now - timedelta(seconds=1)).isoformat()},
        ],
    )

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["stale"] is False
    assert not any(check["code"] == "meter_signal_stale" for check in payload["checks"])


def test_emsctl_diagnose_control_repeated_meter_values_with_unchanged_timestamp_are_stale(tmp_path):
    timestamp = datetime.now(timezone.utc).isoformat()
    write_control_runtime(
        tmp_path,
        control_samples=[
            {"grid_power_w": 18, "timestamp": timestamp},
            {"grid_power_w": 18, "timestamp": timestamp},
            {"grid_power_w": 18, "timestamp": timestamp},
        ],
    )

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
    runtime = write_control_runtime(tmp_path, control_samples=[18, 19, 18])
    runtime["grid_meter"] = {"consecutive_read_failures": 3}
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["meter_quality"]["stale"] is True
    assert payload["control"]["meter_quality"]["stale_reason"] == "read_failures"
    assert any(check["code"] == "meter_signal_stale" for check in payload["checks"])


def test_emsctl_diagnose_control_old_runtime_state_without_live_timestamp_is_info(tmp_path):
    old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    write_control_runtime(tmp_path, timestamp=old_timestamp)

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["control"]["runtime_state"]["stale"] is False
    assert payload["control"]["runtime_state"]["checked"] is False
    assert any(
        check["code"] == "control_staleness_skipped"
        for check in payload["checks"]
    )
    assert not any(
        check["code"] == "control_runtime_state_stale"
        for check in payload["checks"]
    )


def test_emsctl_diagnose_control_old_runtime_state_with_healthy_control_timestamp_is_not_stale(tmp_path):
    old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    runtime = write_control_runtime(tmp_path, timestamp=old_timestamp)
    runtime["controller"]["timestamp"] = datetime.now(timezone.utc).isoformat()
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

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
    old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    runtime = write_control_runtime(tmp_path)
    runtime["controller"]["timestamp"] = old_timestamp
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

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
    assert any(
        check["code"] == "control_runtime_state_stale"
        for check in payload["checks"]
    )
    assert not any(check["level"] == "error" for check in payload["checks"])


def test_emsctl_diagnose_control_soc_imbalance_detection(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["devices"].append({
        "name": "WR2",
        "max_power": 800,
        "pv_priority_factor": 1.0,
        "min_soc": 15,
    })
    config_path.write_text(json.dumps(config))
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 92
    runtime["devices"]["WR2"] = {
        "online": True,
        "enabled": True,
        "soc": 55,
        "min_soc": 15,
        "allocated_target_w": 0,
        "output_w": 0,
        "max_power": 800,
    }
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

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


def test_emsctl_diagnose_control_root_cause_min_soc(tmp_path):
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 14
    runtime["devices"]["WR1"]["min_soc"] = 15
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

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
    write_control_runtime(tmp_path, control_samples=[0, 10, -10, 5])

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
    write_control_runtime(tmp_path, control_samples=[0, 10, -10, 5])

    result = run_emsctl(tmp_path, "diagnose", "--quality", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["options"]["control_quality"] is True


def test_emsctl_diagnose_control_quality_no_export_stable_import(tmp_path):
    write_control_runtime(tmp_path, control_samples=[10, 20, 25, 15])

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["control_quality"]["export_import"]
    assert metrics["status"] == "ok"
    assert metrics["max_export_peak_w"] == 10
    assert metrics["near_zero_duration_percent"] == 100


def test_emsctl_diagnose_control_quality_small_export_peaks_only(tmp_path):
    write_control_runtime(tmp_path, control_samples=[20, -50, 15, 10])

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["control_quality"]["export_import"]
    assert metrics["status"] == "warning"
    assert metrics["max_export_peak_w"] == -50


def test_emsctl_diagnose_control_quality_large_export_peaks(tmp_path):
    write_control_runtime(tmp_path, control_samples=[10, -300, -260, 20])

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 1, result.stderr
    quality = json.loads(result.stdout)["control_quality"]
    assert quality["export_import"]["status"] == "error"
    assert any(cause["code"] == "export_peaks_detected" for cause in quality["root_causes"])


def test_emsctl_diagnose_control_quality_long_export_duration(tmp_path):
    write_control_runtime(tmp_path, control_samples=[-40, -45, -35, 10])

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)["control_quality"]["export_import"]
    assert metrics["status"] == "warning"
    assert metrics["export_duration_percent"] == 75


def test_emsctl_diagnose_control_quality_missing_samples(tmp_path):
    write_control_runtime(tmp_path)
    runtime = json.loads((tmp_path / "runtime-state.json").read_text())
    runtime.pop("grid_power_w")
    runtime.pop("control_samples", None)
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

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
        write_control_runtime(case_dir, control_samples=samples)
        result = run_emsctl(case_dir, "diagnose", "--control-quality", "--json")
        payload = json.loads(result.stdout)
        assert payload["control_quality"]["quality_score"]["classification"] == expected


def test_emsctl_diagnose_control_quality_pv_available_and_used(tmp_path):
    write_control_runtime(
        tmp_path,
        pv_total_w=920,
        inverter_output_w=700,
        battery_power_w=-180,
    )

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    pv = json.loads(result.stdout)["control_quality"]["pv_diagnostics"]
    assert pv["available"] is True
    assert pv["status"] == "ok"
    assert "PV usage looks plausible." in pv["messages"]


def test_emsctl_diagnose_control_quality_pv_limited_by_system_limit(tmp_path):
    write_control_runtime(tmp_path, pv_total_w=1200, inverter_output_w=880)

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
    write_control_runtime(tmp_path, pv_total_w=900, inverter_output_w=490)

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "pv_limited_by_device_limit" for cause in causes)


def test_emsctl_diagnose_control_quality_pv_available_but_unused(tmp_path):
    write_control_runtime(tmp_path, pv_total_w=900, inverter_output_w=50, battery_power_w=-20)

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    pv = json.loads(result.stdout)["control_quality"]["pv_diagnostics"]
    assert pv["status"] == "warning"
    assert any(cause["code"] == "pv_available_but_not_used" for cause in pv["root_causes"])


def test_emsctl_diagnose_control_quality_missing_pv_telemetry(tmp_path):
    write_control_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    pv = json.loads(result.stdout)["control_quality"]["pv_diagnostics"]
    assert pv["available"] is False
    assert pv["status"] == "info"


def test_emsctl_diagnose_control_quality_soc_balanced_devices(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR2"] = dict(runtime["devices"]["WR1"], soc=64, output_w=120)
    runtime["devices"]["WR1"]["soc"] = 62
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "ok"
    assert soc["soc_spread"] == 2


def test_emsctl_diagnose_control_quality_soc_warning_spread(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 80
    runtime["devices"]["WR2"] = dict(runtime["devices"]["WR1"], soc=60, output_w=100)
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "warning"
    assert soc["soc_spread"] == 20


def test_emsctl_diagnose_control_quality_soc_error_spread(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 90
    runtime["devices"]["WR2"] = dict(runtime["devices"]["WR1"], soc=55, output_w=100)
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 1, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "error"
    assert soc["soc_spread"] == 35


def test_emsctl_diagnose_control_quality_lower_soc_device_overused(tmp_path):
    config_path = tmp_path / "config.json"
    write_two_device_config(config_path)
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 80
    runtime["devices"]["WR1"]["output_w"] = 100
    runtime["devices"]["WR2"] = dict(runtime["devices"]["WR1"], soc=55, output_w=520)
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "lower_soc_device_overused" for cause in causes)


def test_emsctl_diagnose_control_quality_min_soc_protected_device(tmp_path):
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 17
    runtime["devices"]["WR1"]["min_soc"] = 15
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    causes = json.loads(result.stdout)["control_quality"]["root_causes"]
    assert any(cause["code"] == "min_soc_protected_device" for cause in causes)


def test_emsctl_diagnose_control_quality_missing_soc_data(tmp_path):
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"].pop("soc")
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control-quality", "--json")

    assert result.returncode == 0, result.stderr
    soc = json.loads(result.stdout)["control_quality"]["soc_balancing"]
    assert soc["status"] == "info"
    assert soc["devices"] == []


def test_emsctl_diagnose_control_quality_support_bundle_export(tmp_path):
    write_control_runtime(tmp_path, control_samples=[0, 10, -10, 5])
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
