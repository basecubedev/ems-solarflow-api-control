# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repair cards appear when there is something to repair — and only then.

Three of the eight manual-panel cards exist for a broken system: Zendure MQTT
telemetry, the older-MQTT migration and the unfinished-workflow recovery. On a
healthy installation they are noise, but hiding one on a failed load would turn
"we could not tell" into "nothing to do here". The rule these contracts pin is
therefore: hide only on a proven negative answer.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "admin", "static")
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_presence_runner.js")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _status_panel():
    html = _read("index.html")
    panel = html.split('id="maintenance-status-panel"', 1)[1].split(
        'id="maintenance-settings-panel"', 1
    )[0]
    assert panel, "status panel slice is empty"
    return panel


def _present(predicate, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"predicate": predicate, "payload": payload}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["present"]


# --- Zendure MQTT telemetry ----------------------------------------------


def test_telemetry_card_is_hidden_when_the_installation_has_no_zendure_mqtt():
    assert (
        _present(
            "maintenanceTelemetryCardPresent",
            {
                "runtime_state": "unavailable",
                "configured_device_count": 0,
                "broker_count": 0,
            },
        )
        is False
    )


def test_telemetry_card_stays_for_a_configured_broker_without_devices():
    """Two brokers and no telemetry device is still something to report."""

    assert (
        _present(
            "maintenanceTelemetryCardPresent",
            {
                "runtime_state": "configured",
                "configured_device_count": 0,
                "broker_configured": True,
                "broker_count": 2,
            },
        )
        is True
    )


def test_telemetry_card_stays_for_a_configured_device():
    assert (
        _present(
            "maintenanceTelemetryCardPresent",
            {"runtime_state": "connected", "configured_device_count": 1},
        )
        is True
    )


def test_telemetry_card_stays_when_a_device_is_misconfigured():
    assert (
        _present(
            "maintenanceTelemetryCardPresent",
            {
                "runtime_state": "unavailable",
                "configured_device_count": 0,
                "invalid_device_count": 2,
            },
        )
        is True
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"load_error": True, "configured_device_count": 0, "broker_count": 0},
        None,
        "broken",
    ],
)
def test_telemetry_card_stays_when_the_answer_could_not_be_read(payload):
    assert _present("maintenanceTelemetryCardPresent", payload) is True


# --- older MQTT device setup ---------------------------------------------


def test_migration_card_is_hidden_when_nothing_needs_migrating():
    assert (
        _present(
            "maintenanceMigrationCardPresent",
            {"status": "ok", "review": {"needs_migration": False}},
        )
        is False
    )


def test_migration_card_stays_when_a_device_needs_migrating():
    assert (
        _present(
            "maintenanceMigrationCardPresent",
            {"status": "ok", "review": {"needs_migration": True}},
        )
        is True
    )


@pytest.mark.parametrize(
    "payload",
    [{"status": "error", "message": "boom"}, {"status": "ok"}, None, []],
)
def test_migration_card_stays_when_the_review_could_not_be_read(payload):
    assert _present("maintenanceMigrationCardPresent", payload) is True


# --- unfinished setup or update ------------------------------------------


def test_recovery_card_is_hidden_when_no_workflow_is_stuck():
    assert (
        _present(
            "maintenanceRecoveryCardPresent",
            {
                "ok": True,
                "lifecycle": {"state": "idle"},
                "safe": {"available": False},
                "advanced": {"available": False},
            },
        )
        is False
    )


@pytest.mark.parametrize("key", ["safe", "advanced"])
def test_recovery_card_stays_when_a_recovery_action_exists(key):
    plan = {
        "ok": True,
        "lifecycle": {"state": "idle"},
        "safe": {"available": False},
        "advanced": {"available": False},
    }
    plan[key] = {"available": True}
    assert _present("maintenanceRecoveryCardPresent", plan) is True


def test_recovery_card_stays_while_a_workflow_is_still_running():
    assert (
        _present(
            "maintenanceRecoveryCardPresent",
            {
                "ok": True,
                "lifecycle": {"state": "active"},
                "safe": {"available": False},
                "advanced": {"available": False},
            },
        )
        is True
    )


@pytest.mark.parametrize("payload", [None, {}, "broken", {"ok": False}])
def test_recovery_card_stays_when_the_state_could_not_be_read(payload):
    assert _present("maintenanceRecoveryCardPresent", payload) is True


# --- order ----------------------------------------------------------------


def test_the_settings_editor_is_not_on_the_status_page_at_all():
    """It became its own page, so the status page reads and repairs only."""

    status = _status_panel()
    assert 'id="maintenance-config-card"' not in status
    # The status page still points at it, so the route is not lost.
    assert 'data-open-maintenance-path="settings-safety"' in status


def test_the_repair_cards_sit_below_the_everyday_ones():
    manual = _status_panel()
    everyday = manual.index('id="maintenance-containers"')
    for repair in (
        'id="maintenance-zendure-mqtt"',
        'id="maintenance-mqtt-migration"',
        'id="maintenance-workflow-recovery"',
    ):
        assert everyday < manual.index(repair)


# --- the pill agrees with the sentence -------------------------------------


def _tone(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"predicate": "maintenanceRecoveryCardTone", "payload": payload}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["tone"]


def test_a_blocked_workflow_is_not_labelled_ready():
    """The card said "Guided Setup is blocked" under a READY pill."""

    assert (
        _tone(
            {
                "ok": True,
                "blocking": True,
                "lifecycle": {"state": "cleanup_pending", "owner": "guided_setup"},
                "safe": {"available": True},
                "advanced": {"available": False},
            }
        )
        == "action"
    )


def test_a_running_operation_reads_as_information_not_as_a_fault():
    assert (
        _tone(
            {
                "ok": True,
                "operation_running": True,
                "lifecycle": {"state": "operation_running"},
                "safe": {"available": False},
                "advanced": {"available": False},
            }
        )
        == "info"
    )


def test_an_unreadable_recovery_state_is_a_warning():
    assert _tone({"ok": False}) == "warn"
    assert _tone(None) == "warn"


def test_an_offered_reset_without_a_block_still_asks_for_attention():
    assert (
        _tone(
            {
                "ok": True,
                "blocking": False,
                "lifecycle": {"state": "idle"},
                "safe": {"available": True},
                "advanced": {"available": False},
            }
        )
        == "action"
    )
