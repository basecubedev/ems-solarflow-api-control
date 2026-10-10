# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance MQTT migration is a complete authenticated review/apply workflow."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC = Path(__file__).resolve().parents[1] / "admin" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_migration_card_exposes_review_backup_apply_validate_stages():
    html = _read("index.html")
    card = html.split('id="maintenance-mqtt-migration"', 1)[1].split(
        "</section>", 1
    )[0]

    for label in ("01 Review", "02 Backup", "03 Apply", "04 Validate"):
        assert label in card
    assert 'id="maintenance-mqtt-migration-backup"' in card
    assert 'id="maintenance-mqtt-migration-apply"' in card
    assert 'id="maintenance-mqtt-migration-devices"' in card
    assert 'type="checkbox"' in card and "checked" in card


def test_migration_browser_submits_fingerprint_confirmation_backup_and_csrf():
    js = _read("admin.js")
    apply = js.split("async function applyMqttMigration", 1)[1].split(
        "\nfunction ", 1
    )[0]

    assert 'fetch("/api/admin/maintenance/zendure-mqtt/migration-apply"' in apply
    assert "revision: mqttMigrationState.revision" in apply
    assert "confirm: true" in apply
    assert "backup" in apply
    # The authenticated fetch wrapper injects the CSRF token for this POST.
    assert 'headers: { "Content-Type": "application/json" }' in apply


def test_migration_rendering_uses_safe_dom_and_never_secret_fields():
    js = _read("admin.js")
    render = js.split("function mqttMigrationDeviceRow", 1)[1].split(
        "\nasync function ", 1
    )[0]

    assert "textContent" in render
    assert "createElement" in render
    for secret in ("password", "api_key", "app_key", "credentials"):
        assert secret not in render.lower()


def test_success_refreshes_config_runtime_and_control_readiness():
    js = _read("admin.js")
    apply = js.split("async function applyMqttMigration", 1)[1].split(
        "\nfunction ", 1
    )[0]

    assert "await loadMaintenanceConfig()" in apply
    assert "await loadZendureMqttRuntimeStatus()" in apply
    assert "await loadMqttMigrationReview()" in apply


def test_a_failed_apply_marks_the_stage_the_server_names():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the migration stage test")
    js = _read("admin.js")
    header = "function markMqttMigrationFailure"
    fn = header + js.split(header, 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    script = fn + """
let stages = {};
function setMqttMigrationStage(name, state) { stages[name] = state; }
const results = {};
for (const stage of ["backup", "apply", "validate", "review", null]) {
  stages = { backup: "running", apply: "running", validate: "running" };
  markMqttMigrationFailure(stage);
  results[String(stage)] = { ...stages };
}
console.log(JSON.stringify(results));
"""
    out = json.loads(
        subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    )

    assert out["backup"]["backup"] == "failed" and out["backup"]["apply"] is None
    assert out["apply"] == {"backup": "done", "apply": "failed", "validate": "running"}
    assert out["validate"]["validate"] == "failed"
    assert out["review"] == {"backup": None, "apply": None, "validate": None}
    assert out["null"]["apply"] == "failed"


def test_the_failed_stage_is_read_from_the_answer_not_the_message():
    js = _read("admin.js")
    apply = js.split("async function applyMqttMigration", 1)[1].split("\nasync function ", 1)[0]

    assert "error.stage = typeof data.stage" in apply
    assert "markMqttMigrationFailure(err.stage || reached)" in apply
    assert '.includes("backup")' not in apply
