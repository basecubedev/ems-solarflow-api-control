# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP contract of the InfluxDB upgrade: confirmation, binding, one job at a time."""

import threading
import time

import pytest

from admin import container_actions
from admin.server import create_server
from tests.admin_auth_helpers import authenticate
from tests.admin_auth_helpers import request as _request
from tests.helpers.system_alignment import SetupReadySystemAlignment

pytestmark = [pytest.mark.contract, pytest.mark.admin, pytest.mark.maintenance]

OLD = "influxdb:2.7"
NEW = "influxdb:2.9.1"
PLAN = "/api/admin/maintenance/influxdb"
UPGRADE = "/api/admin/maintenance/influxdb/upgrade"
ROLLBACK = "/api/admin/maintenance/influxdb/rollback"


class FakeService:
    def __init__(self):
        self.state = "available"
        self.waiting = False
        self.recheck_result = {"ok": True, "status": "completed", "message": "checked"}
        self.hold_recheck = False
        self.rollback_view = {"available": True, "to_image": OLD, "backup": "b.tar.gz", "reason": None}
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []

    def plan(self):
        return {"ok": True, "state": self.state, "message": "no" if self.state != "available" else None,
                "target_image": NEW, "rollback": self.rollback_view}

    def _run(self, kind, image, progress):
        self.calls.append((kind, image))
        self.started.set()
        assert self.release.wait(10)
        progress.record_step({"key": "preflight", "status": "ok", "label": "Check", "detail": None})
        return {"ok": True, "status": "completed", "message": f"{kind} done", "steps": []}

    def upgrade(self, image, progress=None):
        return self._run("upgrade", image, progress)

    def stop_waiting(self):
        self.calls.append(("stop_waiting",))
        return self.waiting

    def recheck(self):
        self.calls.append(("recheck",))
        if self.hold_recheck:
            self.started.set()
            assert self.release.wait(10)
        return self.recheck_result

    def rollback(self, image, progress=None):
        return self._run("rollback", image, progress)


def request(url, method="GET", body=None):
    status, _headers, payload = _request(url, method=method, body=body)
    return status, payload


@pytest.fixture
def served(isolated_install_root):
    service = FakeService()
    srv = create_server("127.0.0.1", 0, system_alignment=SetupReadySystemAlignment(), influx_upgrade=service)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield service, base
    finally:
        service.release.set()
        srv.shutdown()
        srv.server_close()


def _wait_for_job(base, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, payload = request(f"{base}/api/admin/maintenance/influxdb/jobs/{job_id}")
        assert status == 200
        if payload["status"] != "running":
            return payload
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_the_plan_is_read_through_the_service(served):
    _service, base = served

    status, payload = request(f"{base}{PLAN}")

    assert status == 200 and payload["state"] == "available" and payload["target_image"] == NEW


@pytest.mark.parametrize(
    ("path", "body", "fragment"),
    [
        (UPGRADE, {}, "confirmation is required"),
        (UPGRADE, {"confirm": True}, "target_image is required"),
        (UPGRADE, {"confirm": True, "target_image": NEW, "path": "/x"}, "unsupported"),
        (UPGRADE, {"confirm": "yes", "target_image": NEW}, "confirmation is required"),
        (ROLLBACK, {"confirm": True}, "image is required"),
        (ROLLBACK, {"confirm": True, "target_image": OLD}, "unsupported"),
    ],
)
def test_a_start_needs_an_explicit_confirmation_of_one_image(served, path, body, fragment):
    service, base = served

    status, payload = request(f"{base}{path}", method="POST", body=body)

    assert status == 400 and fragment in payload["error"]
    assert service.calls == []


def test_a_confirmation_of_another_image_than_the_plan_offers_is_refused(served):
    service, base = served

    upgrade = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": "influxdb:2.10.1"})
    rollback = request(f"{base}{ROLLBACK}", method="POST", body={"confirm": True, "image": NEW})

    assert upgrade[0] == 409 and "changed" in upgrade[1]["message"]
    assert rollback[0] == 409 and "changed" in rollback[1]["message"]
    assert service.calls == []


def test_a_plan_that_offers_nothing_refuses_the_start(served):
    service, base = served
    service.state = "current"
    service.rollback_view = {"available": False, "to_image": OLD, "reason": "The backup is gone."}

    upgrade = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    rollback = request(f"{base}{ROLLBACK}", method="POST", body={"confirm": True, "image": OLD})

    assert upgrade[0] == 409 and rollback == (409, {"ok": False, "status": "refused", "message": "The backup is gone."})
    assert service.calls == []


@pytest.fixture
def served_server(isolated_install_root):
    service = FakeService()
    srv = create_server("127.0.0.1", 0, system_alignment=SetupReadySystemAlignment(), influx_upgrade=service)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield service, base, srv
    finally:
        service.release.set()
        srv.shutdown()
        srv.server_close()


def test_one_influxdb_job_holds_off_every_container_action_and_a_restore(served_server):
    """Interleaving: a job is running (held on an Event) when a second start, a
    recheck, a container sync, a restore's lock claim and the writers that recreate
    containers or the Admin arrive; all are refused, and once the job ends the
    locks are free again."""

    service, base, srv = served_server

    status, started = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    assert status == 202 and started["status"] == "running"
    assert [step["key"] for step in started["steps"]][:2] == ["preflight", "inventory"]
    assert service.started.wait(5)

    second = request(f"{base}{ROLLBACK}", method="POST", body={"confirm": True, "image": OLD})
    sync = request(f"{base}/api/admin/maintenance/containers/sync", method="POST", body={"confirm": True})
    recheck = request(f"{base}/api/admin/maintenance/influxdb/recheck", method="POST", body={})

    assert second[0] == 409 and second[1]["status"] == "busy"
    assert recheck[0] == 409 and recheck[1]["status"] == "busy" and ("recheck",) not in service.calls
    assert sync[0] == 409 and sync[1]["status"] == "busy"
    assert container_actions.claim_container_actions() is None
    assert srv.backup_service.restore_lock.acquire(blocking=False) is False
    for path in (
        "/api/admin/maintenance/upgrade/execute",
        "/api/admin/maintenance/admin-update/execute",
        "/api/admin/system-alignment/resume",
        "/api/admin/system-alignment/return-to-running-build",
        "/api/setup/deployment/prepare",
        "/api/setup/deployment/start",
        "/api/setup/system-build/update-admin",
    ):
        refused = request(f"{base}{path}", method="POST", body={})
        assert refused[0] == 409 and refused[1]["error"] == "influx_upgrade_running", path

    service.release.set()
    final = _wait_for_job(base, started["job_id"])

    assert final["status"] == "succeeded" and final["result"]["message"] == "upgrade done"
    assert service.calls == [("upgrade", NEW)]
    release = container_actions.claim_container_actions()
    assert release is not None
    release()
    assert srv.backup_service.restore_lock.acquire(blocking=False) is True
    srv.backup_service.restore_lock.release()
    assert getattr(service, "active", None) is None


def test_a_running_restore_holds_off_an_influxdb_job(served_server):
    service, base, srv = served_server
    assert srv.backup_service.restore_lock.acquire(blocking=False)
    try:
        status, payload = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    finally:
        srv.backup_service.restore_lock.release()

    assert status == 409 and payload["status"] == "busy"
    assert service.calls == []
    release = container_actions.claim_container_actions()
    assert release is not None, "a refused start must not keep the container-action lock"
    release()


def test_a_guided_upgrade_or_setup_worker_holds_off_an_influxdb_job(served_server):
    service, base, srv = served_server
    token = srv.operation_coordinator.claim("op-e2e-upgrade")
    try:
        status, payload = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    finally:
        srv.operation_coordinator.release(token)

    assert status == 409 and payload["status"] == "busy"
    assert service.calls == []


def test_a_job_that_raises_still_frees_the_locks(served):
    service, base = served

    def boom(image, progress=None):
        raise RuntimeError("boom")

    service.upgrade = boom

    status, started = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    final = _wait_for_job(base, started["job_id"])

    assert status == 202 and final["status"] == "failed"
    assert "unexpectedly" in final["error"]["message"] and "boom" not in str(final)
    release = container_actions.claim_container_actions()
    assert release is not None
    release()


def test_an_unknown_job_is_not_found(served):
    _service, base = served

    status, payload = request(f"{base}/api/admin/maintenance/influxdb/jobs/nope")

    assert status == 404


def test_a_container_writer_between_its_checks_and_its_claim_holds_off_an_influxdb_job(served_server):
    """Interleaving: a Guided Upgrade has passed the gate and not yet saved its
    transition (it is inside the gate's count) when the InfluxDB start arrives."""

    service, base, srv = served_server
    with srv.container_write_gate.lock:
        srv.container_write_gate.entered += 1
    try:
        status, payload = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    finally:
        with srv.container_write_gate.lock:
            srv.container_write_gate.entered -= 1

    assert status == 409 and payload["status"] == "busy" and service.calls == []


def test_an_admin_update_in_progress_holds_off_an_influxdb_job(served_server, monkeypatch):
    service, base, srv = served_server
    monkeypatch.setattr(srv.admin_update, "update_in_progress", lambda: True, raising=False)

    status, payload = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})

    assert status == 409 and payload["status"] == "busy" and service.calls == []


def test_a_refused_container_writer_leaves_the_gate_count_alone(served_server):
    service, base, srv = served_server
    request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    assert service.started.wait(5)

    refused = request(f"{base}/api/admin/maintenance/upgrade/execute", method="POST", body={})
    service.release.set()

    assert refused[0] == 409
    assert srv.container_write_gate.entered == 0


@pytest.mark.parametrize(
    "path",
    ["/api/admin/maintenance/upgrade/execute", "/api/admin/maintenance/admin-update/execute", "/api/setup/deployment/start"],
)
def test_an_admitted_container_writer_gives_its_place_back(served_server, path):
    """Whatever the writer answers, it leaves the gate as it found it; an InfluxDB job can start after it."""

    service, base, srv = served_server

    request(f"{base}{path}", method="POST", body={})

    assert srv.container_write_gate.wait_until_empty(10), "an admitted writer kept its place in the gate"
    status, started = request(f"{base}{UPGRADE}", method="POST", body={"confirm": True, "target_image": NEW})
    assert status == 202
    service.release.set()
    assert _wait_for_job(base, started["job_id"])["status"] == "succeeded"


def test_stopping_a_wait_needs_a_confirmation_and_a_wait_to_stop(served):
    service, base = served
    stop = f"{base}/api/admin/maintenance/influxdb/stop-waiting"

    assert request(stop, "POST", {})[0] == 400
    assert request(stop, "POST", {"confirm": True, "extra": 1})[0] == 400
    status, payload = request(stop, "POST", {"confirm": True})
    assert status == 409 and payload["status"] == "not_waiting"
    service.waiting = True
    status, payload = request(stop, "POST", {"confirm": True})
    assert status == 202 and payload == {"ok": True, "status": "stopping"}
    assert service.calls.count(("stop_waiting",)) == 2


def test_a_recheck_takes_an_empty_request_and_answers_with_the_service_verdict(served):
    service, base = served
    recheck = f"{base}/api/admin/maintenance/influxdb/recheck"

    assert request(recheck, "POST", {"confirm": True})[0] == 400
    status, payload = request(recheck, "POST", {})
    assert status == 200 and payload["ok"] is True
    service.recheck_result = {"ok": False, "status": "failed", "message": "InfluxDB does not answer yet; check again later."}
    status, payload = request(recheck, "POST", {})
    assert status == 409 and payload["status"] == "failed"
    assert service.calls.count(("recheck",)) == 2


def test_a_running_recheck_holds_off_container_actions_and_a_restore(served_server):
    """Interleaving: a recheck is held inside the service on an Event when a container
    sync and a restore's lock claim arrive; both are refused, and once the recheck
    answers the locks are free again."""

    service, base, srv = served_server
    service.hold_recheck = True
    answers = []
    worker = threading.Thread(
        target=lambda: answers.append(request(f"{base}/api/admin/maintenance/influxdb/recheck", method="POST", body={}))
    )
    worker.start()
    assert service.started.wait(5)

    sync = request(f"{base}/api/admin/maintenance/containers/sync", method="POST", body={"confirm": True})
    restore_free = srv.backup_service.restore_lock.acquire(blocking=False)

    service.release.set()
    worker.join(10)
    assert sync[0] == 409 and sync[1]["status"] == "busy"
    assert restore_free is False
    assert answers[0][0] == 200
    release = container_actions.claim_container_actions()
    assert release is not None
    release()
    assert srv.backup_service.restore_lock.acquire(blocking=False) is True
    srv.backup_service.restore_lock.release()
