# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local recovery CLI.

The web interface must never be the only way back into an appliance, so the
console password reset and the status summary are covered here. These tests
never touch the real host: they only exercise the CLI surface and the appliance
state directory.
"""

import json
import os

import pytest

from appliance.auth import AuthStore
from appliance.cli import (
    EXIT_ERROR,
    EXIT_OK,
    _status_summary,
    build_parser,
    command_backup_access,
    command_password_reset,
    main,
)
from appliance.paths import (
    ENV_CONFIG_DIR,
    ENV_INSTALL_ROOT,
    ENV_LOG_DIR,
    ENV_RUNTIME_DIR,
    ENV_STATE_DIR,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation]


@pytest.fixture
def appliance_env(tmp_path, monkeypatch):
    for variable, name in (
        (ENV_INSTALL_ROOT, "opt"),
        (ENV_CONFIG_DIR, "etc"),
        (ENV_STATE_DIR, "state"),
        (ENV_LOG_DIR, "log"),
        (ENV_RUNTIME_DIR, "run"),
    ):
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(variable, str(directory))
    return tmp_path


class Args:
    def __init__(self, **values):
        self.__dict__.update(values)


def test_the_documented_commands_exist():
    parser = build_parser()
    actions = [
        action
        for action in parser._subparsers._group_actions[0].choices  # noqa: SLF001
    ]
    for expected in ("status", "repair", "password-reset", "rollback-manager", "agent", "web"):
        assert expected in actions


def test_shared_flags_work_before_and_after_the_subcommand():
    parser = build_parser()
    assert parser.parse_args(["--json", "status"]).json is True
    assert parser.parse_args(["status", "--json"]).json is True
    assert parser.parse_args(["status"]).json is False
    assert parser.parse_args(["--local", "repair", "--apply"]).local is True
    assert parser.parse_args(["repair", "--local"]).local is True


def test_status_summary_is_a_compact_operator_view():
    summary = _status_summary(
        {
            "appliance_version": "0.1.0",
            "health": {"level": "degraded", "warnings": [{"code": "docker_not_running"}]},
            "system": {
                "hardware": {"model": "Raspberry Pi 5"},
                "operating_system": {"name": "Raspberry Pi OS"},
                "uptime": {"days": 12},
                "temperature": {"celsius": 51.2},
            },
            "docker": {"daemon": {"state": "stopped"}},
            "admin": {"version": "v1.0.0", "healthy": False},
            "updates": {"security_count": 2, "reboot_required": True},
        }
    )
    assert summary["health"] == "degraded"
    assert summary["model"] == "Raspberry Pi 5"
    assert summary["docker"] == "stopped"
    assert summary["security_updates"] == 2
    assert summary["warnings"] == ["docker_not_running"]


def test_status_summary_tolerates_missing_sections():
    summary = _status_summary({})
    assert summary["health"] is None
    assert summary["warnings"] == []


def test_password_reset_writes_a_new_password_and_rotates_the_generation(appliance_env, capsys):
    from appliance.paths import resolve_paths

    store = AuthStore(resolve_paths().auth_file, iterations=1000)
    store.create("first-appliance-secret")
    first_generation = store.generation()

    exit_code = command_password_reset(Args(password="second-appliance-secret", json=False))

    assert exit_code == 0
    assert store.verify("second-appliance-secret") is True
    assert store.verify("first-appliance-secret") is False
    assert store.generation() != first_generation
    assert "invalidated" in capsys.readouterr().out


def test_password_reset_refuses_a_short_password(appliance_env, capsys):
    exit_code = command_password_reset(Args(password="short", json=False))
    assert exit_code == 1
    assert "at least" in capsys.readouterr().err


def test_password_reset_creates_the_state_directory_when_missing(tmp_path, monkeypatch):
    for variable, name in (
        (ENV_INSTALL_ROOT, "opt"),
        (ENV_CONFIG_DIR, "etc"),
        (ENV_STATE_DIR, "state"),
        (ENV_LOG_DIR, "log"),
        (ENV_RUNTIME_DIR, "run"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))
    assert command_password_reset(Args(password="a-fresh-appliance-secret", json=False)) == 0
    assert (tmp_path / "state" / "web" / "auth" / "auth.json").is_file()


def test_allowlist_command_prints_the_agent_operations(appliance_env, capsys):
    assert main(["--json", "allowlist"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "status.get" in payload["operations"]
    assert "admin.plan_install" in payload["operations"]
    assert not [name for name in payload["operations"] if "exec" in name and "operations" not in name]


def test_a_disable_that_did_not_revoke_anything_reports_failure(appliance_env, monkeypatch):
    """``prerm`` treats a zero exit as proof that backup access is off.

    Without a provable account the service withdraws nothing, so reporting
    success there removes the package while the SSH backup account stays
    reachable.
    """

    class _Service:
        def disable(self, *, reason=""):
            return {
                "state": "degraded",
                "reason": reason,
                "authentication_disabled": False,
                "changed": False,
            }

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        "appliance.backup_confinement.build_activation", lambda **kwargs: _Service()
    )

    assert command_backup_access(Args(action="disable", json=True)) == EXIT_ERROR


def test_a_disable_that_withdrew_authentication_reports_success(appliance_env, monkeypatch):
    class _Service:
        def disable(self, *, reason=""):
            return {
                "state": "degraded",
                "reason": reason,
                "authentication_disabled": True,
                "changed": True,
            }

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        "appliance.backup_confinement.build_activation", lambda **kwargs: _Service()
    )

    assert command_backup_access(Args(action="disable", json=True)) == EXIT_OK
