# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Guided Setup mutation and its termination can never both win.

Archive 99 made every Setup mutation present the active workflow and its exact
preview, but authority was verified once and never held: an Apply that had
passed verification kept committing while a concurrent Abandon marked the same
workflow terminal and removed its artifacts, and an ``/api/setup/abandon`` with
no ID at all was read as permission to discard whatever workflow happened to be
stored. These tests pin the lifecycle contract instead: a destructive Setup
action names its workflow exactly, and mutation and terminal operations are
mutually exclusive claims on that workflow.

Synchronization is explicit (``threading.Event``) — never a sleep.

See ``docs/technical/admin-workflow-state.md``.
"""

import threading

import pytest

from tests.test_admin_server import (
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_preview_authority import (
    _broker_body,
    _draft_a,
    _install_tree_snapshot,
    _live_path,
    _preview,
    _start_workflow,
    _write_live,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _abandon(base, body):
    return _request(f"{base}/api/setup/abandon", method="POST", body=body)


class _PausedPrepare:
    """Block ``ConfigExportService.prepare`` exactly at the pre-commit point."""

    def __init__(self, service):
        self._service = service
        self._real = service.prepare
        self.entered = threading.Event()
        self.release = threading.Event()

    def install(self):
        self._service.prepare = self
        return self

    def __call__(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(20), "the paused apply was never released"
        return self._real(*args, **kwargs)


class _Mutation(threading.Thread):
    """Run one Setup mutation request off-thread and keep its outcome."""

    def __init__(self, base, path, body, extra_headers=None):
        super().__init__(daemon=True)
        self._base = base
        self._path = path
        self._body = body
        self._extra_headers = extra_headers
        self.status = None
        self.payload = None

    def run(self):
        self.status, _, self.payload = _request(
            f"{self._base}{self._path}",
            method="POST",
            body=self._body,
            extra_headers=self._extra_headers,
        )


# --- an unnamed abandon is not authority --------------------------------------


def test_abandon_requires_workflow_id_when_record_exists(tmp_path):
    """A stored workflow record makes the ID mandatory, not optional."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)

        status, _, payload = _abandon(base, {})

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_required"
        status, _, workflow = _request(f"{base}/api/setup/workflow")
        assert (workflow.get("workflow") or {})["workflow_id"] == workflow_id
        assert (workflow.get("workflow") or {})["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_empty_abandon_cannot_discard_newer_workflow(tmp_path):
    """The exact reproduction: A is abandoned, B starts, an empty abandon must
    not adopt B."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        first = _start_workflow(base, srv)
        status, _, payload = _abandon(base, {"setup_workflow_id": first})
        assert status == 200 and payload["ok"] is True, payload

        second = _start_workflow(base)
        assert second != first
        preview_id = _preview(base, second, _draft_a())["config_preview_id"]

        status, _, payload = _abandon(base, {})

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_required"
        # B keeps full mutation authority: nothing about it was cancelled.
        status, _, applied = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body={
                **_draft_a(),
                "setup_workflow_id": second,
                "config_preview_id": preview_id,
            },
        )
        assert status == 200, applied
    finally:
        srv.shutdown()
        srv.server_close()


# --- mutation and termination are mutually exclusive --------------------------


@pytest.mark.parametrize(
    "path", ["/api/setup/config/apply", "/api/setup/config/write"]
)
def test_mutation_and_abandon_are_mutually_exclusive(tmp_path, path):
    """Apply/Write owns the workflow across its whole commit; a concurrent
    Abandon is refused instead of terminalizing underneath it."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        paused = _PausedPrepare(srv.config_export).install()

        mutation = _Mutation(
            base,
            path,
            {
                **_draft_a(),
                "setup_workflow_id": workflow_id,
                "config_preview_id": preview_id,
            },
        )
        mutation.start()
        assert paused.entered.wait(20), "the mutation never reached prepare"

        status, _, payload = _abandon(base, {"setup_workflow_id": workflow_id})

        assert status == 409, payload
        assert payload["error"] == "setup_operation_in_progress"
        assert "draft" not in payload and "cleanup" not in payload

        paused.release.set()
        mutation.join(20)
        assert mutation.status == 200, mutation.payload
        assert mutation.payload["ok"] is True
        status, _, workflow = _request(f"{base}/api/setup/workflow")
        assert (workflow.get("workflow") or {})["status"] == "active"
    finally:
        paused.release.set()
        srv.shutdown()
        srv.server_close()


def test_supersede_cannot_terminalize_running_apply(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        paused = _PausedPrepare(srv.config_export).install()

        mutation = _Mutation(
            base,
            "/api/setup/config/apply",
            {
                **_draft_a(),
                "setup_workflow_id": workflow_id,
                "config_preview_id": preview_id,
            },
        )
        mutation.start()
        assert paused.entered.wait(20)

        status, _, payload = _request(
            f"{base}/api/setup/system-build/supersede",
            method="POST",
            body={"setup_workflow_id": workflow_id, "tag": "v0.8.0"},
        )

        assert status == 409, payload
        assert payload["error"] == "setup_operation_in_progress"
        assert "setup_workflow_id" not in payload, (
            "a refused supersede must not issue a replacement workflow"
        )

        paused.release.set()
        mutation.join(20)
        assert mutation.status == 200, mutation.payload
    finally:
        paused.release.set()
        srv.shutdown()
        srv.server_close()


# --- a terminal claim is a barrier for later mutations ------------------------


def test_apply_cannot_commit_after_abandon_claim(tmp_path):
    """Abandon owning the workflow first is terminal: a later Apply is refused
    and the live config is byte-exact unchanged."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    live = _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]

        with srv.setup_lifecycle.claim_termination(
            workflow_id=workflow_id, operation="abandon"
        ):
            status, _, payload = _request(
                f"{base}/api/setup/config/apply",
                method="POST",
                body={
                    **_draft_a(),
                    "setup_workflow_id": workflow_id,
                    "config_preview_id": preview_id,
                },
            )

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert live.read_text(encoding="utf-8") == '{"live": "A"}\n'
    finally:
        srv.shutdown()
        srv.server_close()


def test_credential_staging_does_not_escape_terminal_barrier(tmp_path):
    """No secret may be staged for a workflow that terminalization already owns."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        body = _broker_body("s3cret-broker-password")
        preview_id = _preview(base, workflow_id, body)["config_preview_id"]
        before = _install_tree_snapshot()

        with srv.setup_lifecycle.claim_termination(
            workflow_id=workflow_id, operation="abandon"
        ):
            status, _, payload = _request(
                f"{base}/api/setup/config/apply",
                method="POST",
                body={
                    **body,
                    "setup_workflow_id": workflow_id,
                    "config_preview_id": preview_id,
                },
            )

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert _install_tree_snapshot() == before, (
            "a refused mutation must not stage a credential or touch the store"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_two_mutations_for_one_workflow_do_not_overlap(tmp_path):
    """The claim is exclusive for the workflow, not just against terminations."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        paused = _PausedPrepare(srv.config_export).install()
        body = {
            **_draft_a(),
            "setup_workflow_id": workflow_id,
            "config_preview_id": preview_id,
        }

        first = _Mutation(base, "/api/setup/config/apply", body)
        first.start()
        assert paused.entered.wait(20)

        status, _, payload = _request(
            f"{base}/api/setup/config/apply", method="POST", body=body
        )

        assert status == 409, payload
        assert payload["error"] == "setup_operation_in_progress"

        paused.release.set()
        first.join(20)
        assert first.status == 200, first.payload
        assert _live_path().read_text(encoding="utf-8") != '{"live": "A"}\n'
    finally:
        paused.release.set()
        srv.shutdown()
        srv.server_close()
