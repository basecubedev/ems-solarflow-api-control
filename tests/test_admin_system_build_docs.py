# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documentation contracts for the productive paired System Build workflow."""

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
PAIRING_DOC = ROOT / "docs" / "technical" / "system-build-pairing.md"
ARCHITECTURE_DOC = ROOT / "docs" / "technical" / "admin-architecture.md"
MAINTENANCE_DOC = ROOT / "docs" / "user" / "admin-maintenance.md"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_system_build_docs_describe_every_supported_identity_and_dev_attempt():
    text = _read(PAIRING_DOC)

    for identity in (
        "local-<short-sha>",
        "local-<short-sha>-dirty",
        "v0.8.0",
        "v0.8.0-RC1",
        "dev-<branch>-<short-sha>-<run-id>-<attempt>",
    ):
        assert identity in text
    assert "workflow retry" in text.lower()
    assert "floating" in text.lower() and "not" in text.lower()


def test_system_build_docs_describe_bootstrap_and_embedded_resource_gate():
    text = _read(PAIRING_DOC).lower()

    assert "bootstrap" in text and "latest" in text
    assert "stable" in text and ("resolve" in text or "select" in text)
    assert "embedded" in text and "resources_verified" in text
    assert "before config" in text or "before any config" in text


def test_system_build_docs_describe_recoverable_lifecycle_and_completion_rule():
    text = _read(PAIRING_DOC)
    lower = text.lower()

    for stage in (
        "admin_update_pending",
        "admin_reconnect_pending",
        "admin_aligned",
        "resources_verified",
        "ems_operation_pending",
        "ems_operation_running",
        "healthcheck_pending",
        "completed",
        "failed_recoverable",
        "cancelled",
    ):
        assert stage in text
    assert "resume" in lower
    assert "return admin" in lower
    assert "known-good" in lower and "health" in lower
    assert "finalized on resume" not in lower


def test_admin_architecture_uses_strict_pairing_not_advisory_compatibility():
    text = _read(ARCHITECTURE_DOC).lower()

    assert "systemalignmentservice" in text or "system_alignment.py" in text
    assert "strict" in text and "paired system build" in text
    assert "advisory signal, not a hard gate" not in text


def test_guided_upgrade_docs_describe_one_target_system_build():
    text = _read(MAINTENANCE_DOC)
    lower = text.lower()

    # One decision: the Target System Build aligns both Admin and EMS.
    assert "Target System Build" in text
    assert "one" in lower and "admin" in lower and "ems" in lower
    # Admin alignment is an automatic stage, not a separate decision.
    assert "automatic" in lower and "admin alignment" in lower
    # The current-state preflight and backup run before Admin alignment.
    assert "before any admin alignment" in lower or "before any admin" in lower
    # The obsolete optional / standalone Admin-update flow is gone from the normal
    # upgrade; only advanced recovery keeps a standalone Admin action.
    assert "continue without updating" not in lower
    assert "advanced" in lower and "recovery" in lower


def test_maintenance_docs_preserve_review_backup_and_mqtt_model_contracts():
    text = _read(MAINTENANCE_DOC)
    lower = text.lower()

    for phrase in (
        "choose the target",
        "review the plan",
        "backup",
        "explicit confirmation",
        "ordered progress",
        "exact hardware model",
        "unknown / telemetry only",
        "zendure mqtt migration",
        "stale",
    ):
        assert phrase in lower
