# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import emsctl

ROOT = Path(__file__).resolve().parents[1]
EMSCTL = ROOT / "emsctl.py"


def write_config(path):
    path.write_text(json.dumps({
        "system": {
            "enabled": True,
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
            "token": "test-token",
        },
        "winter": {
            "enabled": False,
        },
        "devices": [
            {
                "name": "WR1",
                "max_power": 800,
                "pv_priority_factor": 1.1,
            }
        ],
        "grid_meter": {
            "type": "shelly",
            "ip": "192.0.2.10",
        },
    }))


def run_emsctl(tmp_path, *args, input_text=None):
    config_path = tmp_path / "config.json"
    runtime_path = tmp_path / "runtime-state.json"
    if not config_path.exists():
        write_config(config_path)

    return subprocess.run(
        [
            sys.executable,
            str(EMSCTL),
            "--config",
            str(config_path),
            "--runtime-state",
            str(runtime_path),
            "--dashboard-auth",
            str(tmp_path / "dashboard-auth.json"),
            *args,
        ],
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
    )


def run_emsctl_no_args(tmp_path):
    return subprocess.run(
        [sys.executable, str(EMSCTL)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )


def runtime_state(tmp_path):
    return json.loads((tmp_path / "runtime-state.json").read_text())


def write_control_runtime(tmp_path, **overrides):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "grid_power_w": 142,
        "filtered_load_w": 131,
        "inverter_output_w": 130,
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
        "winter": {
            "enabled": False,
        },
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
    for key, value in overrides.items():
        payload[key] = value
    (tmp_path / "runtime-state.json").write_text(json.dumps(payload))
    return payload


def write_two_device_config(path):
    write_config(path)
    config = json.loads(path.read_text())
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "pv_priority_factor": 1.0, "min_soc": 15},
        {"name": "WR2", "max_power": 800, "pv_priority_factor": 1.0, "min_soc": 15},
    ]
    path.write_text(json.dumps(config))
    return config


def write_discovery_config(path, runtime_state_path=None, auth_file=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "system": {},
        "dashboard": {},
        "devices": [],
    }
    if runtime_state_path is not None:
        payload["system"]["runtime_state_path"] = runtime_state_path
    if auth_file is not None:
        payload["dashboard"]["auth_file"] = auth_file
    path.write_text(json.dumps(payload))


def patch_emsctl_base(monkeypatch, base_dir):
    monkeypatch.setattr(emsctl, "BASE_DIR", str(base_dir))
    monkeypatch.setattr(
        emsctl,
        "DEFAULT_CONFIG_PATH",
        str(base_dir / "config.json"),
    )
    monkeypatch.setattr(
        emsctl,
        "DOCKER_CONFIG_PATH",
        str(base_dir / "config" / "config.json"),
    )


def config_args(config=None, runtime_state=None, dashboard_auth=None):
    return SimpleNamespace(
        config=config,
        runtime_state=runtime_state,
        dashboard_auth=dashboard_auth,
    )


def test_emsctl_config_discovery_prefers_legacy_config(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json", "runtime-state.json")
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(tmp_path / "config.json")
    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "runtime-state.json"
    )


def test_emsctl_config_discovery_falls_back_to_docker_config(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
        auth_file="config/dashboard-auth.json",
    )

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(tmp_path / "config" / "config.json")
    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "data" / "runtime-state.json"
    )
    assert emsctl.resolve_dashboard_auth_path(args, config) == str(
        tmp_path / "config" / "dashboard-auth.json"
    )


def test_emsctl_explicit_config_wins(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json", "runtime-state.json")
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )
    custom_config = tmp_path / "custom.json"
    write_discovery_config(custom_config, "custom/runtime-state.json")

    args = config_args(config=str(custom_config))
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(custom_config)
    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "custom" / "runtime-state.json"
    )


def test_emsctl_config_env_var_wins_when_no_explicit_config(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json", "runtime-state.json")
    env_config = tmp_path / "env.json"
    write_discovery_config(env_config, "env/runtime-state.json")
    monkeypatch.setenv("EMS_CONFIG_FILE", str(env_config))

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(env_config)
    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "env" / "runtime-state.json"
    )


def test_emsctl_explicit_config_wins_over_env_var(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    env_config = tmp_path / "env.json"
    custom_config = tmp_path / "custom.json"
    write_discovery_config(env_config, "env/runtime-state.json")
    write_discovery_config(custom_config, "custom/runtime-state.json")
    monkeypatch.setenv("EMS_CONFIG_FILE", str(env_config))

    args = config_args(config=str(custom_config))
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(custom_config)
    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "custom" / "runtime-state.json"
    )


def test_emsctl_runtime_state_comes_from_selected_config_not_existing_files(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )
    (tmp_path / "runtime-state.json").write_text('{"sentinel": "root"}')
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "runtime-state.json").write_text('{"sentinel": "data"}')

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)
    runtime_path = emsctl.resolve_runtime_path(args, config)

    assert runtime_path == str(data_dir / "runtime-state.json")


def test_emsctl_runtime_state_legacy_config_wins_over_existing_data_file(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json", "runtime-state.json")
    (tmp_path / "runtime-state.json").write_text('{"sentinel": "root"}')
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "runtime-state.json").write_text('{"sentinel": "data"}')

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)
    runtime_path = emsctl.resolve_runtime_path(args, config)

    assert runtime_path == str(tmp_path / "runtime-state.json")


def test_emsctl_runtime_state_override_wins(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )
    override = tmp_path / "override.json"

    args = config_args(runtime_state=str(override))
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert emsctl.resolve_runtime_path(args, config) == str(override)


def test_emsctl_missing_runtime_state_path_falls_back_to_legacy_default(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json")

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "runtime-state.json"
    )


def test_emsctl_dashboard_auth_path_comes_from_selected_config(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(
        tmp_path / "config.json",
        "runtime-state.json",
        auth_file="dashboard-auth.json",
    )
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
        auth_file="config/dashboard-auth.json",
    )

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(tmp_path / "config.json")
    assert emsctl.resolve_dashboard_auth_path(args, config) == str(
        tmp_path / "dashboard-auth.json"
    )


def test_emsctl_dashboard_auth_override_wins(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
        auth_file="config/dashboard-auth.json",
    )
    override = tmp_path / "manual-auth.json"

    args = config_args(dashboard_auth=str(override))
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert emsctl.resolve_dashboard_auth_path(args, config) == str(override)


def test_emsctl_status_uses_docker_config_without_root_config(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )

    assert emsctl.main(["status"]) == 0

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["runtime_state_path"] == str(
        tmp_path / "data" / "runtime-state.json"
    )
    assert (tmp_path / "data" / "runtime-state.json").exists()
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_no_args_prints_quick_help_without_runtime_write(tmp_path):
    result = run_emsctl_no_args(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "interactive" in result.stdout
    assert "examples" in result.stdout
    assert "runtime-state.json" in result.stdout
    assert "Common commands" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_help_contains_common_examples(tmp_path):
    result = run_emsctl(tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    assert "Common examples" in result.stdout
    assert "python3 emsctl.py interactive" in result.stdout
    assert "python3 emsctl.py examples" in result.stdout
    assert "python3 emsctl.py completion bash" in result.stdout
    assert "--password" not in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_system_help_contains_system_action(tmp_path):
    result = run_emsctl(tmp_path, "system", "--help")

    assert result.returncode == 0, result.stderr
    assert "max-power" in result.stdout
    assert "python3 emsctl.py system disable" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_examples_prints_cookbook_without_runtime_write(tmp_path):
    result = run_emsctl(tmp_path, "examples")

    assert result.returncode == 0, result.stderr
    assert "python3 emsctl.py interactive" in result.stdout
    assert "System runtime control" in result.stdout
    assert "python3 emsctl.py device WR1 offgrid eco" in result.stdout
    assert "python3 emsctl.py dashboard auth-status" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_completion_bash_contains_commands_and_configured_device(tmp_path):
    result = run_emsctl(tmp_path, "completion", "bash")

    assert result.returncode == 0, result.stderr
    assert "status system device ha ha-control winter dashboard diagnose interactive menu examples completion help" in result.stdout
    assert "set-password change-password disable-auth auth-status" in result.stdout
    assert "off eco standard" in result.stdout
    assert "WR1" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_completion_zsh_contains_commands_and_configured_device(tmp_path):
    result = run_emsctl(tmp_path, "completion", "zsh")

    assert result.returncode == 0, result.stderr
    assert "commands=(status system device ha ha-control winter dashboard diagnose interactive menu examples completion help)" in result.stdout
    assert "dashboard_actions=(set-password change-password disable-auth auth-status)" in result.stdout
    assert "offgrid_modes=(off eco standard)" in result.stdout
    assert "devices=(WR1)" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_examples_and_completion_do_not_modify_existing_runtime_state(tmp_path):
    runtime_path = tmp_path / "runtime-state.json"
    runtime_path.write_text('{"sentinel": true}\n')
    before = runtime_path.read_text()

    commands = [
        ("examples",),
        ("completion", "bash"),
        ("completion", "zsh"),
        ("diagnose",),
        ("diagnose", "--json"),
    ]

    for command in commands:
        result = run_emsctl(tmp_path, *command)
        assert result.returncode == 0, result.stderr
        assert runtime_path.read_text() == before


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


def test_emsctl_diagnose_json_contains_v2_structure(tmp_path):
    result = run_emsctl(tmp_path, "diagnose", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] in ("ok", "warning", "error")
    assert payload["mode"] in ("native", "container")
    assert "mode_sources" in payload
    assert "summary" in payload
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
        assert {
            "diagnose.txt",
            "diagnose.json",
            "redacted-config.json",
            "runtime-state-redacted.json",
            "last-log-lines.txt",
            "project-info.txt",
            "README-SUPPORT-BUNDLE.txt",
        }.issubset(names)
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
    assert "Control disabled" in payload["control"]["root_causes"]
    assert "Dry run enabled" in payload["control"]["root_causes"]


def test_emsctl_diagnose_control_root_cause_min_soc(tmp_path):
    runtime = write_control_runtime(tmp_path)
    runtime["devices"]["WR1"]["soc"] = 14
    runtime["devices"]["WR1"]["min_soc"] = 15
    (tmp_path / "runtime-state.json").write_text(json.dumps(runtime))

    result = run_emsctl(tmp_path, "diagnose", "--control", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "Minimum SOC protection active" in payload["control"]["root_causes"]
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


def test_emsctl_interactive_status_path(tmp_path):
    result = run_emsctl(tmp_path, "interactive", input_text="status\nquit\n")

    assert result.returncode == 0, result.stderr
    assert "EMS Control CLI interactive mode" in result.stdout
    assert "runtime_state_path" in result.stdout
    assert "WR1" in result.stdout
    assert "Bye." in result.stdout


def test_emsctl_interactive_device_offgrid_edit(tmp_path):
    result = run_emsctl(
        tmp_path,
        "interactive",
        input_text="device-offgrid\nWR1\neco\nquit\n",
    )

    assert result.returncode == 0, result.stderr
    assert "updated " in result.stdout
    assert runtime_state(tmp_path)["devices"]["WR1"]["offgrid_socket_mode"] == "eco"


def test_emsctl_interactive_invalid_choice_does_not_traceback(tmp_path):
    result = run_emsctl(tmp_path, "interactive", input_text="not-a-choice\nquit\n")

    assert result.returncode == 0, result.stderr
    assert "ERROR: invalid choice" in result.stdout
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_interactive_invalid_numeric_does_not_modify_state(tmp_path):
    assert run_emsctl(tmp_path, "status").returncode == 0
    before = (tmp_path / "runtime-state.json").read_text()

    result = run_emsctl(
        tmp_path,
        "interactive",
        input_text="system-max-power\nnot-a-number\nquit\n",
    )

    assert result.returncode == 0, result.stderr
    assert "ERROR: system max-power must be numeric" in result.stdout
    assert "Traceback" not in result.stderr
    assert (tmp_path / "runtime-state.json").read_text() == before


def test_emsctl_status_creates_runtime_state_from_config(tmp_path):
    result = run_emsctl(tmp_path, "status")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runtime_state_path"] == str(tmp_path / "runtime-state.json")
    assert payload["state"]["system"]["max_total_power"] == 900
    assert payload["state"]["devices"]["WR1"]["pv_priority_factor"] == 1.1
    assert (tmp_path / "runtime-state.json").exists()


def test_emsctl_updates_system_device_and_sections(tmp_path):
    commands = [
        ("system", "disable"),
        ("system", "max-power", "650"),
        ("device", "WR1", "max-power", "500"),
        ("device", "WR1", "offgrid", "eco"),
        ("ha", "disable"),
        ("ha-control", "disable"),
        ("winter", "enable"),
    ]

    for command in commands:
        result = run_emsctl(tmp_path, *command)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("updated ")

    state = runtime_state(tmp_path)
    assert state["system"]["enabled"] is False
    assert state["system"]["max_total_power"] == 650
    assert state["devices"]["WR1"]["max_power"] == 500
    assert state["devices"]["WR1"]["offgrid_socket_mode"] == "eco"
    assert state["ha"]["enabled"] is False
    assert state["ha"]["control_enabled"] is False
    assert state["winter"]["enabled"] is True


def test_emsctl_rejects_invalid_values_without_modifying_state(tmp_path):
    assert run_emsctl(tmp_path, "status").returncode == 0
    before = (tmp_path / "runtime-state.json").read_text()

    invalid_commands = [
        ("system", "loop-interval", "0"),
        ("system", "max-power", "-1"),
        ("device", "WR1", "offgrid", "invalid"),
        ("device", "UNKNOWN", "disable"),
        ("device", "WR1", "max-power", "nan"),
    ]

    for command in invalid_commands:
        result = run_emsctl(tmp_path, *command)
        assert result.returncode != 0
        assert "ERROR:" in result.stderr
        assert (tmp_path / "runtime-state.json").read_text() == before


def test_emsctl_winter_status_prints_only_winter_state(tmp_path):
    result = run_emsctl(tmp_path, "winter", "status")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == {"winter": {"enabled": False}}


def test_emsctl_dashboard_set_status_change_and_disable_auth(tmp_path):
    result = run_emsctl(
        tmp_path,
        "dashboard",
        "set-password",
        "--password",
        "first-password",
        "--confirm-password",
        "first-password",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dashboard-auth.json").exists()
    assert "first-password" not in (tmp_path / "dashboard-auth.json").read_text()

    result = run_emsctl(tmp_path, "dashboard", "auth-status")
    assert result.returncode == 0
    assert "Dashboard auth: configured" in result.stdout

    result = run_emsctl(
        tmp_path,
        "dashboard",
        "change-password",
        "--current-password",
        "first-password",
        "--new-password",
        "second-password",
        "--confirm-password",
        "second-password",
    )
    assert result.returncode == 0, result.stderr

    result = run_emsctl(tmp_path, "dashboard", "disable-auth")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "dashboard-auth.json").exists()


def test_emsctl_dashboard_rejects_mismatch_and_wrong_current_password(tmp_path):
    result = run_emsctl(
        tmp_path,
        "dashboard",
        "set-password",
        "--password",
        "first-password",
        "--confirm-password",
        "mismatch",
    )
    assert result.returncode != 0
    assert "confirmation does not match" in result.stderr

    result = run_emsctl(
        tmp_path,
        "dashboard",
        "set-password",
        "--password",
        "first-password",
        "--confirm-password",
        "first-password",
    )
    assert result.returncode == 0, result.stderr

    result = run_emsctl(
        tmp_path,
        "dashboard",
        "change-password",
        "--current-password",
        "wrong",
        "--new-password",
        "second-password",
        "--confirm-password",
        "second-password",
    )
    assert result.returncode != 0
    assert "current dashboard password is incorrect" in result.stderr
