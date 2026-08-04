# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment workers stay bound to the workflow that authorized them.

Preparation and start are asynchronous: the HTTP handler returns 202 and a
background worker then writes the live workspace config, the compose file and the
deployment marker. Archive 99 verified nothing at all on the way in, and the
worker stamped the marker with ``setup_workflows.active()`` — whichever workflow
happened to be current when it reached that line. A workflow that was superseded
mid-prepare therefore had its replacement's identity written into the marker, and
an abandon could run while the worker was still writing.

These tests pin the binding: the request names its workflow, the lifecycle claim
exists before the handler answers, the worker carries an immutable identity that
it re-checks before the irreversible writes, abandon is refused while that worker
is live, and a restart adopts nothing it cannot prove.

See ``docs/technical/admin-workflow-state.md``.
"""

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.deployment import DeploymentService
from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.setup_lifecycle import SetupLifecycleCoordinator
from admin.setup_workflow import SetupWorkflowArtifacts
from tests.test_admin_server import (
    _FakeDeployment,
    _own_active_setup_transition,
    _request,
    _serve,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.system_build,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]

WAIT_S = 20


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _Docker:
    """Enough Docker surface for prepare to run to the marker write."""

    def __init__(self):
        self.pulled = []

    @staticmethod
    def probe():
        return {"state": "ready", "code": None, "message": "Docker is available."}

    @staticmethod
    def check():
        return None

    def pull(self, image, progress=None):
        self.pulled.append(image)
        if progress is not None:
            progress(100, "done")

    @staticmethod
    def check_workspace_permissions(workspace, image, puid, pgid):
        return None

    @staticmethod
    def repair_workspace_permissions(workspace, image, puid, pgid):
        return None

    @staticmethod
    def inspect_container(name):
        return None


class _Installer:
    def __init__(self, before_compose=None):
        self._before_compose = before_compose

    def prepare(self, workspace, script, analytics=False, tag=None, on_line=None):
        if self._before_compose is not None:
            self._before_compose()
        Path(workspace, "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )


def _admin_data(tmp_path):
    data_dir = tmp_path / "admin-data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    return data_dir


def _release_manager(data_dir):
    releases = data_dir / "releases"
    (releases / "v0.8.0").mkdir(parents=True, exist_ok=True)
    (releases / "v0.8.0" / "install-docker.sh").write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )
    return SimpleNamespace(
        releases_dir=releases,
        data_dir=data_dir,
        config_template=lambda: {
            "tag": "v0.8.0",
            "template": {},
            "docker_image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.0",
        },
    )


def _owned_generated(store, data_dir):
    """An active workflow with a preview-bound generated config on disk."""

    payload = (json.dumps({"devices": []}, indent=2) + "\n").encode("utf-8")
    workflow_id = store.ensure_active()["workflow_id"]
    digest = hashlib.sha256(payload).hexdigest()
    record = store.record_preview(
        workflow_id,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256=digest,
    )
    preview_id = record["preview"]["preview_id"]
    store.bind_generated_artifacts(workflow_id, preview_id=preview_id)
    artifacts = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)
    target = artifacts.generated_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    artifacts.record_generated(
        workflow_id=workflow_id,
        preview_id=preview_id,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256=digest,
    )
    return workflow_id, preview_id, target


def _service(data_dir, workspace, target, store, *, installer=None):
    return DeploymentService(
        _release_manager(data_dir),
        SimpleNamespace(target_path=target),
        workspace_dir=workspace,
        admin_data_dir=data_dir,
        docker=_Docker(),
        installer=installer or _Installer(),
        runtime_env={"PUID": "1000", "PGID": "1000"},
        setup_workflows=store,
    )


def _wait_for_job(service, job_id):
    for _ in range(2000):
        job = service.job(job_id)
        if job is not None and job["status"] != "running":
            return job
        threading.Event().wait(0.01)
    raise AssertionError("prepare job never settled")


# --- the marker carries the authorized worker's identity ----------------------


def test_prepare_worker_cannot_stamp_a_replacement_workflow(tmp_path):
    """The identity is captured at submission, not looked up at write time."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id, preview_id, target = _owned_generated(store, data_dir)
    replaced = threading.Event()

    def supersede_mid_prepare():
        # A parallel session retires the preparing workflow and starts another.
        store.finish(workflow_id, status="superseded")
        store.start_replacement(selected_system_tag="v0.9.0")
        replaced.set()

    service = _service(
        data_dir,
        workspace,
        target,
        store,
        installer=_Installer(before_compose=supersede_mid_prepare),
    )
    result = service.prepare(overwrite=True, workflow_id=workflow_id)
    assert result["ok"] is True, result
    job = _wait_for_job(service, result["job"]["job_id"])

    assert replaced.is_set()
    assert job["status"] == "failed", job
    assert job["error"]["code"] == "setup_workflow_not_active"
    assert not service.marker_path.exists(), (
        "a superseded workflow's worker must not write the deployment marker"
    )
    replacement = store.active()
    assert replacement["workflow_id"] != workflow_id
    assert preview_id


def test_prepare_stamps_the_marker_with_its_own_workflow(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id, preview_id, target = _owned_generated(store, data_dir)

    service = _service(data_dir, workspace, target, store)
    result = service.prepare(overwrite=True, workflow_id=workflow_id)
    job = _wait_for_job(service, result["job"]["job_id"])

    assert job["status"] == "succeeded", job
    marker = json.loads(service.marker_path.read_text(encoding="utf-8"))
    assert marker["owner"] == "guided_setup"
    assert marker["workflow_id"] == workflow_id
    assert marker["preview_id"] == preview_id


def test_prepare_refuses_a_foreign_workflow_id(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store = GuidedSetupWorkflowStore(data_dir)
    _workflow_id, _preview_id, target = _owned_generated(store, data_dir)

    service = _service(data_dir, workspace, target, store)
    result = service.prepare(overwrite=True, workflow_id="an-older-tab")

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"
    assert "job" not in result
    assert not (workspace / "config" / "config.json").exists()


# --- start requires the marker's own workflow --------------------------------


def _prepared(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id, _preview_id, target = _owned_generated(store, data_dir)
    service = _service(data_dir, workspace, target, store)
    result = service.prepare(overwrite=True, workflow_id=workflow_id)
    _wait_for_job(service, result["job"]["job_id"])
    return service, store, workflow_id


def test_deployment_start_requires_matching_workflow_id(tmp_path):
    service, _store, workflow_id = _prepared(tmp_path)

    refused = service.start(workflow_id="a-different-workflow")
    assert refused["ok"] is False
    assert refused["reason"] == "deployment_marker_invalid"

    accepted = service.start(workflow_id=workflow_id)
    assert accepted.get("reason") != "deployment_marker_invalid", accepted


def test_restart_does_not_adopt_unproven_running_setup_work(tmp_path):
    """After a restart nothing is claimed, and the durable checks still refuse.

    The in-memory claim of a pre-restart worker cannot have survived, so a fresh
    coordinator reports the workflow as unowned — while the marker written by the
    old, now-superseded workflow is still refused by the durable check. Fail
    closed, not "assume the old worker is still running".
    """

    service, store, workflow_id = _prepared(tmp_path)
    assert service.marker_path.exists()
    store.finish(workflow_id, status="superseded")
    replacement = store.start_replacement(selected_system_tag="v0.9.0")

    restarted_lifecycle = SetupLifecycleCoordinator()
    assert restarted_lifecycle.active_operation(workflow_id) is None
    assert restarted_lifecycle.is_terminalized(workflow_id) is False

    refused = service.start(workflow_id=replacement["workflow_id"])
    assert refused["ok"] is False
    assert refused["reason"] == "deployment_marker_invalid", refused


# --- claim lifecycle ---------------------------------------------------------


def test_prepare_claim_exists_before_worker_submission_returns(tmp_path):
    """The claim must be live the moment the handler could answer 202."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id, _preview_id, target = _owned_generated(store, data_dir)
    lifecycle = SetupLifecycleCoordinator()
    inside_worker = threading.Event()
    release_worker = threading.Event()

    def block_worker():
        inside_worker.set()
        assert release_worker.wait(WAIT_S)

    service = _service(
        data_dir, workspace, target, store,
        installer=_Installer(before_compose=block_worker),
    )
    claim = lifecycle.claim_mutation(
        workflow_id=workflow_id, operation="deployment_prepare"
    )
    result = service.prepare(
        overwrite=True, workflow_id=workflow_id, on_settled=claim.release
    )
    assert result["ok"] is True

    assert inside_worker.wait(WAIT_S)
    # Submission has returned and the worker is mid-flight: still claimed.
    assert lifecycle.active_operation(workflow_id) == "deployment_prepare"
    with pytest.raises(Exception) as excinfo:
        lifecycle.claim_termination(workflow_id=workflow_id, operation="abandon")
    assert excinfo.value.code == "setup_operation_in_progress"

    release_worker.set()
    job = _wait_for_job(service, result["job"]["job_id"])
    assert job["status"] == "succeeded", job
    assert lifecycle.active_operation(workflow_id) is None


def test_deployment_worker_releases_claim_on_failure(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id, _preview_id, target = _owned_generated(store, data_dir)
    lifecycle = SetupLifecycleCoordinator()

    def explode():
        raise RuntimeError("installer blew up")

    service = _service(
        data_dir, workspace, target, store,
        installer=_Installer(before_compose=explode),
    )
    claim = lifecycle.claim_mutation(
        workflow_id=workflow_id, operation="deployment_prepare"
    )
    result = service.prepare(
        overwrite=True, workflow_id=workflow_id, on_settled=claim.release
    )
    job = _wait_for_job(service, result["job"]["job_id"])

    assert job["status"] == "failed", job
    assert lifecycle.active_operation(workflow_id) is None, (
        "a worker exception must still release the lifecycle claim"
    )
    # And the workflow stays terminalizable afterwards.
    with lifecycle.claim_termination(workflow_id=workflow_id, operation="abandon"):
        pass


# --- over HTTP: abandon cannot race a live prepare worker --------------------


class _BlockingPrepareDeployment(_FakeDeployment):
    """Prepare submits a worker that parks just before its marker write."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.marker_workflow_id = None

    def prepare(self, overwrite=False, *, workflow_id=None, on_settled=None):
        self.prepared_overwrite = overwrite

        def worker():
            self.entered.set()
            assert self.release.wait(WAIT_S)
            # The point of no return: the marker is stamped with the identity
            # this worker was authorized for, never a freshly looked-up one.
            self.marker_workflow_id = workflow_id
            if on_settled is not None:
                on_settled()

        threading.Thread(target=worker, daemon=True).start()
        return {
            "ok": True,
            "status": 202,
            "job": {
                "job_id": "job-1",
                "status": "running",
                "steps": [],
                "images": [],
                "workspace": "/data/deployment",
            },
        }


def test_abandon_refused_while_prepare_worker_is_active(tmp_path):
    deployment = _BlockingPrepareDeployment()
    srv, base = _serve(deployment=deployment)
    try:
        workflow_id = srv.setup_workflows.ensure_active()["workflow_id"]
        _own_active_setup_transition(srv, base, workflow_id)
        status, _, job = _request(
            f"{base}/api/setup/deployment/prepare",
            method="POST",
            body={"setup_workflow_id": workflow_id, "overwrite": True},
        )
        assert status == 202, job
        assert deployment.entered.wait(WAIT_S)

        status, _, refused = _request(
            f"{base}/api/setup/abandon",
            method="POST",
            body={"setup_workflow_id": workflow_id},
        )
        assert status == 409, refused
        assert refused["error"] == "setup_operation_in_progress"
        assert refused["operation"] == "deployment_prepare"
        # Nothing was terminalized underneath the running worker.
        status, _, view = _request(f"{base}/api/setup/workflow")
        assert view["workflow"]["status"] == "active"
        assert view["workflow"]["workflow_id"] == workflow_id

        deployment.release.set()
        for _ in range(2000):
            if deployment.marker_workflow_id is not None:
                break
            threading.Event().wait(0.01)
        assert deployment.marker_workflow_id == workflow_id, (
            "the marker must carry the workflow the worker was authorized for"
        )

        # Once the worker settled, the same workflow can be abandoned normally.
        status, _, abandoned = _request(
            f"{base}/api/setup/abandon",
            method="POST",
            body={"setup_workflow_id": workflow_id},
        )
        assert status == 200, abandoned
        assert abandoned["ok"] is True
        assert abandoned["workflow"]["status"] == "abandoned"
    finally:
        deployment.release.set()
        srv.shutdown()
        srv.server_close()


def test_prepare_over_http_requires_the_exact_workflow_id(tmp_path):
    deployment = _BlockingPrepareDeployment()
    srv, base = _serve(deployment=deployment)
    try:
        srv.setup_workflows.ensure_active()
        status, _, missing = _request(
            f"{base}/api/setup/deployment/prepare", method="POST", body={}
        )
        assert status == 409, missing
        assert missing["error"] == "setup_workflow_required"

        status, _, foreign = _request(
            f"{base}/api/setup/deployment/prepare",
            method="POST",
            body={"setup_workflow_id": "an-older-tab"},
        )
        assert status == 409, foreign
        assert foreign["error"] == "setup_workflow_not_active"
        assert deployment.prepared_overwrite is None, (
            "no worker may be submitted for an unverified workflow"
        )
    finally:
        deployment.release.set()
        srv.shutdown()
        srv.server_close()
