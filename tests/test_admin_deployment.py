# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment preparation service tests (no real Docker daemon)."""

import itertools
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.deployment import (
    BootstrapInstaller,
    DeploymentService,
    DeploymentJobRegistry,
    DockerCli,
    DockerCompose,
    DockerError,
    StartJob,
    parse_pull_progress,
)
from admin import deployment
from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.releases import ReleaseError
from tests.helpers.setup_config import adopt_generated_config

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.integration,
    pytest.mark.simulation,
]


# --- fakes ---------------------------------------------------------------


class _FakeReleaseManager:
    def __init__(self, releases_dir, tag="v0.6.0"):
        self.releases_dir = Path(releases_dir)
        self.tag = tag
        self.data_dir = Path(releases_dir).parent

    def config_template(self):
        if self.tag is None:
            raise ReleaseError("No release resources prepared yet.", 404)
        return {
            "tag": self.tag,
            "template": {},
            "docker_image": f"ghcr.io/basecubedev/ems-solarflow-api-control:{self.tag}",
        }


class _ConfigExport:
    def __init__(self, target_path):
        self.target_path = Path(target_path)


class _FakeDocker:
    def __init__(
        self, check_error=None, pull_error=None, status=None, containers=None
    ):
        self.check_error = check_error
        self.pull_error = pull_error
        self.status = status or {
            "state": "ready",
            "code": None,
            "message": "Docker is available.",
            "mode": "deployment_controller",
            "socket": "/var/run/docker.sock",
            "server_version": "27.5.1",
        }
        self.checked = False
        self.pulled = []
        self.containers = dict(containers or {})
        self.stopped = []
        self.removed = []
        self.permission_error = None
        self.permission_repair_error = None
        self.permission_checks = []
        self.permission_repairs = []

    def probe(self):
        return dict(self.status)

    def check(self):
        self.checked = True
        if self.check_error is not None:
            raise self.check_error

    def pull(self, image, on_progress=None):
        self.pulled.append(image)
        if on_progress is not None:
            on_progress(40, f"{image}: Downloading")
            on_progress(100, "Status: Downloaded")
        if self.pull_error is not None:
            raise self.pull_error

    def inspect_container(self, container_name):
        container = self.containers.get(container_name)
        return dict(container) if container else None

    def remove_container(self, container_name):
        self.removed.append(container_name)
        self.containers.pop(container_name, None)

    def stop_container(self, container_name):
        self.stopped.append(container_name)
        self.containers[container_name]["status"] = "exited"

    def check_workspace_permissions(self, workspace, image, puid, pgid):
        self.permission_checks.append((str(workspace), image, puid, pgid))
        if self.permission_error is not None:
            raise self.permission_error

    def repair_workspace_permissions(self, workspace, image, puid, pgid):
        self.permission_repairs.append((str(workspace), image, puid, pgid))
        if self.permission_repair_error is not None:
            raise self.permission_repair_error
        self.permission_error = None


class _FakeInstaller:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def prepare(self, workspace, script_path, analytics=False, tag=None, on_line=None):
        self.calls.append(
            {
                "workspace": str(workspace),
                "script": str(script_path),
                "analytics": analytics,
                "tag": tag,
            }
        )
        if on_line is not None:
            on_line("Wrote docker-compose.yml")
        if self.error is not None:
            raise self.error
        Path(workspace, "docker-compose.yml").write_text("services:\n", encoding="utf-8")


class _FakeCompose:
    def __init__(self, services=None, error=None, logs=""):
        self.services = services if services is not None else [
            {
                "name": "ems-solarflow-api-control",
                "service": "ems",
                "image": "ems:test",
                "state": "running",
                "status": "Up",
                "ports": ["8080:8080/tcp"],
            }
        ]
        self.error = error
        self.up_calls = []
        self.ps_calls = []
        self.log_output = logs

    def up(self, workspace, profiles=(), on_line=None):
        self.up_calls.append(
            {"workspace": str(workspace), "profiles": list(profiles)}
        )
        if self.error is not None:
            raise self.error

    def ps(self, workspace):
        self.ps_calls.append(str(workspace))
        if self.error is not None:
            raise self.error
        return list(self.services)

    def logs(self, workspace, service="ems"):
        return self.log_output


class _SyncRegistry:
    """Runs prepare jobs inline so tests observe the final state directly."""

    def __init__(self):
        self._jobs = {}

    def submit(self, job, runner, *, on_complete=None, on_settled=None):
        self._jobs[job.job_id] = job
        try:
            runner(job)
        except DockerError as exc:
            job.fail(exc.code, exc.message, exc.detail, exc.conflict)
        except ReleaseError as exc:
            job.fail("release_error", str(exc))
        except OSError as exc:
            job.fail("workspace_write_failed", str(exc))
        except Exception:
            job.fail("prepare_failed", "Deployment preparation failed unexpectedly.")
        finally:
            if on_settled is not None:
                on_settled()
            if on_complete is not None:
                on_complete(job.snapshot())
        return job

    def get(self, job_id):
        job = self._jobs.get(job_id)
        return job.snapshot() if job is not None else None


# --- helpers -------------------------------------------------------------


def _make_release(tmp_path, tag="v0.6.0", influx_image="influxdb:2.7"):
    releases_dir = tmp_path / "releases"
    root = releases_dir / tag
    (root / "deploy" / "docker").mkdir(parents=True, exist_ok=True)
    (root / "install-docker.sh").write_text("#!/bin/sh\necho installer\n", encoding="utf-8")
    (root / "deploy" / "docker" / "compose.influxdb.yml").write_text(
        f"services:\n  influxdb:\n    image: {influx_image}\n", encoding="utf-8"
    )
    return releases_dir


def _write_config(tmp_path, influx=None):
    target = tmp_path / "generated" / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    config = {"system": {"max_total_power": 800}}
    if influx is not None:
        config["influxdb"] = influx
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return target


def _service(
    tmp_path,
    influx=None,
    docker=None,
    installer=None,
    influx_image="influxdb:2.7",
    compose=None,
    dashboard_probe=None,
):
    releases_dir = _make_release(tmp_path, influx_image=influx_image)
    manager = _FakeReleaseManager(releases_dir)
    target = _write_config(tmp_path, influx)
    service = DeploymentService(
        manager,
        _ConfigExport(target),
        workspace_dir=tmp_path / "deployment",
        docker=docker or _FakeDocker(),
        installer=installer or _FakeInstaller(),
        registry=_SyncRegistry(),
        compose=compose or _FakeCompose(),
        start_registry=_SyncRegistry(),
        dashboard_probe=dashboard_probe or (lambda _url: True),
        sleep=lambda _seconds: None,
        runtime_env={"PUID": "1000", "PGID": "1000"},
        setup_workflows=GuidedSetupWorkflowStore(tmp_path),
    )
    adopt_generated_config(service)
    return service


BUNDLED = {"enabled": True, "mode": "bundled"}
DISABLED = {"enabled": False, "mode": "bundled"}


def _run_prepare(service, overwrite=False):
    result = service.prepare(overwrite=overwrite)
    if not result.get("ok"):
        return result, None
    return result, service.job(result["job"]["job_id"])


def _run_start(service):
    result = service.start()
    if not result.get("ok"):
        return result, None
    return result, service.start_job(result["job"]["job_id"])


def _service_default_workspace(tmp_path, install_root):
    """Build a service that resolves the live target from the install context
    (no explicit ``workspace_dir``), as production does via EMS_INSTALL_DIR."""

    releases_dir = _make_release(tmp_path)
    manager = _FakeReleaseManager(releases_dir)
    target = _write_config(tmp_path)
    service = DeploymentService(
        manager,
        _ConfigExport(target),
        admin_data_dir=Path(install_root) / "data" / "admin",
        docker=_FakeDocker(),
        installer=_FakeInstaller(),
        registry=_SyncRegistry(),
        compose=_FakeCompose(),
        start_registry=_SyncRegistry(),
        dashboard_probe=lambda _url: True,
        sleep=lambda _seconds: None,
        runtime_env={"PUID": "1000", "PGID": "1000"},
        install_context_provider=lambda: SimpleNamespace(
            install_root=Path(install_root)
        ),
        setup_workflows=GuidedSetupWorkflowStore(
            Path(install_root) / "data" / "admin"
        ),
    )
    adopt_generated_config(service)
    return service


def test_job_completion_callback_observes_terminal_success_without_polling(tmp_path):
    registry = DeploymentJobRegistry()
    job = StartJob("start-success", str(tmp_path))
    completed = threading.Event()
    snapshots = []

    registry.submit(
        job,
        lambda handle: handle.succeed(),
        on_complete=lambda snapshot: (snapshots.append(snapshot), completed.set()),
    )

    assert completed.wait(2)
    assert [snapshot["status"] for snapshot in snapshots] == ["succeeded"]


def test_job_completion_callback_observes_terminal_failure_without_polling(tmp_path):
    registry = DeploymentJobRegistry()
    job = StartJob("start-failure", str(tmp_path))
    completed = threading.Event()
    snapshots = []

    def fail(_handle):
        raise DockerError("compose_start_failed", "compose failed")

    registry.submit(
        job,
        fail,
        on_complete=lambda snapshot: (snapshots.append(snapshot), completed.set()),
    )

    assert completed.wait(2)
    assert [snapshot["status"] for snapshot in snapshots] == ["failed"]
    assert snapshots[0]["error"]["code"] == "compose_start_failed"


# --- standard layout / transitional path guard ---------------------------


def test_deployment_targets_standard_install_root_not_admin_deployment_dir(tmp_path):
    install_root = tmp_path / "ems"
    install_root.mkdir()
    service = _service_default_workspace(tmp_path, install_root)

    # Live target is the standard install root, never data/admin/deployment.
    assert service.workspace_dir == install_root
    assert service.workspace_dir.name != "deployment"

    _, job = _run_prepare(service)

    assert job["status"] == "succeeded"
    assert (install_root / "config" / "config.json").is_file()
    assert (install_root / "docker-compose.yml").is_file()
    assert (install_root / "data").is_dir()
    # The transitional live-runtime directory is never created.
    assert not (install_root / "data" / "admin" / "deployment").exists()


def test_admin_marker_and_backups_live_under_admin_data_dir(tmp_path):
    install_root = tmp_path / "ems"
    install_root.mkdir()
    service = _service_default_workspace(tmp_path, install_root)
    admin_dir = install_root / "data" / "admin"

    assert service.marker_path == admin_dir / "state" / ".admin-deployment.json"
    assert service.backup_dir == admin_dir / "backups"

    _run_prepare(service)

    assert service.marker_path.is_file()
    # Admin state never pollutes the install root itself.
    assert not (install_root / ".admin-deployment.json").exists()


def test_prepare_backs_up_existing_config_and_compose(tmp_path):
    install_root = tmp_path / "ems"
    (install_root / "config").mkdir(parents=True)
    (install_root / "config" / "config.json").write_text(
        '{"old": true}\n', encoding="utf-8"
    )
    (install_root / "docker-compose.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    service = _service_default_workspace(tmp_path, install_root)

    # Replacing an existing standard install requires explicit confirmation.
    _, job = _run_prepare(service, overwrite=True)

    assert job["status"] == "succeeded"
    backups = list(service.backup_dir.iterdir())
    names = sorted(path.name for path in backups)
    assert any(name.startswith("config.json.") for name in names)
    assert any(name.startswith("docker-compose.yml.") for name in names)
    saved_config = next(
        path for path in backups if path.name.startswith("config.json.")
    )
    assert saved_config.read_text(encoding="utf-8") == '{"old": true}\n'
    # Backup paths are reported to the UI on the prepared job.
    assert any(str(path) in job["backups"] for path in backups)


# --- existing-install confirmation guard ---------------------------------


def _existing_install_service(tmp_path, config=True, compose=True):
    install_root = tmp_path / "ems"
    (install_root / "config").mkdir(parents=True)
    if config:
        (install_root / "config" / "config.json").write_text(
            '{"old": true}\n', encoding="utf-8"
        )
    if compose:
        (install_root / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
    return install_root, _service_default_workspace(tmp_path, install_root)


@pytest.mark.parametrize(
    ("config", "compose"),
    [(True, False), (False, True), (True, True)],
)
def test_prepare_refuses_existing_install_without_confirmation(
    tmp_path, config, compose
):
    install_root, service = _existing_install_service(
        tmp_path, config=config, compose=compose
    )
    original_config = (
        (install_root / "config" / "config.json").read_bytes() if config else None
    )
    original_compose = (
        (install_root / "docker-compose.yml").read_bytes() if compose else None
    )

    result, job = _run_prepare(service)

    assert job is None
    assert result["ok"] is False
    assert result["reason"] == "existing_install_conflict"
    assert result["status"] == 409
    assert result["requires_confirmation"] is True
    assert result["existing"] == {"config": config, "compose": compose}
    assert result["paths"]["config"] == str(install_root / "config" / "config.json")
    assert result["paths"]["compose"] == str(install_root / "docker-compose.yml")
    assert result["paths"]["data"] == str(install_root / "data")
    # No files are modified and no marker/backups are written before confirmation.
    if config:
        assert (install_root / "config" / "config.json").read_bytes() == original_config
    if compose:
        assert (install_root / "docker-compose.yml").read_bytes() == original_compose
    assert not service.marker_path.exists()
    assert not service.backup_dir.exists()


def test_confirmed_prepare_replaces_files_and_keeps_data(tmp_path):
    install_root, service = _existing_install_service(tmp_path)
    (install_root / "data").mkdir(exist_ok=True)
    runtime_db = install_root / "data" / "runtime-state.json"
    runtime_db.write_text('{"keep": true}\n', encoding="utf-8")
    generated = (tmp_path / "generated" / "config.json").read_bytes()

    _, job = _run_prepare(service, overwrite=True)

    assert job["status"] == "succeeded"
    # Config/compose are replaced with the generated deployment.
    assert (install_root / "config" / "config.json").read_bytes() == generated
    assert (install_root / "docker-compose.yml").is_file()
    # Runtime data under data/ is never deleted.
    assert runtime_db.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert list(service.backup_dir.iterdir())


def test_existing_admin_prepared_install_updates_without_confirmation(tmp_path):
    install_root = tmp_path / "ems"
    install_root.mkdir()
    service = _service_default_workspace(tmp_path, install_root)

    # First prepare makes this an Admin-owned install with a matching marker.
    _, first = _run_prepare(service)
    assert first["status"] == "succeeded"

    # Re-preparing the same release/config is idempotent and needs no confirmation.
    result, second = _run_prepare(service)
    assert result["ok"] is True
    assert second["status"] == "succeeded"


def test_plan_reports_existing_install_state(tmp_path):
    install_root = tmp_path / "ems"
    (install_root / "config").mkdir(parents=True)
    (install_root / "config" / "config.json").write_text("{}\n", encoding="utf-8")
    service = _service_default_workspace(tmp_path, install_root)

    plan = service.plan()

    assert plan["workspace"] == str(install_root)
    existing = plan["existing_install"]
    assert existing["install_root"] == str(install_root)
    assert existing["config_exists"] is True
    assert existing["present"] is True


# --- plan ----------------------------------------------------------------


def test_plan_shows_ems_image_for_selected_release(tmp_path):
    plan = _service(tmp_path).plan()
    assert plan["release"] == "v0.6.0"
    services = {image["service"]: image["image"] for image in plan["images"]}
    assert services["ems"] == "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0"
    assert "influxdb" not in services


def test_plan_includes_influxdb_only_when_bundled_enabled(tmp_path):
    enabled = _service(tmp_path, influx=BUNDLED).plan()
    services = {image["service"] for image in enabled["images"]}
    assert "influxdb" in services
    assert enabled["influxdb"]["planned"] is True

    disabled = _service(tmp_path, influx=DISABLED).plan()
    assert {image["service"] for image in disabled["images"]} == {"ems"}
    assert disabled["influxdb"]["planned"] is False
    assert disabled["influxdb"]["reason"] == "InfluxDB: not enabled in generated config"


def test_influxdb_image_read_from_compose_resource(tmp_path):
    plan = _service(tmp_path, influx=BUNDLED, influx_image="influxdb:2.9-custom").plan()
    influx = next(image for image in plan["images"] if image["service"] == "influxdb")
    assert influx["image"] == "influxdb:2.9-custom"
    assert plan["influxdb"]["image"] == "influxdb:2.9-custom"


def test_plan_reports_not_preparable_without_generated_config(tmp_path):
    releases_dir = _make_release(tmp_path)
    manager = _FakeReleaseManager(releases_dir)
    service = DeploymentService(
        manager,
        _ConfigExport(tmp_path / "generated" / "config.json"),
        workspace_dir=tmp_path / "deployment",
        docker=_FakeDocker(),
        installer=_FakeInstaller(),
        registry=_SyncRegistry(),
    )
    plan = service.plan()
    assert plan["can_prepare"] is False
    assert plan["generated_config"]["ready"] is False


# --- prepare -------------------------------------------------------------


def test_prepare_copies_generated_config_to_workspace(tmp_path):
    service = _service(tmp_path, influx=DISABLED)
    _, job = _run_prepare(service)
    assert job["status"] == "succeeded"
    copied = tmp_path / "deployment" / "config" / "config.json"
    original = (tmp_path / "generated" / "config.json").read_bytes()
    assert copied.read_bytes() == original


def test_prepare_writes_runtime_identity_and_creates_mount_directories(tmp_path):
    service = _service(tmp_path)
    _, job = _run_prepare(service)

    assert job["status"] == "succeeded"
    assert (service.workspace_dir / "config").is_dir()
    assert (service.workspace_dir / "data").is_dir()
    env = (service.workspace_dir / ".env").read_text(encoding="utf-8")
    assert "PUID=1000\n" in env
    assert "PGID=1000\n" in env
    assert job["result"]["permissions_verified"] is True


def test_prepare_prefers_existing_env_identity(tmp_path):
    service = _service(tmp_path)
    service.workspace_dir.mkdir(parents=True)
    (service.workspace_dir / ".env").write_text(
        "PUID=1234\nPGID=1235\nKEEP=value\n", encoding="utf-8"
    )

    _, job = _run_prepare(service)

    assert job["status"] == "succeeded"
    assert job["result"]["puid"] == 1234
    assert job["result"]["pgid"] == 1235
    assert "KEEP=value\n" in (service.workspace_dir / ".env").read_text(encoding="utf-8")


def test_prepare_rejects_missing_non_root_runtime_identity(tmp_path):
    service = _service(tmp_path)
    service._runtime_env = {}

    result, job = _run_prepare(service)

    assert job is None
    assert result["reason"] == "runtime_identity_missing"
    assert not service.workspace_dir.exists()


def test_prepare_does_not_mark_ready_when_permission_repair_fails(tmp_path):
    docker = _FakeDocker()
    docker.permission_repair_error = DockerError(
        "workspace_permission_repair_failed", "Could not repair permissions."
    )
    service = _service(tmp_path, docker=docker)

    _, job = _run_prepare(service)

    assert job["status"] == "failed"
    assert service.plan()["prepared"] is None
    assert not service.marker_path.exists()


def test_prepare_runs_installer_with_no_start_and_pulls_images(tmp_path):
    installer = _FakeInstaller()
    docker = _FakeDocker()
    service = _service(tmp_path, influx=BUNDLED, docker=docker, installer=installer)
    _, job = _run_prepare(service)

    assert job["status"] == "succeeded"
    assert job["prepared"] is True
    assert len(installer.calls) == 1
    call = installer.calls[0]
    assert call["analytics"] is True
    assert call["tag"] == "v0.6.0"
    assert call["script"].endswith("install-docker.sh")
    # Both planned images are pulled with visible progress.
    assert docker.pulled == [
        "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0",
        "influxdb:2.7",
    ]
    ems_image = next(i for i in job["images"] if i["service"] == "ems")
    assert ems_image["status"] == "done"


def test_prepare_installer_skips_analytics_when_influx_disabled(tmp_path):
    installer = _FakeInstaller()
    docker = _FakeDocker()
    service = _service(tmp_path, influx=DISABLED, docker=docker, installer=installer)
    _run_prepare(service)
    assert installer.calls[0]["analytics"] is False
    assert docker.pulled == ["ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0"]


def test_prepare_forwards_installer_output_to_job_log(tmp_path):
    service = _service(tmp_path, influx=BUNDLED, installer=_FakeInstaller())
    _, job = _run_prepare(service)

    # Installer stdout must reach the job log through the on_line callback.
    assert "Wrote docker-compose.yml" in job["log"]


def test_failed_bootstrap_job_surfaces_real_cause(tmp_path):
    tail = (
        "  File \"/app/ems/influx_setup.py\", line 198, in write_env_file\n"
        "    os.makedirs(directory, exist_ok=True)\n"
        "PermissionError: [Errno 13] Permission denied: '/app/deploy'"
    )
    installer = _FakeInstaller(error=deployment._bootstrap_error(tail))
    service = _service(tmp_path, influx=BUNDLED, installer=installer)
    _, job = _run_prepare(service)

    assert job["status"] == "failed"
    error = job["error"]
    # High-level message stays short, but the real cause is not collapsed away.
    assert error["message"].startswith("The bootstrap installer failed")
    assert "PermissionError" in error["detail"]
    assert "/app/deploy" in error["detail"]


def test_bootstrap_error_attaches_tail_and_redacts_secrets(tmp_path):
    tail = "password=hunter2\nPermissionError: [Errno 13] Permission denied: '/app/deploy'"
    error = deployment._bootstrap_error(tail)

    assert error.code == "bootstrap_failed"
    assert "PermissionError" in error.detail
    assert "hunter2" not in error.detail


def test_docker_unavailable_returns_clean_error(tmp_path):
    docker = _FakeDocker(
        check_error=DockerError("docker_cli_missing", "Docker CLI was not found.")
    )
    installer = _FakeInstaller()
    service = _service(tmp_path, influx=DISABLED, docker=docker, installer=installer)
    _, job = _run_prepare(service)

    assert job["status"] == "failed"
    assert job["prepared"] is False
    assert job["error"]["code"] == "docker_cli_missing"
    assert "Docker CLI" in job["error"]["message"]
    # No workspace scaffolding or marker on a failed docker check.
    assert installer.calls == []
    assert service.plan()["prepared"] is None


def test_pull_failure_does_not_mark_prepared(tmp_path):
    docker = _FakeDocker(
        pull_error=DockerError("image_pull_failed", "The image could not be pulled.")
    )
    service = _service(tmp_path, influx=DISABLED, docker=docker)
    _, job = _run_prepare(service)

    assert job["status"] == "failed"
    assert job["prepared"] is False
    assert job["error"]["code"] == "image_pull_failed"
    assert not (tmp_path / "deployment" / ".admin-deployment.json").exists()
    assert service.plan()["prepared"] is None


def test_prepare_is_idempotent_for_same_release_and_config(tmp_path):
    service = _service(tmp_path, influx=DISABLED)
    first, job1 = _run_prepare(service)
    assert job1["status"] == "succeeded"

    second, job2 = _run_prepare(service)
    assert second["ok"] is True
    assert job2["status"] == "succeeded"
    assert service.plan()["prepared"]["release"] == "v0.6.0"


def test_prepare_conflict_on_changed_config_requires_overwrite(tmp_path):
    service = _service(tmp_path, influx=DISABLED)
    _run_prepare(service)

    # Change the generated config so the workspace marker no longer matches.
    _write_config(tmp_path, influx=BUNDLED)
    adopt_generated_config(service)
    conflict, job = _run_prepare(service)
    assert conflict["ok"] is False
    assert conflict["reason"] == "workspace_conflict"
    assert conflict["status"] == 409
    assert job is None

    overwritten, job2 = _run_prepare(service, overwrite=True)
    assert overwritten["ok"] is True
    assert job2["status"] == "succeeded"
    assert service.plan()["prepared"]["config_sha256"] is not None


# --- start ---------------------------------------------------------------


def test_prepare_stamps_the_marker_with_its_workflow(tmp_path):
    service = _service(tmp_path, influx=DISABLED)
    _run_prepare(service)

    marker = json.loads(Path(service.marker_path).read_text(encoding="utf-8"))
    active = service.setup_workflows.active()
    assert marker["workflow_id"] == active["workflow_id"]
    assert marker["preview_id"] == active["preview"]["preview_id"]


def test_start_rejects_a_marker_prepared_by_another_workflow(tmp_path):
    """A superseded workflow's prepared deployment must not start under the
    replacement workflow."""

    service = _service(tmp_path, influx=DISABLED)
    _run_prepare(service)
    store = service.setup_workflows
    store.finish(store.active()["workflow_id"], status="superseded")
    store.ensure_active()

    result, job = _run_start(service)

    assert job is None
    assert result["reason"] == "deployment_marker_invalid"


def test_start_is_blocked_until_deployment_is_prepared(tmp_path):
    result, job = _run_start(_service(tmp_path))
    assert job is None
    assert result["reason"] == "deployment_not_prepared"


def test_start_rejects_missing_workspace_config_and_changed_config(tmp_path):
    missing_compose = _service(tmp_path / "compose")
    _run_prepare(missing_compose)
    (missing_compose.workspace_dir / "docker-compose.yml").unlink()
    result, _ = _run_start(missing_compose)
    assert result["reason"] == "deployment_workspace_missing"

    missing_config = _service(tmp_path / "config")
    _run_prepare(missing_config)
    (missing_config.workspace_dir / "config" / "config.json").unlink()
    result, _ = _run_start(missing_config)
    assert result["reason"] == "generated_config_missing"

    changed_config = _service(tmp_path / "changed")
    _run_prepare(changed_config)
    (changed_config.workspace_dir / "config" / "config.json").write_text(
        '{"changed": true}\n', encoding="utf-8"
    )
    result, _ = _run_start(changed_config)
    assert result["reason"] == "deployment_config_mismatch"

    invalid_marker = _service(tmp_path / "marker")
    _run_prepare(invalid_marker)
    invalid_marker.marker_path.write_text("{}\n", encoding="utf-8")
    result, _ = _run_start(invalid_marker)
    assert result["reason"] == "deployment_marker_invalid"


def test_start_rejects_when_docker_access_is_not_ready(tmp_path):
    docker = _FakeDocker()
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    docker.status = {
        "state": "socket_missing",
        "code": "docker_socket_not_mounted",
        "message": "Docker socket is not mounted.",
    }

    result, job = _run_start(service)
    assert job is None
    assert result["reason"] == "docker_socket_not_mounted"


def test_start_blocks_invalid_workspace_permissions_and_repair_rechecks(tmp_path):
    docker = _FakeDocker()
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    docker.permission_error = DockerError(
        "workspace_permission_denied",
        "Deployment workspace is not writable by EMS.",
        "Failing path: data/",
    )

    result, job = _run_start(service)

    assert job is None
    assert result["reason"] == "workspace_permission_denied"
    assert docker.permission_repairs == [
        (
            str(service.workspace_dir),
            "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0",
            1000,
            1000,
        )
    ]

    repaired = service.repair_workspace_permissions()
    assert repaired == {"ok": True, "repaired": True}
    assert len(docker.permission_repairs) == 2
    assert len(docker.permission_checks) >= 3
    _, started = _run_start(service)
    assert started["status"] == "succeeded"


def test_permission_repair_uses_only_prepared_config_and_data_mounts(tmp_path):
    calls = []

    def _run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    workspace = tmp_path / "deployment"
    (workspace / "config").mkdir(parents=True)
    (workspace / "data").mkdir()
    docker = DockerCli(run=_run)

    docker.repair_workspace_permissions(workspace, "ems:test", 1000, 1000)

    command = calls[0]
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert mounts == [
        f"type=bind,src={workspace / 'config'},dst=/workspace/config",
        f"type=bind,src={workspace / 'data'},dst=/workspace/data",
    ]
    assert all("docker rm" not in value for value in command)


def test_listing_images_keeps_both_identities_of_each_image(tmp_path):
    """A removal names the local ID; a protection record stores the digest."""

    rows = [
        {"ID": "sha256:localid", "Digest": "sha256:repodigest", "Repository": "ems/admin",
         "Tag": "v1", "CreatedAt": "2026-01-02 00:00:00 +0000 UTC"},
        {"ID": "sha256:only", "Digest": "<none>", "Repository": "ems/admin",
         "Tag": "<none>", "CreatedAt": "2026-01-01 00:00:00 +0000 UTC"},
    ]

    def _run(command, **_kwargs):
        assert "--force" not in command
        return SimpleNamespace(
            returncode=0, stdout="\n".join(json.dumps(r) for r in rows), stderr=""
        )

    images = DockerCli(run=_run).list_images("ems/admin")

    assert [image["digest"] for image in images] == ["sha256:localid", "sha256:only"]
    assert images[0]["aliases"] == ["sha256:localid", "sha256:repodigest"]
    assert images[0]["created"] == "2026-01-02 00:00:00 +0000 UTC"


def test_listing_images_of_another_repository_is_discarded(tmp_path):
    """The daemon is asked for one repository; anything else is not ours."""

    row = {"ID": "sha256:x", "Digest": "sha256:x", "Repository": "influxdb",
           "Tag": "2.7", "CreatedAt": "2026-01-01 00:00:00 +0000 UTC"}

    def _run(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="")

    assert DockerCli(run=_run).list_images("ems/admin") == []


def test_listing_images_degrades_instead_of_raising(tmp_path):
    """No candidates is a safe answer; an exception would fail the upgrade."""

    def _missing(*_a, **_k):
        raise FileNotFoundError("docker")

    assert DockerCli(run=_missing).list_images("ems/admin") == []

    def _broken(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout="not json\n{", stderr="")

    assert DockerCli(run=_broken).list_images("ems/admin") == []


def test_removing_an_image_never_forces_and_never_raises(tmp_path):
    """Docker refusing to remove an in-use image is the safety check.

    Forcing would delete the image a running container still needs, which is
    precisely the state retention exists to avoid creating.
    """

    seen = {}

    def _run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert DockerCli(run=_run).remove_image("sha256:abc") is True
    assert seen["command"] == ["docker", "rmi", "sha256:abc"]
    assert "--force" not in seen["command"] and "-f" not in seen["command"]
    # Removing layers is disk work like any other container step.
    assert seen["timeout"] >= 180

    def _refuses(*_a, **_k):
        return SimpleNamespace(returncode=1, stdout="", stderr="image is being used")

    assert DockerCli(run=_refuses).remove_image("sha256:abc") is False

    def _missing(*_a, **_k):
        raise FileNotFoundError("docker")

    assert DockerCli(run=_missing).remove_image("sha256:abc") is False


def test_container_lifecycle_timeouts_survive_a_slow_disk(tmp_path):
    """Container steps are bounded by host fsync latency, not by their own work.

    containerd and dockerd fsync their state at every lifecycle step, so the
    cost is the host's synchronous write latency rather than anything the
    container does. Measured on a Pi 3B+ with a pre-A1 SD card (53 ms per
    fsync): one create+start+delete cycle takes 45-55 s for a trivial
    ``sh -c id`` container, and a 1.5 kB image is no faster than a 363 MB one.

    The previous 30 s bound on the permission check could therefore never pass
    on that hardware, and it surfaced as ``workspace_permission_denied`` -- a
    permission verdict for what was only a slow disk.
    """

    timeouts = []

    def _run(_command, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    workspace = tmp_path / "deployment"
    (workspace / "config").mkdir(parents=True)
    (workspace / "data").mkdir()
    docker = DockerCli(run=_run)

    docker.check_workspace_permissions(workspace, "ems:test", 1000, 1000)
    docker.repair_workspace_permissions(workspace, "ems:test", 1000, 1000)
    docker.stop_container("ems-solarflow")
    docker.remove_container("ems-solarflow")

    # Three times the 55 s worst case measured on the slowest supported host.
    # These bounds catch a hung daemon; they do not pace a healthy slow one.
    assert timeouts, "no docker invocation was recorded"
    assert all(value >= 180 for value in timeouts), timeouts


def test_workspace_permission_detail_lists_resolved_paths_and_runtime_identity(
    tmp_path,
):
    workspace = (tmp_path / "deployment").resolve()

    detail = deployment._workspace_permission_detail(
        workspace, 1000, 1001, "config/"
    )

    assert "runtime user 1000:1001" in detail
    assert f"Workspace: {workspace}" in detail
    assert f"- {workspace / 'config'}" in detail
    assert f"- {workspace / 'data'}" in detail


def test_data_workspace_permission_detail_includes_socket_mapping_hint():
    detail = deployment._workspace_permission_detail(
        Path("/data/deployment"), 1000, 1000, "data/"
    )

    assert "host Docker daemon" in detail
    assert "deploy/admin/start-admin-setup.sh" in detail


def test_start_runs_compose_in_prepared_workspace_with_analytics_profile(tmp_path):
    compose = _FakeCompose()
    service = _service(tmp_path, influx=BUNDLED, compose=compose)
    _run_prepare(service)

    _, job = _run_start(service)

    assert job["status"] == "succeeded"
    assert compose.up_calls == [
        {
            "workspace": str(tmp_path / "deployment"),
            "profiles": ["with-analytics"],
        }
    ]
    assert [step["key"] for step in job["steps"]] == [
        "checking_deployment",
        "starting_containers",
        "checking_containers",
        "checking_dashboard",
    ]
    assert all(step["status"] == "done" for step in job["steps"])
    assert job["dashboard_reachable"] is True


def test_start_announces_healthcheck_before_dashboard_probe(tmp_path):
    events = []

    def dashboard_probe(_url):
        events.append("probe")
        return True

    service = _service(tmp_path, dashboard_probe=dashboard_probe)
    _run_prepare(service)

    result = service.start(
        on_healthcheck=lambda snapshot: events.append(
            ("healthcheck", snapshot["steps"][-1]["key"])
        )
    )
    job = service.start_job(result["job"]["job_id"])

    assert job["status"] == "succeeded"
    assert events == [("healthcheck", "checking_containers"), "probe"]


def test_start_fails_when_only_non_ems_service_is_running(tmp_path):
    compose = _FakeCompose(
        services=[
            {
                "name": "influxdb",
                "service": "influxdb",
                "image": "influxdb:2.7",
                "state": "running",
                "status": "Up",
                "ports": [],
            }
        ]
    )
    service = _service(tmp_path, influx=BUNDLED, compose=compose)
    _run_prepare(service)

    _, job = _run_start(service)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "ems_not_running"
    assert job["steps"][2]["status"] == "failed"


def test_start_classifies_known_ems_workspace_permission_log(tmp_path):
    compose = _FakeCompose(
        services=[],
        logs=(
            "EMS refuses to start as root.\n"
            "The mounted /app/data or /app/config directory is not writable "
            "by the non-root runtime user.\n"
        ),
    )
    service = _service(tmp_path, compose=compose)
    _run_prepare(service)

    _, job = _run_start(service)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "workspace_permission_denied"


def test_failed_start_job_includes_safe_compose_detail(tmp_path):
    compose = _FakeCompose(
        error=DockerError(
            "compose_port_conflict",
            "A required port is already in use.",
            "Bind for 0.0.0.0:8080 failed: port is already allocated",
        )
    )
    service = _service(tmp_path, compose=compose)
    _run_prepare(service)

    _, job = _run_start(service)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "compose_port_conflict"
    assert "port is already allocated" in job["error"]["detail"]


def test_start_retries_dashboard_probe_without_restarting_containers(tmp_path):
    attempts = []

    def _probe(_url):
        attempts.append(True)
        return len(attempts) == 3

    compose = _FakeCompose()
    service = _service(tmp_path, compose=compose, dashboard_probe=_probe)
    _run_prepare(service)

    _, job = _run_start(service)

    assert job["status"] == "succeeded"
    assert job["dashboard_reachable"] is True
    assert len(attempts) == 3
    assert len(compose.up_calls) == 1


def _set_fixed_ems_container(service):
    (service.workspace_dir / "docker-compose.yml").write_text(
        "services:\n  ems:\n    container_name: ems-solarflow-api-control\n",
        encoding="utf-8",
    )


def _set_fixed_ems_and_influx_containers(service):
    (service.workspace_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  ems:\n"
        "    container_name: ems-solarflow-api-control\n"
        "  influxdb:\n"
        "    container_name: ems-influxdb\n",
        encoding="utf-8",
    )


def test_start_preflight_detects_stopped_container_conflict(tmp_path):
    docker = _FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "container_id": "9fffad73b1f2",
                "image": "ems:latest",
                "status": "exited",
                "status_detail": "Exited (1) 9 hours ago",
            }
        }
    )
    compose = _FakeCompose()
    service = _service(tmp_path, docker=docker, compose=compose)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    _, job = _run_start(service)

    assert job["status"] == "failed"
    assert job["conflict"]["type"] == "container_name_conflict"
    assert job["conflict"]["safe_fix_available"] is True
    assert job["conflict"]["selected_image"].endswith(":v0.6.0")
    assert compose.up_calls == []


def test_running_container_conflict_is_not_safely_removable(tmp_path):
    docker = _FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "container_id": "abc",
                "image": "ems:old",
                "status": "running",
                "status_detail": "Up 2 hours",
            }
        }
    )
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    _, job = _run_start(service)

    assert job["conflict"]["safe_fix_available"] is False
    assert job["conflict"]["image_mismatch"] is True
    assert job["conflict"]["replace_available"] is True
    refused = service.resolve_container_conflict(
        "ems-solarflow-api-control", "remove_stopped_and_continue"
    )
    assert refused["reason"] == "container_not_stopped"
    assert docker.removed == []


def test_replace_running_image_conflict_stops_removes_and_continues(tmp_path):
    name = "ems-solarflow-api-control"
    docker = _FakeDocker(
        containers={
            name: {
                "container_name": name,
                "container_id": "abc",
                "image": "ghcr.io/basecubedev/ems-solarflow-api-control:latest",
                "status": "running",
            }
        }
    )
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    resolved = service.resolve_container_conflict(
        name, "replace_running_and_continue"
    )
    _, job = _run_start(service)

    assert resolved["ok"] is True
    assert resolved["replaced"] is True
    assert docker.stopped == [name]
    assert docker.removed == [name]
    assert job["status"] == "succeeded"
    assert "Replaced running container" in job["steps"][0]["label"]


@pytest.mark.parametrize(
    ("container", "expected_reason"),
    [
        (None, "container_conflict_changed"),
        ({"image": "ems:old", "status": "exited"}, "container_conflict_changed"),
        (
            {
                "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0",
                "status": "running",
            },
            "container_conflict_changed",
        ),
    ],
)
def test_replace_running_conflict_rejects_changed_state(
    tmp_path, container, expected_reason
):
    name = "ems-solarflow-api-control"
    docker = _FakeDocker(containers={name: container} if container else {})
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    result = service.resolve_container_conflict(
        name, "replace_running_and_continue"
    )

    assert result["reason"] == expected_reason
    assert docker.stopped == []
    assert docker.removed == []


def test_replace_running_conflict_rejects_unknown_name_and_missing_image(tmp_path):
    name = "ems-solarflow-api-control"
    docker = _FakeDocker(
        containers={name: {"image": "ems:old", "status": "running"}}
    )
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    unknown = service.resolve_container_conflict(
        "unrelated-container", "replace_running_and_continue"
    )
    marker = json.loads(service.marker_path.read_text(encoding="utf-8"))
    marker["images"] = []
    service.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    missing = service.resolve_container_conflict(
        name, "replace_running_and_continue"
    )

    assert unknown["reason"] == "unknown_container_name"
    assert missing["reason"] == "selected_image_missing"
    assert docker.stopped == []


def test_status_running_selected_image_has_no_conflict(tmp_path):
    name = "ems-solarflow-api-control"
    selected = "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0"
    docker = _FakeDocker(
        containers={name: {"image": selected, "status": "running"}}
    )
    compose = _FakeCompose(
        services=[{"name": name, "service": "ems", "image": selected, "state": "running"}]
    )
    service = _service(tmp_path, docker=docker, compose=compose)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    status = service.status()

    assert status["running"] is True
    assert status["conflict"] is None


def test_start_running_selected_image_is_not_a_name_conflict(tmp_path):
    name = "ems-solarflow-api-control"
    selected = "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0"
    docker = _FakeDocker(
        containers={name: {"image": selected, "status": "running"}}
    )
    compose = _FakeCompose(
        services=[{"name": name, "service": "ems", "image": selected, "state": "running"}]
    )
    service = _service(tmp_path, docker=docker, compose=compose)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    _, job = _run_start(service)

    assert job["status"] == "succeeded"
    assert job.get("conflict") is None


def test_status_running_different_image_is_not_selected_deployment(tmp_path):
    name = "ems-solarflow-api-control"
    existing = "ghcr.io/basecubedev/ems-solarflow-api-control:latest"
    docker = _FakeDocker(
        containers={name: {"image": existing, "status": "running"}}
    )
    compose = _FakeCompose(
        services=[{"name": name, "service": "ems", "image": existing, "state": "running"}]
    )
    service = _service(tmp_path, docker=docker, compose=compose)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    status = service.status()

    assert status["running"] is False
    assert status["dashboard_reachable"] is False
    assert status["conflict"]["image_mismatch"] is True
    assert status["conflict"]["replace_available"] is True


def test_resolve_conflict_removes_only_known_stopped_container_without_volumes(tmp_path):
    docker = _FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "container_id": "abc",
                "image": "ems:old",
                "status": "dead",
            }
        }
    )
    service = _service(tmp_path, docker=docker)
    _run_prepare(service)
    _set_fixed_ems_container(service)

    unknown = service.resolve_container_conflict(
        "unrelated-container", "remove_stopped_and_continue"
    )
    resolved = service.resolve_container_conflict(
        "ems-solarflow-api-control", "remove_stopped_and_continue"
    )
    _, job = _run_start(service)

    assert unknown["reason"] == "unknown_container_name"
    assert resolved["ok"] is True
    assert docker.removed == ["ems-solarflow-api-control"]
    assert job["status"] == "succeeded"
    assert job["steps"][0]["key"].startswith("resolved_container_conflict")


def test_resolve_conflicts_reports_each_stack_container_before_start(tmp_path):
    docker = _FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "container_id": "ems-old",
                "image": "ems:old",
                "status": "exited",
            },
            "ems-influxdb": {
                "container_name": "ems-influxdb",
                "container_id": "influx-old",
                "image": "influxdb:2.7",
                "status": "exited",
            },
        }
    )
    compose = _FakeCompose()
    service = _service(tmp_path, influx=BUNDLED, docker=docker, compose=compose)
    _run_prepare(service)
    _set_fixed_ems_and_influx_containers(service)

    first = service.resolve_container_conflict(
        "ems-solarflow-api-control", "remove_stopped_and_continue"
    )

    assert first["ok"] is True
    assert first["continue"] is False
    assert first["conflict"]["container_name"] == "ems-influxdb"
    assert docker.removed == ["ems-solarflow-api-control"]
    assert compose.up_calls == []

    second = service.resolve_container_conflict(
        "ems-influxdb", "remove_stopped_and_continue"
    )
    _, job = _run_start(service)

    assert second["ok"] is True
    assert second["continue"] is True
    assert second["conflict"] is None
    assert docker.removed == ["ems-solarflow-api-control", "ems-influxdb"]
    assert job["status"] == "succeeded"


def test_status_keeps_structured_conflict_visible(tmp_path):
    docker = _FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "container_id": "abc",
                "image": "ems:old",
                "status": "exited",
            }
        }
    )
    service = _service(tmp_path, docker=docker, compose=_FakeCompose(services=[]))
    _run_prepare(service)
    _set_fixed_ems_container(service)

    status = service.status()

    assert status["running"] is False
    assert status["conflict"]["container_name"] == "ems-solarflow-api-control"
    assert status["conflict"]["safe_fix_available"] is True


def test_status_reports_ems_and_influx_service_state(tmp_path):
    compose = _FakeCompose(
        services=[
            {
                "name": "ems",
                "service": "ems",
                "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0",
                "state": "running",
                "status": "Up 5 seconds",
                "ports": ["8080:8080/tcp"],
            },
            {
                "name": "influxdb",
                "service": "influxdb",
                "image": "influxdb:2.7",
                "state": "running",
                "status": "Up 5 seconds",
                "ports": ["8086:8086/tcp"],
            },
        ]
    )
    service = _service(tmp_path, influx=BUNDLED, compose=compose)
    _run_prepare(service)

    status = service.status()

    assert status["prepared"] is True
    assert status["running"] is True
    assert status["docker"]["state"] == "ready"
    assert {item["service"] for item in status["services"]} == {"ems", "influxdb"}
    assert status["dashboard_url"] == "http://localhost:8080"
    assert status["dashboard_reachable"] is True


# --- installer / docker CLI wrappers ------------------------------------


class _FakeProcess:
    def __init__(self, lines, returncode):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def _make_popen(recorder, lines=(), returncode=0):
    def _popen(command, cwd=None, **_kwargs):
        recorder.append({"command": command, "cwd": cwd})
        return _FakeProcess(list(lines), returncode)

    return _popen


def test_bootstrap_installer_uses_no_start_and_never_starts(tmp_path):
    recorder = []
    installer = BootstrapInstaller(
        popen=_make_popen(recorder, lines=["Wrote docker-compose.yml"])
    )
    installer.prepare(tmp_path, tmp_path / "install-docker.sh", analytics=True, tag="v0.6.0")

    command = recorder[0]["command"]
    assert command[:3] == ["sh", str(tmp_path / "install-docker.sh"), "--no-start"]
    assert "--analytics" in command
    assert "--tag" in command and "v0.6.0" in command
    # No start command is ever issued in Step 04.
    assert "up" not in command
    assert "stack" not in command


def test_bootstrap_installer_omits_tag_for_latest(tmp_path):
    recorder = []
    installer = BootstrapInstaller(popen=_make_popen(recorder))
    installer.prepare(tmp_path, tmp_path / "install-docker.sh", analytics=False, tag="latest")
    command = recorder[0]["command"]
    assert "--tag" not in command
    assert "--analytics" not in command


def test_bootstrap_installer_maps_failure_to_clean_error(tmp_path):
    recorder = []
    installer = BootstrapInstaller(
        popen=_make_popen(recorder, lines=["error: Docker is not installed."], returncode=1)
    )
    with pytest.raises(DockerError) as exc:
        installer.prepare(tmp_path, tmp_path / "install-docker.sh")
    assert exc.value.code == "docker_cli_missing"


def test_a_one_off_command_killed_by_its_ceiling_says_it_timed_out(tmp_path):
    """The ceilings that contain these commands rest on the kill being legible.

    A schema sync stopped by its ceiling leaves buckets without downsampling
    tasks, and the trade is only defensible because the operator is told. A
    message that reads the same as a broken Docker installation sends them
    looking in the wrong place.
    """

    import subprocess

    def _timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    compose = DockerCompose(run=_timing_out)

    with pytest.raises(DockerError) as exc:
        compose.run_oneoff(tmp_path, "ems", ["python3", "emsctl.py", "influx", "sync"], timeout=240)

    assert exc.value.code == "docker_compose_run_timeout"
    assert "240 seconds" in exc.value.message


def _oneoff_run_recorder(on_run=None, rm_returncode=0):
    """Record argv *and* kwargs: the ceilings live in the kwargs."""

    calls = []

    def _run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        if on_run is not None and argv[:3] == ["docker", "compose", "run"]:
            on_run(argv, kwargs)
        if argv[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=rm_returncode, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, _run


def _oneoff_container_name(call):
    argv, _kwargs = call
    return argv[argv.index("--name") + 1]


def test_a_one_off_container_is_named_so_it_can_be_found_again(tmp_path):
    """``--rm`` runs in the client, so a killed client cannot clean up after itself.

    The name is the only handle on the container that outlives the client, and
    it has to be on the command before the command can be killed.
    """

    calls, run = _oneoff_run_recorder()
    compose = DockerCompose(run=run)

    compose.run_oneoff(tmp_path, "ems", ["python3", "emsctl.py", "influx", "sync"])

    argv = calls[0][0]
    assert argv[:5] == ["docker", "compose", "run", "--rm", "--name"]
    assert argv[5].startswith(deployment.ONEOFF_CONTAINER_PREFIX)


def test_the_name_stays_an_option_when_stdin_is_piped(tmp_path):
    """``-T`` joins the same option list, and every option precedes the service.

    A name that slipped past the service name would be read as part of the
    command, and the cleanup would then have nothing to remove. This path has
    its own caller (the restore that pipes a password), so it has its own test.
    """

    calls, run = _oneoff_run_recorder()

    DockerCompose(run=run).run_oneoff(
        tmp_path, "ems", ["python3", "emsctl.py", "restore"], input_text="secret\n"
    )

    argv = calls[0][0]
    assert argv[:4] == ["docker", "compose", "run", "--rm"]
    assert argv.index("--name") < argv.index("ems")
    assert argv.index("-T") < argv.index("ems")
    assert argv[argv.index("--name") + 1].startswith(
        deployment.ONEOFF_CONTAINER_PREFIX
    )
    assert calls[0][1]["input"] == "secret\n"


def test_two_one_off_runs_never_share_a_container_name(tmp_path):
    """A fixed name would turn one leftover into a permanent block.

    Every later run would fail on the name conflict rather than on whatever
    the operator was actually trying to do.
    """

    calls, run = _oneoff_run_recorder()
    compose = DockerCompose(run=run)

    compose.run_oneoff(tmp_path, "ems", ["python3", "emsctl.py", "status"])
    compose.run_oneoff(tmp_path, "ems", ["python3", "emsctl.py", "status"])

    assert _oneoff_container_name(calls[0]) != _oneoff_container_name(calls[1])


def test_a_one_off_killed_by_its_ceiling_takes_its_container_with_it(tmp_path):
    """The container outlives the client that was killed, and keeps working.

    An operator told to retry a timed-out `influx sync` would then be running
    two of them at once, and `schema.sync` is not safe against a second copy of
    itself: both can create the same downsampling task.
    """

    import subprocess

    def _timeout(argv, kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    calls, run = _oneoff_run_recorder(on_run=_timeout)
    compose = DockerCompose(run=run)

    with pytest.raises(DockerError) as exc:
        compose.run_oneoff(
            tmp_path, "ems", ["python3", "emsctl.py", "influx", "sync"], timeout=240
        )

    assert exc.value.code == "docker_compose_run_timeout"
    name = _oneoff_container_name(calls[0])
    assert calls[1][0] == ["docker", "rm", "--force", "--volumes", name]
    # Unbounded, it would hang the Admin worker on exactly the wedged daemon
    # that caused the timeout -- the same failure one layer down.
    assert calls[1][1]["timeout"] == deployment.ONEOFF_REMOVE_TIMEOUT_SECONDS


def test_a_removed_container_is_not_mentioned_to_the_operator(tmp_path):
    """Nothing survived, so there is nothing to act on.

    `docker rm --force` on a container that was never created exits 0 too, so a
    run killed before the container existed stays quiet as well.
    """

    import subprocess

    def _timeout(argv, kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    _calls, run = _oneoff_run_recorder(on_run=_timeout)

    with pytest.raises(DockerError) as exc:
        DockerCompose(run=run).run_oneoff(tmp_path, "ems", ["python3", "-V"])

    assert deployment.ONEOFF_CONTAINER_PREFIX not in exc.value.message
    assert "still be running" not in exc.value.message


def test_a_cleanup_that_failed_names_the_container_it_left_behind(tmp_path):
    """Silence here sends the operator into the collision the change prevents.

    The advice after a timeout is to run it again; if the removal failed, doing
    so puts a second `influx sync` beside the first, which is how two active
    downsampling tasks of one name appear. The container is named so it can be
    removed by hand.
    """

    import subprocess

    def _timeout(argv, kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    calls, run = _oneoff_run_recorder(on_run=_timeout, rm_returncode=1)

    with pytest.raises(DockerError) as exc:
        DockerCompose(run=run).run_oneoff(
            tmp_path, "ems", ["python3", "emsctl.py", "influx", "sync"]
        )

    assert exc.value.code == "docker_compose_run_timeout"
    assert _oneoff_container_name(calls[0]) in exc.value.message
    assert "still be running" in exc.value.message
    # Callers that render only their own summary still have to pass it on, so
    # the container is on the error as a value rather than only in prose.
    assert exc.value.leftover == _oneoff_container_name(calls[0])


def test_a_removed_container_leaves_nothing_for_a_caller_to_pass_on(tmp_path):
    """`leftover` is what makes a caller widen its summary, so it must be unset
    whenever there is nothing for the operator to do."""

    import subprocess

    def _timeout(argv, kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    _calls, run = _oneoff_run_recorder(on_run=_timeout)

    with pytest.raises(DockerError) as exc:
        DockerCompose(run=run).run_oneoff(tmp_path, "ems", ["python3", "-V"])

    assert exc.value.leftover is None


def test_a_cleanup_that_raises_still_reports_the_timeout(tmp_path):
    """The timeout is the diagnosis; a broken `docker rm` must not replace it.

    Reporting the cleanup instead would point the operator at Docker when what
    happened was a slow InfluxDB.
    """

    import subprocess

    def _run(argv, **kwargs):
        if argv[:3] == ["docker", "compose", "run"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        raise OSError("docker went away")

    with pytest.raises(DockerError) as exc:
        DockerCompose(run=_run).run_oneoff(
            tmp_path, "ems", ["python3", "emsctl.py", "influx", "sync"]
        )

    assert exc.value.code == "docker_compose_run_timeout"
    assert "still be running" in exc.value.message


def test_an_unreadable_removal_result_does_not_replace_the_diagnosis(tmp_path):
    """The removal is judged inside the handler that reports the timeout.

    Anything raising there loses the timeout entirely and hands the caller an
    exception about the cleanup instead of the failure it was cleaning up after.
    """

    import subprocess

    def _run(argv, **kwargs):
        if argv[:3] == ["docker", "compose", "run"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return SimpleNamespace(returncode=None, stdout="", stderr="")

    with pytest.raises(DockerError) as exc:
        DockerCompose(run=_run).run_oneoff(tmp_path, "ems", ["python3", "-V"])

    assert exc.value.code == "docker_compose_run_timeout"
    assert exc.value.leftover is not None


def test_the_one_off_ceiling_is_the_number_that_was_chosen(tmp_path):
    """Both halves matter: that it is named, and which number it is.

    Asserting only that the default equals the constant compares the value with
    itself and would hold at 18 seconds as readily as at 180.
    """

    recorded = {}

    def _run(argv, **kwargs):
        recorded["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    DockerCompose(run=_run).run_oneoff(tmp_path, "ems", ["python3", "-V"])

    assert recorded["timeout"] == deployment.ONEOFF_TIMEOUT_SECONDS
    assert deployment.ONEOFF_TIMEOUT_SECONDS == 180
    # One create+start cycle costs 45-55 s on the slow reference hardware, and
    # a removal is the same kind of step.
    assert deployment.ONEOFF_REMOVE_TIMEOUT_SECONDS >= 55


def test_every_compose_double_matches_the_real_run_oneoff_signature():
    """A double is a copy of a signature, so something has to compare them.

    What this enforces is the defaults' values and the parameter list, not how
    a default is spelled: a double left at the literal 180 passes while the
    constant is 180 and fails the moment it moves, which is the drift that
    matters. None of them carried `input_text` at all, so the restore path was
    modelled by a double that could not have taken the password it pipes.

    Doubles defined inside a test function cannot be reached from here; they
    are covered only by the tests that use them.
    """

    import inspect

    from tests.test_admin_container_actions import FakeCompose as ActionsCompose
    from tests.test_admin_guided_upgrade import FakeCompose as UpgradeCompose

    real = inspect.signature(DockerCompose.run_oneoff)

    for double in (ActionsCompose, UpgradeCompose):
        assert inspect.signature(double.run_oneoff) == real, (
            f"{double.__module__}.{double.__qualname__} no longer models "
            "DockerCompose.run_oneoff"
        )


def test_docker_compose_start_uses_prepared_workspace_and_no_pull(tmp_path):
    recorder = []
    compose = DockerCompose(popen=_make_popen(recorder, lines=["Container ems Started"]))
    compose.up(tmp_path, profiles=["with-analytics"])

    assert recorder == [
        {
            "command": [
                "docker",
                "compose",
                "--profile",
                "with-analytics",
                "up",
                "-d",
            ],
            "cwd": str(tmp_path),
        }
    ]
    assert "pull" not in recorder[0]["command"]


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("Bind for 0.0.0.0:8080 failed: port is already allocated", "compose_port_conflict"),
        (
            'Conflict. The container name "/ems" is already in use by container "abc".',
            "compose_container_name_conflict",
        ),
        ("pull access denied for private/ems, repository does not exist", "compose_image_unavailable"),
        (
            "EMS refuses to start as root. The mounted /app/data or /app/config "
            "directory is not writable by the non-root runtime user.",
            "workspace_permission_denied",
        ),
    ],
)
def test_docker_compose_start_classifies_common_failures(tmp_path, output, code):
    compose = DockerCompose(
        popen=_make_popen([], lines=[output], returncode=1)
    )
    with pytest.raises(DockerError) as exc:
        compose.up(tmp_path)
    assert exc.value.code == code
    assert exc.value.detail == output
    if code == "compose_container_name_conflict":
        assert exc.value.conflict["container_name"] == "ems"


def test_docker_cli_inspects_stops_and_removes_without_volume_flag():
    calls = []

    def _run(command, **_kwargs):
        calls.append(command)
        if command[1] == "ps":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ID": "abc",
                        "Image": "ems:old",
                        "State": "exited",
                        "Status": "Exited (1)",
                        "Names": "ems-solarflow-api-control",
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")

    docker = DockerCli(run=_run)
    existing = docker.inspect_container("ems-solarflow-api-control")
    docker.stop_container("ems-solarflow-api-control")
    docker.remove_container("ems-solarflow-api-control")

    assert existing["status"] == "exited"
    assert calls[0][4] == "name=^/ems-solarflow-api-control$"
    assert calls[1] == [
        "docker",
        "stop",
        "--time",
        "20",
        "ems-solarflow-api-control",
    ]
    assert calls[2] == ["docker", "rm", "ems-solarflow-api-control"]
    assert "-v" not in calls[2]


def test_docker_compose_status_normalizes_services_and_ports(tmp_path):
    output = json.dumps(
        [
            {
                "Name": "ems",
                "Service": "ems",
                "Image": "ems:test",
                "State": "running",
                "Status": "Up",
                "Publishers": [
                    {
                        "PublishedPort": 8080,
                        "TargetPort": 8080,
                        "Protocol": "tcp",
                    }
                ],
            }
        ]
    )

    def _run(command, cwd=None, **_kwargs):
        assert command == ["docker", "compose", "ps", "--format", "json"]
        assert cwd == str(tmp_path)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    services = DockerCompose(run=_run).ps(tmp_path)
    assert services == [
        {
            "name": "ems",
            "service": "ems",
            "image": "ems:test",
            "state": "running",
            "status": "Up",
            "ports": ["8080:8080/tcp"],
        }
    ]


def test_docker_cli_pull_reports_progress_and_command():
    recorder = []
    lines = [
        "abc123: Pulling fs layer",
        "abc123: Download complete",
        "Status: Downloaded newer image for ems:latest",
    ]
    docker = DockerCli(popen=_make_popen(recorder, lines=lines, returncode=0))
    seen = []
    docker.pull("ems:latest", on_progress=lambda percent, _line: seen.append(percent))
    assert recorder[0]["command"] == ["docker", "pull", "ems:latest"]
    assert seen[-1] == 100


def test_docker_cli_pull_failure_raises_clean_error():
    docker = DockerCli(
        popen=_make_popen([], lines=["denied: requested access to the resource is denied"], returncode=1)
    )
    with pytest.raises(DockerError) as exc:
        docker.pull("ems:bad")
    assert exc.value.code == "image_pull_failed"


def test_docker_cli_check_missing_cli_is_clean():
    def _run(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    with pytest.raises(DockerError) as exc:
        DockerCli(run=_run).check()
    assert exc.value.code == "docker_cli_missing"


def test_docker_cli_check_daemon_unreachable(tmp_path):
    socket = tmp_path / "docker.sock"
    socket.write_bytes(b"")  # present but the daemon does not answer

    def _run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        )

    with pytest.raises(DockerError) as exc:
        DockerCli(run=_run, socket_path=str(socket)).check()
    assert exc.value.code == "docker_daemon_unreachable"


def test_docker_probe_socket_not_mounted_is_distinct_from_client_missing(tmp_path):
    """A present CLI + missing socket is discovery-only, not a missing client."""

    def _run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        )

    status = DockerCli(run=_run, socket_path=str(tmp_path / "absent.sock")).probe()
    assert status["state"] == "socket_missing"
    assert status["code"] == "docker_socket_not_mounted"
    assert status["mode"] == "discovery_only"


def test_docker_probe_client_missing(tmp_path):
    def _run(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    # Even with a socket present, a missing CLI is reported as client_missing.
    socket = tmp_path / "docker.sock"
    socket.write_bytes(b"")
    status = DockerCli(run=_run, socket_path=str(socket)).probe()
    assert status["state"] == "client_missing"
    assert status["code"] == "docker_cli_missing"
    assert status["mode"] == "discovery_only"


def test_docker_probe_permission_denied(tmp_path):
    socket = tmp_path / "docker.sock"
    socket.write_bytes(b"")

    def _run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="permission denied while trying to connect to the Docker daemon socket",
        )

    status = DockerCli(run=_run, socket_path=str(socket)).probe()
    assert status["state"] == "permission_denied"
    assert status["code"] == "docker_permission_denied"


def test_docker_probe_ready_reports_server_version(tmp_path):
    socket = tmp_path / "docker.sock"
    socket.write_bytes(b"")

    def _run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="27.5.1\n", stderr="")

    status = DockerCli(run=_run, socket_path=str(socket)).probe()
    assert status["state"] == "ready"
    assert status["code"] is None
    assert status["server_version"] == "27.5.1"
    assert status["mode"] == "deployment_controller"


def test_plan_includes_docker_status(tmp_path):
    plan = _service(tmp_path).plan()
    assert plan["docker"]["state"] == "ready"
    assert plan["docker"]["mode"] == "deployment_controller"


def test_parse_pull_progress_counts_completed_layers():
    state = {}
    assert parse_pull_progress(state, "l1: Pulling fs layer") == 0
    parse_pull_progress(state, "l2: Pulling fs layer")
    assert parse_pull_progress(state, "l1: Pull complete") == 50
    assert parse_pull_progress(state, "Status: Downloaded newer image") == 100


def _ps_run(stdout):
    def _run(cmd, **kwargs):
        del cmd, kwargs
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return _run


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json\n",
        "{ broken\n",
        '{"Names": "ems-admin-updater-op-1"}\nnot-json\n',
    ],
)
def test_inspect_container_refuses_to_read_unusable_docker_output(stdout):
    """Unreadable output is a failure, never proof that a container is absent.

    ``docker ps`` exiting 0 with output this wrapper cannot parse says nothing
    about what is running. Returning ``None`` there is indistinguishable from a
    verified absence, and callers that must not act on a guess would act.
    """

    docker = DockerCli(run=_ps_run(stdout))

    with pytest.raises(DockerError) as excinfo:
        docker.inspect_container("ems-admin-updater-op-1")

    assert excinfo.value.code == "docker_container_inspect_unreadable"


@pytest.mark.parametrize("stdout", ["not-json\n", "{ broken\n"])
def test_list_containers_refuses_to_read_unusable_docker_output(stdout):
    docker = DockerCli(run=_ps_run(stdout))

    with pytest.raises(DockerError) as excinfo:
        docker.list_containers("ems-admin-updater-")

    assert excinfo.value.code == "docker_container_inspect_unreadable"


def test_container_reads_survive_blank_and_absent_docker_output():
    """Empty output is a valid answer: nothing matched the filter."""

    docker = DockerCli(run=_ps_run("\n\n"))

    assert docker.inspect_container("ems-admin-updater-op-1") is None
    assert docker.list_containers("ems-admin-updater-") == []


def test_inspect_container_still_reads_a_well_formed_row():
    row = json.dumps(
        {"Names": "/ems-admin-updater-op-1", "ID": "abc", "State": "running"}
    )
    docker = DockerCli(run=_ps_run(row + "\n"))

    container = docker.inspect_container("ems-admin-updater-op-1")

    assert container["container_name"] == "ems-admin-updater-op-1"
    assert container["status"] == "running"


# --- a docker call that goes silent is not a docker call that is working ---
#
# A pull or a recreate holds the thread that started it, and in the Admin that
# thread owns the operation claim the abandon escape is gated on. Neither had
# any bound at all: a stalled daemon kept the pipe open, the claim was never
# released, and the console told the operator to wait for an operation that was
# never going to finish -- the one wedge that outlasts even the deadline.


class _SilentProcess:
    """Says one thing, then nothing, until something kills it."""

    def __init__(self, returncode=-9):
        self.killed = threading.Event()
        self._returncode = returncode
        self._spoke = False

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if not self._spoke:
            self._spoke = True
            return "abc123: Pulling fs layer\n"
        # Bounded only so a broken watchdog fails the test instead of hanging it.
        assert self.killed.wait(10), "the stalled process was never killed"
        raise StopIteration

    def kill(self):
        self.killed.set()

    def wait(self):
        return self._returncode


def _every_reading_is_a_stall():
    """A clock on which any two readings lie further apart than any timeout.

    The watchdog decides on this clock, never on wall time; the timeout the
    wrappers are given below only paces how often the watchdog looks.
    """

    ticks = itertools.count(0, 100)
    return lambda: next(ticks)


def _stalls_once_spoken(lines):
    """A clock that stands still until ``lines`` holds the first line.

    The reader stamps its last-spoke time before it hands the line on, so the
    first stall reading can only come after that line was captured: the kill
    is ordered after the child has said who it is.
    """

    ticks = itertools.count(100, 100)
    return lambda: next(ticks) if lines else 0.0


def _process_state(pid):
    """The kernel's one-letter state for ``pid``, or ``None`` once it is gone.

    A task reaped between the open and the read answers ESRCH rather than
    vanishing from the filesystem, and both mean gone.
    """

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return None


def test_a_reaped_child_reads_as_gone_rather_than_raising(monkeypatch):
    """``/proc/<pid>/stat`` can be opened and then refuse to be read.

    A task reaped between the open and the read answers ESRCH, which is a
    ProcessLookupError and not the FileNotFoundError a missing directory
    raises. Both mean the child is gone, which is the only thing the caller
    asks; letting one of them out turns a successful kill into a test failure,
    and that is how the full suite reported one while the child was dead.
    """

    def _reaped(*_args, **_kwargs):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr("builtins.open", _reaped)
    assert _process_state(4242) is None


class _TalkingProcess:
    """Slow, but never silent: every line resets the watchdog."""

    def __init__(self, lines):
        self.killed = threading.Event()
        self.stdout = iter(lines)

    def kill(self):
        self.killed.set()

    def wait(self):
        return 0


def test_a_stall_whose_process_exited_zero_is_still_a_stall():
    """The exit code left behind by the kill is not the verdict.

    The stalled command is rarely the direct child, and the direct child can be
    a shell that already exited 0 while the child holding the pipe kept the read
    blocked. Its 0 says the shell ended well, not that the work finished: the
    read ended because the watchdog killed the group. Reading the code as the
    verdict reports a command that was killed mid-run as a completed one.
    """

    process = _SilentProcess(returncode=0)
    docker = DockerCli(
        popen=lambda *_a, **_k: process,
        stall_timeout=0.05,
        monotonic=_every_reading_is_a_stall(),
    )

    with pytest.raises(DockerError) as exc:
        docker.pull("ems:latest")

    assert exc.value.code == "image_pull_stalled"
    assert process.killed.is_set()


def test_a_pull_that_goes_silent_is_killed_rather_than_waited_on():
    process = _SilentProcess()
    docker = DockerCli(
        popen=lambda *_a, **_k: process,
        stall_timeout=0.05,
        monotonic=_every_reading_is_a_stall(),
    )

    with pytest.raises(DockerError) as exc:
        docker.pull("ems:latest")

    assert exc.value.code == "image_pull_stalled"
    assert process.killed.is_set()


def test_a_slow_pull_that_keeps_talking_is_never_killed():
    lines = [
        "abc123: Pulling fs layer\n",
        "abc123: Download complete\n",
        "Status: Downloaded newer image for ems:latest\n",
    ]
    process = _TalkingProcess(lines)
    ticks = iter([step * 1.0 for step in range(100)])
    docker = DockerCli(
        popen=lambda *_a, **_k: process,
        stall_timeout=10,
        monotonic=lambda: next(ticks),
    )
    seen = []

    docker.pull("ems:latest", on_progress=lambda percent, _line: seen.append(percent))

    assert not process.killed.is_set()
    assert seen[-1] == 100


def test_a_compose_recreate_that_goes_silent_is_killed_too(tmp_path):
    process = _SilentProcess()
    compose = DockerCompose(
        popen=lambda *_a, **_k: process,
        stall_timeout=0.05,
        monotonic=_every_reading_is_a_stall(),
    )

    with pytest.raises(DockerError) as exc:
        compose.up(tmp_path, services=("ems",), force_recreate=True)

    assert exc.value.code == "compose_up_stalled"
    assert process.killed.is_set()


def test_a_compose_stop_that_goes_silent_is_killed_too(tmp_path):
    """Same wrapper, same pipe, same thread holding the same claim."""

    process = _SilentProcess()
    compose = DockerCompose(
        popen=lambda *_a, **_k: process,
        stall_timeout=0.05,
        monotonic=_every_reading_is_a_stall(),
    )

    with pytest.raises(DockerError) as exc:
        compose.stop(tmp_path, services=("influxdb",))

    assert exc.value.code == "compose_stop_stalled"
    assert process.killed.is_set()


def test_a_bootstrap_installer_that_goes_silent_is_killed_too(tmp_path):
    """A half-run installer is recoverable by running it again; a wedge is not."""

    process = _SilentProcess()
    installer = BootstrapInstaller(
        popen=lambda *_a, **_k: process,
        stall_timeout=0.05,
        monotonic=_every_reading_is_a_stall(),
    )

    with pytest.raises(DockerError) as exc:
        installer.prepare(tmp_path, tmp_path / "install-docker.sh")

    assert exc.value.code == "bootstrap_stalled"
    assert process.killed.is_set()


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads process state from /proc")
def test_a_stalled_installer_is_killed_with_the_children_holding_its_pipe(tmp_path):
    """The stalled command is rarely the direct child.

    ``sh script`` runs docker as a child and ``docker compose`` runs its plugin
    as one; both inherit the pipe. A kill that reaches only the parent leaves
    the read blocked on them exactly as before, and the thread holding the
    operation claim with it. The child here outlives its shell on purpose and,
    if it survives the kill, says so through the pipe it kept.
    """

    script = tmp_path / "install-docker.sh"
    script.write_text("(sleep 8; echo survived) &\necho $!\nwait\n", encoding="utf-8")
    lines = []
    installer = BootstrapInstaller(
        stall_timeout=0.05, monotonic=_stalls_once_spoken(lines)
    )

    with pytest.raises(DockerError) as exc:
        installer.prepare(tmp_path, script, on_line=lambda line: lines.append(line.strip()))

    assert exc.value.code == "bootstrap_stalled"
    assert "survived" not in lines, "the child kept the pipe and outlived the kill"
    child = int(lines[0])
    # The signal is delivered at once; the state change is the kernel's to
    # schedule. Bounded only so a child that was never signalled fails here.
    deadline = time.monotonic() + 5.0
    while _process_state(child) not in (None, "Z", "X") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _process_state(child) in (None, "Z", "X"), "the child was never killed"


# --- what each container probe asks Docker for ---------------------------
#
# Both probes are one `docker container inspect --format` call and differ only
# in the field. A wrong template returns nothing rather than failing, and every
# caller then degrades to "unknown" without a sign that anything broke.


def _inspect_recorder(stdout="", returncode=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return run, calls


def test_the_image_reference_probe_asks_for_the_reference_it_was_created_from():
    run, calls = _inspect_recorder("ghcr.io/org/ems@sha256:" + "a" * 64)

    ref = DockerCli(run=run).inspect_container_image_ref("ems")

    assert calls[0][:4] == ["docker", "container", "inspect", "--format"]
    assert calls[0][4] == "{{.Config.Image}}"
    assert calls[0][5] == "ems"
    assert ref == "ghcr.io/org/ems@sha256:" + "a" * 64


def test_the_image_id_probe_asks_for_the_immutable_id():
    run, calls = _inspect_recorder("sha256:" + "b" * 64)

    image_id = DockerCli(run=run).inspect_container_image_id("ems")

    assert calls[0][4] == "{{.Image}}"
    assert image_id == "sha256:" + "b" * 64


def test_the_image_id_probe_still_refuses_anything_that_is_not_an_id():
    run, _calls = _inspect_recorder("ghcr.io/org/ems:v1")

    assert DockerCli(run=run).inspect_container_image_id("ems") is None


def test_the_reference_probe_keeps_a_reference_that_is_not_an_id():
    """It is the reference, not the id -- the id filter must not leak into it."""

    run, _calls = _inspect_recorder("ghcr.io/org/ems:v1")

    assert DockerCli(run=run).inspect_container_image_ref("ems") == "ghcr.io/org/ems:v1"


def test_a_failed_container_inspect_reads_as_unknown_for_both_probes():
    run, _calls = _inspect_recorder("", returncode=1)
    docker = DockerCli(run=run)

    assert docker.inspect_container_image_ref("ems") is None
    assert docker.inspect_container_image_id("ems") is None


def test_an_empty_container_name_never_reaches_docker():
    run, calls = _inspect_recorder("whatever")

    assert DockerCli(run=run).inspect_container_image_ref("  ") is None
    assert calls == []
