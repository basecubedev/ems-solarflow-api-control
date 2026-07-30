# SPDX-License-Identifier: AGPL-3.0-or-later
"""Complete Fresh Install deployment contract over the real service boundary.

These tests drive the productive HTTP routes against the real
``SystemAlignmentService`` and durable ``PendingTransitionStore``. Docker
execution is represented by a deterministic deployment adapter that exposes
the productive container-start / health-check callback boundary, so every
durable stage between build confirmation and ``completed`` is observable.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    EMS_IMAGE_REPO,
    PendingTransitionStore,
    make_transition_record,
)
from admin.deployment import DeploymentService
from admin.guided_upgrade import UpgradeJob
from admin.operation_coordinator import OperationCoordinator
from admin.setup_lifecycle import SetupLifecycleCoordinator
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.server import ScanRegistry, create_server
from admin.system_alignment import SystemAlignmentService
from admin.system_build import SystemBuild
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.setup_config import authorize_setup_mutation

pytestmark = [pytest.mark.simulation, pytest.mark.system_build]

TAG = "v0.8.0"
REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
T0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

FRESH_INSTALL_STAGES = (
    "admin_aligned",
    "resources_verified",
    "ems_operation_pending",
    "ems_operation_running",
    "healthcheck_pending",
    "completed",
)


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    return isolated_install_root


def _request(url, method="GET", body=None, extra_headers=None):
    data = None
    headers = dict(auth_headers(url, method))
    headers.update(extra_headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read() or b"null")


def _fresh_setup_intent(base):
    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": False},
    )
    assert status == 200
    assert payload["ok"] is True
    return payload["setup_intent_id"]


def _fake_scan(cidr, timeout_ms=600, max_workers=32, progress_callback=None):
    return [], []


def _build():
    return SystemBuild(
        requested_tag=TAG,
        canonical_tag=TAG,
        channel="stable",
        revision=REVISION,
        build_id=f"{TAG}-f7265fc",
        admin_image=f"{ADMIN_IMAGE_REPO}:{TAG}",
        admin_digest="sha256:admin",
        ems_image=f"{EMS_IMAGE_REPO}:{TAG}",
        ems_digest="sha256:ems",
        release_tag=TAG,
    )


class _Resolver:
    def __init__(self, build):
        self._build = build

    def resolve(self, tag):
        return self._build


class _Embedded:
    def verify(self, *, running_build):
        return running_build

    def import_into_cache(self, *, running_build):
        return running_build.get("canonical_tag")


class _ReleaseArchive:
    def import_into_cache(self, *, running_build):
        return running_build.get("canonical_tag")


class _ReleaseManager:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.template = {
            "devices": [
                {
                    "name": "inverter_1",
                    "ip": "192.0.2.1",
                    "sn": "YOUR_SN",
                    "max_power": 800,
                }
            ]
        }

    def list_releases(self, *, for_upgrade=True):
        return {"releases": [], "warnings": []}

    def prepare(self, tag, *, revision=None):
        return {"status": "ready", "tag": tag, "warnings": []}

    def config_template(self):
        return {
            "tag": TAG,
            "template": self.template,
            "source": f"/cache/{TAG}/config.template.json",
        }


class _RecordingTransitionStore(PendingTransitionStore):
    """Capture every durable stage write in order."""

    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.stages = []

    def _write_raw(self, data):
        super()._write_raw(data)
        self.stages.append((data or {}).get("stage"))

    def distinct_stages(self):
        return [
            stage
            for index, stage in enumerate(self.stages)
            if index == 0 or stage != self.stages[index - 1]
        ]


class _ScriptedDeployment(DeploymentService):
    """Deterministic Docker-execution stand-in with the productive callbacks.

    The test drives the container-start and terminal boundaries explicitly so
    the durable ``ems_operation_running`` and ``healthcheck_pending`` stages
    stay observable instead of collapsing inside one synchronous worker.
    """

    def __init__(self, release_manager, admin_data_dir, workspace_dir):
        super().__init__(
            release_manager,
            config_export=None,
            admin_data_dir=admin_data_dir,
            workspace_dir=workspace_dir,
            dashboard_probe=lambda _url: True,
            sleep=lambda _s: None,
        )
        self.prepare_calls = 0
        self.start_calls = 0
        self.healthy = True
        self._on_complete = None
        self._on_healthcheck = None
        self._start_snapshot = None

    def prepare(self, overwrite=False, *, workflow_id=None, on_settled=None):
        self.prepare_calls += 1
        self.prepare_workflow_id = workflow_id
        if on_settled is not None:
            on_settled()
        return {
            "ok": True,
            "status": 202,
            "job": {
                "job_id": "prep-1",
                "status": "running",
                "steps": [],
                "images": [],
                "workspace": "/data/deployment",
            },
        }

    def job(self, job_id):
        if job_id != "prep-1":
            return None
        return {
            "job_id": "prep-1",
            "status": "succeeded",
            "prepared": True,
            "steps": [],
            "images": [],
        }

    def start(
        self, *, on_complete=None, on_healthcheck=None, workflow_id=None,
        on_settled=None,
    ):
        self.start_calls += 1
        self._on_complete = on_complete
        self._on_healthcheck = on_healthcheck
        self.start_workflow_id = workflow_id
        self._on_settled = on_settled
        self._start_snapshot = {
            "job_id": "start-1",
            "status": "running",
            "phase": "Starting EMS containers",
            "services": [],
            "dashboard_url": "http://localhost:8080",
            "dashboard_reachable": None,
            "error": None,
        }
        return {"ok": True, "status": 202, "job": dict(self._start_snapshot)}

    def start_job(self, job_id):
        if job_id != "start-1" or self._start_snapshot is None:
            return None
        return dict(self._start_snapshot)

    def containers_running(self):
        self._on_healthcheck(dict(self._start_snapshot))

    def finish(self, *, dashboard_reachable):
        self._start_snapshot.update(
            status="succeeded", dashboard_reachable=dashboard_reachable
        )
        self._on_complete(dict(self._start_snapshot))

    def fail(self, code="ems_deployment_failed", message="EMS deployment failed."):
        self._start_snapshot.update(
            status="failed", error={"code": code, "message": message}
        )
        self._on_complete(dict(self._start_snapshot))

    def status(self):
        return {
            "prepared": True,
            "running": True,
            "services": [],
            "docker": {"state": "ready", "code": None},
            "dashboard_url": "http://localhost:8080",
            "dashboard_reachable": self.healthy,
            "errors": [],
        }


def _serve_fresh_install(tmp_path):
    state_dir = tmp_path / "admin-state"
    store = _RecordingTransitionStore(state_dir / "state")
    known_good = KnownGoodStore(state_dir / "state")
    build = _build()
    service = SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=store,
        embedded_resources=_Embedded(),
        release_archive_resources=_ReleaseArchive(),
        known_good_store=known_good,
        current_identity=lambda: ImageIdentity(
            image_ref=build.admin_image,
            digest=build.admin_digest,
            revision=build.revision,
            build_id=build.build_id,
        ),
        current_ems_identity=lambda: ImageIdentity(
            image_ref=build.ems_image,
            digest=build.ems_digest,
            revision=build.revision,
            channel=build.channel,
            build_id=build.build_id,
            release_tag=build.release_tag,
        ),
        persistent_ref=lambda: build.admin_image,
        launcher=lambda record: None,
        now=lambda: T0,
    )
    manager = _ReleaseManager(tmp_path / "release-data")
    deployment = _ScriptedDeployment(
        manager, tmp_path / "admin-data", tmp_path / "workspace"
    )
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        release_manager=manager,
        deployment=deployment,
        system_alignment=service,
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base, deployment, store, known_good


def _sample_transition(base):
    status, _, payload = _request(f"{base}/api/admin/system-alignment/status")
    assert status == 200
    return payload.get("transition") or {}


def _walk_to_started_deployment(base, deployment):
    """Steps 01-06: select, confirm, discovery, config, prepare, start."""

    status, _, validated = _request(
        f"{base}/api/admin/system-alignment/validate",
        method="POST",
        body={"tag": TAG},
    )
    assert status == 200
    assert validated["valid"] is True
    assert validated["next_allowed"] is True

    intent_id = _fresh_setup_intent(base)
    status, _, confirmed = _request(
        f"{base}/api/setup/system-build/confirm",
        method="POST",
        body={"tag": TAG, "acknowledge_risk": False},
        extra_headers={"X-Setup-Intent-ID": intent_id},
    )
    assert status == 200
    assert confirmed["resources_verified"] is True
    operation_id = confirmed["operation_id"]
    assert _sample_transition(base)["stage"] == "resources_verified"

    status, _, blocked = _request(
        f"{base}/api/setup/discovery/run", method="POST", body={"refresh": False}
    )
    assert status == 409
    assert blocked["error"] == "setup_operation_required"
    status, _, devices = _request(
        f"{base}/api/setup/discovery/run",
        method="POST",
        body={"refresh": False},
        extra_headers={"X-Setup-Operation-ID": operation_id},
    )
    assert status == 200
    assert "devices" in devices

    status, _, written = _request(
        f"{base}/api/setup/config/write",
        method="POST",
        body=authorize_setup_mutation(base, _request, {
            "devices": [
                {
                    "config_name": "inverter_1",
                    "display_name": "SolarFlow",
                    "role": "inverter",
                    "enabled": True,
                    "ip": "192.0.2.10",
                    "serial_number": "SN-FRESH-INSTALL",
                    "device_type": "zendure_solarflow_800_pro",
                    "api_family": "zendure_local_http",
                }
            ],
            "supported_grid_meter_count": 0,
        }),
    )
    assert status == 200
    assert written["ok"] is True

    status, headers, prepared = _request(
        f"{base}/api/setup/deployment/prepare",
        method="POST",
        body={"setup_workflow_id": _setup_workflow_id(base)},
    )
    assert status == 202
    assert headers["Content-Type"].startswith("application/json")
    assert prepared["job_id"] == "prep-1"
    assert prepared["transition"]["operation_id"] == operation_id
    assert prepared["transition"]["stage"] == "resources_verified"
    # Before the deployment worker claims the operation, the embedded status
    # already carries a successful liveness verdict: proven inactive.
    assert prepared["transition"]["worker_active"] is False
    assert prepared["transition"]["worker_status_available"] is True
    # Preparing the workspace never advances the EMS operation.
    assert deployment.start_calls == 0
    assert _sample_transition(base)["stage"] == "resources_verified"

    status, _, started = _request(
        f"{base}/api/setup/deployment/start",
        method="POST",
        body={"setup_workflow_id": _setup_workflow_id(base)},
    )
    assert status == 202
    assert started["job_id"] == "start-1"
    assert deployment.start_calls == 1
    return operation_id


def test_fresh_install_completes_through_observable_forward_stages(tmp_path):
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        _walk_to_started_deployment(base, deployment)

        running = _sample_transition(base)
        assert running["stage"] == "ems_operation_running"
        status, _, polled = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert status == 200
        assert polled["status"] == "running"
        assert polled["transition"]["stage"] == "ems_operation_running"

        deployment.containers_running()
        pending = _sample_transition(base)
        assert pending["stage"] == "healthcheck_pending"
        status, _, polled = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert status == 200
        assert polled["status"] == "running"
        assert polled["transition"]["stage"] == "healthcheck_pending"
        # Success is reported only after health verification.
        assert known_good.current() is None

        deployment.finish(dashboard_reachable=True)
        completed = _sample_transition(base)
        assert completed["stage"] == "completed"
        assert known_good.current()["build_id"] == f"{TAG}-f7265fc"

        assert store.distinct_stages() == list(FRESH_INSTALL_STAGES)
    finally:
        srv.shutdown()
        srv.server_close()


def test_failed_deployment_never_reports_completion(tmp_path):
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        _walk_to_started_deployment(base, deployment)

        deployment.fail()
        transition = _sample_transition(base)
        assert transition["stage"] == "failed_recoverable"
        assert transition["failed_stage"] == "ems_operation_running"
        assert transition["resume_stage"] == "ems_operation_pending"
        assert transition["error_code"] == "ems_deployment_failed"
        assert "completed" not in store.stages
        assert known_good.current() is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_failed_healthcheck_never_reports_completion(tmp_path):
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        _walk_to_started_deployment(base, deployment)

        deployment.containers_running()
        assert _sample_transition(base)["stage"] == "healthcheck_pending"

        deployment.finish(dashboard_reachable=False)
        transition = _sample_transition(base)
        assert transition["stage"] == "failed_recoverable"
        assert transition["failed_stage"] == "healthcheck_pending"
        assert transition["resume_stage"] == "healthcheck_pending"
        assert transition["error_code"] == "healthcheck_failed"
        assert "completed" not in store.stages
        assert known_good.current() is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_reload_recovers_durable_stage_and_completes_after_restart(tmp_path):
    srv, base, deployment, _, _ = _serve_fresh_install(tmp_path)
    try:
        operation_id = _walk_to_started_deployment(base, deployment)
        deployment.containers_running()
        assert _sample_transition(base)["stage"] == "healthcheck_pending"
    finally:
        # The Admin process dies before the health check can conclude.
        srv.shutdown()
        srv.server_close()

    srv2, base2, deployment2, store2, known_good2 = _serve_fresh_install(tmp_path)
    try:
        recovered = _sample_transition(base2)
        assert recovered["stage"] == "healthcheck_pending"
        assert recovered["operation_id"] == operation_id
        assert recovered["resume_available"] is True

        status, _, resumed = _request(
            f"{base2}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": operation_id},
        )
        assert status == 200
        assert resumed["stage"] == "completed"
        assert _sample_transition(base2)["stage"] == "completed"
        assert known_good2.current()["build_id"] == f"{TAG}-f7265fc"
        assert deployment2.start_calls == 0
    finally:
        srv2.shutdown()
        srv2.server_close()


# --- expired abandonment is worker-aware over the HTTP boundary -------------
#
# An expired ems_operation_running transition must not be abandoned over HTTP
# while its mutating worker is still tracked as live. Only once the worker is
# gone (e.g. the Admin process restarted and its in-memory registry reset) may
# the durable orphan be cancelled.

LATER = datetime(2026, 7, 17, 14, 0, 0, tzinfo=timezone.utc)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _cancel(base, operation_id):
    return _request(
        f"{base}/api/admin/system-alignment/cancel",
        method="POST",
        body={"operation_id": operation_id, "confirm": True},
    )


def _setup_workflow_id(base):
    """The workflow id every Setup deployment route requires by name."""

    workflow = (_request(f"{base}/api/setup/workflow")[2] or {}).get("workflow") or {}
    return workflow.get("workflow_id")


def _abandon(base):
    return _request(
        f"{base}/api/setup/abandon",
        method="POST",
        body={"setup_workflow_id": _setup_workflow_id(base)},
    )


def test_deployment_start_job_poll_embeds_worker_aware_status(tmp_path):
    # The browser renders the transition embedded in the start-job poll. That
    # embed must carry the same coordinator-backed worker verdict as the
    # dedicated status endpoint, or the poll re-enables Abandon against a live
    # worker and the cancel then 409s.
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        operation_id = _walk_to_started_deployment(base, deployment)
        assert srv.operation_coordinator.is_active(operation_id) is True

        status, _, polled = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert status == 200
        transition = polled["transition"]
        assert transition["worker_active"] is True
        assert transition["worker_status_available"] is True
        assert transition["cancel_available"] is False

        srv.system_alignment._now = lambda: LATER
        status, _, polled = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert status == 200
        transition = polled["transition"]
        assert transition["expired"] is True
        assert transition["worker_active"] is True
        assert transition["worker_status_available"] is True
        assert transition["cancel_available"] is False
        assert transition["resume_available"] is False

        deployment.finish(dashboard_reachable=True)
        assert _wait_until(
            lambda: not srv.operation_coordinator.is_active(operation_id)
        )
        status, _, polled = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert status == 200
        transition = polled["transition"]
        assert transition["worker_active"] is False
        assert transition["worker_status_available"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_upgrade_job_poll_embeds_worker_aware_status(tmp_path):
    # Same contract for the Guided Upgrade job poll: its embedded transition
    # must report the live coordinator claim, not an unconditional "inactive".
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        build = _build()
        record = store.begin(
            make_transition_record(
                mode="guided_upgrade",
                system_tag=build.canonical_tag,
                build_id=build.build_id,
                revision=build.revision,
                admin_image=build.admin_image,
                admin_digest=build.admin_digest,
                ems_image=build.ems_image,
                ems_digest=build.ems_digest,
                stage="ems_operation_running",
                ttl_seconds=60,
                now=T0,
            ),
            now=T0,
        )
        operation_id = record.operation_id

        release = threading.Event()
        job = UpgradeJob("upgrade-job", [])

        def runner(handle):
            release.wait(5)
            handle.finish(
                {"ok": True, "status": "succeeded", "steps": [], "warnings": []}
            )

        submitted, created = srv.upgrade_jobs.get_or_submit(
            operation_id, job, runner, coordinator=srv.operation_coordinator
        )
        assert created is True and submitted is not None
        assert _wait_until(
            lambda: srv.operation_coordinator.is_active(operation_id)
        )
        srv.system_alignment._now = lambda: LATER

        try:
            status, _, polled = _request(
                f"{base}/api/admin/maintenance/upgrade/jobs/upgrade-job"
            )
            assert status == 200
            transition = polled["transition"]
            assert transition["expired"] is True
            assert transition["worker_active"] is True
            assert transition["worker_status_available"] is True
            assert transition["cancel_available"] is False
            assert transition["resume_available"] is False
        finally:
            release.set()

        assert _wait_until(
            lambda: not srv.operation_coordinator.is_active(operation_id)
        )
        status, _, polled = _request(
            f"{base}/api/admin/maintenance/upgrade/jobs/upgrade-job"
        )
        assert status == 200
        transition = polled["transition"]
        assert transition["worker_active"] is False
        assert transition["worker_status_available"] is True
        assert transition["cancel_available"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_resume_rejection_embeds_worker_aware_transition(tmp_path):
    # The transition_active 409 says the worker is still running; its embedded
    # transition must report the same verdict instead of "inactive".
    srv, base, deployment, store, _ = _serve_fresh_install(tmp_path)
    try:
        operation_id = _walk_to_started_deployment(base, deployment)
        assert srv.operation_coordinator.is_active(operation_id) is True

        status, _, body = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": operation_id},
        )
        assert status == 409
        assert body["error"] == "transition_active"
        transition = body["transition"]
        assert transition["worker_active"] is True
        assert transition["worker_status_available"] is True
        assert transition["cancel_available"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_expired_running_transition_cancel_blocked_while_deployment_worker_live(
    tmp_path,
):
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        operation_id = _walk_to_started_deployment(base, deployment)
        assert store.read().stage == "ems_operation_running"
        # The deployment worker claimed the operation before it began mutating,
        # so its liveness is observable for the whole run.
        assert srv.operation_coordinator.is_active(operation_id) is True

        # Expire the transition without the deployment worker completing.
        srv.system_alignment._now = lambda: LATER

        transition = _sample_transition(base)
        assert transition["expired"] is True
        assert transition["worker_active"] is True
        assert transition["worker_status_available"] is True
        assert transition["cancel_available"] is False
        assert transition["resume_available"] is False

        # The narrow cancel never terminates a Setup-owned transition; the
        # worker-aware refusal comes from the owning abandon route. The live
        # deployment worker still owns its workflow, so the abandon is refused
        # by the Setup lifecycle before the transition is even consulted.
        status, _, body = _cancel(base, operation_id)
        assert status == 409
        assert body["error"] == "setup_abandon_required"
        status, _, body = _abandon(base)
        assert status == 409
        assert body["error"] == "setup_operation_in_progress"
        assert body["operation"] == "deployment_start"
        assert store.read().stage == "ems_operation_running"

        # The Admin process restarts: both in-memory coordinators reset while the
        # durable transition survives. The orphan holds no claim and is escapable.
        srv.operation_coordinator = OperationCoordinator()
        srv.setup_lifecycle = SetupLifecycleCoordinator()

        transition = _sample_transition(base)
        assert transition["worker_active"] is False
        assert transition["worker_status_available"] is True
        assert transition["cancel_available"] is True

        status, _, abandoned = _abandon(base)
        assert status == 200
        assert abandoned["ok"] is True
        assert abandoned["transition"]["stage"] == "cancelled"
        assert store.read().stage == "cancelled"

        # The orphaned worker later reaches its terminal callback: it must never
        # revive the cancelled transition or write known-good behind the abandon.
        deployment.finish(dashboard_reachable=True)
        assert store.read().stage == "cancelled"
        assert known_good.current() is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_expired_running_transition_cancel_blocked_while_upgrade_worker_live(tmp_path):
    srv, base, deployment, store, known_good = _serve_fresh_install(tmp_path)
    try:
        build = _build()
        record = store.begin(
            make_transition_record(
                mode="guided_upgrade",
                system_tag=build.canonical_tag,
                build_id=build.build_id,
                revision=build.revision,
                admin_image=build.admin_image,
                admin_digest=build.admin_digest,
                ems_image=build.ems_image,
                ems_digest=build.ems_digest,
                stage="ems_operation_running",
                ttl_seconds=60,
                now=T0,
            ),
            now=T0,
        )
        operation_id = record.operation_id

        release = threading.Event()
        job = UpgradeJob("upgrade-job", [])

        def runner(handle):
            release.wait(5)
            handle.finish(
                {"ok": True, "status": "succeeded", "steps": [], "warnings": []}
            )

        submitted, created = srv.upgrade_jobs.get_or_submit(
            operation_id, job, runner, coordinator=srv.operation_coordinator
        )
        assert created is True and submitted is not None
        assert _wait_until(
            lambda: srv.operation_coordinator.is_active(operation_id)
        )
        srv.system_alignment._now = lambda: LATER

        try:
            transition = _sample_transition(base)
            assert transition["expired"] is True
            assert transition["worker_active"] is True
            assert transition["cancel_available"] is False

            status, _, body = _cancel(base, operation_id)
            assert status == 409
            assert body["error"] == "transition_worker_active"
            assert store.read().stage == "ems_operation_running"
        finally:
            release.set()

        assert _wait_until(
            lambda: not srv.operation_coordinator.is_active(operation_id)
        )
        status, _, cancelled = _cancel(base, operation_id)
        assert status == 200
        assert cancelled["stage"] == "cancelled"
    finally:
        srv.shutdown()
        srv.server_close()
