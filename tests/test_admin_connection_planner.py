# SPDX-License-Identifier: AGPL-3.0-or-later
"""The connection planner's full action matrix.

One planner owns keep/replace/add/block for both Setup and Maintenance, so the
whole matrix — identity state x capability continuity x operator intent — lives
here rather than being restated at each endpoint. What counts as physical
identity is Core's contract (``tests/test_device_identity.py``); this file only
pins the decision built on top of it.
"""

import json

import pytest

from admin.connection_planner import (
    ACTION_ADD_AS_NEW_DEVICE,
    ACTION_BLOCK_CAPABILITY_LOSS,
    ACTION_BLOCK_IDENTITY_CONFLICT,
    ACTION_BLOCK_UNRESOLVED_IDENTITY,
    ACTION_KEEP_CURRENT,
    ACTION_REPLACE_WITH_CONFIRMATION,
    ACTION_USE_CANDIDATE,
    CONTROL_GAINED,
    CONTROL_LOST,
    CONTROL_NOT_REQUIRED,
    CONTROL_PRESERVED,
    CONTROL_UNKNOWN,
    INTENT_ADD_DEVICE,
    INTENT_SWITCH_CONNECTION,
    plan_connection_change,
)
from ems.device_identity import (
    STATUS_AMBIGUOUS,
    STATUS_CONFIRMED,
    STATUS_CONFLICT,
    STATUS_PROBABLE,
    STATUS_UNRESOLVED,
)

pytestmark = pytest.mark.simulation

TOKEN_KEY = b"connection-planner-test-key-32by"

MASKED = "••••"


def _api_device(*, serial=None, ip="10.0.0.1", port=80):
    device = {"type": "zendure_local_http", "ip": ip, "port": port}
    if serial is not None:
        device["serial_number"] = serial
    return device


def _mqtt_device(
    *,
    serial=None,
    source="zendure_cloud_mqtt",
    broker_ref="zendure_cloud",
    product_key="PRODUCT_A",
    device_id="ROUTE_1234",
):
    device = {
        "type": "zendure_mqtt",
        "mqtt": {
            "source": source,
            "broker_ref": broker_ref,
            "product_key": product_key,
            "device_id": device_id,
        },
    }
    if serial is not None:
        device["serial_number"] = serial
    return device


def _plan(**kwargs):
    kwargs.setdefault("identity_token_key", TOKEN_KEY)
    return plan_connection_change(**kwargs)


# --- identity states ---------------------------------------------------------


def test_same_serial_across_transports_may_be_replaced():
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_USE_CANDIDATE
    assert plan.same_physical_device is True
    assert plan.identity_status == STATUS_CONFIRMED
    assert plan.replacement_allowed is True
    assert plan.confirmation_required is False
    assert plan.control_continuity == CONTROL_PRESERVED
    assert plan.blocked is False


def test_contradictory_serials_on_one_route_block_replacement():
    plan = _plan(
        current_device=_mqtt_device(serial="SERIAL-1"),
        candidate=_mqtt_device(serial="SERIAL-2"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_BLOCK_IDENTITY_CONFLICT
    assert plan.identity_status == STATUS_CONFLICT
    assert plan.identity_conflict is True
    assert plan.replacement_allowed is False
    assert plan.blocked is True


def test_ambiguous_write_route_blocks_replacement_without_claiming_a_conflict():
    plan = _plan(
        current_device=_mqtt_device(product_key="PK_A"),
        candidate=_mqtt_device(product_key="PK_B"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_BLOCK_IDENTITY_CONFLICT
    assert plan.identity_status == STATUS_AMBIGUOUS
    assert plan.identity_conflict is False
    assert plan.reason == "ambiguous_mqtt_write_route"


def test_shared_serial_survives_route_ambiguity_but_needs_confirmation():
    """Physical identity and write-address ambiguity are separate answers.

    One inverter observed on two precise product routes is still one inverter,
    so it is not a conflict — but the write address became ambiguous, so the
    replacement is not silent.
    """

    plan = _plan(
        current_device=_mqtt_device(serial="SERIAL-1", product_key="PK_A"),
        candidate=_mqtt_device(serial="SERIAL-1", product_key="PK_B"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.same_physical_device is True
    assert plan.identity_status == STATUS_CONFIRMED
    assert plan.identity_conflict is False
    assert plan.action == ACTION_REPLACE_WITH_CONFIRMATION
    assert plan.reason == "ambiguous_mqtt_write_route"


def test_two_masked_observations_never_replace_a_configured_device():
    """The RC blocker, at the decision layer: display text is not identity."""

    plan = _plan(
        current_device=_api_device(serial=MASKED, ip="10.0.0.1"),
        candidate=_api_device(serial=MASKED, ip="10.0.0.2"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_BLOCK_UNRESOLVED_IDENTITY
    assert plan.same_physical_device is False
    assert plan.replacement_allowed is False


def test_unresolved_identity_can_still_be_added_as_a_separate_device():
    plan = _plan(
        current_device=_api_device(serial=MASKED, ip="10.0.0.1"),
        candidate=_api_device(serial=MASKED, ip="10.0.0.2"),
        intent=INTENT_ADD_DEVICE,
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_ADD_AS_NEW_DEVICE
    assert plan.replacement_allowed is False


def test_setup_adoption_without_a_configured_device_adds():
    plan = _plan(
        current_device=None,
        candidate=_mqtt_device(serial="EOD1AAA111"),
        intent=INTENT_ADD_DEVICE,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_ADD_AS_NEW_DEVICE
    assert plan.reason == "no_configured_device"
    assert plan.control_continuity == CONTROL_GAINED
    assert plan.physical_device_id.startswith("opaque:v1:")


def test_no_candidate_keeps_the_current_connection():
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=None,
        current_control_supported=True,
    )

    assert plan.action == ACTION_KEEP_CURRENT
    assert plan.reason == "no_candidate_connection"


def test_candidate_that_is_already_the_current_connection_keeps_it():
    device = _mqtt_device(serial="EOD1AAA111")
    plan = _plan(
        current_device=device,
        candidate=dict(device),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_KEEP_CURRENT
    assert plan.same_physical_device is True
    assert plan.current_connection_id == plan.candidate_connection_id


def test_serialless_route_match_is_probable_and_replaceable():
    plan = _plan(
        current_device=_mqtt_device(),
        candidate=_mqtt_device(),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.identity_status == STATUS_PROBABLE
    assert plan.same_physical_device is True


# --- capability continuity ---------------------------------------------------


@pytest.mark.parametrize(
    ("current_supported", "candidate_supported", "continuity"),
    [
        (True, True, CONTROL_PRESERVED),
        (True, False, CONTROL_LOST),
        (False, True, CONTROL_GAINED),
        (False, False, CONTROL_NOT_REQUIRED),
        (True, None, CONTROL_UNKNOWN),
        (None, True, CONTROL_UNKNOWN),
    ],
)
def test_control_continuity_matrix(current_supported, candidate_supported, continuity):
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=current_supported,
        candidate_control_supported=candidate_supported,
    )

    assert plan.control_continuity == continuity


def test_losing_control_requires_an_explicit_confirmation():
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=True,
        candidate_control_supported=False,
        candidate_control_block_reason="broker_source_write_unverified",
    )

    assert plan.action == ACTION_REPLACE_WITH_CONFIRMATION
    assert plan.confirmation_required is True
    assert plan.replacement_allowed is True
    assert plan.control_continuity == CONTROL_LOST
    assert "broker_source_write_unverified" in plan.notes


def test_confirmed_control_loss_proceeds_as_a_downgrade():
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=True,
        candidate_control_supported=False,
        operator_confirmed=True,
    )

    assert plan.action == ACTION_USE_CANDIDATE
    assert plan.control_continuity == CONTROL_LOST


def test_control_loss_fails_closed_when_control_is_required():
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=True,
        candidate_control_supported=False,
        control_required=True,
        operator_confirmed=True,
    )

    assert plan.action == ACTION_BLOCK_CAPABILITY_LOSS
    assert plan.replacement_allowed is False
    assert plan.blocked is True


def test_unresolved_capability_never_counts_as_capable():
    plan = _plan(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=True,
        candidate_control_supported=None,
    )

    assert plan.action == ACTION_REPLACE_WITH_CONFIRMATION
    assert plan.control_continuity == CONTROL_UNKNOWN


def test_gaining_control_needs_no_confirmation():
    plan = _plan(
        current_device=_mqtt_device(serial="EOD1AAA111"),
        candidate=_api_device(serial="EOD1AAA111"),
        current_control_supported=False,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_USE_CANDIDATE
    assert plan.confirmation_required is False


# --- identity beats capability ----------------------------------------------


def test_identity_conflict_wins_over_a_capability_gain():
    """A better candidate is still not this device."""

    plan = _plan(
        current_device=_mqtt_device(serial="SERIAL-1"),
        candidate=_mqtt_device(serial="SERIAL-2"),
        current_control_supported=False,
        candidate_control_supported=True,
    )

    assert plan.action == ACTION_BLOCK_IDENTITY_CONFLICT


# --- payload safety ----------------------------------------------------------


def test_plan_payload_carries_no_raw_identity_material():
    plan = _plan(
        current_device=_api_device(serial="SECRET-SERIAL-1", ip="10.9.9.9"),
        candidate=_mqtt_device(serial="SECRET-SERIAL-1"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    payload = json.dumps(plan.to_dict())

    assert "SECRET-SERIAL-1" not in payload
    assert "10.9.9.9" not in payload
    assert "ROUTE_1234" not in payload
    assert "PRODUCT_A" not in payload
    assert plan.current_connection_id.startswith("conn:v1:")
    assert plan.candidate_connection_id.startswith("conn:v1:")


def test_without_a_token_key_no_connection_ids_are_invented():
    plan = plan_connection_change(
        current_device=_api_device(serial="EOD1AAA111"),
        candidate=_mqtt_device(serial="EOD1AAA111"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.current_connection_id is None
    assert plan.candidate_connection_id is None


def test_switch_and_add_intents_share_one_identity_rule():
    """Setup and Maintenance may differ in workflow, never in identity rules."""

    conflict = {
        "current_device": _mqtt_device(serial="SERIAL-1"),
        "candidate": _mqtt_device(serial="SERIAL-2"),
        "current_control_supported": True,
        "candidate_control_supported": True,
    }

    switching = _plan(intent=INTENT_SWITCH_CONNECTION, **conflict)
    adding = _plan(intent=INTENT_ADD_DEVICE, **conflict)

    assert switching.action == adding.action == ACTION_BLOCK_IDENTITY_CONFLICT
    assert switching.identity_status == adding.identity_status == STATUS_CONFLICT


def test_unresolved_status_is_reported_not_guessed():
    plan = _plan(
        current_device=_api_device(serial=MASKED),
        candidate=_api_device(serial=MASKED, ip="10.0.0.2"),
        current_control_supported=True,
        candidate_control_supported=True,
    )

    assert plan.identity_status == STATUS_UNRESOLVED


# --- endpoint wiring ---------------------------------------------------------
# Representative delegation checks only; the matrix above is not restated here.


def test_maintenance_delegates_its_same_device_decision_to_the_planner():
    """Maintenance must adapt inputs, never re-derive replacement eligibility."""

    import pathlib

    import admin.maintenance_config as maintenance

    source = pathlib.Path(maintenance.__file__).read_text()
    assert "from admin.connection_planner import" in source
    assert "plan_connection_change(" in source
    # The old local predicate must not come back beside the planner.
    assert "same_physical_inverter_evidence(" not in source


def test_maintenance_plan_blocks_a_foreign_connection_selection():
    from admin.maintenance_config import (
        TRUSTED_CONNECTION_SELECTION_FIELD,
        trusted_selection_targets_other_inverter,
    )

    config = {"devices": []}
    stored = _mqtt_device(serial="SERIAL-1")
    foreign = dict(_mqtt_device(serial="SERIAL-2"))
    foreign[TRUSTED_CONNECTION_SELECTION_FIELD] = True

    assert (
        trusted_selection_targets_other_inverter(
            config, stored, foreign, identity_token_key=TOKEN_KEY
        )
        is True
    )


def test_maintenance_plan_accepts_the_same_inverter_on_another_route():
    from admin.maintenance_config import (
        TRUSTED_CONNECTION_SELECTION_FIELD,
        trusted_selection_targets_other_inverter,
    )

    config = {"devices": []}
    stored = _api_device(serial="SERIAL-1")
    same = dict(_mqtt_device(serial="SERIAL-1"))
    same[TRUSTED_CONNECTION_SELECTION_FIELD] = True

    assert (
        trusted_selection_targets_other_inverter(
            config, stored, same, identity_token_key=TOKEN_KEY
        )
        is False
    )
