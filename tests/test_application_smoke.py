# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ems.config import default_safe_config

pytestmark = [
    pytest.mark.e2e,
]


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ems-solarflow-api-control.py"


def write_simulation_config(tmp_path):
    config = default_safe_config()
    config["system"]["runtime_state_path"] = str(tmp_path / "runtime-state.json")
    config["dashboard"]["database_path"] = str(tmp_path / "dashboard.sqlite")
    config["dashboard"]["enabled"] = False
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    return config_path


def run_app(*args):
    return subprocess.run(
        [sys.executable, str(APP), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_application_help_smoke():
    result = run_app("--help")

    assert result.returncode == 0
    assert "--simulate" in result.stdout
    assert "--max-cycles" in result.stdout


def test_application_self_test_smoke(tmp_path):
    config_path = write_simulation_config(tmp_path)

    result = run_app("--config", str(config_path), "--self-test")

    assert result.returncode == 0, result.stderr
    assert "event=self_test_ok" in result.stderr


def test_application_builtin_simulation_smoke_uses_temp_runtime_state(tmp_path):
    config_path = write_simulation_config(tmp_path)

    result = run_app(
        "--config",
        str(config_path),
        "--simulate",
        "--max-cycles",
        "2",
    )

    assert result.returncode == 0, result.stderr
    assert "event=replay_frame" in result.stderr
    assert "event=replay_stopped" in result.stderr
    assert "reason=max_cycles" in result.stderr
    assert (tmp_path / "runtime-state.json").exists()
