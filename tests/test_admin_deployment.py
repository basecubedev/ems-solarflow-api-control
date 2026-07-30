# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment preparation service tests (no real Docker daemon)."""

import json
import threading
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

pytestmark = pytest.mark.simulation


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
