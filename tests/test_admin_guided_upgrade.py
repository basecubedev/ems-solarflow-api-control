# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided EMS upgrade executor tests (no Docker/network required)."""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.deployment import DockerError
from admin.ems_tool import EmsToolRunner
from admin.guided_upgrade import (
    GuidedUpgradeExecutor,
    guided_upgrade_request_fingerprint,
    plan_upgrade_steps,
)
from admin.image_identity import (
    OLDER_THAN_RUNNING_BUILD,
    UPGRADE_AVAILABLE,
    UpgradeAssessment,
)
from admin.install_context import detect_install_context
from admin.system_build import digest_pinned_ref
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import auth_headers, authenticate

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root

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


BACKUP_ARCHIVE_PATH = "data/backups/ems-config-manual-2026-07-15-120000.tar.gz"


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
        # Model ``backup create --verify``: a zero exit means the archive was
        # created and verified, and its exact path is printed for the caller.
        output = ""
        if "backup" in command and "create" in command and self._backup_returncode == 0:
            output = f"Backup created:\n  {BACKUP_ARCHIVE_PATH}\nVerified: 2 files\n"
        return self._backup_returncode, output


class FakeDockerCli:
    def __init__(self, local_digests=None):
        self.pulled = []
        # Content digests already present locally (e.g. left by a prior Verify).
        # A digest-pinned ref inspects only when its content is local, exactly
        # like a real daemon: a pull makes the pulled digest's content local.
        self._local = set(local_digests or ())

    def pull(self, image, on_progress=None):
        self.pulled.append(image)
        if "@sha256:" in str(image):
            self._local.add(str(image).split("@", 1)[1])

    def inspect_image(self, ref):
        ref = str(ref)
        if "@sha256:" in ref:
            digest = ref.split("@", 1)[1]
            if digest in self._local:
                return {"image_ref": ref, "digest": digest, "labels": {}}
        return None


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


def _control_migration_config():
    return {
        "config_schema_version": 3,
        "system": {"max_total_power": 800},
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Legacy",
                "product": "Hyper 2000",
                "mqtt": {
                    "broker_ref": "local_a",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV",
                    "product_key": "PK",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


def _executor_for(tmp_path, config):
    install = _install(tmp_path, config=config)
    releases = _prepared_release(tmp_path)
    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases, prepared=TAG),
        compose=FakeCompose(),
        docker_cli=FakeDockerCli(),
        ems_cli=FakeEmsCli("ok"),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    return executor


def test_preflight_includes_unmigrated_mqtt_control_config_in_plan(tmp_path):
    # The visible plan carries the EMS-owned migration review instead of
    # pointing at a separate Maintenance action.
    executor = _executor_for(tmp_path, _control_migration_config())
    rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    assert rejection is None
    assert run_context.migration["required"] is True
    assert run_context.migration["revision"]
    assert run_context.migration["review"]["changes"][0]["device"] == "Legacy"


def test_prepare_alignment_backs_up_then_applies_mqtt_migration(tmp_path):
    executor = _executor_for(tmp_path, _control_migration_config())
    rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    assert rejection is None

    failure, preparation = executor.prepare_alignment(run_context)

    assert failure is None
    ids = [step["id"] for step in preparation.steps]
    assert ids.index("migration_review") < ids.index("backup")
    assert ids.index("backup") < ids.index("mqtt_migration")
    migrated = json.loads(run_context.context.config_path.read_text(encoding="utf-8"))
    assert migrated["devices"][0]["hardware_profile"] == "hyper_2000"
    assert preparation.current_config == migrated


def test_mqtt_migration_write_failure_stops_before_container_replacement(tmp_path):
    executor = _executor_for(tmp_path, _control_migration_config())
    context = executor._install_context_provider()
    original = Path(context.config_path).read_bytes()

    class _FailingConfigApply:
        def apply_maintenance(self, *_args, **_kwargs):
            raise OSError("atomic write failed")

    executor.config_apply = _FailingConfigApply()
    rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    assert rejection is None

    failure, preparation = executor.prepare_alignment(run_context)

    assert preparation is None
    assert failure["ok"] is False
    migration = next(step for step in failure["steps"] if step["id"] == "mqtt_migration")
    assert migration["status"] == "error"
    assert "atomic write failed" in migration["detail"]
    assert Path(context.config_path).read_bytes() == original
    assert executor.docker_cli.pulled == []
    assert executor.compose.calls == []


def test_preflight_allows_migrated_mqtt_control_config(tmp_path):
    config = _control_migration_config()
    config["devices"][0]["hardware_profile"] = "hyper_2000"
    del config["devices"][0]["product"]
    executor = _executor_for(tmp_path, config)
    rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    assert rejection is None
    assert run_context is not None


def _make_executor_no_cache(tmp_path, *, prepared=None, backup_returncode=0):
    """Executor whose target release cache has NOT been imported yet.

    Models the real pre-alignment world: the target System Build's resources
    live in its Admin image and are imported only after Admin alignment.
    """

    install = _install(tmp_path)
    releases = tmp_path / "releases"
    releases.mkdir()
    compose = FakeCompose(backup_returncode=backup_returncode)
    docker = FakeDockerCli()
    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases, prepared=prepared),
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli("ok"),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    return executor, install, compose, docker


def test_preflight_allows_unprepared_target_before_alignment(tmp_path):
    # The target resource cache is NOT required before Admin alignment: an
    # unprepared, non-cached target still reaches Admin alignment.
    executor, _install, compose, docker = _make_executor_no_cache(tmp_path)
    rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    assert rejection is None
    assert run_context is not None and run_context.target_release == TAG
    assert compose.calls == [] and docker.pulled == []


def test_backup_completes_before_target_cache_is_available(tmp_path):
    # The backup runs during pre-alignment, before the target resources exist.
    executor, _install, compose, _docker = _make_executor_no_cache(tmp_path)
    _rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    failure, preparation = executor.prepare_alignment(run_context)
    assert failure is None
    assert compose.oneoff_calls, "the pre-upgrade backup must run before alignment"
    backup = next(s for s in preparation.steps if s["id"] == "backup")
    assert backup["status"] == "ok"


def test_run_requires_target_config_template_after_import(tmp_path):
    # After Admin alignment the target resources must exist; a missing template
    # stops the run before any config or EMS change.
    executor, install, compose, docker = _make_executor_no_cache(tmp_path)
    original_config = (install / "config" / "config.json").read_text(encoding="utf-8")
    _rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)
    failure, preparation = executor.prepare_alignment(run_context)
    assert failure is None

    result = executor.run(run_context, pre_alignment=preparation)

    assert result["ok"] is False and result["status"] == "failed"
    step = next(s for s in result["steps"] if s["id"] == "target_resources")
    assert step["status"] == "error"
    # No config write / pull / compose / recreate happened.
    assert docker.pulled == [] and compose.calls == []
    assert (install / "config" / "config.json").read_text(encoding="utf-8") == original_config


def test_run_with_imported_target_resources_proceeds(tmp_path):
    # The normal case: once the target cache is present the run proceeds and the
    # target_resources verification passes.
    executor, install, _compose, _docker = _make_executor(tmp_path)
    result = executor.execute(TAG, ALL_OPTIONS, confirm=True)
    assert result["ok"] is True and result["status"] == "completed"
    step = next(s for s in result["steps"] if s["id"] == "target_resources")
    assert step["status"] == "ok"


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
    assert call["command"] == ["python3", "emsctl.py", "backup", "create", "--verify"]
    backup = next(s for s in result["steps"] if s["id"] == "backup")
    assert backup["status"] == "ok"
    assert backup["detail"] == "via compose one-off EMS container"
    assert backup["archive"] == BACKUP_ARCHIVE_PATH
    assert backup["verified"] is True


def test_backup_uses_docker_exec_when_ems_container_running(tmp_path):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    docker = FakeDockerWithContainer(container=_running_ems())
    fake_run = FakeRun(
        returncode=0, stdout=f"Backup created:\n  {BACKUP_ARCHIVE_PATH}\nVerified: 2 files"
    )
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
            "python3", "/app/emsctl.py", "backup", "create", "--verify",
        ]
    ]
    assert compose.oneoff_calls == []
    backup = next(s for s in result["steps"] if s["id"] == "backup")
    assert backup["status"] == "ok"
    assert backup["detail"] == "via running EMS container"


def test_backup_step_records_verified_archive_reference(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    _rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)

    _failure, preparation = executor.prepare_alignment(run_context)

    backup = next(s for s in preparation.steps if s["id"] == "backup")
    assert backup["status"] == "ok"
    assert backup["verified"] is True
    # The reference is the exact created archive path, not an execution-context
    # description like "via running EMS container".
    assert backup["archive"] == BACKUP_ARCHIVE_PATH
    assert "container" not in backup["archive"]


def test_backup_verification_failure_stops_before_admin_replacement(tmp_path):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    compose = FakeCompose(backup_returncode=1)  # backup create --verify fails
    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases, prepared=TAG),
        compose=compose,
        docker_cli=FakeDockerCli(),
        ems_cli=FakeEmsCli("ok"),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    _rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)

    failure, preparation = executor.prepare_alignment(run_context)

    # A failed verification blocks the run before any Admin alignment starts.
    assert preparation is None
    assert failure["status"] == "failed"
    backup = next(s for s in failure["steps"] if s["id"] == "backup")
    assert backup["status"] == "error"


def test_resume_alignment_consumes_durable_backup_state(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    _rejection, run_context = executor.preflight(TAG, ALL_OPTIONS, confirm=True)

    preparation = executor.resume_alignment(
        run_context,
        backup={"completed": True, "verified": True, "reference": BACKUP_ARCHIVE_PATH},
    )

    backup = next(s for s in preparation.steps if s["id"] == "backup")
    # The backup step reflects the recorded exact verified archive, not a
    # generic "completed because the option was enabled" placeholder.
    assert backup["archive"] == BACKUP_ARCHIVE_PATH
    assert backup["verified"] is True


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


# --- resolved System Build is the sole build-identity source --------------


def _resolved_build(tag=TAG, ems_digest="sha256:target-ems"):
    return {
        "requested_tag": tag,
        "canonical_tag": tag,
        "channel": "stable",
        "revision": "f7265fc747c2223f126f0ee7801e030c6226edf4",
        "build_id": f"{tag}-f7265fc",
        "admin_image": f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
        "admin_digest": "sha256:target-admin",
        "ems_image": f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}",
        "ems_digest": ems_digest,
        "release_tag": tag,
    }


def test_executor_uses_exactly_the_system_build_ems_image_and_digest(tmp_path):
    executor, install, compose, docker = _make_executor(tmp_path)
    sb = _resolved_build(ems_digest="sha256:exact-target-ems")
    runtime_ref = digest_pinned_ref(sb["ems_image"], sb["ems_digest"])

    result = executor.execute(TAG, ALL_OPTIONS, confirm=True, system_build=sb)

    assert result["ok"] is True and result["status"] == "completed"
    # The readable release identity stays the tag ref; the digest is retained.
    assert result["target_image"] == sb["ems_image"]
    assert result["target_release"] == TAG
    assert result["target_digest"] == sb["ems_digest"]
    # The runtime identity Docker pulls and Compose persists is digest-pinned.
    assert result["runtime_image"] == runtime_ref
    assert docker.pulled == [runtime_ref]
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["digest"] == sb["ems_digest"]
    assert verify["image"] == sb["ems_image"]
    # Compose is pinned to the exact verified digest, not the mutable tag.
    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert runtime_ref in compose_text
    assert sb["ems_image"] not in compose_text
    assert "influxdb:2.7" in compose_text


def test_divergent_legacy_release_assessment_cannot_block_valid_system_build(tmp_path):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    compose = FakeCompose()
    docker = FakeDockerCli()
    # This release manager would BLOCK the move as a downgrade — it must never be
    # consulted for a resolved System Build.
    rm = VerifyingReleaseManager(
        releases,
        prepared=TAG,
        assessment=UpgradeAssessment(OLDER_THAN_RUNNING_BUILD, "build_serial"),
    )
    executor = GuidedUpgradeExecutor(
        release_manager=rm,
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli("ok"),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )

    result = executor.execute(
        TAG, ALL_OPTIONS, confirm=True, system_build=_resolved_build()
    )

    assert result["ok"] is True and result["status"] == "completed"
    verify = next(s for s in result["steps"] if s["id"] == "verify_image")
    assert verify["status"] == "ok"
    # The legacy verify_upgrade_target assessment was never consulted.
    assert rm.verified == []


def test_request_tag_cannot_inject_a_different_ems_repository(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    # The System Build resolved to a v0.7.0 image while the request tag says
    # v0.6.1: the deployed EMS image is the resolved one, never reconstructed
    # from the request, so a request can never smuggle a different image/repo.
    sb = _resolved_build(tag="v0.7.0")

    _rejection, run_context = executor.preflight(
        "v0.6.1", ALL_OPTIONS, confirm=True, system_build=sb
    )

    assert run_context.target_image == "ghcr.io/basecubedev/ems-solarflow-api-control:v0.7.0"
    assert run_context.target_image != "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1"
    assert run_context.target_digest == sb["ems_digest"]
    # The deployed runtime ref is the resolved image pinned to its verified
    # digest — never reconstructed from the request tag.
    assert run_context.target_runtime_image == digest_pinned_ref(
        sb["ems_image"], sb["ems_digest"]
    )


def test_pull_and_recreate_are_mandatory_deploy_steps(tmp_path):
    executor, _install, compose, docker = _make_executor(tmp_path)
    sb = _resolved_build()
    # Even if the request tried to disable them, deploying the System Build is
    # binding: the image is pulled and the EMS container is recreated.
    weakened = {**ALL_OPTIONS, "pull_image": False, "recreate": False}
    result = executor.execute(TAG, weakened, confirm=True, system_build=sb)

    assert result["ok"] is True and result["status"] == "completed"
    assert docker.pulled == [digest_pinned_ref(sb["ems_image"], sb["ems_digest"])]
    assert compose.calls and compose.calls[0]["force_recreate"] is True
    keys = [step["key"] for step in plan_upgrade_steps(weakened)]
    assert "pull_image" in keys and "recreate_ems" in keys


# --- digest-pinned deployment (verified digest == deployed image) ----------
#
# After verification the release tag is display/catalogue metadata only; the
# runtime image Docker pulls and Compose persists is the exact verified digest,
# so a registry tag moved after Verify can never change the installed image.

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_ADMIN_REPO = "ghcr.io/basecubedev/ems-solarflow-admin"
_EMS_REPO = "ghcr.io/basecubedev/ems-solarflow-api-control"


def _digest_executor_for(tmp_path, tag, docker, *, ems_status="ok"):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path, tag=tag)
    compose = FakeCompose()
    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases, prepared=tag),
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli(ems_status),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    return executor, install, compose


class _MovingTagResolverDocker:
    """A real-resolver-shaped docker for one stable paired build.

    The mutable ``:tag`` can be moved to a new digest. Content is local only once
    it has been pulled (a Verify pulls the tag), and a digest-pinned ref inspects
    only when its content is local — exactly like a real daemon.
    """

    def __init__(self, tag, revision, admin_digest, ems_digest):
        self._tag = tag
        self._labels = {
            "org.opencontainers.image.version": tag,
            "org.opencontainers.image.revision": revision,
            "de.basecubedev.ems.build_id": f"{tag}-{revision[:7]}",
            "de.basecubedev.ems.channel": "stable",
            "de.basecubedev.ems.release_tag": tag,
        }
        self._tag_digest = {_ADMIN_REPO: admin_digest, _EMS_REPO: ems_digest}
        self.pulled = []

        self._local = set()

    def move_ems_tag(self, digest):
        self._tag_digest[_EMS_REPO] = digest

    def pull(self, ref, on_progress=None):
        self.pulled.append(ref)
        if "@sha256:" in ref:
            self._local.add(ref.split("@", 1)[1])
            return
        repo, _, tag = ref.rpartition(":")
        if tag == self._tag and repo in self._tag_digest:
            self._local.add(self._tag_digest[repo])
            return
        raise RuntimeError(f"pull failed: {ref}")

    def inspect_image(self, ref):
        if "@sha256:" in ref:
            digest = ref.split("@", 1)[1]
            if digest in self._local:
                return {"image_ref": ref, "digest": digest, "labels": self._labels}
            return None
        repo, _, tag = ref.rpartition(":")
        if tag == self._tag and repo in self._tag_digest:
            return {"image_ref": ref, "digest": self._tag_digest[repo],
                    "labels": self._labels}
        return None


def test_cached_pair_reuses_local_verified_digest_after_tag_moves(tmp_path):
    # Full path: the productive CachingBuildResolver + SystemBuildResolver resolve
    # and pin pair A (leaving digest A local), the registry tag then moves to B,
    # and the executor must still deploy digest A — reusing the local verified
    # image with zero registry pulls, never the moved tag.
    from admin.system_build import CachingBuildResolver, SystemBuildResolver

    revision = "f7265fc747c2223f126f0ee7801e030c6226edf4"
    tag = "v9.9.9"
    registry = _MovingTagResolverDocker(
        tag, revision, admin_digest=_DIGEST_A, ems_digest=_DIGEST_A
    )
    resolver = CachingBuildResolver(
        SystemBuildResolver(
            docker=registry, admin_repo=_ADMIN_REPO, ems_repo=_EMS_REPO
        )
    )
    pair_a = resolver.resolve(tag)
    assert pair_a.ems_digest == _DIGEST_A
    # The tag moves to a different image; the resolver cache still holds pair A.
    registry.move_ems_tag(_DIGEST_B)
    assert resolver.resolve(tag).ems_digest == _DIGEST_A
    registry.pulled.clear()

    executor, install, compose = _digest_executor_for(tmp_path, tag, registry)
    runtime_ref = digest_pinned_ref(pair_a.ems_image, pair_a.ems_digest)
    result = executor.execute(tag, ALL_OPTIONS, confirm=True, system_build=pair_a)

    assert result["ok"] is True and result["runtime_image"] == runtime_ref
    # The exact verified digest was already local from Verify, so execute made no
    # registry request; the moved digest B was never touched.
    assert registry.pulled == []
    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert runtime_ref in compose_text
    assert f"{_EMS_REPO}:{tag}" not in compose_text


class _MovedTagAfterComparisonDocker(FakeDockerCli):
    """Pulling the mutable ``:tag`` raises; only digest-pinned pulls are honoured,
    proving the executor never resolves the tag."""

    def pull(self, image, on_progress=None):
        if "@sha256:" not in str(image):
            raise AssertionError("executor pulled the mutable tag after verification")
        super().pull(image)


def test_tag_move_after_comparison_still_deploys_verified_digest(tmp_path):
    # The server already compared the fingerprint; the tag then moves. The digest
    # is not local, so the executor pulls it by digest — never the mutable tag.
    docker = _MovedTagAfterComparisonDocker()
    executor, install, compose = _digest_executor_for(tmp_path, TAG, docker)
    sb = _resolved_build(ems_digest=_DIGEST_A)
    runtime_ref = digest_pinned_ref(sb["ems_image"], sb["ems_digest"])

    result = executor.execute(TAG, ALL_OPTIONS, confirm=True, system_build=sb)

    assert result["ok"] is True
    assert docker.pulled == [runtime_ref]
    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert runtime_ref in compose_text


class _WrongDigestDocker(FakeDockerCli):
    """Inspection reports a different digest than the one that was pulled."""

    def __init__(self, actual_digest):
        super().__init__()
        self._actual = actual_digest

    def inspect_image(self, ref):
        return {"image_ref": ref, "digest": self._actual, "labels": {}}


def test_digest_mismatch_fails_before_compose_and_recreate(tmp_path):
    docker = _WrongDigestDocker(actual_digest=_DIGEST_B)
    executor, install, compose = _digest_executor_for(tmp_path, TAG, docker)
    sb = _resolved_build(ems_digest=_DIGEST_A)

    result = executor.execute(TAG, ALL_OPTIONS, confirm=True, system_build=sb)

    assert result["ok"] is False and result["status"] == "failed"
    assert result["reason"] == "target_digest_mismatch"
    pull = next(s for s in result["steps"] if s["id"] == "pull_image")
    assert pull["status"] == "error" and pull["reason"] == "target_digest_mismatch"
    # Fails closed: no compose write, no backup file, no recreate.
    assert not any(s["id"] == "update_compose" for s in result["steps"])
    assert compose.calls == []
    assert not (install / "docker-compose.yml.bak").exists()
    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert "@sha256:" not in compose_text


def test_reuses_exact_local_verified_digest_with_zero_pulls(tmp_path):
    # The exact verified digest is already local from Verify: execute reuses it
    # without any registry request, yet still pins Compose and recreates EMS.
    sb = _resolved_build(ems_digest=_DIGEST_A)
    docker = FakeDockerCli(local_digests={_DIGEST_A})
    executor, install, compose = _digest_executor_for(tmp_path, TAG, docker)
    runtime_ref = digest_pinned_ref(sb["ems_image"], sb["ems_digest"])

    result = executor.execute(TAG, ALL_OPTIONS, confirm=True, system_build=sb)

    assert result["ok"] is True and result["runtime_image"] == runtime_ref
    assert docker.pulled == []  # reused, zero registry pulls
    pull = next(s for s in result["steps"] if s["id"] == "pull_image")
    assert pull["status"] == "skipped"
    assert "locally" in pull["detail"].lower()
    # Digest pinning and the mandatory recreate still happen.
    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert runtime_ref in compose_text
    assert compose.calls and compose.calls[0]["force_recreate"] is True


class _TagLocalButDigestMissingDocker(FakeDockerCli):
    """The exact digest ref is not resolvable until pulled; a mutable tag is never
    inspected as digest proof (inspecting a tag raises)."""

    def inspect_image(self, ref):
        if "@sha256:" in str(ref):
            return super().inspect_image(ref)
        raise AssertionError("a mutable tag must not be inspected as digest proof")


def test_mutable_tag_is_not_accepted_as_local_digest_proof(tmp_path):
    # Even if content is present under a mutable tag, only the exact digest ref
    # counts: it is not local, so the executor pulls it by digest.
    docker = _TagLocalButDigestMissingDocker()
    sb = _resolved_build(ems_digest=_DIGEST_A)
    executor, install, compose = _digest_executor_for(tmp_path, TAG, docker)
    runtime_ref = digest_pinned_ref(sb["ems_image"], sb["ems_digest"])

    result = executor.execute(TAG, ALL_OPTIONS, confirm=True, system_build=sb)

    assert result["ok"] is True
    assert docker.pulled == [runtime_ref]


def test_upgraded_compose_is_digest_pinned_and_recreate_stable(tmp_path):
    docker = FakeDockerCli()
    executor, install, compose = _digest_executor_for(tmp_path, TAG, docker)
    sb = _resolved_build(ems_digest=_DIGEST_A)
    runtime_ref = digest_pinned_ref(sb["ems_image"], sb["ems_digest"])

    executor.execute(TAG, ALL_OPTIONS, confirm=True, system_build=sb)

    compose_path = install / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    assert runtime_ref in compose_text
    # A later container recreation reuses the persisted digest-pinned compose, so
    # re-applying the same runtime ref is a no-op: a recreate can only reference
    # the verified digest, never a newer image behind the old tag.
    step = executor._update_compose(compose_path, runtime_ref)
    assert step["status"] == "skipped"


@pytest.mark.parametrize(
    "tag,channel",
    [
        ("v9.9.0", "stable"),
        ("v9.9.0-RC1", "rc"),
        ("dev-feature-x-f7265fc-42-1", "development"),
    ],
)
def test_all_channels_persist_digest_pinned_compose(tmp_path, tag, channel):
    docker = FakeDockerCli()
    executor, install, compose = _digest_executor_for(tmp_path, tag, docker)
    sb = {**_resolved_build(tag=tag, ems_digest=_DIGEST_A), "channel": channel}
    runtime_ref = digest_pinned_ref(sb["ems_image"], sb["ems_digest"])

    result = executor.execute(tag, ALL_OPTIONS, confirm=True, system_build=sb)

    assert result["ok"] is True
    assert result["runtime_image"] == runtime_ref
    assert result["target_release"] == tag
    assert docker.pulled == [runtime_ref]
    compose_text = (install / "docker-compose.yml").read_text(encoding="utf-8")
    assert runtime_ref in compose_text


# --- endpoint wiring ------------------------------------------------------


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    headers = dict(auth_headers(url, "POST"))
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _get(url):
    req = urllib.request.Request(url, headers=auth_headers(url, "GET"), method="GET")
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


class _AllowAdminUpdate:
    """Admin update stub that never blocks the EMS upgrade (self-update is a
    separate concern, covered by tests/test_admin_update.py)."""

    def ems_upgrade_allowed(self, target_release):
        return {"allowed": True, "reason": "admin_update_not_required"}


class _AlignedSystemBuild:
    """Strict paired-build gate used by the HTTP executor tests."""

    # The digest pair a resolve/validate reports. Mutable so a test can move a
    # tag (re-push a new digest) between Verify and Upgrade and prove the
    # selection fingerprint the operator verified is enforced at execute.
    _REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"

    def __init__(self, stage="resources_verified"):
        self.stage = stage
        self.active = False
        self.start_calls = []
        self.resolve_calls = []
        self.validate_calls = []
        self.request_fingerprint = None
        self.development_risk_acknowledged = None
        self.ems_calls = []
        self._claimed = False
        self.channel = "stable"
        self.revision = self._REVISION
        self.admin_digest = "sha256:admin"
        self.ems_digest = "sha256:ems"
        # The EMS digest bound into the durable transition at start time. A
        # resume reads it back (never a fresh resolve), so moving ``ems_digest``
        # after start proves the deploy stays pinned to the verified digest.
        self.bound_ems_digest = None

    def _build(self, requested_tag):
        return {
            "requested_tag": requested_tag,
            "canonical_tag": requested_tag,
            "channel": self.channel,
            "revision": self.revision,
            "build_id": f"{requested_tag}-f7265fc",
            "admin_image": f"ghcr.io/basecubedev/ems-solarflow-admin:{requested_tag}",
            "admin_digest": self.admin_digest,
            "ems_image": f"ghcr.io/basecubedev/ems-solarflow-api-control:{requested_tag}",
            "ems_digest": self.ems_digest,
        }

    @staticmethod
    def selection_fingerprint(build):
        return ":".join(
            str(build.get(key) or "")
            for key in (
                "canonical_tag",
                "channel",
                "revision",
                "build_id",
                "admin_digest",
                "ems_digest",
            )
        )

    def resolve(self, requested_tag):
        self.resolve_calls.append(requested_tag)
        return self._build(requested_tag)

    def transition_build(self, *, operation_id):
        # Reconstruct from the durable transition's bound pair, not a fresh
        # resolve, so a moved tag after start cannot change the resumed digest.
        build = self._build(TAG)
        if self.bound_ems_digest is not None:
            build["ems_digest"] = self.bound_ems_digest
        return build

    def validate_upgrade_target(self, *, requested_tag):
        self.validate_calls.append(requested_tag)
        build = self._build(requested_tag)
        return {
            "ok": True,
            "valid": True,
            "selected_tag": requested_tag,
            "system_build": {
                "canonical_tag": requested_tag,
                "revision": self.revision,
                "build_id": f"{requested_tag}-f7265fc",
                "ems_image": (
                    f"ghcr.io/basecubedev/ems-solarflow-api-control:{requested_tag}"
                ),
                "ems_digest": self.ems_digest,
            },
            "selection_fingerprint": self.selection_fingerprint(build),
            "alignment": "admin_update_required",
            "admin_update_required": True,
            "upgrade_allowed": True,
            "upgrade_state": "upgrade_available",
        }

    def start_resolved(
        self,
        *,
        system_build,
        mode,
        request_fingerprint=None,
        development_risk_acknowledged=False,
        pre_launch=None,
    ):
        requested_tag = system_build["canonical_tag"]
        self.request_fingerprint = request_fingerprint
        self.development_risk_acknowledged = development_risk_acknowledged
        # Bind the resolved digest into the (simulated) durable transition.
        self.bound_ems_digest = system_build.get("ems_digest")
        # Persist-before-launch: run the durable-context callback before the
        # transition is started/launched, so a persistence failure aborts here.
        # A reused (already active) transition returns early like the real
        # service and never re-persists.
        if pre_launch is not None and not self.active:
            pre_launch(SimpleNamespace(operation_id="upgrade-op"))
        return self.start(requested_tag=requested_tag, mode=mode)

    def development_acknowledgement_allows_automatic_resume(self, *, requested_tag):
        return bool(self.active and self.development_risk_acknowledged)

    def start(self, *, requested_tag, mode):
        self.start_calls.append((requested_tag, mode))
        self.active = True
        reconnect = self.stage != "resources_verified"
        return {
            "status": "admin_alignment_started" if reconnect else "ready_for_ems",
            "stage": self.stage,
            "operation_id": "upgrade-op",
            "reconnect": reconnect,
            "system_build": {
                "canonical_tag": requested_tag,
                "revision": "f7265fc747c2223f126f0ee7801e030c6226edf4",
                "build_id": f"{requested_tag}-f7265fc",
            },
        }

    def resume(self, *, operation_id):
        self.ems_calls.append(("resume", operation_id))
        if self.stage == "admin_reconnect_pending":
            self.stage = "admin_aligned"
        return {"status": self.stage, "stage": self.stage, "operation_id": operation_id}

    def verify_resources(self, *, operation_id):
        self.ems_calls.append(("verify_resources", operation_id))
        if self.stage == "admin_aligned":
            self.stage = "resources_verified"
        return {"status": self.stage, "stage": self.stage, "operation_id": operation_id}

    def resources_verified(self):
        return self.stage in {
            "resources_verified",
            "ems_operation_pending",
            "ems_operation_running",
            "healthcheck_pending",
            "completed",
        }

    def status(self):
        return {
            "active": self.active and self.stage not in {"completed", "cancelled"},
            "transition": (
                {
                    "operation_id": "upgrade-op",
                    "mode": "guided_upgrade",
                    "stage": self.stage,
                    "system_tag": TAG,
                    "request_fingerprint": self.request_fingerprint,
                }
                if self.active
                else None
            ),
            "known_good": None,
        }

    def begin_ems_operation(self, *, operation_id):
        self.ems_calls.append(("begin", operation_id))
        if self.stage == "resources_verified":
            self.stage = "ems_operation_pending"
        return {"status": self.stage, "operation_id": operation_id}

    def claim_ems_operation(self, *, operation_id):
        self.ems_calls.append(("claim", operation_id))
        if self.stage != "ems_operation_pending" or self._claimed:
            return False
        self._claimed = True
        self.stage = "ems_operation_running"
        return True

    def finish_ems_operation(self, *, operation_id, succeeded, **_kwargs):
        self.ems_calls.append(("finish", operation_id, succeeded))
        self.stage = "healthcheck_pending" if succeeded else "failed_recoverable"
        return {"status": self.stage, "operation_id": operation_id}

    def finish_healthcheck(self, *, operation_id, passed, **_kwargs):
        self.stage = "completed" if passed else "failed_recoverable"
        return {"status": self.stage, "operation_id": operation_id}


def _server(guided_upgrade, admin_update=None, system_alignment=None,
            guided_upgrade_context=None):
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server(
        "127.0.0.1",
        0,
        registry=registry,
        guided_upgrade=guided_upgrade,
        admin_update=admin_update or _AllowAdminUpdate(),
        system_alignment=system_alignment or _AlignedSystemBuild(),
        guided_upgrade_context=guided_upgrade_context,
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


# The selection fingerprint a client obtains from Verify System Build for the
# default aligned pair. A confirmed execute must send it back so the server can
# prove the resolved pair is exactly the one the operator verified.
_FP = _AlignedSystemBuild()
FINGERPRINT = _FP.selection_fingerprint(_FP._build(TAG))


def _execute_body(target=TAG, fingerprint=FINGERPRINT, **overrides):
    body = {
        "confirm": True,
        "target_release": target,
        "options": ALL_OPTIONS,
        "selection_fingerprint": fingerprint,
    }
    body.update(overrides)
    return body


class _WarnAdminUpdate:
    """Admin update stub that recommends an update but does not block."""

    def ems_upgrade_allowed(self, target_release):
        return {
            "allowed": True,
            "severity": "warning",
            "reason": "admin_update_recommended",
            "message": "Admin Console update recommended.",
        }


class _InProgressAdminUpdate:
    """Admin update stub for a genuinely in-flight Admin self-update (blocks)."""

    def ems_upgrade_allowed(self, target_release):
        return {
            "allowed": False,
            "error": "admin_update_in_progress",
            "message": "An Admin Console update is currently running.",
        }


class _RejectingPreflightExecutor:
    """Reject before any paired Admin alignment is allowed to start."""

    def __init__(self):
        self.preflight_calls = []

    def preflight(self, target_release, options, *, confirm=False, system_build=None):
        self.preflight_calls.append((target_release, options, confirm))
        return (
            {
                "ok": False,
                "status": "rejected",
                "reason": "config_missing",
                "message": "config/config.json was not found.",
                "target_release": target_release,
            },
            None,
        )


def test_guided_preflight_failure_does_not_start_admin_alignment(tmp_path):
    executor = _RejectingPreflightExecutor()
    alignment = _AlignedSystemBuild(stage="resources_verified")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()

    assert status in {400, 409}
    assert body["reason"] == "config_missing"
    assert executor.preflight_calls == [(TAG, ALL_OPTIONS, True)]
    assert alignment.start_calls == []


def test_guided_upgrade_rejects_stale_mqtt_migration_review_before_alignment(
    tmp_path,
):
    executor = _executor_for(tmp_path, _control_migration_config())
    alignment = _AlignedSystemBuild(stage="resources_verified")
    original = Path(executor._install_context_provider().config_path).read_bytes()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {
                "confirm": True,
                "target_release": TAG,
                "options": ALL_OPTIONS,
                "migration_revision": "stale-review",
                "selection_fingerprint": FINGERPRINT,
            },
        )
    finally:
        srv.shutdown()

    assert status == 409
    assert body["reason"] == "mqtt_migration_review_stale"
    assert alignment.start_calls == []
    assert Path(executor._install_context_provider().config_path).read_bytes() == original


def test_guided_backup_failure_happens_before_admin_alignment(tmp_path):
    executor, install, compose, _docker = _make_executor(
        tmp_path, backup_returncode=1
    )
    alignment = _AlignedSystemBuild(stage="resources_verified")
    original_config = (install / "config" / "config.json").read_bytes()
    original_compose = (install / "docker-compose.yml").read_bytes()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        # The old implementation starts a background EMS job before discovering
        # the backup failure. Let it settle so the RED assertion is deterministic.
        if status == 202 and body.get("job_id"):
            _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()

    assert compose.oneoff_calls, "the requested pre-upgrade backup must run"
    assert alignment.start_calls == []
    assert (install / "config" / "config.json").read_bytes() == original_config
    assert (install / "docker-compose.yml").read_bytes() == original_compose


def test_guided_reconnect_does_not_repeat_completed_pre_alignment_backup(tmp_path):
    executor, _install, compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        first_status, first = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert first_status == 202
        assert first["reconnect"] is True
        assert len(compose.oneoff_calls) == 1

        # Simulate the replacement Admin reconnecting and verifying its embedded
        # resources. Re-posting the confirmed request may rebuild safe context,
        # but it must not execute the already committed backup again.
        alignment.stage = "resources_verified"
        second_status, second = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert second_status == 202
        if second.get("job_id"):
            _wait_job(base, second["job_id"])
    finally:
        srv.shutdown()

    assert len(compose.oneoff_calls) == 1


def test_guided_reconnect_rejects_changed_operation_options(tmp_path):
    executor, _install, compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        first_status, _first = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert first_status == 202
        assert len(compose.oneoff_calls) == 1

        alignment.stage = "resources_verified"
        changed = {**ALL_OPTIONS, "config_comments": False}
        second_status, second = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": changed, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()

    assert second_status == 409
    assert second["error"] == "transition_context_mismatch"
    assert len(compose.oneoff_calls) == 1


def test_guided_unexpected_executor_failure_releases_durable_ems_claim(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="resources_verified")

    def crash(*_args, **_kwargs):
        raise RuntimeError("unexpected executor crash")

    executor.run = crash
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()

    assert job["status"] == "failed"
    assert alignment.stage == "failed_recoverable"
    assert any(
        call[0] == "finish" and call[2] is False
        for call in getattr(alignment, "ems_calls", [])
    )


def test_guided_malformed_executor_result_releases_durable_ems_claim(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="resources_verified")
    executor.run = lambda *_args, **_kwargs: None
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()

    assert job["status"] == "failed"
    assert alignment.stage == "failed_recoverable"
    assert any(
        call[0] == "finish" and call[2] is False
        for call in getattr(alignment, "ems_calls", [])
    )


def test_execute_rejects_a_failed_recoverable_transition_with_recovery_guidance(tmp_path):
    """A failed_recoverable transition must be recovered through its own route; a
    fresh execute is refused with HTTP 409 system_transition_in_progress instead of
    starting a second EMS operation, so the operator uses the recovery flow."""
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="resources_verified")

    def crash(*_args, **_kwargs):
        raise RuntimeError("unexpected executor crash")

    executor.run = crash
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            _execute_body(),
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
        assert job["status"] == "failed"
        assert alignment.stage == "failed_recoverable"

        ems_calls_after_failure = list(getattr(alignment, "ems_calls", []))
        retry_status, retry_body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            _execute_body(),
        )
    finally:
        srv.shutdown()

    assert retry_status == 409
    assert retry_body["error"] == "system_transition_in_progress"
    assert "recover" in retry_body["message"].lower()
    assert retry_body["transition"]["stage"] == "failed_recoverable"
    assert "job_id" not in retry_body
    assert list(getattr(alignment, "ems_calls", [])) == ems_calls_after_failure


def test_execute_uses_strict_system_alignment_not_legacy_advisory_gate(tmp_path):
    # The paired SystemAlignmentService is authoritative. A stale v1 advisory
    # signal is neither consulted nor copied into the response.
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild()
    srv, base = _server(
        executor,
        admin_update=_WarnAdminUpdate(),
        system_alignment=alignment,
    )
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()
    assert status == 202
    assert body["ok"] is True
    assert isinstance(body["job_id"], str) and body["job_id"]
    assert "admin_update" not in body
    assert alignment.start_calls == [(TAG, "guided_upgrade")]


def test_execute_waits_for_shared_admin_alignment_before_ems_upgrade(tmp_path):
    executor, _install_dir, _compose, docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(
        executor,
        admin_update=_InProgressAdminUpdate(),
        system_alignment=alignment,
    )
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()
    assert status == 202
    assert body["status"] == "admin_alignment_started"
    assert body["reconnect"] is True
    assert alignment.start_calls == [(TAG, "guided_upgrade")]
    # Gated before any legacy executor/job runs, so EMS was untouched.
    assert docker.pulled == []


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
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
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
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
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


def test_pre_alignment_failed_step_is_returned_before_job_start(tmp_path):
    executor, _install_dir, _compose, docker = _make_executor(
        tmp_path, backup_returncode=1
    )
    srv, base = _server(executor)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()
    assert status == 409
    assert body["status"] == "failed"
    assert "job_id" not in body
    backup = next(s for s in body["steps"] if s["id"] == "backup")
    assert backup["status"] == "error"
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


# --- read-only System Build validation (no transition side effects) ------


def test_validate_endpoint_is_read_only_and_reports_direction(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/validate",
            {"tag": TAG},
        )
    finally:
        srv.shutdown()

    assert status == 200
    assert body["valid"] is True
    assert body["upgrade_allowed"] is True
    assert body["system_build"]["canonical_tag"] == TAG
    assert alignment.validate_calls == [TAG]
    # Validation must never start a transition or claim an EMS operation.
    assert alignment.start_calls == []
    assert alignment.ems_calls == []


def test_validate_endpoint_rejects_unknown_fields(tmp_path):
    executor, _install_dir, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/validate",
            {"tag": TAG, "confirm": True},
        )
    finally:
        srv.shutdown()

    assert status == 400
    assert body["error"] == "unsupported_field"
    assert alignment.validate_calls == []


# --- development-build acknowledgement (end-to-end to the server) ---------

DEV_TAG = "dev-feature-x-f7265fc-42-1"
DEV_FINGERPRINT = _FP.selection_fingerprint(_FP._build(DEV_TAG))


# --- selection-fingerprint enforcement (verified build == executed build) ---
#
# Verify System Build returns an immutable selection fingerprint of the resolved
# Admin/EMS pair. Upgrade System must send it back and the server must reject a
# missing or changed fingerprint BEFORE any preflight or mutation, so a moving
# tag (e.g. latest re-pushed to a new digest) cannot be silently installed.


def test_execute_without_fingerprint_is_rejected_before_preflight(tmp_path):
    executor = _RejectingPreflightExecutor()
    alignment = _AlignedSystemBuild()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        # No selection_fingerprint at all.
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"target_release": TAG, "confirm": True, "options": ALL_OPTIONS},
        )
    finally:
        srv.shutdown()

    assert status == 409
    assert body["reason"] == "system_build_verification_required"
    # Rejected before any preflight, resolve-side mutation or transition.
    assert executor.preflight_calls == []
    assert alignment.start_calls == []


def test_execute_with_matching_fingerprint_continues_to_preflight(tmp_path):
    executor = _RejectingPreflightExecutor()
    alignment = _AlignedSystemBuild()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            _execute_body(),
        )
    finally:
        srv.shutdown()

    # The fingerprint matches, so the live installation preflight runs as before
    # (this executor then rejects on its own current-state check).
    assert executor.preflight_calls == [(TAG, ALL_OPTIONS, True)]
    assert body["reason"] == "config_missing"
    assert alignment.start_calls == []


def test_execute_with_stale_fingerprint_is_rejected_before_preflight(tmp_path):
    executor = _RejectingPreflightExecutor()
    alignment = _AlignedSystemBuild()
    verified = FINGERPRINT
    # The tag moves to a new EMS digest after the operator verified it.
    alignment.ems_digest = "sha256:ems-moved"
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            _execute_body(fingerprint=verified),
        )
    finally:
        srv.shutdown()

    assert status == 409
    assert body["reason"] == "system_build_verification_stale"
    # No preflight, no transition: the changed build is refused up front.
    assert executor.preflight_calls == []
    assert alignment.start_calls == []


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("ems_digest", "sha256:ems-moved"),
        ("admin_digest", "sha256:admin-moved"),
        ("revision", "0000000000000000000000000000000000000000"),
        ("channel", "latest"),
    ],
)
def test_execute_rejects_any_changed_identity_dimension(tmp_path, attribute, value):
    executor = _RejectingPreflightExecutor()
    alignment = _AlignedSystemBuild()
    verified = _FP.selection_fingerprint(_FP._build(TAG))
    setattr(alignment, attribute, value)
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            _execute_body(fingerprint=verified),
        )
    finally:
        srv.shutdown()

    assert status == 409
    assert body["reason"] == "system_build_verification_stale"
    assert executor.preflight_calls == []
    assert alignment.start_calls == []


def test_execute_rejects_changed_build_id(tmp_path):
    # build_id is part of the fingerprint; the fake derives it from the tag, so a
    # verified fingerprint for a different tag must be refused for THIS target.
    executor = _RejectingPreflightExecutor()
    alignment = _AlignedSystemBuild()
    verified = _FP.selection_fingerprint(_FP._build("v0.6.2"))
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            _execute_body(fingerprint=verified),
        )
    finally:
        srv.shutdown()

    assert status == 409
    assert body["reason"] == "system_build_verification_stale"
    assert executor.preflight_calls == []
    assert alignment.start_calls == []


def test_development_upgrade_requires_acknowledgement(tmp_path):
    executor, _install, compose, docker = _make_executor(tmp_path, prepared=DEV_TAG)
    alignment = _AlignedSystemBuild()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": DEV_TAG, "options": ALL_OPTIONS, "selection_fingerprint": DEV_FINGERPRINT},
        )
    finally:
        srv.shutdown()

    assert status == 400
    assert body["error"] == "acknowledgement_required"
    # No transition, no backup, no image touched without acknowledgement.
    assert alignment.start_calls == []
    assert compose.oneoff_calls == [] and docker.pulled == []


def test_development_upgrade_with_acknowledgement_starts(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path, prepared=DEV_TAG)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {
                "confirm": True,
                "target_release": DEV_TAG,
                "options": ALL_OPTIONS,
                "acknowledge_risk": True,
                "selection_fingerprint": DEV_FINGERPRINT,
            },
        )
    finally:
        srv.shutdown()

    assert status == 202
    assert alignment.start_calls == [(DEV_TAG, "guided_upgrade")]
    assert alignment.development_risk_acknowledged is True


def test_stable_upgrade_needs_no_acknowledgement(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()

    assert status == 202
    assert alignment.start_calls == [(TAG, "guided_upgrade")]
    assert alignment.development_risk_acknowledged is False


def test_execute_persists_durable_guided_upgrade_context(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, _body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert status == 202
        context = srv.guided_upgrade_context.load(
            operation_id="upgrade-op", target_system_tag=TAG
        )
    finally:
        srv.shutdown()

    assert context is not None
    assert context.options == ALL_OPTIONS
    assert context.request_fingerprint == guided_upgrade_request_fingerprint(
        TAG, ALL_OPTIONS
    )
    # The pre-alignment backup ran and its verified, exact archive is recorded.
    assert context.backup_completed is True
    assert context.backup_verified is True
    assert context.backup_reference == BACKUP_ARCHIVE_PATH


class _FailingContextStore:
    """Context store whose durable save always fails."""

    def __init__(self):
        self.load_calls = []

    def save(self, **_kwargs):
        raise OSError("disk full")

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        return None

    def clear(self):
        pass


def test_execute_context_save_failure_fails_closed_without_admin_update(tmp_path):
    executor, _install, _compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(
        executor,
        system_alignment=alignment,
        guided_upgrade_context=_FailingContextStore(),
    )
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
    finally:
        srv.shutdown()

    assert status >= 400
    assert body["error"] == "guided_upgrade_context_persistence_failed"
    # No reconnect response, no Admin replacement, no active transition started.
    assert body.get("reconnect") is not True
    assert alignment.start_calls == []
    assert alignment.active is False


# --- automatic resume after Admin reconnect ------------------------------


def _start_reconnecting_upgrade(base, compose):
    status, first = _post(
        base + "/api/admin/maintenance/upgrade/execute",
        {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
    )
    assert status == 202 and first["reconnect"] is True
    assert len(compose.oneoff_calls) == 1  # the pre-alignment backup ran once
    return first


def test_reconnect_resumes_guided_upgrade_automatically(tmp_path):
    executor, _install, compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        _start_reconnecting_upgrade(base, compose)
        # The replacement Admin reconnects. Resume takes ONLY the operation id;
        # the browser resends no target, options, or plan.
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/resume",
            {"operation_id": "upgrade-op"},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()

    assert job["status"] == "succeeded"
    # Backup and preflight were restored from durable state, not repeated.
    assert len(compose.oneoff_calls) == 1


def test_duplicate_resume_creates_no_second_job(tmp_path):
    executor, _install, compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        _start_reconnecting_upgrade(base, compose)
        _s1, first = _post(
            base + "/api/admin/maintenance/upgrade/resume",
            {"operation_id": "upgrade-op"},
        )
        _s2, second = _post(
            base + "/api/admin/maintenance/upgrade/resume",
            {"operation_id": "upgrade-op"},
        )
        job = _wait_job(base, first["job_id"])
    finally:
        srv.shutdown()

    # Both resume requests reference the exact same single job.
    assert first["job_id"] == second["job_id"]
    assert job["status"] == "succeeded"
    # Exactly one durable EMS claim happened (no second execution).
    claims = [c for c in alignment.ems_calls if c[0] == "claim"]
    assert len(claims) == 1
    assert len(compose.oneoff_calls) == 1


def test_resume_rejects_a_wrong_operation_id(tmp_path):
    executor, _install, compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        _start_reconnecting_upgrade(base, compose)
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/resume",
            {"operation_id": "not-the-operation"},
        )
    finally:
        srv.shutdown()

    assert status == 409
    assert body["error"] == "operation_mismatch"
    # No EMS work started for the mismatched operation.
    assert len(compose.oneoff_calls) == 1


def test_resume_options_are_restored_from_durable_context(tmp_path):
    # The resume body carries only the operation id, so a successful config-aware
    # run proves the options came from the durable context, not the request.
    executor, install, compose, _docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        _start_reconnecting_upgrade(base, compose)
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/resume",
            {"operation_id": "upgrade-op"},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()

    assert job["status"] == "succeeded"
    # config_add_keys was in the stored options: the target key was written.
    written = json.loads((install / "config" / "config.json").read_text(encoding="utf-8"))
    assert written["system"]["new_target_key"] == 42


def test_resume_after_admin_restart_deploys_bound_digest_not_moved_tag(tmp_path):
    # The replacement Admin has an empty resolver cache. If the tag moved during
    # the restart, a fresh resolve would pick a different digest; the resume must
    # instead deploy the exact digest bound into the durable transition.
    executor, _install, compose, docker = _make_executor(tmp_path)
    alignment = _AlignedSystemBuild(stage="admin_reconnect_pending")
    srv, base = _server(executor, system_alignment=alignment)
    try:
        _start_reconnecting_upgrade(base, compose)
        bound_digest = alignment.bound_ems_digest
        # The tag moves after the Admin was replaced.
        alignment.ems_digest = "sha256:moved-after-restart"
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/resume",
            {"operation_id": "upgrade-op"},
        )
        assert status == 202
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()

    assert job["status"] == "succeeded"
    runtime_ref = digest_pinned_ref(
        f"ghcr.io/basecubedev/ems-solarflow-api-control:{TAG}", bound_digest
    )
    assert docker.pulled == [runtime_ref]
    assert not any("moved-after-restart" in ref for ref in docker.pulled)


# --- fail-closed known-good gate on the applied upgrade ------------------


class _RawEmsCli:
    """EMS CLI stub returning an arbitrary (possibly malformed) diagnosis."""

    def __init__(self, payload):
        self._payload = payload

    def run(self, check_ids=None):
        return self._payload


class _RecordingSystemBuild(_AlignedSystemBuild):
    """Aligned-build stub that records the health verdict it is handed and
    surfaces the persisted error on the transition, like the real service."""

    def __init__(self, stage="resources_verified"):
        super().__init__(stage)
        self.healthchecks = []
        self._error_code = None
        self._error_message = None

    def finish_healthcheck(
        self, *, operation_id, passed, error_code=None, error_message=None, **_kwargs
    ):
        self.healthchecks.append(
            {"passed": passed, "error_code": error_code, "error_message": error_message}
        )
        self._error_code = None if passed else error_code
        self._error_message = None if passed else error_message
        self.stage = "completed" if passed else "failed_recoverable"
        return {"status": self.stage, "operation_id": operation_id}

    def status(self):
        base = super().status()
        transition = base.get("transition")
        if transition and self._error_code:
            transition["error_code"] = self._error_code
            transition["error_message"] = self._error_message
        return base


def _executor_with_diagnostics(tmp_path, payload):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    return GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases),
        compose=FakeCompose(),
        docker_cli=FakeDockerCli(),
        ems_cli=_RawEmsCli(payload),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )


def _run_applied_upgrade(tmp_path, payload):
    executor = _executor_with_diagnostics(tmp_path, payload)
    alignment = _RecordingSystemBuild()
    srv, base = _server(executor, system_alignment=alignment)
    try:
        status, body = _post(
            base + "/api/admin/maintenance/upgrade/execute",
            {"confirm": True, "target_release": TAG, "options": ALL_OPTIONS, "selection_fingerprint": FINGERPRINT},
        )
        assert status == 202, body
        job = _wait_job(base, body["job_id"])
    finally:
        srv.shutdown()
    return job, alignment


@pytest.mark.parametrize(
    "payload, expected_reason",
    [
        ({}, "healthcheck_result_invalid"),
        ({"available": True}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": None}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "banana"}}, "healthcheck_result_invalid"),
        ({"available": True, "summary": {"status": "failed"}}, "healthcheck_failed"),
        ({"available": True, "summary": {"status": "unavailable"}}, "healthcheck_unavailable"),
        ({"available": False, "summary": {"status": "ok"}}, "healthcheck_unavailable"),
    ],
)
def test_applied_upgrade_fails_closed_on_bad_diagnostics(tmp_path, payload, expected_reason):
    job, alignment = _run_applied_upgrade(tmp_path, payload)

    assert job["result"]["status"] == "failed"
    assert job["result"]["ok"] is False
    assert job["result"]["reason"] == expected_reason
    # known-good is never marked: finish_healthcheck saw an explicit failure.
    assert alignment.healthchecks[-1]["passed"] is False
    assert alignment.healthchecks[-1]["error_code"] == expected_reason
    assert alignment.stage == "failed_recoverable"


@pytest.mark.parametrize("status_value", ["ok", "warning"])
def test_applied_upgrade_completes_on_explicit_success(tmp_path, status_value):
    payload = {"available": True, "summary": {"status": status_value}}
    job, alignment = _run_applied_upgrade(tmp_path, payload)

    assert job["result"]["status"] == "completed"
    assert alignment.healthchecks[-1]["passed"] is True
    assert alignment.stage == "completed"


# --- typed digest-pull failures are preserved through the executor ---------

PULL_ONLY_OPTIONS = {
    "backup": False,
    "config_check": False,
    "config_add_keys": False,
    "config_comments": False,
    "pull_image": True,
    "recreate": True,
    "diagnostics": False,
}


class RaisingPullDocker(FakeDockerCli):
    def __init__(self, error):
        super().__init__()
        self._error = error

    def pull(self, image, on_progress=None):
        self.pulled.append(image)
        raise self._error


@pytest.mark.parametrize(
    "code, message",
    [
        ("system_build_registry_rate_limited", "GitHub Container Registry rate limit reached."),
        ("image_pull_rate_limited", "GitHub Container Registry rate limit reached."),
        ("image_pull_network_error", "The image could not be downloaded because of a network error."),
        ("image_pull_failed", "The image could not be pulled."),
    ],
)
def test_pull_failure_preserves_typed_docker_error(tmp_path, code, message):
    install = _install(tmp_path)
    releases = _prepared_release(tmp_path)
    compose = FakeCompose()
    docker = RaisingPullDocker(DockerError(code, message))
    executor = GuidedUpgradeExecutor(
        release_manager=FakeReleaseManager(releases),
        compose=compose,
        docker_cli=docker,
        ems_cli=FakeEmsCli("ok"),
        install_context_provider=lambda: detect_install_context(base_dir=str(install)),
    )
    original_compose = (install / "docker-compose.yml").read_text(encoding="utf-8")
    original_config = (install / "config" / "config.json").read_text(encoding="utf-8")

    result = executor.execute(
        TAG, PULL_ONLY_OPTIONS, confirm=True, system_build=_resolved_build()
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["reason"] == code
    assert result["message"] == message
    pull = next(s for s in result["steps"] if s["id"] == "pull_image")
    assert pull["status"] == "error"
    assert pull["code"] == code
    assert pull["detail"] == message

    assert (install / "docker-compose.yml").read_text(encoding="utf-8") == original_compose
    assert not (install / "docker-compose.yml.bak").exists()
    assert compose.calls == []
    assert (install / "config" / "config.json").read_text(encoding="utf-8") == original_config
