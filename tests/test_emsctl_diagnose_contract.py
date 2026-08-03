# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
EMSCTL = ROOT / "emsctl.py"
REQUIRED_BUNDLE_FILES = {
    "diagnosis.json",
    "diagnosis.txt",
    "control-diagnostics.json",
    "control-diagnostics.txt",
    "control-quality.json",
    "control-quality.txt",
    "redacted-config.json",
    "runtime-state.json",
    "bundle-metadata.json",
}
ROOT_CAUSE_FIELDS = {
    "code",
    "severity",
    "title",
    "message",
    "suggested_next_check",
}


def write_config(path):
    path.write_text(json.dumps({
        "system": {
            "enabled": True,
            "dry_run": False,
            "max_total_power": 900,
            "max_device_power": 800,
            "loop_interval": 5,
            "min_output_limit": 35,
            "runtime_state_path": "runtime-state.json",
        },
        "ha": {
            "enabled": True,
            "control_enabled": True,
            "url": "http://homeassistant.local:8123",
            "token": "contract-ha-token",
        },
        "winter": {"enabled": False},
        "devices": [
            {
                "name": "WR1",
                "max_power": 800,
                "pv_priority_factor": 1.0,
                "min_soc": 15,
            }
        ],
        "grid_meter": {"type": "ha"},
    }))


def write_runtime(tmp_path, **updates):
    runtime = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "grid_power_w": 142,
        "filtered_load_w": 131,
        "inverter_output_w": 130,
        "control_samples": [10, -120, -130, 15],
        "controller": {
            "enabled": True,
            "effective_target_total_w": 130,
            "commanded_total_w": 130,
            "filtered_load_w": 131,
        },
        "system": {
            "enabled": True,
            "max_total_power": 900,
            "min_output_limit": 35,
            "loop_interval": 5,
        },
        "winter": {"enabled": False},
        "devices": {
            "WR1": {
                "online": True,
                "enabled": True,
                "soc": 55,
                "min_soc": 15,
                "allocated_target_w": 130,
                "target_w": 130,
                "output_w": 130,
                "max_power": 800,
                "output_limit_w": 800,
            }
        },
    }
    for key, value in updates.items():
        runtime[key] = value
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))
    return runtime


def run_emsctl(tmp_path, *args):
    config_path = tmp_path / "config.json"
    if not config_path.exists():
        write_config(config_path)
    return subprocess.run(
        [
            sys.executable,
            str(EMSCTL),
            "--config",
            str(config_path),
            "--runtime-state",
            str(tmp_path / "runtime-state.json"),
            "--dashboard-auth",
            str(tmp_path / "dashboard-auth.json"),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_cli_status_matches_payload(result, payload):
    expected = 1 if payload["status"] == "error" else 0
    assert result.returncode == expected, result.stderr


def assert_root_cause_contract(cause):
    assert set(cause) == ROOT_CAUSE_FIELDS
    assert cause["code"]
    assert cause["severity"] in {"info", "warning", "error"}
    assert cause["title"]
    assert cause["message"]
    assert cause["suggested_next_check"]


def test_diagnose_json_contract_matrix(tmp_path):
    write_runtime(tmp_path)
    variants = [
        ("diagnose", "--json"),
        ("diagnose", "--deep", "--json"),
        ("diagnose", "--hardware", "--json"),
        ("diagnose", "--control", "--json"),
        ("diagnose", "--control-quality", "--json"),
    ]

    for args in variants:
        result = run_emsctl(tmp_path, *args)
        payload = json.loads(result.stdout)

        assert_cli_status_matches_payload(result, payload)
        assert payload["schema_version"] == 1
        assert payload["diagnosis"]["version"] == 1
        assert payload["diagnosis"]["timestamp"] == payload["generated_at"]
        assert payload["diagnosis"]["status"] == payload["status"]
        assert isinstance(payload["sections"], list)
        assert isinstance(payload["metrics"], dict)
        assert isinstance(payload["warnings"], list)
        assert isinstance(payload["errors"], list)
        assert isinstance(payload["root_causes"], list)
        for cause in payload["root_causes"]:
            assert_root_cause_contract(cause)


def test_root_causes_are_structured_for_control_and_quality(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["system"]["dry_run"] = True
    config_path.write_text(json.dumps(config))
    runtime = write_runtime(tmp_path)
    runtime["system"]["enabled"] = False
    runtime["devices"]["WR1"]["soc"] = 14
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control", "--control-quality", "--json")
    payload = json.loads(result.stdout)

    assert payload["root_causes"]
    for cause in payload["root_causes"]:
        assert_root_cause_contract(cause)
    assert any(cause["code"] == "control_disabled" for cause in payload["control"]["root_causes"])
    assert any(cause["code"] == "dry_run_enabled" for cause in payload["control"]["root_causes"])
    assert any(cause["code"] == "minimum_soc_protection_active" for cause in payload["control"]["root_causes"])
    assert any(cause["code"] == "export_peaks_detected" for cause in payload["control_quality"]["root_causes"])


def test_support_bundle_contract_files_and_metadata(tmp_path):
    write_runtime(tmp_path)
    output_path = tmp_path / "support-bundle.zip"

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--control",
        "--control-quality",
        "--support-bundle",
        "--output",
        str(output_path),
    )

    assert result.returncode in (0, 1), result.stderr
    with zipfile.ZipFile(output_path) as bundle:
        assert set(bundle.namelist()) == REQUIRED_BUNDLE_FILES
        diagnosis = json.loads(bundle.read("diagnosis.json"))
        metadata = json.loads(bundle.read("bundle-metadata.json"))
        control = json.loads(bundle.read("control-diagnostics.json"))
        quality = json.loads(bundle.read("control-quality.json"))

    assert diagnosis["schema_version"] == 1
    assert metadata["bundle_version"] == 1
    assert metadata["schema_version"] == 1
    assert metadata["generated_at"] == diagnosis["generated_at"]
    # ems_version is present but honest: null for a local/dev build, never a
    # hardcoded fallback. A best-effort build label rides alongside it.
    assert metadata["ems_version"] is None
    assert "build_label" in metadata
    assert control["snapshot"]["grid_power_w"] == 142
    assert quality["export_import"]["samples"] == 4


def test_support_bundle_masks_cloud_route_and_full_topic(tmp_path):
    route = "ACCOUNT_ROUTE_1234"
    topic = f"iot/PRODUCT_SECRET/{route}/properties/write"
    write_runtime(
        tmp_path,
        zendure_mqtt={
            "brokers": [
                {
                    "broker_ref": "cloud_a",
                    "source": "zendure_cloud_mqtt",
                    "password": "BROKER_PASSWORD",
                }
            ],
            "devices": [
                {
                    "name": "WR1",
                    "broker_ref": "cloud_a",
                    "source": "zendure_cloud_mqtt",
                    "identifier": route,
                    "last_command": {
                        "device_id": route,
                        "topic": topic,
                        "correlation_id": "safe-correlation-id",
                    },
                }
            ],
        },
    )
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["zendure_mqtt"] = {
        "brokers": {
            "cloud_a": {
                "source": "zendure_cloud_mqtt",
                "host": "mqtt.example.invalid",
                "credentials_ref": "zendure_cloud:account-a",
            }
        }
    }
    config["devices"].append(
        {
            "name": "Cloud inverter",
            "type": "zendure_mqtt",
            "mqtt": {
                "broker_ref": "cloud_a",
                "product_key": "PRODUCT_SECRET",
                "device_id": route,
                "write_topic": topic,
            },
        }
    )
    config_path.write_text(json.dumps(config))
    output_path = tmp_path / "support-bundle.zip"

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--support-bundle",
        "--output",
        str(output_path),
    )

    assert result.returncode in (0, 1), result.stderr
    with zipfile.ZipFile(output_path) as bundle:
        contents = b"\n".join(bundle.read(name) for name in bundle.namelist())
    for secret in (route, topic, b"BROKER_PASSWORD".decode()):
        assert secret.encode() not in contents
    assert b"safe-correlation-id" in contents


def test_support_bundle_redacts_known_secret_shapes(tmp_path):
    secrets = {
        "zendure_token": "zendure-token-secret",
        "ha_token": "ha-token-secret",
        "dashboard_hash": "dashboard-password-hash-secret",
        "mqtt_user": "mqtt-user-secret",
        "mqtt_password": "mqtt-password-secret",
        "api_secret": "api-secret-value",
        "auth_secret": "auth-secret-value",
    }
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["zendure"] = {"token": secrets["zendure_token"]}
    config["ha"]["token"] = secrets["ha_token"]
    config["dashboard"] = {"password_hash": secrets["dashboard_hash"]}
    config["mqtt"] = {
        "username": secrets["mqtt_user"],
        "password": secrets["mqtt_password"],
    }
    config["api_secret"] = secrets["api_secret"]
    config["auth_secret"] = secrets["auth_secret"]
    config_path.write_text(json.dumps(config))
    write_runtime(tmp_path)
    output_path = tmp_path / "redacted.zip"

    result = run_emsctl(
        tmp_path,
        "diagnose",
        "--control",
        "--support-bundle",
        "--output",
        str(output_path),
    )

    assert result.returncode in (0, 1), result.stderr
    combined = ""
    with zipfile.ZipFile(output_path) as bundle:
        for name in bundle.namelist():
            combined += bundle.read(name).decode("utf-8", errors="replace")
    for secret in secrets.values():
        assert secret not in combined
    assert "<redacted>" in combined


def test_diagnose_text_output_stability_anchors(tmp_path):
    write_runtime(tmp_path)

    result = run_emsctl(tmp_path, "diagnose")
    assert result.returncode == 0, result.stderr
    assert "EMS Diagnose" in result.stdout
    assert "Mode:" in result.stdout
    assert "Runtime state" in result.stdout
    assert "Result:" in result.stdout

    result = run_emsctl(tmp_path, "diagnose", "--control")
    assert result.returncode == 0, result.stderr
    assert "Control Snapshot" in result.stdout
    assert "Decision Explanation" in result.stdout
    assert "Write Path" in result.stdout

    result = run_emsctl(tmp_path, "diagnose", "--control-quality")
    assert result.returncode in (0, 1), result.stderr
    assert "Export / Import Quality" in result.stdout
    assert "Regulation Quality" in result.stdout
    assert "Likely Causes" in result.stdout


def test_diagnose_broken_output_stability_anchors(tmp_path):
    (tmp_path / "config.json").write_text("{broken json")

    result = run_emsctl(tmp_path, "diagnose")

    assert result.returncode == 1
    assert "EMS Diagnose" in result.stdout
    assert "[ERROR] config.json is invalid JSON" in result.stdout
    assert "Result: error" in result.stdout
