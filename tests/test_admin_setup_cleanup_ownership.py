# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Setup cleanup removes only what it can prove it owns.

``SetupWorkflowArtifacts.clear()`` used to unlink the global legacy paths
unconditionally — ``<admin_data>/generated/config.json``, its sidecar and
``<admin_data>/state/.admin-deployment.json`` — whether or not those files had
anything to do with the workflow being abandoned. An install that predated
workflow ownership, a marker another owner wrote, or a malformed sidecar were all
deleted silently.

These tests pin the ownership rules instead: a workflow directory is removed only
when its normalized path *is* that workflow's own directory, a global artifact
only when its validated content names that workflow as owner, and everything else
is kept and reported as review-required.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.setup_workflow import (
    SetupWorkflowArtifacts,
    abandon_setup_workflow,
    cleanup_state_from_results,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]


class _Alignment:
    """No transition at all: cleanup semantics are what these tests observe."""

    def status(self, *, operation_active=None):
        del operation_active
        return {"ok": True, "active": False, "transition": None}

    def cancel(self, *, operation_id, coordinator=None):  # pragma: no cover
        raise AssertionError("no transition should be cancelled here")


def _admin_data(tmp_path):
    data_dir = tmp_path / "admin-data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "generated").mkdir(parents=True, exist_ok=True)
    return data_dir


def _workflow(data_dir):
    store = GuidedSetupWorkflowStore(data_dir)
    workflow_id = store.ensure_active()["workflow_id"]
    return store, workflow_id, SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)


def _scoped_generated(artifacts, *, sidecar=True):
    path = artifacts.generated_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"devices": []}\n', encoding="utf-8")
    if sidecar:
        artifacts.record_generated(
            workflow_id=artifacts.workflow_id,
            preview_id="pv-" + "0" * 16,
            draft_fingerprint="sha256:" + "0" * 64,
            base_config_revision={"expected_revision": None, "expect_absent": True},
            prepared_config_sha256="0" * 64,
        )
    return path


def _marker(data_dir, payload):
    path = data_dir / "state" / ".admin-deployment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _statuses(cleanup):
    return {entry["kind"]: entry["status"] for entry in cleanup}


# --- workflow-scoped directories ---------------------------------------------


def test_abandon_removes_only_matching_workflow_directory(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _workflow(data_dir)
    generated = _scoped_generated(artifacts)

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert not generated.exists()
    assert not artifacts.workflow_dir.exists()
    assert result["ok"] is True
    assert result["cleanup_state"] == "complete"
    assert workflow_id in str(generated)


def test_abandon_keeps_foreign_workflow_directory(tmp_path):
    """Another workflow's preserved terminal state is never collateral damage."""

    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    _scoped_generated(artifacts)
    foreign = data_dir / "workflows" / "guided-setup" / "an-other-workflow-id"
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / "generated").mkdir()
    kept = foreign / "generated" / "config.json"
    kept.write_text('{"other": true}\n', encoding="utf-8")

    abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert kept.exists()
    assert kept.read_text(encoding="utf-8") == '{"other": true}\n'


def test_abandon_refuses_a_workflow_directory_outside_its_root(tmp_path):
    data_dir = _admin_data(tmp_path)
    outside = data_dir / "generated" / "config.json"
    outside.write_text('{"legacy": true}\n', encoding="utf-8")

    artifacts = SetupWorkflowArtifacts(data_dir, workflow_id="../../generated")
    cleanup = artifacts.clear()

    assert outside.exists(), "a traversal-shaped id must not address another path"
    assert _statuses(cleanup)["workflow_directory"] == "review_required"
    reasons = {entry["kind"]: entry.get("reason") for entry in cleanup}
    assert reasons["workflow_directory"] == "setup_artifact_owner_mismatch"


def test_no_cleanup_path_can_escape_admin_data_directory(tmp_path):
    """A symlinked workflow directory is refused, target untouched."""

    data_dir = _admin_data(tmp_path)
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    (victim / "precious.json").write_text('{"keep": true}\n', encoding="utf-8")
    root = data_dir / "workflows" / "guided-setup"
    root.mkdir(parents=True, exist_ok=True)
    workflow_id = "a" * 20
    os.symlink(victim, root / workflow_id)

    artifacts = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)
    cleanup = artifacts.clear()

    assert (victim / "precious.json").exists()
    assert victim.is_dir()
    assert _statuses(cleanup)["workflow_directory"] == "review_required"


# --- legacy singleton generated config ---------------------------------------


def test_abandon_keeps_sidecarless_legacy_generated_config(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    legacy = artifacts.legacy_generated_config_path
    legacy.write_text('{"devices": []}\n', encoding="utf-8")

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert legacy.exists(), "a config that cannot prove its owner must be reviewed"
    assert result["ok"] is False
    assert result["error"] == "setup_artifact_review_required"
    assert result["status"] == 409
    assert result["cleanup_state"] == "review_required"
    statuses = _statuses(result["cleanup"])
    assert statuses["legacy_generated_config"] == "review_required"
    reasons = {entry["kind"]: entry.get("reason") for entry in result["cleanup"]}
    assert reasons["legacy_generated_config"] == "generated_config_review_required"


def test_abandon_keeps_malformed_legacy_metadata(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    legacy = artifacts.legacy_generated_config_path
    legacy.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.legacy_generated_meta_path.write_text("{not json", encoding="utf-8")

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert legacy.exists()
    assert artifacts.legacy_generated_meta_path.exists()
    statuses = _statuses(result["cleanup"])
    assert statuses["legacy_generated_config"] == "review_required"
    assert statuses["legacy_generated_metadata"] == "review_required"


def test_abandon_keeps_a_foreign_owned_legacy_generated_config(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    legacy = artifacts.legacy_generated_config_path
    legacy.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.legacy_generated_meta_path.write_text(
        json.dumps({"owner": "guided_setup", "workflow_id": "someone-else"}) + "\n",
        encoding="utf-8",
    )

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert legacy.exists()
    assert result["cleanup_state"] == "review_required"


def test_abandon_removes_a_legacy_config_this_workflow_proves_it_owns(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _workflow(data_dir)
    legacy = artifacts.legacy_generated_config_path
    legacy.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.legacy_generated_meta_path.write_text(
        json.dumps({"owner": "guided_setup", "workflow_id": workflow_id}) + "\n",
        encoding="utf-8",
    )

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert not legacy.exists()
    assert not artifacts.legacy_generated_meta_path.exists()
    assert result["ok"] is True


# --- the global deployment marker --------------------------------------------


def test_abandon_removes_matching_owned_deployment_marker(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, workflow_id, artifacts = _workflow(data_dir)
    marker = _marker(
        data_dir,
        {"release": "v0.8.0", "owner": "guided_setup", "workflow_id": workflow_id},
    )

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert not marker.exists()
    assert result["ok"] is True
    assert str(marker) in result["removed"]


@pytest.mark.parametrize(
    "payload",
    (
        {"release": "v0.8.0"},
        {"release": "v0.8.0", "workflow_id": "another-workflow"},
        {"release": "v0.8.0", "owner": "guided_upgrade", "workflow_id": "x"},
        {"release": "v0.8.0", "owner": "guided_setup", "workflow_id": None},
    ),
    ids=("legacy", "no-owner-field", "foreign-owner", "null-workflow"),
)
def test_abandon_keeps_foreign_deployment_marker(tmp_path, payload):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    marker = _marker(data_dir, payload)

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert marker.exists(), "an unproven marker owner must never be deleted"
    assert result["ok"] is False
    assert result["error"] == "setup_artifact_review_required"
    assert _statuses(result["cleanup"])["deployment_marker"] == "review_required"


def test_abandon_keeps_a_malformed_deployment_marker(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    marker = data_dir / "state" / ".admin-deployment.json"
    marker.write_text("{ truncated", encoding="utf-8")

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert marker.exists()
    assert result["cleanup_state"] == "review_required"


# --- reported cleanup state ---------------------------------------------------


def test_unknown_legacy_artifact_reports_review_required(tmp_path):
    """Review-required is distinct from a failed removal: a retry cannot fix it."""

    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    artifacts.legacy_generated_config_path.write_text("{}\n", encoding="utf-8")

    first = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())
    second = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert first["cleanup_state"] == "review_required"
    assert second["cleanup_state"] == "review_required", (
        "a retry must not convert an unknown owner into a clean state"
    )
    assert artifacts.legacy_generated_config_path.exists()


def test_a_failed_owned_removal_is_pending_not_review_required(tmp_path):
    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    _scoped_generated(artifacts)
    real_rmtree = shutil.rmtree

    def guarded(path, *args, **kwargs):
        if Path(path) == artifacts.workflow_dir:
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *args, **kwargs)

    with mock.patch.object(shutil, "rmtree", guarded):
        result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert result["cleanup_state"] == "pending"
    assert result["error"] == "abandon_cleanup_incomplete"
    assert result["status"] == 500


def test_cleanup_state_records_no_paths_and_no_os_errors():
    state = cleanup_state_from_results(
        [
            {
                "kind": "generated_config",
                "path": "/data/admin/workflows/guided-setup/w/generated/config.json",
                "status": "failed",
                "error": "Permission denied",
            },
            {"kind": "deployment_marker", "path": "/data/admin/state/x", "status": "absent"},
        ]
    )

    assert state["state"] == "pending"
    assert state["failed_count"] == 1
    assert state["review_count"] == 0
    assert state["artifacts"] == [{"kind": "generated_config", "status": "failed"}]
    serialized = json.dumps(state)
    assert "/data" not in serialized
    assert "Permission denied" not in serialized


# --- claim authority: a workflow only cleans up what it recorded --------------


_INSTALLED_MARKER = {
    "format_version": 1,
    "release": "v0.6.0-rc",
    "installed_at": "2026-06-01T10:00:00Z",
    "source": "admin_install",
}


def test_empty_workflow_ignores_preexisting_legacy_config_and_deployment_marker(
    tmp_path,
):
    """The production deadlock: an empty workflow blocked on foreign files.

    A workflow that recorded no artifact has nothing to clean up. The installed
    system's generated config and deployment marker are not its leftovers, so
    they are neither inspected nor reported as unresolved — keeping them was
    always right, blocking on them was not.
    """

    data_dir = _admin_data(tmp_path)
    store, workflow_id, artifacts = _workflow(data_dir)
    legacy = artifacts.legacy_generated_config_path
    legacy.write_text('{"devices": [{"name": "INV_1"}]}\n', encoding="utf-8")
    marker = _marker(data_dir, _INSTALLED_MARKER)
    legacy_bytes = legacy.read_bytes()
    marker_bytes = marker.read_bytes()
    assert store.load()["artifacts"] == {
        "generated_config": None,
        "generated_metadata": None,
        "generated_preview_id": None,
        "deployment_marker": None,
    }

    result = abandon_setup_workflow(
        artifacts=artifacts,
        alignment=_Alignment(),
        workflows=store,
        workflow_id=workflow_id,
    )

    assert result["ok"] is True
    assert "error" not in result
    assert result["cleanup_state"] == "complete"
    assert legacy.read_bytes() == legacy_bytes
    assert marker.read_bytes() == marker_bytes
    unresolved = {
        entry["kind"]
        for entry in result["cleanup"]
        if entry["status"] in {"review_required", "failed"}
    }
    assert unresolved == set()
    kinds = {entry["kind"] for entry in result["cleanup"]}
    assert not kinds & {
        "legacy_generated_config",
        "legacy_generated_metadata",
        "deployment_marker",
    }
    stored = store.load()
    assert stored["status"] == "abandoned"
    assert stored["cleanup"]["state"] == "complete"
    assert stored["cleanup"]["artifacts"] == []


def test_recorded_deployment_marker_is_still_reviewed_when_foreign(tmp_path):
    data_dir = _admin_data(tmp_path)
    store, workflow_id, artifacts = _workflow(data_dir)
    marker = _marker(data_dir, _INSTALLED_MARKER)
    store.record_deployment_marker(workflow_id)
    marker_bytes = marker.read_bytes()

    result = abandon_setup_workflow(
        artifacts=artifacts,
        alignment=_Alignment(),
        workflows=store,
        workflow_id=workflow_id,
    )

    assert result["cleanup_state"] == "review_required"
    assert marker.read_bytes() == marker_bytes
    assert _statuses(result["cleanup"])["deployment_marker"] == "review_required"


def test_recorded_generated_artifacts_are_still_removed(tmp_path):
    data_dir = _admin_data(tmp_path)
    store, workflow_id, artifacts = _workflow(data_dir)
    scoped = _scoped_generated(artifacts)
    store.bind_generated_artifacts(workflow_id, preview_id="pv-" + "0" * 16)

    result = abandon_setup_workflow(
        artifacts=artifacts,
        alignment=_Alignment(),
        workflows=store,
        workflow_id=workflow_id,
    )

    assert result["ok"] is True
    assert not scoped.exists()
    assert store.load()["cleanup"]["state"] == "complete"


def test_legacy_cleanup_without_a_record_stays_fail_closed(tmp_path):
    """No record means no claim evidence at all — the old rules still apply."""

    data_dir = _admin_data(tmp_path)
    _store, _workflow_id, artifacts = _workflow(data_dir)
    artifacts.legacy_generated_config_path.write_text("{}\n", encoding="utf-8")
    _marker(data_dir, _INSTALLED_MARKER)

    result = abandon_setup_workflow(artifacts=artifacts, alignment=_Alignment())

    assert result["cleanup_state"] == "review_required"
    assert artifacts.legacy_generated_config_path.exists()


def test_unclaimed_but_self_owned_artifacts_are_still_this_workflow_s(tmp_path):
    """Content that names this workflow is scope evidence, not just proof.

    The claim can be missing because the process died between writing the file
    and persisting the claim; the file is still nobody else's.
    """

    data_dir = _admin_data(tmp_path)
    store, workflow_id, artifacts = _workflow(data_dir)
    legacy = artifacts.legacy_generated_config_path
    legacy.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.legacy_generated_meta_path.write_text(
        json.dumps({"owner": "guided_setup", "workflow_id": workflow_id}) + "\n",
        encoding="utf-8",
    )
    marker = _marker(
        data_dir,
        {"release": "v0.8.0", "owner": "guided_setup", "workflow_id": workflow_id},
    )

    result = abandon_setup_workflow(
        artifacts=artifacts,
        alignment=_Alignment(),
        workflows=store,
        workflow_id=workflow_id,
    )

    assert result["ok"] is True
    assert not legacy.exists()
    assert not marker.exists()


def test_a_claim_outside_the_canonical_paths_is_never_resolved(tmp_path):
    data_dir = _admin_data(tmp_path)
    store, workflow_id, artifacts = _workflow(data_dir)
    record = store.load()
    record["artifacts"]["deployment_marker"] = "state/other-marker.json"
    store.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    marker = _marker(data_dir, _INSTALLED_MARKER)
    marker_bytes = marker.read_bytes()

    result = abandon_setup_workflow(
        artifacts=artifacts,
        alignment=_Alignment(),
        workflows=store,
        workflow_id=workflow_id,
    )

    assert result["cleanup_state"] == "review_required"
    assert marker.read_bytes() == marker_bytes
