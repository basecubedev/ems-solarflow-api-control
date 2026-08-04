# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI tests for bundled InfluxDB backup/restore (emsctl wiring).

Docker is never executed: ``run_docker_compose`` is monkeypatched to record the
commands it would run and to emulate the container side (``influx backup`` /
``cp``) by writing/reading the host staging directory.
"""

import os


import emsctl
import pytest

from ems import backup, influx_setup

pytestmark = [
    pytest.mark.backup_restore,
    pytest.mark.unit,
]


def bundled_config(**overrides):
    influx = {"enabled": True, "mode": "bundled", "org": "ems",
              "bucket_prefix": "ems", "token": "tok-abc"}
    influx.update(overrides)
    return {"influxdb": influx, "devices": []}


def fake_backup_runner(*, files=("backup.bolt.gz",)):
    def runner(influx_out_dir):
        for name in files:
            with open(os.path.join(influx_out_dir, name), "wb") as handle:
                handle.write(b"INFLUX-" + name.encode())

    return runner


def _scripted_prompt_text(answers):
    def fake(label, default=None):
        low = label.lower()
        for keyword, value in answers.items():
            if keyword in low:
                return value
        return default if default is not None else "n"

    return fake


def _args(**overrides):
    base = dict(
        config=None, runtime_state=None, dashboard_auth=None,
        action=None, file=None, diff_file=None, type="influxdb",
        compression_level=backup.DEFAULT_COMPRESSION_LEVEL,
        password=False, on_conflict=None, rollback=None, dry_run=False,
    )
    base.update(overrides)
    return emsctl.make_args(**base)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_cli_create_influxdb_skips_when_disabled(monkeypatch, capsys):
    config = {"influxdb": {"enabled": False}}
    rc = emsctl.handle_backup_create_influxdb(_args(), config)
    out = capsys.readouterr().out
    assert rc == 0
    assert "disabled" in out.lower()


def test_cli_create_influxdb_rejects_external(monkeypatch, capsys):
    config = {"influxdb": {"enabled": True, "mode": "external"}}
    rc = emsctl.handle_backup_create_influxdb(_args(), config)
    out = capsys.readouterr().out
    assert rc == 1
    assert "external" in out.lower()


def test_cli_create_influxdb_makes_archive(tmp_path, monkeypatch):
    config = bundled_config()
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        emsctl, "make_influx_backup_runner",
        lambda cfg, **kw: fake_backup_runner(),
    )

    rc = emsctl.handle_backup_create_influxdb(_args(), config)
    assert rc == 0
    backups = os.listdir(os.path.join(str(tmp_path), "data", "backups"))
    assert any(name.startswith("ems-influxdb-manual-") for name in backups)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def _make_influx_archive(tmp_path, config, *, password=None):
    return backup.create_influxdb_backup(
        config, base_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backup"),
        password=password,
        backup_runner=fake_backup_runner(),
    )


def test_influxdb_restore_rejects_external_mode(tmp_path, monkeypatch, capsys):
    archive = _make_influx_archive(tmp_path, bundled_config())
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)

    external = {"influxdb": {"enabled": True, "mode": "external"}}
    rc = emsctl.handle_backup_restore(
        _args(), external, archive, interactive=True
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "external" in out.lower()


def test_influxdb_restore_prompts_for_password_when_encrypted(
    tmp_path, monkeypatch
):
    config = bundled_config()
    archive = _make_influx_archive(tmp_path, config, password="srcpw123")
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)

    gp_calls = []
    monkeypatch.setattr(
        emsctl.getpass, "getpass",
        lambda prompt="": (gp_calls.append(prompt) or "srcpw123"),
    )
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({"create rollback": "n", "strategy": "r"}),
    )
    restored = {}
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner",
        lambda cfg, **kw: (lambda influx_dir: restored.setdefault(
            "files", os.listdir(influx_dir))),
    )

    rc = emsctl.handle_backup_restore(_args(), config, archive, interactive=True)
    assert rc == 0
    assert any("password" in p.lower() for p in gp_calls)
    assert restored["files"]


def test_influxdb_restore_requires_replace_confirmation_interactive(
    tmp_path, monkeypatch
):
    config = bundled_config()
    archive = _make_influx_archive(tmp_path, config)
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        emsctl, "prompt_text",
        # No rollback; do NOT confirm replace (answer abort).
        _scripted_prompt_text({"create rollback": "n", "strategy": "a"}),
    )
    called = {"restore": False}
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner",
        lambda cfg, **kw: (
            lambda influx_dir: called.__setitem__("restore", True)),
    )

    rc = emsctl.handle_backup_restore(_args(), config, archive, interactive=True)
    assert rc == 0
    assert called["restore"] is False  # restore must not run without confirm


def test_influxdb_restore_can_create_rollback_backup(tmp_path, monkeypatch):
    config = bundled_config()
    archive = _make_influx_archive(tmp_path, config)
    backup_dir = os.path.join(str(tmp_path), "data", "backups")
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({
            "create rollback": "y", "protect rollback": "n", "strategy": "r",
        }),
    )
    monkeypatch.setattr(
        emsctl, "make_influx_backup_runner",
        lambda cfg, **kw: fake_backup_runner(),
    )
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner",
        lambda cfg, **kw: (lambda influx_dir: None),
    )

    rc = emsctl.handle_backup_restore(_args(), config, archive, interactive=True)
    assert rc == 0
    rollbacks = [
        name for name in os.listdir(backup_dir)
        if name.startswith("ems-influxdb-rollback-")
    ]
    assert rollbacks


def test_influxdb_restore_rollback_can_be_encrypted(tmp_path, monkeypatch):
    config = bundled_config()
    archive = _make_influx_archive(tmp_path, config)
    backup_dir = os.path.join(str(tmp_path), "data", "backups")
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        emsctl.getpass, "getpass", lambda prompt="": "rbpw9999"
    )
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({
            "create rollback": "y", "protect rollback": "y", "strategy": "r",
        }),
    )
    monkeypatch.setattr(
        emsctl, "make_influx_backup_runner",
        lambda cfg, **kw: fake_backup_runner(),
    )
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner",
        lambda cfg, **kw: (lambda influx_dir: None),
    )

    rc = emsctl.handle_backup_restore(_args(), config, archive, interactive=True)
    assert rc == 0
    rollbacks = [
        name for name in os.listdir(backup_dir)
        if name.startswith("ems-influxdb-rollback-")
    ]
    assert rollbacks
    assert all(name.endswith(".tar.gz.enc") for name in rollbacks)
    rb = os.path.join(backup_dir, rollbacks[0])
    assert backup.is_encrypted(rb)
    assert backup.inspect_backup(rb)["manifest"] is None
    manifest = backup.inspect_backup(rb, password="rbpw9999")["manifest"]
    assert manifest["backup_type"] == "influxdb"
    assert manifest["backup_purpose"] == "rollback"


def test_influxdb_restore_runs_status_or_prints_required_followup(
    tmp_path, monkeypatch, capsys
):
    config = bundled_config()
    archive = _make_influx_archive(tmp_path, config)
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({"create rollback": "n", "strategy": "r"}),
    )
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner",
        lambda cfg, **kw: (lambda influx_dir: None),
    )

    rc = emsctl.handle_backup_restore(_args(), config, archive, interactive=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "influx status" in out
    assert "diagnose" in out


def test_influxdb_restore_non_interactive_requires_replace_flag(
    tmp_path, monkeypatch
):
    config = bundled_config()
    archive = _make_influx_archive(tmp_path, config)
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: False)
    called = {"restore": False}
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner",
        lambda cfg, **kw: (
            lambda influx_dir: called.__setitem__("restore", True)),
    )

    # No --on-conflict replace -> must refuse.
    rc = emsctl.handle_backup_restore(
        _args(rollback=False), config, archive, interactive=True
    )
    assert rc != 0
    assert called["restore"] is False


# ---------------------------------------------------------------------------
# runner command wiring (docker compose, mocked)
# ---------------------------------------------------------------------------

def test_backup_runner_uses_official_commands(tmp_path, monkeypatch):
    config = bundled_config()
    # Host-side runner test: pin host mode so it stays deterministic even when
    # the suite itself runs inside a generic container (where /.dockerenv exists).
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.setattr(emsctl, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(influx_setup, "ensure_data_dir", lambda *a, **k: "data/influxdb")
    commands = []

    def fake_run(command, cwd, dry_run=False, stdout_to_stderr=False):
        commands.append(command)
        if "cp" in command:
            # Emulate `docker compose cp influxdb:/...  <host_dir>`.
            host_dir = command[-1]
            os.makedirs(host_dir, exist_ok=True)
            with open(os.path.join(host_dir, "fake.bolt.gz"), "wb") as handle:
                handle.write(b"x")
        return 0

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_run)

    runner = emsctl.make_influx_backup_runner(config)
    out_dir = tmp_path / "influxdb"
    out_dir.mkdir()
    runner(str(out_dir))

    flat = [" ".join(c) for c in commands]
    assert any("up -d influxdb" in c for c in flat)
    assert any("influx backup" in c for c in flat)
    assert any("cp" in c.split() for c in flat)


# ---------------------------------------------------------------------------
# container-mode runners (Docker-first: direct influx CLI, no docker compose)
# ---------------------------------------------------------------------------

def _force_container(monkeypatch):
    monkeypatch.setattr(influx_setup, "is_container_runtime", lambda **k: True)


def _forbid_docker_compose(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("container mode must not call docker compose")

    monkeypatch.setattr(emsctl, "run_docker_compose", boom)


def test_container_backup_runner_uses_direct_cli(tmp_path, monkeypatch):
    config = bundled_config(url="http://influxdb:8086")
    _force_container(monkeypatch)
    _forbid_docker_compose(monkeypatch)
    calls = []

    def fake_cli(argv, env, *, json_output=False):
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(emsctl, "run_influx_cli", fake_cli)

    runner = emsctl.make_influx_backup_runner(config)
    out_dir = tmp_path / "influxdb"
    out_dir.mkdir()
    runner(str(out_dir))

    assert len(calls) == 1
    argv, env = calls[0]
    assert argv[:2] == ["influx", "backup"]
    assert str(out_dir) in argv
    assert env["INFLUX_HOST"] == "http://influxdb:8086"
    assert env["INFLUX_TOKEN"] == "tok-abc"


def test_container_restore_runner_uses_direct_cli(tmp_path, monkeypatch):
    config = bundled_config(url="http://influxdb:8086")
    _force_container(monkeypatch)
    _forbid_docker_compose(monkeypatch)
    calls = []

    def fake_cli(argv, env, *, json_output=False):
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(emsctl, "run_influx_cli", fake_cli)

    runner = emsctl.make_influx_restore_runner(config)
    in_dir = tmp_path / "influxdb"
    in_dir.mkdir()
    runner(str(in_dir))

    assert len(calls) == 1
    argv, env = calls[0]
    assert argv[:3] == ["influx", "restore", "--full"]
    assert str(in_dir) in argv
    assert env["INFLUX_TOKEN"] == "tok-abc"


def test_container_runner_keeps_token_out_of_argv_and_trace(tmp_path, monkeypatch, capsys):
    config = bundled_config(url="http://influxdb:8086", token="super-secret-token")
    _force_container(monkeypatch)
    _forbid_docker_compose(monkeypatch)

    captured = {}

    def fake_subprocess_run(argv, env=None, stdout=None, stderr=None):
        captured["argv"] = argv
        captured["env"] = env

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(emsctl.subprocess, "run", fake_subprocess_run)

    runner = emsctl.make_influx_backup_runner(config)
    out_dir = tmp_path / "influxdb"
    out_dir.mkdir()
    runner(str(out_dir))

    assert "super-secret-token" not in " ".join(captured["argv"])
    assert captured["env"]["INFLUX_TOKEN"] == "super-secret-token"
    trace = capsys.readouterr().err
    assert "super-secret-token" not in trace


def test_container_backup_runner_missing_token_raises(monkeypatch):
    config = bundled_config(url="http://influxdb:8086")
    config["influxdb"].pop("token")
    _force_container(monkeypatch)
    _forbid_docker_compose(monkeypatch)
    monkeypatch.setattr(influx_setup, "runtime_influx_token", lambda *a, **k: "")

    try:
        emsctl.make_influx_backup_runner(config)
    except backup.BackupError as exc:
        assert "token" in str(exc).lower()
    else:
        raise AssertionError("expected BackupError when no token resolves")


def test_container_runner_missing_url_raises(monkeypatch):
    config = bundled_config()
    config["influxdb"].pop("url", None)
    _force_container(monkeypatch)
    _forbid_docker_compose(monkeypatch)
    monkeypatch.setattr(influx_setup, "runtime_influx_url", lambda *a, **k: "")

    try:
        emsctl.make_influx_restore_runner(config)
    except backup.BackupError as exc:
        assert "url" in str(exc).lower()
    else:
        raise AssertionError("expected BackupError when URL cannot resolve")


def test_container_backup_runner_missing_cli_raises(tmp_path, monkeypatch):
    config = bundled_config(url="http://influxdb:8086")
    _force_container(monkeypatch)
    _forbid_docker_compose(monkeypatch)

    def missing(argv, env=None, stdout=None, stderr=None):
        raise FileNotFoundError("influx")

    monkeypatch.setattr(emsctl.subprocess, "run", missing)

    runner = emsctl.make_influx_backup_runner(config)
    out_dir = tmp_path / "influxdb"
    out_dir.mkdir()
    try:
        runner(str(out_dir))
    except backup.BackupError as exc:
        assert "influx" in str(exc).lower()
    else:
        raise AssertionError("expected BackupError when influx CLI is missing")
