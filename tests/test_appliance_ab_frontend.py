# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the A/B section of the appliance UI is allowed to say.

The lifecycle label is derived from backend authority only. The frontend never
decides that an update succeeded, that a slot is healthy or that a trial
committed — it reports what the status payload proves, and the states it can
distinguish are exactly the states the backend distinguishes.

Style family: Control / Energy stage. The section reuses the existing stage,
card, status-value and tone tokens; no new visual system is introduced.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "appliance" / "static" / "app.js"
APP = APP_JS.read_text(encoding="utf-8")

node = shutil.which("node")
requires_node = pytest.mark.skipif(node is None, reason="node is required to evaluate app.js")

def extract_lifecycle():
    """The lifecycle helper on its own; it depends on nothing but its argument."""

    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function abLifecycle(ab) {")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("abLifecycle is not a closed function")


def lifecycle(payload):
    script = (
        extract_lifecycle()
        + "\nconsole.log(JSON.stringify(abLifecycle("
        + json.dumps(payload)
        + ")));\n"
    )
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def ab(**overrides):
    payload = {
        "mode": "ab",
        "ab_supported": True,
        "tryboot": False,
        "drift": [],
        "ab_state": {},
    }
    payload.update(overrides)
    return payload


# --- the function is extractable ---------------------------------------------


def test_the_lifecycle_helper_is_a_named_function():
    """A state machine buried in a render call cannot be tested at all."""

    assert "function abLifecycle(ab)" in APP_JS.read_text(encoding="utf-8")


# --- every distinguishable state ---------------------------------------------


@requires_node
def test_a_single_slot_appliance_is_named_as_one():
    assert lifecycle(ab(mode="single_slot", ab_supported=False))["label"] == (
        "Single-slot appliance"
    )


@requires_node
def test_a_proven_idle_appliance_is_ready():
    result = lifecycle(ab())

    assert result["label"] == "A/B appliance ready"
    assert result["tone"] == "ok"


@requires_node
def test_layout_drift_requires_manual_action():
    result = lifecycle(ab(drift=["the selector points at partition 3"]))

    assert result["label"] == "Manual action required"
    assert result["tone"] == "bad"


@requires_node
def test_a_staged_and_armed_update_is_a_pending_trial_reboot():
    result = lifecycle(
        ab(ab_state={"pending_trial": {"target_slot": "B", "committed": False}})
    )

    assert result["label"] == "Trial reboot pending"
    assert result["tone"] == "warn"


@requires_node
def test_a_running_trial_is_named_as_health_checking():
    result = lifecycle(
        ab(tryboot=True, ab_state={"pending_trial": {"target_slot": "B", "committed": False}})
    )

    assert result["label"] == "Trial boot active — health checking"
    assert result["tone"] == "warn"


@requires_node
def test_a_committed_trial_is_named_as_committed():
    result = lifecycle(
        ab(tryboot=True, ab_state={"pending_trial": {"target_slot": "B", "committed": True}})
    )

    assert result["label"] == "Committed"
    assert result["tone"] == "ok"


@requires_node
def test_a_tryboot_without_a_pending_operation_requires_manual_action():
    """Nothing guesses which slot is safe when the trial cannot identify itself."""

    result = lifecycle(ab(tryboot=True))

    assert result["label"] == "Manual action required"


@requires_node
def test_an_unacknowledged_fallback_is_reported_before_anything_else():
    result = lifecycle(
        ab(
            ab_state={
                "last_fallback": {"target_slot": "B", "source_slot": "A", "acknowledged": False}
            }
        )
    )

    assert result["label"] == "Fallback observed"
    assert result["tone"] == "bad"


@requires_node
def test_an_acknowledged_fallback_no_longer_dominates_the_state():
    result = lifecycle(
        ab(
            ab_state={
                "last_fallback": {"target_slot": "B", "source_slot": "A", "acknowledged": True}
            }
        )
    )

    assert result["label"] == "A/B appliance ready"


# --- what the UI must never show ---------------------------------------------


def test_the_ui_never_renders_a_block_device_path():
    """A device path is a technical diagnostic, never appliance-console text."""

    source = APP_JS.read_text(encoding="utf-8")

    for forbidden in ("/dev/", "boot_device", "root_device", "partuuid", "PARTUUID"):
        assert forbidden not in source, forbidden


def test_the_ui_offers_no_writable_device_input():
    source = APP_JS.read_text(encoding="utf-8")

    assert "by-slot" not in source
    assert "mmcblk" not in source


# --- update readiness ---------------------------------------------------------


def test_the_readiness_card_names_every_backend_prerequisite():
    """The list is the backend's, not the frontend's idea of one."""

    block = APP.split("AB_READINESS = [")[1].split("];")[0]
    declared = set(re.findall(r'\["(\w+)", "[^"]+", "[^"]+"\]', block))
    fields = {
        "hardware_supported",
        "artifact_decoder_ready",
        "sparse_decoder_ready",
        "persistence_ready",
        "host_identity_ready",
        "docker_reconstruction_ready",
        "layout_ready",
    }

    assert declared == fields


def test_the_plan_buttons_are_disabled_until_every_prerequisite_holds():
    assert 'disabled: !ab.may_mutate || !readiness.ready' in APP
    assert 'disabled: !ab.may_mutate || !abState.previous_slot || !readiness.ready' in APP


def test_a_missing_prerequisite_is_explained_rather_than_only_disabling():
    assert '"data-test": "ab-not-ready"' in APP
    assert "OS updates are unavailable: " in APP


def test_the_readiness_card_uses_the_existing_card_family():
    """No new visual system: the same card()/tone()/fact() helpers."""

    block = APP.split('card("Update readiness"')[1].split('"ab-readiness"')[0]
    assert "tone(" in block
    assert "fact(" in block
    assert "class=" not in block.replace('class: "status-value"', "")
