# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import emsctl
from ems import paths as ems_paths
from ems.state_store import BatteryFullChargeStateStore

from _emsctl_test_helpers import (
    EMSCTL,
    ROOT,
    assert_diagnose_help_discovery,
    assert_diagnose_option_flags,
    config_args,
    diagnose_args,
    patch_emsctl_base,
    run_emsctl,
    run_emsctl_no_args,
    runtime_state,
    write_config,
    write_control_runtime,
    write_discovery_config,
    write_two_device_config,
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
    assert "Common commands" in result.stdout
    assert "Usage:" in result.stdout
    assert "Diagnostics:" in result.stdout
    assert "Runtime control:" in result.stdout
    assert "Dashboard:" in result.stdout
    assert "More help:" in result.stdout
    assert_diagnose_help_discovery(result.stdout)
    assert_diagnose_option_flags(result.stdout)
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_help_contains_common_examples(tmp_path):
    result = run_emsctl(tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    assert "Common examples" in result.stdout
    assert "python3 emsctl.py interactive" in result.stdout
    assert "python3 emsctl.py examples" in result.stdout
    assert "python3 emsctl.py completion bash" in result.stdout
    assert_diagnose_help_discovery(result.stdout)
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
    assert_diagnose_help_discovery(result.stdout)
    assert "python3 emsctl.py diagnose --control-quality --sample-seconds 60" in result.stdout
    assert "python3 emsctl.py diagnose --quality --json" in result.stdout
    assert "python3 emsctl.py diagnose --support-bundle --output /tmp/ems-support.zip" in result.stdout
    assert "docker compose exec ems python3 emsctl.py diagnose" in result.stdout
    assert "docker compose exec ems python3 emsctl.py diagnose --control" in result.stdout
    assert "docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60" in result.stdout
    assert "docker compose exec ems python3 emsctl.py diagnose --support-bundle" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_completion_bash_contains_commands_and_configured_device(tmp_path):
    result = run_emsctl(tmp_path, "completion", "bash")

    assert result.returncode == 0, result.stderr
    assert "status system device ha ha-control winter dashboard diagnose interactive menu examples completion help" in result.stdout
    assert "set-password change-password disable-auth auth-status" in result.stdout
    assert "off eco standard" in result.stdout
    assert "output input" in result.stdout
    assert "WR1" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_completion_zsh_contains_commands_and_configured_device(tmp_path):
    result = run_emsctl(tmp_path, "completion", "zsh")

    assert result.returncode == 0, result.stderr
    assert "commands=(status system device ha ha-control winter dashboard diagnose interactive menu examples completion help)" in result.stdout
    assert "dashboard_actions=(set-password change-password disable-auth auth-status)" in result.stdout
    assert "offgrid_modes=(off eco standard)" in result.stdout
    assert "ac_modes=(output input)" in result.stdout
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


def test_emsctl_device_help_contains_ac_mode(tmp_path):
    result = run_emsctl(tmp_path, "device", "--help")

    assert result.returncode == 0, result.stderr
    assert "ac-mode [output|input]" in result.stdout
    assert "ac-charge-power WATTS" in result.stdout
    assert "python3 emsctl.py device WR1 ac-mode output" in result.stdout
    assert "python3 emsctl.py device WR1 ac-charge-power 200" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_updates_system_device_and_sections(tmp_path):
    commands = [
        ("system", "disable"),
        ("system", "max-power", "650"),
        ("device", "WR1", "max-power", "500"),
        ("device", "WR1", "offgrid", "eco"),
        ("device", "WR1", "ac-mode", "input"),
        ("device", "WR1", "ac-charge-power", "200"),
        ("ha", "disable"),
        ("ha-control", "disable"),
        ("winter", "enable"),
    ]

    for command in commands:
        result = run_emsctl(tmp_path, *command)
        assert result.returncode == 0, result.stderr
        if len(command) > 2 and command[2] == "ac-charge-power":
            assert result.stdout == "Set WR1 AC charge power to 200 W.\n"
        else:
            assert result.stdout.startswith("updated ")

    state = runtime_state(tmp_path)
    assert state["system"]["enabled"] is False
    assert state["system"]["max_total_power"] == 650
    assert state["devices"]["WR1"]["max_power"] == 500
    assert state["devices"]["WR1"]["offgrid_socket_mode"] == "eco"
    assert state["devices"]["WR1"]["runtime_role"] == "ac_input"
    assert state["devices"]["WR1"]["runtime_role_reason"] == "emsctl"
    assert state["devices"]["WR1"]["ac_charge_power_w"] == 200
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
        ("device", "WR1", "ac-mode", "invalid"),
        ("device", "WR1", "ac-charge-power", "-1"),
        ("device", "WR1", "ac-charge-power", "200.5"),
        ("device", "UNKNOWN", "disable"),
        ("device", "UNKNOWN", "ac-charge-power", "200"),
        ("device", "WR1", "max-power", "nan"),
    ]

    for command in invalid_commands:
        result = run_emsctl(tmp_path, *command)
        assert result.returncode != 0
        assert "ERROR:" in result.stderr
        assert (tmp_path / "runtime-state.json").read_text() == before


def test_emsctl_device_ac_mode_output_and_status(tmp_path):
    result = run_emsctl(tmp_path, "device", "WR1", "ac-mode", "output")

    assert result.returncode == 0, result.stderr
    state = runtime_state(tmp_path)
    assert state["devices"]["WR1"]["runtime_role"] == "ac_output"
    assert state["devices"]["WR1"]["runtime_role_reason"] == "emsctl"

    result = run_emsctl(tmp_path, "device", "WR1", "ac-mode")
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert payload["device"] == "WR1"
    assert payload["runtime_role"] == "ac_output"
    assert payload["command"] == "output"
    assert payload["desired_ac_mode"] == 2
    assert payload["output_control_allowed"] is True


def test_emsctl_device_ac_mode_input_writes_runtime_state_only(tmp_path):
    result = run_emsctl(tmp_path, "device", "WR1", "ac-mode", "input")

    assert result.returncode == 0, result.stderr
    state = runtime_state(tmp_path)
    assert state["devices"]["WR1"]["runtime_role"] == "ac_input"
    assert state["devices"]["WR1"]["runtime_role_reason"] == "emsctl"


def test_emsctl_device_ac_charge_power_writes_runtime_state(tmp_path):
    result = run_emsctl(tmp_path, "device", "WR1", "ac-charge-power", "200")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Set WR1 AC charge power to 200 W.\n"
    state = runtime_state(tmp_path)
    assert state["devices"]["WR1"]["ac_charge_power_w"] == 200

    result = run_emsctl(tmp_path, "device", "WR1", "ac-charge-power", "0")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Set WR1 AC charge power to 0 W.\n"
    state = runtime_state(tmp_path)
    assert state["devices"]["WR1"]["ac_charge_power_w"] == 0


def test_emsctl_ac_charge_power_preserves_runtime_role(tmp_path):
    assert run_emsctl(tmp_path, "device", "WR1", "ac-mode", "input").returncode == 0

    result = run_emsctl(tmp_path, "device", "WR1", "ac-charge-power", "200")

    assert result.returncode == 0, result.stderr
    state = runtime_state(tmp_path)
    assert state["devices"]["WR1"]["runtime_role"] == "ac_input"
    assert state["devices"]["WR1"]["runtime_role_reason"] == "emsctl"
    assert state["devices"]["WR1"]["ac_charge_power_w"] == 200


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
