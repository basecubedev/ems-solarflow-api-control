# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generated Setup configs deploy only under their owning workflow.

The generated config, its metadata sidecar and the deployment marker used to be
process-global singletons: any authenticated caller, an old tab or a superseded
Setup attempt could address them, and a generated config written before the
metadata sidecar existed was deployed without any ownership or base-revision
proof. These tests pin workflow-owned artifacts: deployment accepts only the
active workflow's generated config, bound to its exact preview, hash and base
revision — and a legacy sidecar-less artifact requires regeneration instead of
silently keeping the unsafe path.

See ``docs/technical/admin-workflow-state.md``.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.deployment import DeploymentService

pytestmark = pytest.mark.simulation


def _admin_data(tmp_path):
    data_dir = tmp_path / "admin-data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    return data_dir


def _deployment_service(data_dir, *, workspace, target_path, workflows):
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
    return DeploymentService(
        manager,
        SimpleNamespace(target_path=target_path),
        workspace_dir=workspace,
        admin_data_dir=data_dir,
        docker=SimpleNamespace(probe=lambda: None),
        runtime_env={"PUID": "1000", "PGID": "1000"},
        setup_workflows=workflows,
    )


def _payload():
    return (json.dumps({"devices": []}, indent=2) + "\n").encode("utf-8")


def _owned_generated_config(data_dir, *, payload=None):
    """An active workflow with a preview-bound generated config on disk."""

    from admin.guided_setup_workflow import GuidedSetupWorkflowStore
    from admin.setup_workflow import SetupWorkflowArtifacts

    payload = payload if payload is not None else _payload()
    store = GuidedSetupWorkflowStore(data_dir)
    record = store.ensure_active()
    workflow_id = record["workflow_id"]
    record = store.record_preview(
        workflow_id,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256=hashlib.sha256(payload).hexdigest(),
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
        prepared_config_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return store, workflow_id, target


# --- legacy artifacts require review -----------------------------------------


def test_prepare_rejects_a_sidecar_less_legacy_generated_config(tmp_path):
    """A generated config that cannot prove its owner and base revision must be
    regenerated, not silently deployed."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    legacy = data_dir / "generated" / "config.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(_payload())

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=legacy, workflows=None
    )
    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"
    assert result["status"] == 409
    assert "job" not in result
    assert not (workspace / "config" / "config.json").exists(), (
        "no live workspace write may occur before the rejection"
    )


def test_prepare_rejects_metadata_without_workflow_identity(tmp_path):
    """The Archive-98 sidecar (owner + base revision, no workflow) is no longer
    enough to deploy: it cannot prove which workflow reviewed it."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    legacy = data_dir / "generated" / "config.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(_payload())
    (legacy.parent / "config.meta.json").write_text(
        json.dumps(
            {
                "owner": "guided_setup",
                "base_config_revision": None,
                "recorded_at": "2026-07-29T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=legacy, workflows=None
    )
    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"
    assert not (workspace / "config" / "config.json").exists()


# --- workflow-owned artifacts ------------------------------------------------


def test_prepare_accepts_the_active_workflows_generated_config(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store, _workflow_id, target = _owned_generated_config(data_dir)

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=target, workflows=store
    )
    result = service.prepare(overwrite=True)

    assert result.get("reason") is None, result
    assert result["ok"] is True


def test_a_later_preview_does_not_disown_the_generated_artifact(tmp_path):
    """Revisiting Config Preview issues newer previews; the artifact written
    earlier in the same workflow must stay deployable."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store, workflow_id, target = _owned_generated_config(data_dir)
    store.record_preview(
        workflow_id,
        draft_fingerprint="sha256:" + "1" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256=hashlib.sha256(b"a later draft").hexdigest(),
    )

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=target, workflows=store
    )
    result = service.prepare(overwrite=True)

    assert result.get("reason") is None, result
    assert result["ok"] is True


def test_an_abandoned_workflows_artifact_cannot_deploy(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store, workflow_id, target = _owned_generated_config(data_dir)
    store.finish(workflow_id, status="abandoned")

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=target, workflows=store
    )
    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"
    assert not (workspace / "config" / "config.json").exists()


def test_another_workflows_artifact_cannot_deploy(tmp_path):
    """Workflow A's generated config must not deploy under workflow B."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store, workflow_id, target = _owned_generated_config(data_dir)
    store.finish(workflow_id, status="superseded")
    replacement = store.ensure_active()
    assert replacement["workflow_id"] != workflow_id

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=target, workflows=store
    )
    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"


def test_tampered_generated_bytes_cannot_deploy(tmp_path):
    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    store, _workflow_id, target = _owned_generated_config(data_dir)
    target.write_bytes(
        (json.dumps({"devices": [{"injected": True}]}, indent=2) + "\n").encode(
            "utf-8"
        )
    )

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=target, workflows=store
    )
    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "generated_config_review_required"


def test_a_changed_live_base_still_rejects_an_owned_artifact(tmp_path):
    """Workflow ownership does not replace the base-revision freshness check."""

    data_dir = _admin_data(tmp_path)
    workspace = tmp_path / "install"
    live = workspace / "config" / "config.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text('{"live": "original"}\n', encoding="utf-8")
    payload = _payload()

    from admin.guided_setup_workflow import GuidedSetupWorkflowStore
    from admin.setup_workflow import SetupWorkflowArtifacts

    store = GuidedSetupWorkflowStore(data_dir)
    record = store.ensure_active()
    workflow_id = record["workflow_id"]
    base = {
        "expected_revision": hashlib.sha256(live.read_bytes()).hexdigest(),
        "expect_absent": False,
    }
    record = store.record_preview(
        workflow_id,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision=base,
        prepared_config_sha256=hashlib.sha256(payload).hexdigest(),
    )
    store.bind_generated_artifacts(
        workflow_id, preview_id=record["preview"]["preview_id"]
    )
    artifacts = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)
    target = artifacts.generated_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    artifacts.record_generated(
        workflow_id=workflow_id,
        preview_id=record["preview"]["preview_id"],
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision=base,
        prepared_config_sha256=hashlib.sha256(payload).hexdigest(),
    )
    live.write_text('{"live": "maintenance-edit"}\n', encoding="utf-8")

    service = _deployment_service(
        data_dir, workspace=workspace, target_path=target, workflows=store
    )
    result = service.prepare(overwrite=True)

    assert result["ok"] is False
    assert result["reason"] == "stale_generated_config"
    assert live.read_text(encoding="utf-8") == '{"live": "maintenance-edit"}\n'


def test_abandoning_a_workflow_removes_only_its_directory(tmp_path):
    """Cleanup is scoped to the workflow's own artifacts plus the legacy
    singleton paths — never another workflow's preserved terminal state."""

    from admin.guided_setup_workflow import GuidedSetupWorkflowStore
    from admin.setup_workflow import SetupWorkflowArtifacts

    data_dir = _admin_data(tmp_path)
    store = GuidedSetupWorkflowStore(data_dir)
    record = store.ensure_active()
    workflow_id = record["workflow_id"]
    artifacts = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)
    target = artifacts.generated_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_payload())
    unrelated = data_dir / "workflows" / "guided-setup" / "other" / "keep.json"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("{}\n", encoding="utf-8")

    cleanup = artifacts.clear()

    assert not target.exists()
    assert not Path(target).parent.parent.exists(), (
        "the workflow directory itself must be removed"
    )
    assert unrelated.exists()
    assert all(entry["status"] in {"removed", "absent"} for entry in cleanup)
