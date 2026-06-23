# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the bundled InfluxDB zero-config setup helpers and CLI wiring.

Docker is never executed here: the host-side docker compose runner and the
InfluxDB schema operation are mocked so these stay deterministic and offline.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import emsctl
from ems import influx_setup
from ems.config import normalize_influxdb_config

ROOT = Path(__file__).resolve().parents[1]
EMSCTL = ROOT / "emsctl.py"


def bundled_config(**overrides):
    cfg = {"enabled": True, "mode": "bundled"}
    cfg.update(overrides)
    return normalize_influxdb_config(cfg)


# --- secret env file generation / merge -------------------------------------


def test_ensure_secret_file_creates_with_generated_secrets(tmp_path):
    cfg = bundled_config()
    report = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))

    path = Path(report["path"])
    assert path.exists()
    assert report["created"] is True

    values = influx_setup.parse_env_file(path.read_text())
    # All managed keys present and non-empty.
    for key in (
        "INFLUXDB_ORG",
        "INFLUXDB_TOKEN",
        "DOCKER_INFLUXDB_INIT_PASSWORD",
        "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN",
        "DOCKER_INFLUXDB_INIT_BUCKET",
    ):
        assert values.get(key), key

    # Admin token mirrors the API token on a fresh install.
    assert values["INFLUXDB_TOKEN"] == values["DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"]
    # Init bucket derives from bucket_prefix + "_raw".
    assert values["DOCKER_INFLUXDB_INIT_BUCKET"] == "ems_raw"


def test_secret_file_uses_restrictive_permissions(tmp_path):
    cfg = bundled_config()
    report = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    mode = stat.S_IMODE(os.stat(report["path"]).st_mode)
    # Owner-only; no group/other access.
    assert mode & 0o077 == 0


def test_init_bucket_uses_custom_prefix(tmp_path):
    cfg = bundled_config(bucket_prefix="house1", org="home")
    report = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    values = influx_setup.parse_env_file(Path(report["path"]).read_text())
    assert values["DOCKER_INFLUXDB_INIT_BUCKET"] == "house1_raw"
    assert values["INFLUXDB_ORG"] == "home"


def test_ensure_secret_file_preserves_existing_secrets(tmp_path):
    cfg = bundled_config()
    first = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    before = influx_setup.parse_env_file(Path(first["path"]).read_text())

    second = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    after = influx_setup.parse_env_file(Path(second["path"]).read_text())

    assert second["created"] is False
    assert second["generated_keys"] == []
    assert after["INFLUXDB_TOKEN"] == before["INFLUXDB_TOKEN"]
    assert (
        after["DOCKER_INFLUXDB_INIT_PASSWORD"]
        == before["DOCKER_INFLUXDB_INIT_PASSWORD"]
    )


def test_missing_keys_appended_without_overwriting(tmp_path):
    secret_path = tmp_path / "deploy" / "docker" / "influxdb.env"
    secret_path.parent.mkdir(parents=True)
    # Pre-existing file with only a token; other keys missing.
    secret_path.write_text("INFLUXDB_TOKEN=preexisting-token\n")

    cfg = bundled_config()
    report = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    values = influx_setup.parse_env_file(secret_path.read_text())

    assert values["INFLUXDB_TOKEN"] == "preexisting-token"
    # Admin token mirrors the existing token, not a fresh random value.
    assert values["DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"] == "preexisting-token"
    # A password was generated since it was missing.
    assert values["DOCKER_INFLUXDB_INIT_PASSWORD"]
    assert "DOCKER_INFLUXDB_INIT_PASSWORD" in report["generated_keys"]
    assert "INFLUXDB_TOKEN" not in report["generated_keys"]


def test_unknown_keys_preserved(tmp_path):
    secret_path = tmp_path / "deploy" / "docker" / "influxdb.env"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text("CUSTOM_KEY=keepme\n")

    cfg = bundled_config()
    influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    values = influx_setup.parse_env_file(secret_path.read_text())
    assert values["CUSTOM_KEY"] == "keepme"


def test_report_summary_redacts_secrets(tmp_path):
    cfg = bundled_config()
    report = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    summary = report["summary"]
    for key in influx_setup.SECRET_KEYS:
        assert "redacted" in summary[key]
    # Non-secret values are shown plainly.
    assert summary["DOCKER_INFLUXDB_INIT_BUCKET"] == "ems_raw"


def test_public_influx_env_report_redacts_secret_fields(tmp_path):
    cfg = bundled_config()
    report = influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    public = emsctl.public_influx_env_report(report)

    assert public["relative_path"] == report["relative_path"]
    assert public["created"] is True
    assert public["generated_keys"] == ["<redacted>"] * len(report["generated_keys"])
    for key in influx_setup.SECRET_KEYS:
        assert public["summary"][key] == "<redacted>"
    assert public["summary"]["DOCKER_INFLUXDB_INIT_BUCKET"] == "ems_raw"


def test_read_secret_file_token(tmp_path):
    cfg = bundled_config()
    influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    token = influx_setup.read_secret_file_token(cfg, base_dir=str(tmp_path))
    assert token
    # Matches the value written to the file.
    values = influx_setup.parse_env_file(
        (tmp_path / "deploy" / "docker" / "influxdb.env").read_text()
    )
    assert token == values["INFLUXDB_TOKEN"]


def test_read_secret_file_token_missing_file(tmp_path):
    cfg = bundled_config()
    assert influx_setup.read_secret_file_token(cfg, base_dir=str(tmp_path)) == ""


# --- compose file selection / command construction --------------------------


def test_compose_files_bundled_includes_overlay():
    files = influx_setup.compose_files(bundled_config())
    assert files == [
        "docker-compose.example.yml",
        "deploy/docker/compose.influxdb.yml",
        "deploy/docker/compose.ems-influx-env.yml",
    ]


def test_compose_files_disabled_base_only():
    files = influx_setup.compose_files(normalize_influxdb_config({"enabled": False}))
    assert files == ["docker-compose.example.yml"]


def test_compose_files_external_base_only():
    files = influx_setup.compose_files(
        normalize_influxdb_config({"enabled": True, "mode": "external"})
    )
    assert files == ["docker-compose.example.yml"]


def test_build_compose_command():
    files = ["a.yml", "b.yml"]
    cmd = influx_setup.build_compose_command(files, action=("up", "-d"))
    assert cmd == ["docker", "compose", "-f", "a.yml", "-f", "b.yml", "up", "-d"]


# --- bundled InfluxDB local data directory ----------------------------------


def test_compose_influxdb_uses_local_bind_mount():
    # The bundled InfluxDB compose file must bind-mount ./data/influxdb and must
    # not declare a Docker named volume for it anymore.
    text = (ROOT / "deploy" / "docker" / "compose.influxdb.yml").read_text()
    assert "./data/influxdb:/var/lib/influxdb2" in text
    # No top-level named-volume definition / mapping for bundled data.
    assert "influxdb-data:/var/lib/influxdb2" not in text
    import re
    # No `volumes:` top-level key defining a named volume (only the service-level
    # `volumes:` list mapping the bind mount should remain).
    assert not re.search(r"(?m)^volumes:\s*$", text)


def test_ensure_data_dir_creates_idempotently(tmp_path):
    rel = influx_setup.ensure_data_dir(base_dir=str(tmp_path))
    assert rel == "data/influxdb"
    assert (tmp_path / "data" / "influxdb").is_dir()
    # Second call is a no-op and still returns the relative path.
    assert influx_setup.ensure_data_dir(base_dir=str(tmp_path)) == "data/influxdb"
    assert (tmp_path / "data" / "influxdb").is_dir()


# --- host-side URL + secret-file path helpers -------------------------------


def test_host_cli_url_bundled_uses_host_url():
    cfg = bundled_config(host_url="http://127.0.0.1:8086", url="http://influxdb:8086")
    assert influx_setup.host_cli_url(cfg) == "http://127.0.0.1:8086"


def test_host_cli_url_bundled_defaults_when_missing():
    # Simulate a legacy config dict that lost host_url after normalization.
    cfg = dict(bundled_config())
    cfg["host_url"] = ""
    assert influx_setup.host_cli_url(cfg) == "http://127.0.0.1:8086"


def test_host_cli_url_external_uses_url():
    cfg = normalize_influxdb_config(
        {"enabled": True, "mode": "external", "url": "http://192.168.1.5:8086"}
    )
    assert influx_setup.host_cli_url(cfg) == "http://192.168.1.5:8086"


def test_uses_default_secret_file_true_for_default():
    assert influx_setup.uses_default_secret_file(bundled_config()) is True


def test_uses_default_secret_file_false_for_custom():
    cfg = bundled_config(secret_file="deploy/docker/other.env")
    assert influx_setup.uses_default_secret_file(cfg) is False


# --- CLI behavior (docker + schema sync mocked) -----------------------------


def fake_schema_op(report=None):
    """A drop-in for execute_influx_schema_op returning (code, result)."""
    report = report if report is not None else {"buckets": [], "tasks": []}

    def _op(influx_config, action):
        return 0, {"action": action, "ok": True, "url": "", "report": report}

    return _op


def init_args(**overrides):
    values = {
        "action": "init",
        "json": False,
        "no_start": False,
        "no_sync": False,
        "force_disabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def stack_args(**overrides):
    values = {
        "stack_action": "up",
        "json": False,
        "no_sync": False,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def patch_base(monkeypatch, tmp_path):
    monkeypatch.setattr(influx_setup, "BASE_DIR", str(tmp_path))
    return tmp_path


def test_influx_init_fails_when_disabled(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    rc = emsctl.handle_influx_command(
        init_args(), {"influxdb": {"enabled": False}}
    )
    assert rc == 2


def test_influx_init_force_disabled_creates_secrets(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(force_disabled=True, no_start=True),
        {"influxdb": {"enabled": False, "mode": "bundled"}},
    )
    assert rc == 0
    assert (patch_base / "deploy" / "docker" / "influxdb.env").exists()


def test_influx_init_force_disabled_without_no_start_fails(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(force_disabled=True),
        {"influxdb": {"enabled": False, "mode": "bundled"}},
    )
    # Disabled + start requested is rejected (compose has no influxdb service).
    assert rc == 2
    assert not (patch_base / "deploy" / "docker" / "influxdb.env").exists()


def test_influx_init_disabled_prints_helpful_message(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(init_args(), {"influxdb": {"enabled": False}})
    assert rc == 2
    err = capsys.readouterr().err
    assert "InfluxDB is disabled in config.json" in err
    # No Docker services and no secret file when disabled.
    assert not (patch_base / "deploy" / "docker" / "influxdb.env").exists()


def test_influx_init_disabled_json_single_object(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    rc = emsctl.handle_influx_command(
        init_args(json=True), {"influxdb": {"enabled": False}}
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["command"] == "influx init"
    assert payload["started"] is False


def test_influx_init_external_validates_and_syncs(patch_base, monkeypatch):
    calls = {"docker": 0}
    monkeypatch.setattr(
        emsctl, "run_docker_compose",
        lambda *a, **k: calls.__setitem__("docker", calls["docker"] + 1) or 0,
    )

    def fake_execute(influx_config, action):
        calls["action"] = action
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_execute)
    rc = emsctl.handle_influx_command(
        init_args(),
        {
            "influxdb": {
                "enabled": True,
                "mode": "external",
                "url": "http://192.168.1.5:8086",
                "org": "ems",
                "token": "external-token",
            }
        },
    )
    assert rc == 0
    # External mode never touches Docker, and syncs when auto_sync is on.
    assert calls["docker"] == 0
    assert calls["action"] == "sync"


def test_influx_init_external_incomplete_config_fails(patch_base, monkeypatch):
    calls = {"docker": 0, "execute": 0}
    monkeypatch.setattr(
        emsctl, "run_docker_compose",
        lambda *a, **k: calls.__setitem__("docker", calls["docker"] + 1) or 0,
    )
    monkeypatch.setattr(
        emsctl, "execute_influx_schema_op",
        lambda *a, **k: calls.__setitem__("execute", calls["execute"] + 1) or (0, {}),
    )
    rc = emsctl.handle_influx_command(
        init_args(),
        {"influxdb": {"enabled": True, "mode": "external"}},  # no token
    )
    assert rc != 0
    # No connectivity attempt and no Docker when settings are incomplete.
    assert calls["docker"] == 0
    assert calls["execute"] == 0


def test_influx_init_external_json_single_object(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(json=True),
        {
            "influxdb": {
                "enabled": True,
                "mode": "external",
                "url": "http://192.168.1.5:8086",
                "org": "ems",
                "token": "external-token",
            }
        },
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "external"
    assert payload["started"] is False
    assert payload["ready"] is True
    assert payload["synced"] is True
    assert payload["user_managed"] is True


def test_influx_init_non_default_secret_file_refuses_start(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(),
        {
            "influxdb": {
                "enabled": True,
                "mode": "bundled",
                "secret_file": "deploy/docker/other.env",
            }
        },
    )
    assert rc == 2
    # Nothing was created since starting is refused before secret generation.
    assert not (patch_base / "deploy" / "docker" / "other.env").exists()


def test_influx_init_non_default_secret_file_allowed_with_no_start(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(no_start=True),
        {
            "influxdb": {
                "enabled": True,
                "mode": "bundled",
                "secret_file": "deploy/docker/other.env",
            }
        },
    )
    assert rc == 0
    assert (patch_base / "deploy" / "docker" / "other.env").exists()


def test_influx_init_starts_and_syncs(patch_base, monkeypatch):
    calls = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        calls["docker"] = command
        return 0

    def fake_sync(influx_config, action):
        calls["sync"] = action
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_sync)

    rc = emsctl.handle_influx_command(
        init_args(), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    # Starts only the influxdb service via the full compose file set.
    assert "influxdb" in calls["docker"]
    assert calls["sync"] == "sync"


def test_influx_init_creates_data_dir_before_docker(patch_base, monkeypatch):
    # The local bind-mount target must exist by the time docker compose runs.
    observed = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        observed["data_dir_exists"] = (patch_base / "data" / "influxdb").is_dir()
        return 0

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    assert observed["data_dir_exists"] is True
    assert (patch_base / "data" / "influxdb").is_dir()


def test_influx_init_json_reports_data_dir(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(json=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_dir"] == "data/influxdb"


def test_influx_init_no_start_skips_data_dir(patch_base, monkeypatch):
    # --no-start only writes the secret file; it must not create the data dir.
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(no_start=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    assert not (patch_base / "data" / "influxdb").exists()


def test_influx_init_auto_sync_false_starts_without_sync(patch_base, monkeypatch, capsys):
    actions = []

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        return 0

    def fake_execute(influx_config, action):
        actions.append(action)
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_execute)
    rc = emsctl.handle_influx_command(
        init_args(),
        {"influxdb": {"enabled": True, "mode": "bundled", "auto_sync": False}},
    )
    assert rc == 0
    # Readiness is probed via 'status'; the schema is never synced.
    assert actions == ["status"]
    out = capsys.readouterr().out
    assert "python3 emsctl.py influx sync" in out


def test_influx_init_bundled_json_reports_ready(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(json=True), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "bundled"
    assert payload["started"] is True
    assert payload["ready"] is True
    assert payload["synced"] is True


def test_influx_init_uses_host_url_for_sync(patch_base, monkeypatch):
    captured = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        return 0

    def fake_execute(influx_config, action):
        captured["url"] = influx_setup.host_cli_url(influx_config)
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_execute)
    rc = emsctl.handle_influx_command(
        init_args(), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    # Bundled host-side ops connect via the loopback host URL, not the Docker
    # service name in influxdb.url.
    assert captured["url"] == "http://127.0.0.1:8086"


def test_influx_init_no_start_skips_docker_and_sync(patch_base, monkeypatch):
    calls = {"docker": 0, "sync": 0}
    monkeypatch.setattr(
        emsctl, "run_docker_compose",
        lambda *a, **k: calls.__setitem__("docker", calls["docker"] + 1) or 0,
    )

    def fake_sync(influx_config, action):
        calls["sync"] += 1
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_sync)
    rc = emsctl.handle_influx_command(
        init_args(no_start=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    assert calls["docker"] == 0
    assert calls["sync"] == 0


def test_influx_init_json_is_single_object_without_secrets(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_influx_command(
        init_args(json=True, no_start=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out
    # Exactly one JSON object on stdout (json.loads of the whole stdout works).
    payload = json.loads(out)
    assert payload["command"] == "influx init"
    assert payload["ok"] is True
    # No docker trace leaked onto stdout.
    assert "+ docker compose" not in out
    # The real token written to disk must not appear in JSON output.
    secret_path = patch_base / "deploy" / "docker" / "influxdb.env"
    values = influx_setup.parse_env_file(secret_path.read_text())
    assert values["INFLUXDB_TOKEN"] not in out
    assert payload["summary"]["INFLUXDB_TOKEN"] == "<redacted>"
    assert payload["generated_keys"] == ["<redacted>"] * 3
    assert payload["token"]["redacted"] == "********"


def test_influx_init_json_embeds_sync_result_single_object(patch_base, monkeypatch, capsys):
    # When sync actually runs, the nested schema result must be embedded into the
    # single outer JSON object — not printed as a second standalone document.
    report = {"buckets": [{"name": "ems_raw"}], "tasks": ["downsample_1m"]}
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op(report))
    rc = emsctl.handle_influx_command(
        init_args(json=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    out = capsys.readouterr().out
    # json.loads on the whole stdout fails with "Extra data" if a second JSON
    # document was printed by the nested sync path.
    payload = json.loads(out)
    assert payload["command"] == "influx init"
    assert payload["started"] is True
    assert payload["synced"] is True
    assert payload["sync_result"] == report


def test_stack_up_json_embeds_sync_result_single_object(patch_base, monkeypatch, capsys):
    report = {"buckets": [{"name": "ems_raw"}], "tasks": ["downsample_1m"]}
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op(report))
    rc = emsctl.handle_stack_command(
        stack_args(json=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["command"] == "stack up"
    assert payload["synced"] is True
    assert payload["sync_result"] == report


def test_execute_influx_schema_op_never_prints_to_stdout():
    # The nested schema op must stay print-free so callers own the single JSON
    # document; a stray print here would emit a second JSON object on stdout.
    import inspect

    source = inspect.getsource(emsctl.execute_influx_schema_op)
    assert "print(" not in source


def test_run_docker_compose_trace_goes_to_stderr(capsys):
    # dry_run=True avoids invoking docker; the command trace must land on
    # stderr so stdout stays a clean JSON channel.
    code = emsctl.run_docker_compose(
        ["docker", "compose", "up", "-d"], cwd=".", dry_run=True
    )
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "+ docker compose up -d" in captured.err


def test_run_docker_compose_stdout_to_stderr_redirects_child_streams(monkeypatch):
    # In JSON mode the child's stdout (docker compose status lines) must be
    # routed to this process's stderr so it cannot corrupt the JSON document.
    captured = {}

    def fake_run(command, cwd=None, stdout=None, stderr=None):
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    code = emsctl.run_docker_compose(
        ["docker", "compose", "up", "-d"], cwd=".", stdout_to_stderr=True
    )
    assert code == 0
    assert captured["stdout"] is sys.stderr
    assert captured["stderr"] is sys.stderr


def test_run_docker_compose_default_inherits_child_streams(monkeypatch):
    # Non-JSON mode keeps Docker output on stdout for humans (stdout=None means
    # the child inherits this process's stdout/stderr).
    captured = {}

    def fake_run(command, cwd=None, stdout=None, stderr=None):
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    code = emsctl.run_docker_compose(["docker", "compose", "up", "-d"], cwd=".")
    assert code == 0
    assert captured["stdout"] is None
    assert captured["stderr"] is None


def _fake_docker_run_emitting_status():
    """Fake subprocess.run that mimics docker compose printing a status line.

    Writes to whatever stdout stream run_docker_compose hands it, so in JSON
    mode (stdout=sys.stderr) the noise lands on stderr, never on stdout.
    """

    def fake_run(command, cwd=None, stdout=None, stderr=None):
        (stdout or sys.stdout).write("Container ems-influxdb Running\n")
        return SimpleNamespace(returncode=0)

    return fake_run


def test_stack_up_json_keeps_docker_text_off_stdout(patch_base, monkeypatch, capsys):
    # End-to-end: docker emits a status line, yet stdout stays exactly one JSON
    # object and the docker noise is diverted to stderr.
    monkeypatch.setattr(subprocess, "run", _fake_docker_run_emitting_status())
    monkeypatch.setattr(
        emsctl, "execute_influx_schema_op", fake_schema_op({"buckets": [], "tasks": []})
    )
    rc = emsctl.handle_stack_command(
        stack_args(json=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)  # raises if docker text leaked onto stdout
    assert payload["command"] == "stack up"
    assert "Container ems-influxdb Running" not in out.out
    assert "Container ems-influxdb Running" in out.err


def test_influx_init_json_keeps_docker_text_off_stdout(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", _fake_docker_run_emitting_status())
    report = {"buckets": [{"name": "ems_raw"}], "tasks": []}
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op(report))
    rc = emsctl.handle_influx_command(
        init_args(json=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)  # single JSON object, no docker noise
    assert payload["command"] == "influx init"
    assert payload["started"] is True
    # The nested sync report stays embedded in the single JSON object.
    assert payload["sync_result"] == report
    assert "Container ems-influxdb Running" not in out.out
    assert "Container ems-influxdb Running" in out.err


def test_stack_up_non_json_passes_through_docker_output(patch_base, monkeypatch):
    # Human (non-JSON) mode must not redirect: run_docker_compose receives
    # stdout_to_stderr=False so docker output stays on stdout for the operator.
    captured = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        captured["stdout_to_stderr"] = stdout_to_stderr
        return 0

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(json=False),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    assert captured["stdout_to_stderr"] is False


def test_stack_up_disabled_uses_base_file_only(patch_base, monkeypatch):
    captured = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        captured["command"] = command
        return 0

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    rc = emsctl.handle_stack_command(
        stack_args(), {"influxdb": {"enabled": False}}
    )
    assert rc == 0
    assert captured["command"] == [
        "docker", "compose", "-f", "docker-compose.example.yml", "up", "-d",
    ]


def test_stack_up_bundled_includes_overlays_and_syncs(patch_base, monkeypatch):
    captured = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        captured["command"] = command
        return 0

    def fake_sync(influx_config, action):
        captured["sync"] = action
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_sync)
    rc = emsctl.handle_stack_command(
        stack_args(), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    assert "deploy/docker/compose.influxdb.yml" in captured["command"]
    assert "deploy/docker/compose.ems-influx-env.yml" in captured["command"]
    # Base compose file comes first so env_file paths resolve at the repo root.
    assert captured["command"][2:4] == ["-f", "docker-compose.example.yml"]
    assert captured["sync"] == "sync"
    assert (patch_base / "deploy" / "docker" / "influxdb.env").exists()


def test_stack_up_bundled_creates_data_dir_before_docker(patch_base, monkeypatch):
    observed = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        observed["data_dir_exists"] = (patch_base / "data" / "influxdb").is_dir()
        return 0

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    assert observed["data_dir_exists"] is True
    assert (patch_base / "data" / "influxdb").is_dir()


def test_stack_up_bundled_json_reports_data_dir(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(json=True), {"influxdb": {"enabled": True, "mode": "bundled"}}
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_dir"] == "data/influxdb"


def test_stack_up_disabled_skips_data_dir(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    rc = emsctl.handle_stack_command(
        stack_args(), {"influxdb": {"enabled": False}}
    )
    assert rc == 0
    # Non-bundled stack must not create the bundled InfluxDB data directory.
    assert not (patch_base / "data" / "influxdb").exists()


def test_stack_up_external_mode_no_overlays(patch_base, monkeypatch):
    captured = {}

    def fake_docker(command, cwd, dry_run=False, stdout_to_stderr=False):
        captured["command"] = command
        return 0

    monkeypatch.setattr(emsctl, "run_docker_compose", fake_docker)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(), {"influxdb": {"enabled": True, "mode": "external"}}
    )
    assert rc == 0
    # External mode must not pull in the bundled InfluxDB overlays.
    assert "deploy/docker/compose.influxdb.yml" not in captured["command"]
    assert "deploy/docker/compose.ems-influx-env.yml" not in captured["command"]
    assert not (patch_base / "deploy" / "docker" / "influxdb.env").exists()


def test_stack_up_non_default_secret_file_refused(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(),
        {
            "influxdb": {
                "enabled": True,
                "mode": "bundled",
                "secret_file": "deploy/docker/other.env",
            }
        },
    )
    assert rc == 2


def test_stack_up_auto_init_false_skips_secret_creation(patch_base, monkeypatch):
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(),
        {"influxdb": {"enabled": True, "mode": "bundled", "auto_init": False}},
    )
    assert rc == 0
    assert not (patch_base / "deploy" / "docker" / "influxdb.env").exists()


def test_stack_up_auto_sync_false_skips_sync(patch_base, monkeypatch):
    calls = {"sync": 0}
    monkeypatch.setattr(emsctl, "run_docker_compose", lambda *a, **k: 0)

    def fake_sync(influx_config, action):
        calls["sync"] += 1
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_sync)
    rc = emsctl.handle_stack_command(
        stack_args(),
        {"influxdb": {"enabled": True, "mode": "bundled", "auto_sync": False}},
    )
    assert rc == 0
    assert calls["sync"] == 0


def test_stack_up_dry_run_no_side_effects(patch_base, monkeypatch):
    calls = {"sync": 0}

    def fake_sync(influx_config, action):
        calls["sync"] += 1
        return 0, {"action": action, "ok": True, "url": "", "report": {}}

    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_sync)
    rc = emsctl.handle_stack_command(
        stack_args(dry_run=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    # No secret file created, no sync run during a dry run.
    assert not (patch_base / "deploy" / "docker" / "influxdb.env").exists()
    assert calls["sync"] == 0


def test_stack_up_dry_run_json_is_single_object(patch_base, monkeypatch, capsys):
    monkeypatch.setattr(emsctl, "execute_influx_schema_op", fake_schema_op())
    rc = emsctl.handle_stack_command(
        stack_args(dry_run=True, json=True),
        {"influxdb": {"enabled": True, "mode": "bundled"}},
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["command"] == "stack up"
    assert payload["dry_run"] is True
    assert "+ docker compose" not in out


# --- CLI help / discovery ---------------------------------------------------


def test_influx_init_appears_in_help():
    result = subprocess.run(
        [sys.executable, str(EMSCTL), "influx", "--help"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert "init" in result.stdout


def test_stack_command_appears_in_help():
    result = subprocess.run(
        [sys.executable, str(EMSCTL), "stack", "--help"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert "up" in result.stdout


# --- runtime URL / token resolution (native host vs container) --------------


def _force_native_host(monkeypatch):
    """Pretend the test process is not inside a container.

    ``is_container_runtime`` falls back to probing the real ``/.dockerenv``
    marker when there is no explicit override. The test runner itself may run
    inside a container, so neutralize that filesystem check to keep these
    native-host assertions deterministic.
    """
    real_exists = os.path.exists
    monkeypatch.setattr(
        influx_setup.os.path,
        "exists",
        lambda path: False if path == "/.dockerenv" else real_exists(path),
    )


def test_runtime_url_bundled_native_uses_host_url(monkeypatch):
    _force_native_host(monkeypatch)
    cfg = bundled_config(
        url="http://influxdb:8086", host_url="http://127.0.0.1:8086"
    )
    # No /.dockerenv and no override -> treated as native host.
    assert (
        influx_setup.runtime_influx_url(cfg, environ={})
        == "http://127.0.0.1:8086"
    )


def test_runtime_url_bundled_native_missing_host_url_falls_back_to_loopback(
    monkeypatch,
):
    _force_native_host(monkeypatch)
    cfg = bundled_config(url="http://influxdb:8086", host_url="")
    # normalize fills host_url with the default; force it empty to exercise the
    # runtime fallback path directly.
    cfg["host_url"] = ""
    assert (
        influx_setup.runtime_influx_url(cfg, environ={})
        == influx_setup.DEFAULT_HOST_URL
    )


def test_runtime_url_bundled_container_uses_service_url():
    cfg = bundled_config(
        url="http://influxdb:8086", host_url="http://127.0.0.1:8086"
    )
    assert (
        influx_setup.runtime_influx_url(cfg, environ={"EMS_IN_CONTAINER": "1"})
        == "http://influxdb:8086"
    )


def test_runtime_url_external_uses_configured_url_even_in_container():
    cfg = normalize_influxdb_config(
        {"enabled": True, "mode": "external", "url": "http://nas.local:8086"}
    )
    assert (
        influx_setup.runtime_influx_url(cfg, environ={"EMS_IN_CONTAINER": "1"})
        == "http://nas.local:8086"
    )
    assert (
        influx_setup.runtime_influx_url(cfg, environ={})
        == "http://nas.local:8086"
    )


def test_is_container_runtime_respects_explicit_override():
    assert influx_setup.is_container_runtime(environ={"EMS_IN_CONTAINER": "1"})
    assert not influx_setup.is_container_runtime(
        environ={"EMS_IN_CONTAINER": "0"}
    )


def test_runtime_token_explicit_token_wins(tmp_path):
    # Even with a bundled secret file present, an explicit token takes priority.
    cfg = bundled_config(token="explicit-token")
    influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    assert (
        influx_setup.runtime_influx_token(
            cfg, environ={"INFLUXDB_TOKEN": "env-token"}, base_dir=str(tmp_path)
        )
        == "explicit-token"
    )


def test_runtime_token_env_wins_over_secret_file(tmp_path):
    cfg = bundled_config(token="")
    influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    assert (
        influx_setup.runtime_influx_token(
            cfg, environ={"INFLUXDB_TOKEN": "env-token"}, base_dir=str(tmp_path)
        )
        == "env-token"
    )


def test_runtime_token_bundled_reads_secret_file_when_env_missing(tmp_path):
    cfg = bundled_config(token="")
    influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    secret_values = influx_setup.parse_env_file(
        Path(
            influx_setup.resolve_secret_file_path(cfg, base_dir=str(tmp_path))
        ).read_text()
    )
    resolved = influx_setup.runtime_influx_token(
        cfg, environ={}, base_dir=str(tmp_path)
    )
    assert resolved == secret_values["INFLUXDB_TOKEN"]
    assert resolved


def test_runtime_token_external_does_not_read_secret_file(tmp_path):
    # A secret file written for bundled mode must not leak into external mode.
    bundled = bundled_config(token="")
    influx_setup.ensure_secret_file(bundled, base_dir=str(tmp_path))
    external = normalize_influxdb_config(
        {"enabled": True, "mode": "external", "url": "http://nas.local:8086"}
    )
    assert (
        influx_setup.runtime_influx_token(
            external, environ={}, base_dir=str(tmp_path)
        )
        == ""
    )


def test_runtime_token_bundled_missing_secret_file_returns_empty(tmp_path):
    cfg = bundled_config(token="")
    # No secret file created under tmp_path.
    assert (
        influx_setup.runtime_influx_token(cfg, environ={}, base_dir=str(tmp_path))
        == ""
    )
