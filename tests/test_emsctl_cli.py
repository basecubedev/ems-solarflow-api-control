import json
import subprocess
import sys
from pathlib import Path


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
    }))


def run_emsctl(tmp_path, *args):
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
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def runtime_state(tmp_path):
    return json.loads((tmp_path / "runtime-state.json").read_text())


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
