# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend-owned Guided Setup abandonment and stale generated-config protection.

Guided Setup's "Start over" used to be a browser-only reset: it cleared
localStorage and in-memory wizard state while the durable artifacts it had
created — the generated config, the deployment marker and the pending System
Build transition — stayed on disk with no owner. These tests pin the backend
contract that makes abandonment authoritative, and the freshness check that
stops a stale generated config from silently replacing a live config another
workflow changed in the meantime.

Cleanup here is ownership-proving: the pre-workflow singleton paths and a global
deployment marker are only removed when their own content names the abandoning
workflow. Ownership-specific cases live in
``tests/test_admin_setup_cleanup_ownership.py``.

See ``docs/technical/admin-workflow-state.md`` for the full state inventory.
"""

import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from admin.admin_update import PendingTransitionStore, make_transition_record
from admin.deployment import DeploymentService
from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.setup_workflow import (
    SetupWorkflowAbandonError,
    SetupWorkflowArtifacts,
    abandon_setup_workflow,
)
from admin.system_build import SystemBuild
from tests.helpers.setup_config import adopt_generated_config

pytestmark = pytest.mark.simulation


REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


# --- fixtures / fakes ----------------------------------------------------


def _build(tag="v0.8.0"):
    return SystemBuild(
        requested_tag=tag,
        canonical_tag=tag,
        channel="stable",
        revision=REVISION,
        build_id=f"{tag}-f7265fc",
        admin_image=f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
        admin_digest="sha256:target-admin",
        ems_image=f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}",
        ems_digest="sha256:target-ems",
        release_tag=tag,
    )


def _record(operation_id, *, stage="resources_verified", mode="fresh_install"):
    build = _build()
    return make_transition_record(
        mode=mode,
        system_tag=build.canonical_tag,
        build_id=build.build_id,
        revision=build.revision,
        admin_image=build.admin_image,
        admin_digest=build.admin_digest,
        ems_image=build.ems_image,
        ems_digest=build.ems_digest,
        operation_id=operation_id,
        stage=stage,
        now=NOW,
    )


class _Alignment:
    """Minimal alignment surface: a readable status and a recording cancel."""

    def __init__(self, transition=None):
        self.transition = transition
        self.cancelled = []

    def status(self, *, operation_active=None):
        del operation_active
        if self.transition is None:
            return {"ok": True, "active": False, "transition": None}
        return {"ok": True, "active": True, "transition": dict(self.transition)}

    def cancel(self, *, operation_id, coordinator=None):
        del coordinator
        self.cancelled.append(operation_id)
        self.transition = dict(self.transition or {}, stage="cancelled")
        return {"ok": True, "stage": "cancelled", "operation_id": operation_id}


def _setup_transition(stage="resources_verified", mode="fresh_install"):
    return {"operation_id": "op-setup-1", "mode": mode, "stage": stage}


def _admin_data(tmp_path):
    data_dir = tmp_path / "admin-data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "generated").mkdir(parents=True, exist_ok=True)
    return data_dir


def _owned_workflow(data_dir):
    """An active workflow plus the artifact view scoped to it."""

    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id = store.ensure_active()["workflow_id"]
    return store, workflow_id, SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)


def _write_generated(artifacts, config=None):
    """The workflow's own generated config with its ownership sidecar."""

    path = artifacts.generated_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config or {"devices": []}, indent=2) + "\n", encoding="utf-8"
    )
    artifacts.record_generated(
        workflow_id=artifacts.workflow_id,
        preview_id="pv-" + "0" * 16,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256="0" * 64,
    )
    return path


def _write_marker(data_dir, workflow_id):
    """A deployment marker whose content proves this workflow prepared it."""

    path = data_dir / "state" / ".admin-deployment.json"
    path.write_text(
        json.dumps(
            {
                "release": "v0.8.0",
                "owner": "guided_setup",
                "workflow_id": workflow_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# --- abandonment ---------------------------------------------------------


def test_abandon_cancels_the_owning_setup_transition(tmp_path):
    data_dir = _admin_data(tmp_path)
    alignment = _Alignment(_setup_transition())

    result = abandon_setup_workflow(
        artifacts=SetupWorkflowArtifacts(data_dir), alignment=alignment
    )

    assert alignment.cancelled == ["op-setup-1"]
    assert result["ok"] is True
    assert result["transition"]["stage"] == "cancelled"


def test_abandon_removes_the_generated_config_and_deployment_marker(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    generated = _write_generated(artifacts)
    marker = _write_marker(data_dir, workflow_id)

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert not generated.exists()
    assert not marker.exists()
    assert str(generated) in result["removed"]
    assert str(marker) in result["removed"]
    assert result["cleanup_state"] == "complete"


def test_abandon_leaves_the_live_config_untouched(tmp_path):
    data_dir = _admin_data(tmp_path)
    live = tmp_path / "install" / "config" / "config.json"
    live.parent.mkdir(parents=True)
    live.write_text('{"live": true}\n', encoding="utf-8")
    _store, _workflow_id, artifacts = _owned_workflow(data_dir)
    _write_generated(artifacts)

    abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert live.read_text(encoding="utf-8") == '{"live": true}\n'


def test_abandon_is_idempotent(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    _write_generated(artifacts)
    _write_marker(data_dir, workflow_id)

    first = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())
    second = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["removed"] == []


def test_abandon_does_not_adopt_a_guided_upgrade_transition(tmp_path):
    """A Setup abandon must never touch another workflow's transition.

    A non-Setup transition is not merely left uncancelled — the abandon fails
    closed. A Setup owner that cannot prove which transition is its own also
    cannot prove which files belong to it, so it removes nothing either.
    """

    data_dir = _admin_data(tmp_path)
    generated = _write_generated(SetupWorkflowArtifacts(data_dir))
    alignment = _Alignment(
        _setup_transition(stage="ems_operation_pending", mode="guided_upgrade")
    )

    with pytest.raises(SetupWorkflowAbandonError) as exc:
        abandon_setup_workflow(
            artifacts=SetupWorkflowArtifacts(data_dir), alignment=alignment
        )

    assert exc.value.code == "setup_transition_owner_unproven"
    assert alignment.cancelled == []
    assert generated.exists()


def test_abandon_reports_the_resulting_authoritative_state(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _owned_workflow(data_dir)
    _write_generated(artifacts)

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert result["generated_config"]["exists"] is False
    assert result["deployment_marker"]["exists"] is False


def test_abandon_clears_a_failed_recoverable_setup_transition(tmp_path):
    """Recovery stays reachable: a broken transition is still abandonable."""

    data_dir = _admin_data(tmp_path)
    alignment = _Alignment(_setup_transition(stage="failed_recoverable"))

    result = abandon_setup_workflow(
        artifacts=SetupWorkflowArtifacts(data_dir), alignment=alignment
    )

    assert alignment.cancelled == ["op-setup-1"]
    assert result["ok"] is True


# --- stale generated-config protection ------------------------------------
# Ownership and base-revision semantics for workflow-owned generated configs
# live in tests/test_admin_setup_artifact_ownership.py. This file keeps the
# presence-matrix cases below on top of the same owned-artifact provisioning.


def _deployment_service(tmp_path, data_dir, *, workspace):
    releases_dir = data_dir / "releases"
    (releases_dir / "v0.8.0").mkdir(parents=True, exist_ok=True)
    (releases_dir / "v0.8.0" / "install-docker.sh").write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )
    manager = SimpleNamespace(
        releases_dir=releases_dir,
        data_dir=data_dir,
        config_template=lambda: {
            "tag": "v0.8.0",
            "template": {},
            "docker_image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.0",
        },
    )
    store = GuidedSetupWorkflowStore(data_dir)
    record = store.ensure_active()
    return DeploymentService(
        manager,
        SimpleNamespace(
            target_path=store.generated_config_path(record["workflow_id"])
        ),
        workspace_dir=workspace,
        admin_data_dir=data_dir,
        docker=SimpleNamespace(probe=lambda: None),
        runtime_env={"PUID": "1000", "PGID": "1000"},
        setup_workflows=store,
    )


def _owned_generated(service, config=None):
    """Write generated bytes into the active workflow and adopt them."""

    target = Path(service.config_export.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config or {"devices": []}, indent=2) + "\n", encoding="utf-8"
    )
    adopt_generated_config(service)
    return target


def _live_config(workspace, payload):
    path = workspace / "config" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


# --- restart consistency --------------------------------------------------


def test_admin_restart_preserves_one_workflow_interpretation(tmp_path):
    """A second store over the same state dir reads back the same transition."""

    state_dir = _admin_data(tmp_path) / "state"
    PendingTransitionStore(state_dir).begin(_record("op-restart-1"), now=NOW)

    restarted = PendingTransitionStore(state_dir).read()

    assert restarted is not None
    assert restarted.operation_id == "op-restart-1"
    assert restarted.stage == "resources_verified"
    assert restarted.mode == "fresh_install"


def test_abandoned_artifacts_stay_gone_across_an_admin_restart(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    generated = _write_generated(artifacts)
    marker = _write_marker(data_dir, workflow_id)

    abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())
    # A restart builds fresh stores over the same directory.
    restarted = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)

    assert not generated.exists()
    assert not marker.exists()
    assert restarted.state()["generated_config"]["exists"] is False


# --- an abandoned Setup must not keep blocking Maintenance ----------------


class _StoreBackedAlignment:
    """Alignment surface over the real durable store.

    ``_reject_unrelated_transition_write`` blocks Maintenance config apply on
    ``is_transition_pending()``, which is true for every non-terminal record.
    Abandonment must therefore leave the persisted record terminal.
    """

    def __init__(self, store):
        self._store = store

    def status(self, *, operation_active=None):
        del operation_active
        record = self._store.read()
        if record is None:
            return {"ok": True, "active": False, "transition": None}
        return {
            "ok": True,
            "active": record.stage not in {"completed", "cancelled"},
            "transition": {
                "operation_id": record.operation_id,
                "mode": record.mode,
                "stage": record.stage,
            },
        }

    def cancel(self, *, operation_id, coordinator=None):
        del coordinator
        record = self._store.cancel(operation_id=operation_id, now=NOW)
        return {"ok": True, "stage": record.stage, "operation_id": operation_id}

    def is_transition_pending(self):
        record = self._store.read()
        return bool(record is not None and record.stage not in {"completed", "cancelled"})


def test_abandon_unblocks_maintenance_config_writes(tmp_path):
    data_dir = _admin_data(tmp_path)
    store = PendingTransitionStore(data_dir / "state")
    store.begin(_record("op-blocking-1"), now=NOW)
    alignment = _StoreBackedAlignment(store)
    assert alignment.is_transition_pending() is True

    abandon_setup_workflow(
        artifacts=SetupWorkflowArtifacts(data_dir), alignment=alignment
    )

    assert alignment.is_transition_pending() is False
    assert store.read().stage == "cancelled"


def test_abandon_fails_closed_when_the_transition_state_is_unreadable(tmp_path):
    """An unknown owner must not lead to a half-cleared workflow."""

    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _owned_workflow(data_dir)
    generated = _write_generated(artifacts)

    with pytest.raises(SetupWorkflowAbandonError) as excinfo:
        abandon_setup_workflow(
            artifacts=artifacts,
            alignment=_Alignment(),
            status={"ok": False, "active": True, "transition": None},
        )

    assert excinfo.value.code == "transition_status_unavailable"
    assert generated.exists()


# --- revision states: presence and absence both count --------------------


def _workspace_untouched(workspace):
    """No deployment artifact may appear when prepare is rejected."""

    return not (workspace / "docker-compose.yml").exists() and not (
        workspace / ".env"
    ).exists()


def test_prepare_rejects_when_the_live_config_was_deleted(tmp_path):
    """Deletion is a change: an existing base that vanished invalidates the draft."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    live = _live_config(workspace, '{"live": "original"}\n')
    service = _deployment_service(tmp_path, data_dir, workspace=workspace)
    _owned_generated(service)
    live.unlink()

    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "stale_generated_config"
    assert result["status"] == 409
    assert not live.exists()
    assert _workspace_untouched(workspace)


def test_prepare_rejects_a_foreign_live_config_after_a_fresh_install_draft(tmp_path):
    """A config that appeared after a fresh-install draft is a conflict."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    service = _deployment_service(tmp_path, data_dir, workspace=workspace)
    _owned_generated(service)
    live = _live_config(workspace, '{"live": "appeared-elsewhere"}\n')

    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "stale_generated_config"
    assert live.read_text(encoding="utf-8") == '{"live": "appeared-elsewhere"}\n'
    assert _workspace_untouched(workspace)


def test_prepare_allows_a_fresh_install_whose_generated_bytes_are_already_live(
    tmp_path,
):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    service = _deployment_service(tmp_path, data_dir, workspace=workspace)
    generated = _owned_generated(service)
    _live_config(workspace, generated.read_text(encoding="utf-8"))

    assert service.prepare(overwrite=True).get("reason") != "stale_generated_config"


def test_prepare_allows_an_existing_base_relaunch_of_its_own_deployed_config(tmp_path):
    """base=<sha>, live now equals the generated bytes: a redeploy, not a conflict."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    live = _live_config(workspace, '{"live": "original"}\n')
    service = _deployment_service(tmp_path, data_dir, workspace=workspace)
    generated = _owned_generated(service)
    live.write_text(generated.read_text(encoding="utf-8"), encoding="utf-8")

    assert service.prepare(overwrite=True).get("reason") != "stale_generated_config"


def test_prepare_allows_a_missing_live_config_for_a_fresh_install_base(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    service = _deployment_service(tmp_path, data_dir, workspace=workspace)
    _owned_generated(service)

    assert service.prepare(overwrite=True).get("reason") != "stale_generated_config"


def test_prepare_without_metadata_requires_regeneration(tmp_path):
    """A sidecar-less legacy artifact must be reviewed again, never deployed."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    service = _deployment_service(tmp_path, data_dir, workspace=workspace)
    target = Path(service.config_export.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"devices": []}) + "\n", encoding="utf-8")

    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"
    assert _workspace_untouched(workspace)


# --- cleanup failure semantics -------------------------------------------


@contextmanager
def _rmtree_fails_for(target):
    """Make exactly one workflow directory's removal fail, leaving others real."""

    real_rmtree = shutil.rmtree

    def guarded(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *args, **kwargs)

    with mock.patch.object(shutil, "rmtree", guarded):
        yield


class _RefusingAlignment(_Alignment):
    def cancel(self, *, operation_id, coordinator=None):
        raise RuntimeError("a worker is still active")


def test_a_refused_cancellation_removes_no_artifact(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    generated = _write_generated(artifacts)
    marker = _write_marker(data_dir, workflow_id)

    with pytest.raises(RuntimeError):
        abandon_setup_workflow(
            artifacts=artifacts, alignment=_RefusingAlignment(_setup_transition())
        )

    assert generated.exists()
    assert marker.exists()
    assert artifacts.generated_meta_path.exists()


def test_cleanup_attempts_every_artifact_after_one_removal_failure(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    generated = _write_generated(artifacts)
    marker = _write_marker(data_dir, workflow_id)

    with _rmtree_fails_for(artifacts.workflow_dir):
        result = abandon_setup_workflow(
            artifacts=artifacts, alignment=_Alignment(_setup_transition())
        )

    # The failure must not short-circuit the remaining removals.
    assert generated.exists()
    assert not marker.exists()
    assert result["ok"] is False
    assert result["error"] == "abandon_cleanup_incomplete"
    assert result["cleanup_state"] == "pending"
    statuses = {entry["path"]: entry["status"] for entry in result["cleanup"]}
    assert statuses[str(generated)] == "failed"
    assert statuses[str(marker)] == "removed"
    # The authoritative state is reported truthfully, not optimistically.
    assert result["generated_config"]["exists"] is True
    assert result["deployment_marker"]["exists"] is False
    assert result["transition"]["stage"] == "cancelled"


def test_partial_cleanup_leaves_the_live_config_untouched(tmp_path):
    data_dir = _admin_data(tmp_path)
    live = tmp_path / "install" / "config" / "config.json"
    live.parent.mkdir(parents=True)
    live.write_text('{"live": true}\n', encoding="utf-8")
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    _write_generated(artifacts)
    _write_marker(data_dir, workflow_id)

    with _rmtree_fails_for(artifacts.workflow_dir):
        abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert live.read_text(encoding="utf-8") == '{"live": true}\n'


def test_retry_after_a_partial_cleanup_converges_to_a_clean_state(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _owned_workflow(data_dir)
    generated = _write_generated(artifacts)
    _write_marker(data_dir, workflow_id)

    with _rmtree_fails_for(artifacts.workflow_dir):
        first = abandon_setup_workflow(
            artifacts=artifacts, alignment=_Alignment(_setup_transition())
        )
    assert first["ok"] is False

    second = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert second["ok"] is True
    assert "error" not in second
    assert not generated.exists()
    assert second["generated_config"]["exists"] is False
    assert second["deployment_marker"]["exists"] is False
