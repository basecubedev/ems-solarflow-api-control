# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for emsctl root auto-drop UID/GID resolution in the Docker container."""

import os

import pytest

from ems import cli_privilege


def _mkdir_owned(path, uid, gid, monkeypatch):
    """Pretend ``path`` is owned by ``uid``/``gid`` via a fake os.stat."""
    real_stat = os.stat

    class _Info:
        st_uid = uid
        st_gid = gid

    def fake_stat(target, *args, **kwargs):
        if target == path:
            return _Info()
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(cli_privilege.os, "stat", fake_stat)


def test_explicit_puid_pgid_wins(monkeypatch):
    _mkdir_owned("/app/data", 4321, 4321, monkeypatch)
    env = {"PUID": "1000", "PGID": "1001"}
    assert cli_privilege.resolve_runtime_ids(env) == (1000, 1001)


def test_data_owner_used_when_no_puid(monkeypatch):
    _mkdir_owned("/app/data", 1500, 1600, monkeypatch)
    assert cli_privilege.resolve_runtime_ids({}) == (1500, 1600)


def test_config_owner_used_when_data_missing(monkeypatch):
    # /app/data not owned by a positive uid; /app/config is.
    _mkdir_owned("/app/config", 1700, 1700, monkeypatch)
    target = cli_privilege.resolve_runtime_ids(
        {}, data_dir="/nonexistent/data"
    )
    assert target == (1700, 1700)


def test_invalid_puid_falls_back_to_path_owner(monkeypatch):
    _mkdir_owned("/app/data", 1234, 1234, monkeypatch)
    env = {"PUID": "abc", "PGID": "0"}
    assert cli_privilege.resolve_runtime_ids(env) == (1234, 1234)


def test_no_target_when_nothing_resolvable(monkeypatch):
    monkeypatch.setattr(
        cli_privilege, "_run_as_user_ids", lambda *_: None
    )
    target = cli_privilege.resolve_runtime_ids(
        {}, data_dir="/nonexistent/data", config_dir="/nonexistent/config"
    )
    assert target is None


def test_run_as_user_fallback(monkeypatch):
    monkeypatch.setattr(
        cli_privilege, "_run_as_user_ids", lambda *_: (1313, 1313)
    )
    target = cli_privilege.resolve_runtime_ids(
        {}, data_dir="/nonexistent/data", config_dir="/nonexistent/config"
    )
    assert target == (1313, 1313)


def test_no_drop_when_not_in_container():
    env = {}
    assert cli_privilege.maybe_drop_privileges(env) is None


def test_no_drop_when_guard_set():
    env = {"EMS_CLI_PRIVILEGE_DROPPED": "1", "EMS_IN_CONTAINER": "1"}
    assert cli_privilege.maybe_drop_privileges(env) is None


def test_no_drop_when_not_root(monkeypatch):
    monkeypatch.setattr(cli_privilege.os, "geteuid", lambda: 1000)
    env = {"EMS_IN_CONTAINER": "1"}
    assert cli_privilege.maybe_drop_privileges(env) is None


def test_drop_sets_guard_and_calls_setids(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli_privilege.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        cli_privilege, "resolve_runtime_ids", lambda env: (1000, 1001)
    )
    monkeypatch.setattr(
        cli_privilege.os, "setgroups", lambda g: calls.setdefault("groups", g)
    )
    monkeypatch.setattr(
        cli_privilege.os, "setgid", lambda g: calls.setdefault("gid", g)
    )
    monkeypatch.setattr(
        cli_privilege.os, "setuid", lambda u: calls.setdefault("uid", u)
    )
    env = {"EMS_IN_CONTAINER": "1"}
    monkeypatch.setattr(cli_privilege.os, "environ", {})

    assert cli_privilege.maybe_drop_privileges(env) == (1000, 1001)
    assert env["EMS_CLI_PRIVILEGE_DROPPED"] == "1"
    assert calls == {"groups": [1001], "gid": 1001, "uid": 1000}


def test_error_when_target_is_root(monkeypatch):
    monkeypatch.setattr(cli_privilege.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        cli_privilege, "resolve_runtime_ids", lambda env: (0, 0)
    )
    env = {"EMS_IN_CONTAINER": "1"}
    with pytest.raises(cli_privilege.PrivilegeDropError):
        cli_privilege.maybe_drop_privileges(env)


def test_error_when_no_target_resolvable(monkeypatch):
    monkeypatch.setattr(cli_privilege.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli_privilege, "resolve_runtime_ids", lambda env: None)
    env = {"EMS_IN_CONTAINER": "1"}
    with pytest.raises(cli_privilege.PrivilegeDropError):
        cli_privilege.maybe_drop_privileges(env)
