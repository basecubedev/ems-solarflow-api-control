# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided EMS upgrade executor tests (no Docker/network required)."""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.ems_tool import EmsToolRunner
from admin.guided_upgrade import GuidedUpgradeExecutor
from admin.image_identity import (
    ALREADY_CURRENT,
    IDENTITY_UNKNOWN,
    OLDER_THAN_RUNNING_BUILD,
    UPGRADE_AVAILABLE,
    UpgradeAssessment,
)
from admin.install_context import detect_install_context
from admin.server import ScanRegistry, create_server

pytestmark = pytest.mark.simulation

TAG = "v0.6.1"

COMPOSE_TEXT = """services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0
    container_name: ems-solarflow-api-control
  influxdb:
    image: influxdb:2.7
"""

# new_target_key exists only here, so config_write must source it from the target.
TARGET_TEMPLATE = {
    "config_schema_version": 3,
    "system": {"max_total_power": 800, "new_target_key": 42},
    "devices": [],
}

ALL_OPTIONS = {
    "backup": True,
    "config_check": True,
    "config_add_keys": True,
    "config_comments": True,
    "pull_image": True,
    "recreate": True,
    "diagnostics": True,
}


class FakeReleaseManager:
    def __init__(self, releases_dir, prepared=TAG):
        self.releases_dir = Path(releases_dir)
        self._prepared = prepared

    def prepared_release(self):
        return self._prepared


class VerifyingReleaseManager(FakeReleaseManager):
    """FakeReleaseManager that scripts the target-image verification verdict."""

    def __init__(self, releases_dir, prepared=TAG, assessment=None):
        super().__init__(releases_dir, prepared=prepared)
        self._assessment = assessment or UpgradeAssessment(UPGRADE_AVAILABLE, "build_serial")
        self.verified = []
        self.pull_arg = None

    def verify_upgrade_target(self, tag, *, pull=None):
        self.verified.append(tag)
        self.pull_arg = pull
        return self._assessment


class FakeCompose:
    def __init__(self, backup_returncode=0):
        self.calls = []
        self.oneoff_calls = []
        self._backup_returncode = backup_returncode

    def up(self, workspace, services=(), force_recreate=False, **kwargs):
        self.calls.append(
            {"workspace": str(workspace), "services": tuple(services),
             "force_recreate": force_recreate}
        )

    def run_oneoff(self, workspace, service, command, timeout=180):
        self.oneoff_calls.append(
            {"workspace": str(workspace), "service": service, "command": list(command)}
        )
        return self._backup_returncode, ""


class FakeDockerCli:
    def __init__(self):
        self.pulled = []

    def pull(self, image, on_progress=None):
        self.pulled.append(image)


class FakeDockerWithContainer:
    """DockerCli-like: a ready daemon exposing one running EMS container."""

    def __init__(self, container=None):
        self._container = container
        self.pulled = []

    def probe(self):
        return {"state": "ready"}

    def inspect_container(self, name):
        return dict(self._container) if self._container else None

    def pull(self, image, on_progress=None):
        self.pulled.append(image)


class FakeRun:
    """Records argv and returns one scripted CompletedProcess (docker exec)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        return self._result


def _running_ems():
    return {
        "container_name": "ems-solarflow-api-control",
        "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
        "status": "running",
    }


class FakeEmsCli:
    def __init__(self, status="ok"):
        self._status = status

    def run(self, check_ids=None):
        return {"available": True, "summary": {"status": self._status}}


def _install(tmp_path, config=None):
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "data").mkdir()
    (install / "config" / "config.json").write_text(
        json.dumps(config or {"config_schema_version": 3, "system": {"max_total_power": 800}}),
        encoding="utf-8",
    )
    (install / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    return install


def _prepared_release(tmp_path, tag=TAG, template=None):
    releases = tmp_path / "releases"
    (releases / tag).mkdir(parents=True)
    (releases / tag / "config.template.json").write_text(
        json.dumps(template or TARGET_TEMPLATE), encoding="utf-8"
    )
    return releases


def _make_executor(tmp_path, *, prepared=TAG, ems_status="ok", backup_returncode=0):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    compose = FakeCompose(backup_returncode=backup_returncode)
    docker = FakeDockerCli()

    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases, prepared=prepared),
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli(ems_status),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    return executor, install, compose, docker


def test_rejects_missing_confirm(tmp_path):
    executor, _install_dir, compose, docker = _make_executor(tmp_path)
    result = executor.execute(TAG, ALL_OPTIONS, confirm=False)
    assert result["ok"] is False
    assert result["reason"] == "confirm_required"
    assert compose.calls == [] and docker.pulled == []


def test_rejects_unprepared_target(tmp_path):
    executor, _install_dir, compose, docker = _make_executor(tmp_path, prepared="v0.6.0")
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)
    assert result["ok"] is False
    assert result["reason"] == "target_not_prepared"
    assert compose.calls == [] and docker.pulled == []


def test_writes_compose_image_and_force_recreates(tmp_path):
    executor, install, compose, docker = _make_executor(tmp_path)
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is True and result["status"] == "completed"
    target_image = "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1"
    assert result["target_image"] == target_image
    assert docker.pulled == [target_image]

    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert target_image in compose_text
    # The bundled InfluxDB image reference must be left untouched.
    assert "influxdb:2.7" in compose_text
    assert (install / "docker-compose.yml.bak").exists()

    assert len(compose.calls) == 1
    assert compose.calls[0]["services"] == ("ems",)
    assert compose.calls[0]["force_recreate"] is True


def test_config_write_uses_target_prepared_template(tmp_path):
    executor, install, _compose, _docker = _make_executor(tmp_path)
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is True
    written = json.loads((install / "config" / "config.json").read_text(encoding="utf-8"))
    # The new key only exists in the prepared target template.
    assert written["system"]["new_target_key"] == 42
    write_step = next(s for s in result["steps"] if s["id"] == "config_write")
    assert write_step["status"] == "ok"


def test_disabled_backup_still_writes_config_with_warning(tmp_path):
    executor, install, _compose, _docker = _make_executor(tmp_path)
    options = {**ALL_OPTIONS, "backup": False}
    result = executor.execute(TAG, options, confirm=True)

    assert result["ok"] is True
    assert not any(s["id"] == "backup" for s in result["steps"])
    write_step = next(s for s in result["steps"] if s["id"] == "config_write")
    assert write_step["status"] == "ok"
    written = json.loads((install / "config" / "config.json").read_text(encoding="utf-8"))
    assert written["system"]["new_target_key"] == 42
    assert any("backup" in warning.lower() for warning in result["warnings"])


def test_backup_uses_compose_run_when_no_running_container(tmp_path):
    executor, _install_dir, compose, _docker = _make_executor(tmp_path)
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)
    assert compose.oneoff_calls
    call = compose.oneoff_calls[0]
    assert call["service"] == "ems"
    assert call["command"] == ["python3", "emsctl.py", "backup", "create"]
    backup = next(s for s in result["steps"] if s["id"] == "backup")
    assert backup["status"] == "ok"
    assert backup["detail"] == "via compose one-off EMS container"


def test_backup_uses_docker_exec_when_ems_container_running(tmp_path):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    docker = FakeDockerWithContainer(container=_running_ems())
    fake_run = FakeRun(returncode=0, stdout="backup ok")
    compose = FakeCompose()
    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases, prepared=TAG),
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli("ok"),
        ems_tool=EmsToolRunner(docker=docker, compose=compose, run=fake_run),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is True
    exec_calls = [c["argv"] for c in fake_run.calls if c["argv"][:2] == ["docker", "exec"]]
    assert exec_calls == [
        [
            "docker", "exec", "ems-solarflow-api-control",
            "python3", "/app/emsctl.py", "backup", "create",
        ]
    ]
    assert compose.oneoff_calls == []
    backup = next(s for s in result["steps"] if s["id"] == "backup")
    assert backup["status"] == "ok"
    assert backup["detail"] == "via running EMS container"


def test_backup_blocked_step_when_no_ems_tool_context(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    noctx = tmp_path / "noctx"
    (noctx / "config").mkdir(parents=True)
    (noctx / "data").mkdir()
    (noctx / "config" / "config.json").write_text("{}", encoding="utf-8")
    context = detect_install_context(base_dir=str(noctx))
    assert context.compose_exists is False

    step = executor._run_backup(context)

    assert step["id"] == "backup" and step["status"] == "error"
    assert step["detail"]  # a friendly blocked message, not a traceback


def test_target_image_command_builder_uses_docker_run_with_binds(tmp_path):
    install = _install(tmp_path)
    context = detect_install_context(base_dir=str(install))
    runner = EmsToolRunner(docker=FakeDockerCli(), compose=FakeCompose())
    target_image = "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1"

    argv = runner.build_target_image_command(
        context, target_image, ("config", "upgrade", "--dry-run")
    )

    assert argv[:3] == ["docker", "run", "--rm"]
    assert target_image in argv
    assert argv[-5:] == [
        "python3", "/app/emsctl.py", "config", "upgrade", "--dry-run",
    ]
    joined = " ".join(argv)
    assert f"type=bind,src={install / 'config'},dst=/app/config" in joined
    assert f"type=bind,src={install / 'data'},dst=/app/data" in joined


def test_backup_failure_aborts_before_recreate(tmp_path):
    executor, _install_dir, compose, docker = _make_executor(tmp_path, backup_returncode=1)
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)
    assert result["ok"] is False and result["status"] == "failed"
    assert next(s for s in result["steps"] if s["id"] == "backup")["status"] == "error"
    assert docker.pulled == [] and compose.calls == []


# --- target-image verification (before any mutating step) -----------------


def _make_verifying_executor(tmp_path, assessment):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    compose = FakeCompose()
    docker = FakeDockerCli()
    release_manager = VerifyingReleaseManager(releases, prepared=TAG, assessment=assessment)
    executor = GuidedUpgradeExecutor(
        release_manager=release_manager,
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli("ok"),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    return executor, install, compose, docker, release_manager


def test_verify_upgrade_available_allows_run(tmp_path):
    executor, install, compose, docker, rm = _make_verifying_executor(
        tmp_path, UpgradeAssessment(UPGRADE_AVAILABLE, "build_serial")
    )
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is True and result["status"] == "completed"
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["status"] == "ok"
    assert verify["detail"] == "Target image identity verified."
    # The verify step runs first and receives a working pull callable.
    assert rm.verified == [TAG] and callable(rm.pull_arg)
    assert result["steps"][0]["id"] == "verify_image"
    # The run proceeds through the mutating steps only after verification.
    assert compose.calls and compose.calls[0]["services"] == ("ems",)


def test_verify_lower_build_serial_blocks_before_mutation(tmp_path):
    executor, install, compose, docker, rm = _make_verifying_executor(
        tmp_path, UpgradeAssessment(OLDER_THAN_RUNNING_BUILD, "build_serial")
    )
    original_config = (install / "config" / "config.json").read_text(encoding="utf-8")
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is False and result["status"] == "failed"
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["status"] == "error"
    assert "older than the running EMS build" in verify["detail"]
    # No backup / config / pull / compose / recreate happened.
    assert [s["id"] for s in result["steps"]] == ["verify_image"]
    assert docker.pulled == [] and compose.calls == [] and compose.oneoff_calls == []
    assert (install / "config" / "config.json").read_text(encoding="utf-8") == original_config


def test_verify_same_digest_blocks_as_noop(tmp_path):
    executor, _install, compose, docker, _rm = _make_verifying_executor(
        tmp_path, UpgradeAssessment(ALREADY_CURRENT, "digest")
    )
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is False and result["status"] == "failed"
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["status"] == "error"
    assert "already installed" in verify["detail"]
    assert docker.pulled == [] and compose.calls == [] and compose.oneoff_calls == []


def test_verify_legacy_unverified_allows_and_surfaces_warning(tmp_path):
    executor, install, compose, docker, rm = _make_verifying_executor(
        tmp_path,
        UpgradeAssessment(
            UPGRADE_AVAILABLE,
            "legacy_unverified",
            warning="Legacy image metadata missing. Allowed by admin test override.",
        ),
    )
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is True and result["status"] == "completed"
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["status"] == "ok"
    assert "Legacy image metadata missing" in verify["detail"]
    # The legacy warning is surfaced to the operator on the run result.
    assert any("Legacy image metadata missing" in w for w in result["warnings"])
    # The run still proceeds through the mutating steps after verification.
    assert compose.calls and compose.calls[0]["services"] == ("ems",)


def test_verify_unknown_identity_blocks_before_mutation(tmp_path):
    executor, install, compose, docker, _rm = _make_verifying_executor(
        tmp_path, UpgradeAssessment(IDENTITY_UNKNOWN, "none")
    )
    original_config = (install / "config" / "config.json").read_text(encoding="utf-8")
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)

    assert result["ok"] is False and result["status"] == "failed"
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["status"] == "error"
    assert "could not be verified" in verify["detail"]
    assert [s["id"] for s in result["steps"]] == ["verify_image"]
    assert docker.pulled == [] and compose.calls == [] and compose.oneoff_calls == []
    assert (install / "config" / "config.json").read_text(encoding="utf-8") == original_config


# --- endpoint wiring ------------------------------------------------------


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _get(url):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _wait_job(base, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _get(
            base + "/api/admin/maintenance/upgrade/jobs/" + job_id
        )
        if status == 200 and body.get("status") in ("succeeded", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("guided upgrade job did not finish in time")


def _server(guided_upgrade):
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry, guided_upgrade=guided_upgrade)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_endpoint_requires_confirm(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    srv, base = _server(executor)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": False, "target_release": TAG, "options": ALL_OPTIONS},
        )
    finally:
        srv.shutdown()
    assert status == 400
    assert body["reason"] == "confirm_required"


def test_endpoint_returns_job_id(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    srv, base = _server(executor)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS},
        )
    finally:
        srv.shutdown()
    assert status == 202
    assert body["ok"] is True
    assert isinstance(body["job_id"], str) and body["job_id"]
    assert body["status"] in ("running", "succeeded")
    assert any(step["key"] == "recreate_ems" for step in body["steps"])


def test_job_status_exposes_step_states(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    srv, base = _server(executor)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()
    assert job["status"] == "succeeded"
    assert job["result"]["status"] == "completed"
    recreate = next(s for s in job["steps"] if s["key"] == "recreate_ems")
    assert recreate["state"] == "done"
    assert all(
        s["state"] in ("done", "skipped", "pending", "running", "failed")
        for s in job["steps"]
    )


def test_failed_step_visible_in_job_status(tmp_path):
    executor, _install_dir, _compose, docker = _make_executor(
        tmp_path, backup_returncode=1
    )
    srv, base = _server(executor)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()
    assert job["status"] == "failed"
    backup = next(s for s in job["steps"] if s["key"] == "backup")
    assert backup["state"] == "failed"
    assert docker.pulled == []


def test_unknown_job_id_returns_404(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    srv, base = _server(executor)
    try:
        status, body = _get(
            base + "/api/admin/maintenance/upgrade/jobs/does-not-exist"
        )
    finally:
        srv.shutdown()
    assert status == 404
    assert body["ok"] is False
