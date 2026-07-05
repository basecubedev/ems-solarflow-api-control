# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import shutil
import stat
import subprocess
import sys

import emsctl
from ems import config as cfg
from ems import config_init as config_init_mod

from _emsctl_test_helpers import (
    EMSCTL,
    ROOT,
    assert_diagnose_help_discovery,
    assert_diagnose_option_flags,
    config_args,
    patch_emsctl_base,
    run_emsctl,
    run_emsctl_no_args,
    runtime_state,
    write_config,
    write_discovery_config,
)


def visible_input(responses):
    def read(prompt=""):
        print(prompt, end="")
        return next(responses)

    return read


def test_emsctl_config_discovery_prefers_canonical_config(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json", "runtime-state.json")
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )

    args = config_args()
    selected = emsctl.resolve_config_path(args)
    config = emsctl.load_config(selected)

    assert selected == str(tmp_path / "config" / "config.json")
    assert emsctl.resolve_runtime_path(args, config) == str(
        tmp_path / "data" / "runtime-state.json"
    )


def test_backup_create_output_dir_overrides_default(tmp_path):
    output_dir = tmp_path / "explicit-backups"

    result = run_emsctl(
        tmp_path,
        "backup",
        "create",
        "--output-dir",
        str(output_dir),
    )

    assert result.returncode == 0, result.stderr
    archives = list(output_dir.glob("ems-config-manual-*.tar.gz"))
    assert len(archives) == 1
    assert f"  {archives[0]}" in result.stdout


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


def test_emsctl_config_discovery_prefers_docker_config_in_container(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    write_discovery_config(tmp_path / "config.json", "runtime-state.json")
    write_discovery_config(
        tmp_path / "config" / "config.json",
        "data/runtime-state.json",
    )
    monkeypatch.setenv("EMS_IN_CONTAINER", "1")

    args = config_args()
    selected = emsctl.resolve_config_path(args)

    assert selected == str(tmp_path / "config" / "config.json")


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

    assert selected == str(tmp_path / "config" / "config.json")
    assert emsctl.resolve_dashboard_auth_path(args, config) == str(
        tmp_path / "config" / "dashboard-auth.json"
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
    assert "status system device ha ha-control winter dashboard influx stack diagnose backup config interactive menu examples completion help" in result.stdout
    assert "set-password change-password disable-auth auth-status" in result.stdout
    assert "off eco standard" in result.stdout
    assert "output input" in result.stdout
    assert "WR1" in result.stdout
    assert not (tmp_path / "runtime-state.json").exists()


def test_emsctl_completion_zsh_contains_commands_and_configured_device(tmp_path):
    result = run_emsctl(tmp_path, "completion", "zsh")

    assert result.returncode == 0, result.stderr
    assert "commands=(status system device ha ha-control winter dashboard influx stack diagnose backup config interactive menu examples completion help)" in result.stdout
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


def test_config_init_dry_run_does_not_write_file(tmp_path):
    config_path = tmp_path / "guided.json"

    result = subprocess.run(
        [
            sys.executable,
            str(EMSCTL),
            "--config",
            str(config_path),
            "config",
            "init",
            "--dry-run",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Dry run: config preview follows" in result.stdout
    assert not config_path.exists()


def test_config_init_missing_config_creates_valid_config_when_confirmed(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    responses = iter([
        "y",
        "1",
        "192.0.2.50",
        "2",
        "",
        "192.0.2.100",
        "SN100",
        "",
        "",
        "",
        "",
        "",
        "",
        "192.0.2.101",
        "SN101",
        "",
        "",
        "",
        "",
        "",
        "900",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "y",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    code = emsctl.main(["--config", str(config_path), "config", "init"])

    assert code == 0
    created = json.loads(config_path.read_text())
    assert created["grid_meter"]["type"] == "shelly"
    assert created["grid_meter"]["ip"] == "192.0.2.50"
    assert [device["name"] for device in created["devices"]] == ["WR1", "WR2"]
    assert created["devices"][0]["ip"] == "192.0.2.100"
    assert created["devices"][1]["sn"] == "SN101"
    assert created["system"]["max_total_power"] == 900


def test_config_init_fresh_creates_standard_config_layout(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")

    code = emsctl.main(["config", "init", "--yes", "--analytics"])

    assert code == 0
    standard = tmp_path / "config" / "config.json"
    assert standard.exists()
    assert (tmp_path / "config").is_dir()
    assert not (tmp_path / "config.json").exists()


def test_config_init_legacy_root_only_warns_before_editing(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    legacy = tmp_path / "config.json"
    shutil.copy(ROOT / "config.template.json", legacy)

    code = emsctl.main(["config", "init", "--dry-run"])

    output = capsys.readouterr()
    assert code == 0
    assert "Legacy root config.json detected." in output.out
    assert str(tmp_path / "config" / "config.json") in output.out
    # Dry run must not create the standard-layout config as a side effect.
    assert not (tmp_path / "config" / "config.json").exists()


def test_config_init_standard_layout_does_not_warn_about_legacy(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")

    code = emsctl.main(["config", "init", "--dry-run"])

    output = capsys.readouterr()
    assert code == 0
    assert "Legacy root config.json detected." not in output.out


def test_config_init_template_config_uses_first_run_continue_wording(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    shutil.copy(ROOT / "config.template.json", config_path)
    monkeypatch.setattr("builtins.input", visible_input(iter(["n"])))

    code = emsctl.main(["--config", str(config_path), "config", "init"])

    output = capsys.readouterr()
    assert code == 0
    assert "This config still looks like the default template." in output.out
    assert "The setup assistant will fill it with your answers." in output.out
    assert "Continue? [Y/n]" in output.out


def test_config_init_required_placeholders_are_not_prompt_defaults(
    monkeypatch,
    capsys,
):
    responses = iter(["", "REAL_SN"])
    monkeypatch.setattr("builtins.input", visible_input(responses))

    value = config_init_mod.ask_text(
        "Device 1 serial number",
        "YOUR_SN",
        required=True,
    )

    output = capsys.readouterr()
    assert value == "REAL_SN"
    assert "Device 1 serial number [YOUR_SN]" not in output.out
    assert "Device 1 serial number: " in output.out
    assert "Please enter a value." in output.out


def test_config_init_yes_rejects_template_placeholders(tmp_path):
    config_path = tmp_path / "config.json"
    shutil.copy(ROOT / "config.template.json", config_path)

    result = run_emsctl(tmp_path, "config", "init", "--yes")

    assert result.returncode == 2
    assert "Grid meter IP address is required" in result.stderr


def test_config_init_edited_config_preserves_unknown_keys(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "custom_top": {"keep": True},
        "system": {"max_total_power": 777, "min_output_limit": 40},
        "grid_meter": {
            "type": "shelly",
            "ip": "192.0.2.10",
            "custom_meter": "keep",
        },
        "devices": [{
            "name": "REAL",
            "ip": "192.0.2.20",
            "sn": "REAL_SN",
            "custom_device": "keep",
        }],
    }))

    code = emsctl.main([
        "--config",
        str(config_path),
        "config",
        "init",
        "--yes",
        "--no-backup",
    ])

    assert code == 0
    updated = json.loads(config_path.read_text())
    assert updated["custom_top"] == {"keep": True}
    assert updated["grid_meter"]["custom_meter"] == "keep"
    assert updated["devices"][0]["custom_device"] == "keep"
    assert updated["devices"][0]["name"] == "REAL"
    assert updated["system"]["max_total_power"] == 777


def test_config_init_cleans_stale_grid_meter_fields_when_switching_type(
    tmp_path,
    monkeypatch,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "system": {"max_total_power": 777, "min_output_limit": 40},
        "grid_meter": {
            "type": "tasmota_http",
            "url": "http://tasmota.local/cm?cmnd=Status%2010",
            "power_path": "StatusSNS.SML.Power_curr",
            "custom_meter": "keep",
        },
        "devices": [{
            "name": "REAL",
            "ip": "192.0.2.20",
            "sn": "REAL_SN",
        }],
    }))
    responses = iter(["y", "1", "192.0.2.50", *([""] * 18), "y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    code = emsctl.main(["--config", str(config_path), "config", "init"])

    assert code == 0
    updated = json.loads(config_path.read_text())
    assert updated["grid_meter"]["type"] == "shelly"
    assert updated["grid_meter"]["ip"] == "192.0.2.50"
    assert "url" not in updated["grid_meter"]
    assert "power_path" not in updated["grid_meter"]
    assert "mqtt" not in updated["grid_meter"]
    assert updated["grid_meter"]["custom_meter"] == "keep"


def test_config_init_edited_config_asks_for_backup_by_default(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "custom": True,
        "system": {
            "enabled": True,
            "max_total_power": 900,
            "min_output_limit": 35,
        },
        "dashboard": {"enabled": True},
        "winter": {"enabled": False},
        "battery_full_charge_assist": {"enabled": False},
        "influxdb": {"enabled": False},
        "ha": {"enabled": False},
        "grid_meter": {"type": "shelly", "ip": "192.0.2.10"},
        "devices": [{
            "name": "WR1",
            "ip": "192.0.2.20",
            "sn": "REAL_SN",
            "max_power": 800,
            "pv_kwp": 1.0,
            "battery_kwh": 1.0,
            "min_soc": 15,
            "max_soc": 100,
        }],
    }))
    responses = iter(["y", *([""] * 19), "y", "n"])
    monkeypatch.setattr("builtins.input", visible_input(responses))

    code = emsctl.main(["--config", str(config_path), "config", "init"])

    output = capsys.readouterr()
    assert code == 0
    assert "Existing config detected." in output.out
    assert "Create backup before writing? [Y/n]" in output.out


def test_config_init_noninteractive_edited_config_requires_backup_policy(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)

    result = run_emsctl(tmp_path, "config", "init", "--yes")

    assert result.returncode == 2
    assert "--backup or --no-backup" in result.stderr


def test_config_init_grid_meter_choices_are_runtime_supported():
    assert config_init_mod.SUPPORTED_GRID_METER_TYPES == (
        "shelly",
        "shelly_3em_gen1",
        "ecotracker",
        "tasmota_http",
        "zendure_smartmeter_d0",
        "mqtt",
    )


def test_config_init_zendure_smartmeter_d0_generates_mqtt_topic(monkeypatch):
    responses = iter([
        "5",
        "mqtt.local",
        "",
        "",
        "D0SN",
        "y",
        "10",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))
    monkeypatch.setattr(config_init_mod.getpass, "getpass", lambda prompt: "")

    result = config_init_mod.ask_grid_meter({})

    assert result == {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "host": "mqtt.local",
            "port": 1883,
            "username": "",
            "password": "",
            "topic": "Zendure/sensor/D0SN/totalPower",
            "payload_format": "number",
            "max_age_seconds": 10,
        },
    }


def test_config_init_generic_mqtt_keeps_advanced_payload_flow(monkeypatch):
    responses = iter([
        "6",
        "mqtt.local",
        "",
        "",
        "meter/grid",
        "json",
        "power.total",
        "10",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))
    monkeypatch.setattr(config_init_mod.getpass, "getpass", lambda prompt: "")

    result = config_init_mod.ask_grid_meter({})

    assert result == {
        "type": "mqtt",
        "mqtt": {
            "host": "mqtt.local",
            "port": 1883,
            "username": "",
            "password": "",
            "topic": "meter/grid",
            "payload_format": "json",
            "value_path": "power.total",
            "max_age_seconds": 10,
        },
    }


def test_config_init_mqtt_password_prompt_keeps_default_without_exposing_it(
    monkeypatch,
    capsys,
):
    password = "super-secret-mqtt-password"
    responses = iter(["", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    def press_enter(prompt):
        print(prompt, end="")
        return ""

    monkeypatch.setattr(config_init_mod.getpass, "getpass", press_enter)
    result = config_init_mod.ask_grid_meter({
        "type": "mqtt",
        "mqtt": {
            "host": "mqtt.local",
            "port": 1883,
            "username": "meter",
            "password": password,
            "topic": "meter/grid",
            "payload_format": "number",
            "max_age_seconds": 15,
        },
    })

    output = capsys.readouterr()
    assert result["mqtt"]["password"] == password
    assert password not in output.out + output.err
    assert "configured, press Enter to keep" in output.out


def test_config_init_mqtt_new_password_is_stored_without_being_printed(
    monkeypatch,
    capsys,
):
    password = "new-super-secret-mqtt-password"
    responses = iter(["6", "mqtt.local", "", "", "meter/grid", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))
    monkeypatch.setattr(
        config_init_mod.getpass,
        "getpass",
        lambda prompt: password,
    )

    result = config_init_mod.ask_grid_meter({})

    output = capsys.readouterr()
    assert result["mqtt"]["password"] == password
    assert password not in output.out + output.err


def test_config_init_dry_run_redacts_home_assistant_token(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["ha"]["enabled"] = True
    config["ha"]["token"] = "super-secret-token"
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "config", "init", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "super-secret-token" not in result.stdout
    assert "<redacted>" in result.stdout


def test_config_init_dry_run_redacts_mqtt_password(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["grid_meter"] = {
        "type": "mqtt",
        "mqtt": {
            "host": "mqtt.local",
            "port": 1883,
            "username": "meter",
            "password": "super-secret-mqtt-password",
            "topic": "meter/grid",
            "payload_format": "number",
            "max_age_seconds": 15,
        },
    }
    config_path.write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "config", "init", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "super-secret-mqtt-password" not in result.stdout
    assert "<redacted>" in result.stdout


def write_upgrade_candidate(path):
    path.write_text(json.dumps({
        "ha": {"enabled": True},
        "system": {
            "enabled": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
        },
        "devices": [],
        "shelly": {"ip": "192.168.1.50"},
    }))


def write_current_config_with_outdated_comment(path):
    template_text = (ROOT / "config.template.json").read_text()
    config = json.loads(template_text)
    config["devices"] = []
    config["system"]["_comment"] = "Outdated system comment."
    layout = cfg._extract_template_layout(template_text)
    cfg.write_config_json_atomic(str(path), config, layout=layout)
    return config


def prepare_config_upgrade_base(tmp_path, monkeypatch):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)
    return config_path


def prepare_config_upgrade_with_outdated_comment(tmp_path, monkeypatch):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    config = json.loads(config_path.read_text())
    config["system"]["_comment"] = "Outdated system comment."
    config_path.write_text(json.dumps(config))
    return config_path


def test_config_upgrade_dry_run_reports_missing_keys(tmp_path):
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)

    result = run_emsctl(tmp_path, "config", "upgrade", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Config upgrade plan:" in result.stdout
    assert "Schema migrations:" in result.stdout
    assert "1 -> 2" in result.stdout
    assert "2 -> 3" in result.stdout
    assert "dashboard.animation_mode" in result.stdout
    assert "grid_meter.ip" in result.stdout
    assert "No existing user values will be overwritten." in result.stdout
    assert "Missing keys will be added from config.template.json." in result.stdout
    assert "Review live-write settings before restarting EMS." in result.stdout


def test_config_upgrade_dry_run_rejects_missing_config(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.json"
    calls = []
    monkeypatch.setattr(
        emsctl.backup_mod,
        "create_config_backup",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    code = emsctl.main([
        "--config",
        str(missing),
        "config",
        "upgrade",
        "--dry-run",
    ])

    output = capsys.readouterr()
    assert code != 0
    assert not missing.exists()
    assert calls == []
    assert f"config file does not exist: {missing}" in output.err
    assert "Copy config.template.json first" in output.err


def test_config_upgrade_apply_rejects_missing_config(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.json"
    calls = []
    monkeypatch.setattr(
        emsctl.backup_mod,
        "create_config_backup",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    code = emsctl.main([
        "--config",
        str(missing),
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    ])

    output = capsys.readouterr()
    assert code != 0
    assert not missing.exists()
    assert calls == []
    assert f"config file does not exist: {missing}" in output.err
    assert "Copy config.template.json first" in output.err


def test_config_upgrade_dry_run_does_not_write_file(tmp_path):
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)
    original = config_path.read_text()

    result = run_emsctl(tmp_path, "config", "upgrade", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == original


def test_config_upgrade_interactive_offers_backup_before_write(
    tmp_path,
    monkeypatch,
):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    calls = []

    def fake_backup(*args, **kwargs):
        calls.append(kwargs)
        return str(tmp_path / "backup" / "ems-config-manual-test.tar.gz")

    responses = iter(["y", "y", "n"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(emsctl.backup_mod, "create_config_backup", fake_backup)

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert calls
    assert json.loads(config_path.read_text())["config_schema_version"] == 3


def test_config_upgrade_interactive_can_write_without_backup(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    calls = []

    responses = iter(["y", "n"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(
        emsctl.backup_mod,
        "create_config_backup",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert calls == []
    output = capsys.readouterr().out
    assert "No existing user values will be overwritten." in output
    assert "Missing keys will be added from config.template.json." in output
    assert "Review live-write settings before restarting EMS." in output
    assert "Continuing without backup" in output
    assert json.loads(config_path.read_text())["config_schema_version"] == 3


def test_config_upgrade_uses_existing_backup_tool_when_selected(
    tmp_path,
    monkeypatch,
):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    calls = []

    def fake_backup(config, **kwargs):
        calls.append((config, kwargs))
        return str(tmp_path / "backup" / "ems-config-manual-test.tar.gz")

    monkeypatch.setattr(emsctl.backup_mod, "create_config_backup", fake_backup)

    code = emsctl.main([
        "--config",
        str(config_path),
        "config",
        "upgrade",
        "--yes",
        "--backup",
    ])

    assert code == 0
    assert calls
    assert calls[0][1]["backup_purpose"] == "manual"


def test_config_upgrade_aborts_if_selected_backup_fails(tmp_path, monkeypatch):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    original = config_path.read_text()

    def fail_backup(*args, **kwargs):
        raise emsctl.backup_mod.BackupError("boom")

    monkeypatch.setattr(emsctl.backup_mod, "create_config_backup", fail_backup)

    code = emsctl.main([
        "--config",
        str(config_path),
        "config",
        "upgrade",
        "--yes",
        "--backup",
    ])

    assert code == 1
    assert config_path.read_text() == original


def test_config_upgrade_noninteractive_requires_explicit_backup_policy(tmp_path):
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)
    original = config_path.read_text()

    result = run_emsctl(tmp_path, "config", "upgrade", "--yes")

    assert result.returncode == 2
    assert "--backup or --no-backup" in result.stderr
    assert config_path.read_text() == original


def test_config_upgrade_noninteractive_with_backup_uses_backup_tool(
    tmp_path,
    monkeypatch,
):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        emsctl.backup_mod,
        "create_config_backup",
        lambda config, **kwargs: calls.append(kwargs)
        or str(tmp_path / "backup" / "ems-config-manual-test.tar.gz"),
    )

    code = emsctl.main([
        "--config",
        str(config_path),
        "config",
        "upgrade",
        "--yes",
        "--backup",
    ])

    assert code == 0
    assert calls
    assert json.loads(config_path.read_text())["config_schema_version"] == 3


def test_config_upgrade_noninteractive_no_backup_writes_without_backup(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = prepare_config_upgrade_base(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(
        emsctl.backup_mod,
        "create_config_backup",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    code = emsctl.main([
        "--config",
        str(config_path),
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    ])

    assert code == 0
    assert calls == []
    assert "Continuing without backup" in capsys.readouterr().out
    assert json.loads(config_path.read_text())["config_schema_version"] == 3


def test_encrypted_backup_inspect_and_restore_accept_stdin_password(tmp_path):
    backup_dir = tmp_path / "backups"
    password = "stdin-backup-password"

    create = run_emsctl(
        tmp_path,
        "backup",
        "create",
        "--type",
        "config",
        "--output-dir",
        str(backup_dir),
        "--password",
        input_text=f"{password}\n{password}\n",
    )
    assert create.returncode == 0, create.stderr
    assert password not in create.stdout + create.stderr
    archive = next(backup_dir.glob("*.tar.gz.enc"))

    inspect = run_emsctl(
        tmp_path,
        "backup",
        "inspect",
        str(archive),
        input_text=f"{password}\n",
    )
    assert inspect.returncode == 0, inspect.stderr
    assert "encrypted:  True" in inspect.stdout
    assert password not in inspect.stdout + inspect.stderr

    restore = run_emsctl(
        tmp_path,
        "backup",
        "restore",
        str(archive),
        "--dry-run",
        "--on-conflict",
        "keep",
        "--no-rollback",
        input_text=f"{password}\n",
    )
    assert restore.returncode == 0, restore.stderr
    assert "Dry run: no files were changed" in restore.stdout
    assert password not in restore.stdout + restore.stderr

    wrong = run_emsctl(
        tmp_path,
        "backup",
        "restore",
        str(archive),
        "--dry-run",
        "--on-conflict",
        "keep",
        "--no-rollback",
        input_text="wrong-password\n",
    )
    assert wrong.returncode != 0
    assert "incorrect password or corrupted backup" in wrong.stderr


def strip_comment_keys(value):
    if isinstance(value, dict):
        return {
            key: strip_comment_keys(item)
            for key, item in value.items()
            if not key.startswith("_comment")
        }
    if isinstance(value, list):
        return [strip_comment_keys(item) for item in value]
    return value


def test_config_upgrade_dry_run_reports_comment_only_changes(tmp_path):
    template = json.loads((ROOT / "config.template.json").read_text())
    config = strip_comment_keys(template)
    config["devices"] = []
    (tmp_path / "config.json").write_text(json.dumps(config))

    result = run_emsctl(tmp_path, "config", "upgrade", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Config is already up to date." not in result.stdout
    assert "Add explanatory comments:" in result.stdout


def test_config_upgrade_dry_run_reports_outdated_comments(tmp_path):
    config_path = tmp_path / "config.json"
    original = write_current_config_with_outdated_comment(config_path)

    result = run_emsctl(tmp_path, "config", "upgrade", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Outdated comment entries: 1" in result.stdout
    assert json.loads(config_path.read_text()) == original


def test_config_upgrade_yes_does_not_refresh_outdated_comments(tmp_path):
    config_path = tmp_path / "config.json"
    write_current_config_with_outdated_comment(config_path)

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 0, result.stderr
    assert "Refresh explanatory comments" not in result.stdout
    assert json.loads(config_path.read_text())["system"]["_comment"] == (
        "Outdated system comment."
    )


def test_config_upgrade_interactive_refreshes_comments_without_second_backup(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = prepare_config_upgrade_with_outdated_comment(tmp_path, monkeypatch)
    backups = []
    writes = []
    original_write = cfg.write_config_json_atomic

    def fake_write(path, data, *, layout=None):
        writes.append(data)
        original_write(path, data, layout=layout)

    responses = iter(["y", "n", "y"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(
        emsctl,
        "create_upgrade_backup",
        lambda args, config: backups.append(config)
        or (str(tmp_path / "backup.tar.gz"), "ok"),
    )
    monkeypatch.setattr(emsctl.config_mod, "write_config_json_atomic", fake_write)

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert backups == []
    assert len(writes) == 1
    output = capsys.readouterr().out
    assert "Refresh explanatory comments as part of this upgrade?" in output
    assert "Continuing without backup. Existing config.json will be modified." in output
    assert "Backup created" not in output
    assert "Refreshed explanatory comments: 1" in output
    upgraded = json.loads(config_path.read_text())
    template = json.loads((ROOT / "config.template.json").read_text())
    assert upgraded["config_schema_version"] == 3
    assert upgraded["system"]["_comment"] == template["system"]["_comment"]


def test_config_upgrade_interactive_creates_one_backup_before_combined_write(
    tmp_path,
    monkeypatch,
):
    config_path = prepare_config_upgrade_with_outdated_comment(tmp_path, monkeypatch)
    events = []
    original_write = cfg.write_config_json_atomic

    def fake_backup(args, config):
        events.append(("backup", config.get("config_schema_version")))
        return str(tmp_path / "backup.tar.gz"), "ok"

    def fake_write(path, data, *, layout=None):
        events.append(("write", data.get("config_schema_version")))
        original_write(path, data, layout=layout)

    responses = iter(["y", "y", "y"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(emsctl, "create_upgrade_backup", fake_backup)
    monkeypatch.setattr(emsctl.config_mod, "write_config_json_atomic", fake_write)

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert events == [("backup", None), ("write", 3)]
    upgraded = json.loads(config_path.read_text())
    template = json.loads((ROOT / "config.template.json").read_text())
    assert upgraded["config_schema_version"] == 3
    assert upgraded["system"]["_comment"] == template["system"]["_comment"]


def test_config_upgrade_interactive_refreshes_comment_only_without_backup(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    write_current_config_with_outdated_comment(config_path)
    backups = []
    prompts = []

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    responses = iter(["y", "n"])
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or next(responses),
    )
    monkeypatch.setattr(
        emsctl,
        "create_upgrade_backup",
        lambda args, config: backups.append(config)
        or (str(tmp_path / "backup.tar.gz"), "ok"),
    )

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert backups == []
    output = capsys.readouterr().out
    assert "Refresh explanatory comments from template?" in output
    assert any(
        "Create config backup before refreshing comments? [y/n]" in prompt
        for prompt in prompts
    )
    assert (
        "Continuing without backup. Existing config.json comments will be modified."
        in output
    )
    assert "Backup created" not in output
    assert "Refreshed explanatory comments: 1" in output
    refreshed = json.loads(config_path.read_text())
    template = json.loads((ROOT / "config.template.json").read_text())
    assert refreshed["system"]["_comment"] == template["system"]["_comment"]


def test_config_upgrade_interactive_refreshes_comment_only_with_backup(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    write_current_config_with_outdated_comment(config_path)
    backups = []

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(
        emsctl,
        "create_upgrade_backup",
        lambda args, config: backups.append(config)
        or (str(tmp_path / "backup.tar.gz"), "ok"),
    )

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert len(backups) == 1
    output = capsys.readouterr().out
    assert "Backup created" in output
    assert "Refreshed explanatory comments: 1" in output
    refreshed = json.loads(config_path.read_text())
    template = json.loads((ROOT / "config.template.json").read_text())
    assert refreshed["system"]["_comment"] == template["system"]["_comment"]


def test_config_upgrade_interactive_comment_only_aborts_on_backup_prompt(
    tmp_path,
    monkeypatch,
    capsys,
):
    patch_emsctl_base(monkeypatch, tmp_path)
    shutil.copy(ROOT / "config.template.json", tmp_path / "config.template.json")
    config_path = tmp_path / "config.json"
    original = write_current_config_with_outdated_comment(config_path)
    backups = []

    def fake_input(prompt):
        if "before refreshing comments" in prompt:
            raise EOFError
        return "y"

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(
        emsctl,
        "create_upgrade_backup",
        lambda args, config: backups.append(config)
        or (str(tmp_path / "backup.tar.gz"), "ok"),
    )

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert backups == []
    output = capsys.readouterr().out
    assert "Aborted." in output
    assert "Refreshed explanatory comments" not in output
    assert json.loads(config_path.read_text()) == original


def test_config_upgrade_interactive_declines_comment_refresh(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = prepare_config_upgrade_with_outdated_comment(tmp_path, monkeypatch)
    writes = []
    original_write = cfg.write_config_json_atomic

    def fake_write(path, data, *, layout=None):
        writes.append(data)
        original_write(path, data, layout=layout)

    responses = iter(["y", "n", "n"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    monkeypatch.setattr(emsctl.config_mod, "write_config_json_atomic", fake_write)

    code = emsctl.main(["--config", str(config_path), "config", "upgrade"])

    assert code == 0
    assert len(writes) == 1
    output = capsys.readouterr().out
    assert "Refresh explanatory comments as part of this upgrade?" in output
    assert "Refreshed explanatory comments" not in output
    upgraded = json.loads(config_path.read_text())
    assert upgraded["config_schema_version"] == 3
    assert upgraded["system"]["_comment"] == "Outdated system comment."


def test_config_upgrade_future_schema_aborts_without_writing(tmp_path):
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)
    config = json.loads(config_path.read_text())
    config["config_schema_version"] = 999
    config_path.write_text(json.dumps(config))
    original = config_path.read_text()

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 2
    assert "newer EMS version" in result.stderr
    assert config_path.read_text() == original


def test_config_upgrade_restores_removed_dashboard_block(tmp_path):
    config = json.loads((ROOT / "config.template.json").read_text())
    config.pop("dashboard")
    config["devices"] = []
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 0, result.stderr
    upgraded = json.loads(config_path.read_text())
    assert upgraded["dashboard"]["animation_mode"] == "normal"
    assert "_comment" in upgraded["dashboard"]


def test_config_upgrade_restores_removed_normal_key(tmp_path):
    config = json.loads((ROOT / "config.template.json").read_text())
    config["devices"] = []
    config["dashboard"]["host"] = "127.0.0.1"
    config["dashboard"].pop("animation_mode")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 0, result.stderr
    upgraded = json.loads(config_path.read_text())
    assert upgraded["dashboard"]["animation_mode"] == "normal"
    assert upgraded["dashboard"]["host"] == "127.0.0.1"


def test_config_upgrade_restores_removed_influx_key(tmp_path):
    config = json.loads((ROOT / "config.template.json").read_text())
    config["devices"] = []
    config["influxdb"]["enabled"] = True
    config["influxdb"].pop("raw_write_interval_seconds")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 0, result.stderr
    upgraded = json.loads(config_path.read_text())
    assert upgraded["influxdb"]["raw_write_interval_seconds"] == 0
    assert upgraded["influxdb"]["enabled"] is True


def test_config_upgrade_enriches_existing_device(tmp_path):
    config = json.loads((ROOT / "config.template.json").read_text())
    config["devices"] = [{
        "name": "REAL",
        "ip": "192.0.2.55",
        "sn": "REAL_SN",
    }]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 0, result.stderr
    device = json.loads(config_path.read_text())["devices"][0]
    assert device["name"] == "REAL"
    assert device["ip"] == "192.0.2.55"
    assert device["sn"] == "REAL_SN"
    assert device["max_power"] == 800
    assert "_comment_smart_mode" in device
    assert "_comment_soc" in device


def test_config_upgrade_is_idempotent_after_template_format_write(tmp_path):
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)

    first = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )
    assert first.returncode == 0, first.stderr
    after_first = config_path.read_text()

    second = run_emsctl(tmp_path, "config", "upgrade")

    assert second.returncode == 0, second.stderr
    assert "Config is already up to date." in second.stdout
    assert config_path.read_text() == after_first


def test_config_upgrade_preserves_config_permissions(tmp_path):
    config_path = tmp_path / "config.json"
    write_upgrade_candidate(config_path)
    config_path.chmod(0o600)

    result = run_emsctl(
        tmp_path,
        "config",
        "upgrade",
        "--yes",
        "--no-backup",
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
