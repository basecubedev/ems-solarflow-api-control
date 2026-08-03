# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedicated real-Docker Admin replacement browser gate contracts."""

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "admin-replacement-canary.yml"
RUNNER = ROOT / "tests" / "e2e" / "run-admin-replacement-canary.sh"
SPEC = ROOT / "tests" / "e2e" / "admin-replacement-canary.spec.ts"


def test_real_replacement_canary_is_scheduled_and_manually_runnable():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "schedule:" in text
    assert "admin-replacement-canary" in text
    assert "playwright.admin-replacement.config.ts" in text
    assert "/var/run/docker.sock" in RUNNER.read_text(encoding="utf-8")


def test_real_replacement_browser_asserts_durable_reconnect_contract():
    text = SPEC.read_text(encoding="utf-8")

    for contract in (
        "admin_update",
        "old container no longer active",
        "target digest",
        "persistent Admin reference",
        "authenticated",
        "reauthenticateAfterReconnect",
        "resources_verified",
        "continueToDevices",
        "replacement events",
    ):
        assert contract in text
