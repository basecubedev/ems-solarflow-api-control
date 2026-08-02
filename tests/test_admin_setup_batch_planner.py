# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup's automatic transport selection is backend work, composed from the planner.

Guided Setup has to answer a question Maintenance never asks: given every
observation and proposal from all three discovery sources, which *one*
connection per physical device is configured? That batch question is
orchestration — source priority, manual-vs-automatic origin, one connection per
logical device — and it lives in ``admin.setup_planner``.

Every domain answer underneath it is borrowed, never re-derived:

physical grouping
    ``ems.device_identity`` evidence comparison

keep / replace / add / block
    ``admin.connection_planner.plan_connection_change`` — the same pairwise
    authority Maintenance calls

This module pins both halves: the matrix the JavaScript reconciler used to own,
and the parity that proves Setup and Maintenance cannot drift apart.
"""

import pytest

from admin.connection_planner import (
    ACTION_BLOCK_IDENTITY_CONFLICT,
    INTENT_SWITCH_CONNECTION,
    plan_connection_change,
)
from admin.maintenance_config import plan_trusted_selection
from admin.setup_planner import build_setup_plan, plan_setup_connection_switch

pytestmark = pytest.mark.simulation

KEY = b"setup-batch-planner-contract-key-0123456789abcdef"

SERIAL_A = "EOD1AAA111"
SERIAL_B = "EOD1BBB222"

ALL_SOURCES = {"local_api": True, "local_mqtt": True, "zendure_mqtt": True}
API_FIRST = ["local_api", "local_mqtt", "zendure_mqtt"]
MQTT_FIRST = ["zendure_mqtt", "local_mqtt", "local_api"]


def _observation(serial=SERIAL_A, ip="10.0.0.11", **overrides):
    device = {
        "role_suggestion": "inverter",
        "device_type": "solarflow",
        "api_family": "solarflow",
        "ip": ip,
        "port": 8080,
        "serial_number": serial,
        "verified": True,
        "usable_for_config": True,
    }
    device.update(overrides)
    return device


def _proposal(proposal_id="cloud-1", serial=SERIAL_A, device_id="DEV1", **overrides):
    proposal = {
        "id": proposal_id,
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "serial_number": serial,
        "device_id": device_id,
        "product_key": "PK1",
        "config_fragment": {
            "mqtt": {
                "source": "zendure_cloud_mqtt",
                "broker_ref": "zendure_cloud",
                "device_id": device_id,
                "product_key": "PK1",
            }
        },
    }
    proposal.update(overrides)
    return proposal


def _draft(serial=SERIAL_A, ip="10.0.0.11", item_id="item-1", auto_added=True):
    return {
        "draft_item_id": item_id,
        "source_id": "solarflow:" + serial,
        "role": "inverter",
        "serial_number": serial,
        "ip": ip,
        "port": 8080,
        "auto_added": auto_added,
        "config_name": "INV_1",
    }


def _selection(proposal_id="cloud-1", origin="automatic", **overrides):
    entry = {
        "id": proposal_id,
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "selection_origin": origin,
    }
    entry.update(overrides)
    return entry


def _plan(state, *, observations=(), proposals=(), priority=None, enabled=None, **kwargs):
    return build_setup_plan(
        state,
        observations=list(observations),
        proposals=list(proposals),
        priority=list(priority or API_FIRST),
        enabled_sources=dict(enabled or ALL_SOURCES),
        identity_token_key=KEY,
        **kwargs,
    )


# --- the matrix the JavaScript reconciler used to own ------------------------
def test_late_mqtt_priority_replaces_the_local_api_draft():
    plan = _plan(
        {"draft_items": [_draft()]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=MQTT_FIRST,
    )

    assert plan["operations"]["drop_draft_items"] == ["item-1"]
    assert [op["id"] for op in plan["operations"]["select_mqtt_proposals"]] == ["cloud-1"]


def test_batch_selection_is_idempotent():
    first = _plan(
        {"draft_items": [_draft()], "mqtt_selections": [_selection()]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=MQTT_FIRST,
    )
    second = _plan(
        {"mqtt_selections": [_selection()]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=MQTT_FIRST,
    )

    assert first["operations"]["drop_draft_items"] == ["item-1"]
    assert second["operations"] == {
        "drop_draft_items": [],
        "drop_mqtt_selections": [],
        "select_mqtt_proposals": [],
        "adopt_observations": [],
    }


def test_manual_local_api_choice_is_not_overridden_by_priority():
    plan = _plan(
        {"draft_items": [_draft(auto_added=False)]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=MQTT_FIRST,
    )

    assert plan["operations"]["drop_draft_items"] == []
    assert plan["operations"]["select_mqtt_proposals"] == []
    assert plan["groups"][0]["selection_origin"] == "manual"


def test_manual_mqtt_choice_survives_a_priority_flip_to_local_api():
    plan = _plan(
        {"mqtt_selections": [_selection(origin="manual")]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=API_FIRST,
    )

    assert plan["operations"]["drop_mqtt_selections"] == []
    assert plan["groups"][0]["selected_source"] == "zendure_mqtt"


def test_automatic_mqtt_selection_yields_to_a_local_api_priority_flip():
    plan = _plan(
        {"mqtt_selections": [_selection(origin="automatic")]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=API_FIRST,
    )

    assert plan["operations"]["drop_mqtt_selections"] == ["cloud-1"]
    assert plan["groups"][0]["selected_source"] == "local_api"


def test_a_disabled_source_is_never_selected():
    plan = _plan(
        {"draft_items": [_draft()]},
        observations=[_observation()],
        proposals=[_proposal()],
        priority=MQTT_FIRST,
        enabled={"local_api": True, "local_mqtt": True, "zendure_mqtt": False},
    )

    assert plan["groups"][0]["selected_source"] == "local_api"
    assert plan["operations"]["select_mqtt_proposals"] == []


def test_two_serials_are_two_groups_and_never_merge():
    plan = _plan(
        {"draft_items": [_draft(), _draft(serial=SERIAL_B, ip="10.0.0.12", item_id="item-2")]},
        observations=[_observation(), _observation(serial=SERIAL_B, ip="10.0.0.12")],
    )

    assert len(plan["groups"]) == 2


def test_a_shared_route_with_contradictory_serials_never_merges():
    """The route claims one inverter, the serials claim two: fail closed."""

    plan = _plan(
        {},
        proposals=[
            _proposal(proposal_id="cloud-1", serial=SERIAL_A),
            _proposal(proposal_id="cloud-2", serial=SERIAL_B),
        ],
    )

    assert len(plan["groups"]) == 2


def test_a_dismissed_physical_device_selects_nothing():
    plan = _plan(
        {
            "draft_items": [_draft()],
            "mqtt_selections": [_selection()],
            "physical_dismissals": [SERIAL_A],
        },
        observations=[_observation()],
        proposals=[_proposal()],
    )

    assert plan["operations"]["drop_draft_items"] == ["item-1"]
    assert plan["operations"]["drop_mqtt_selections"] == ["cloud-1"]
    assert plan["groups"][0]["selected_source"] is None


def test_an_mqtt_only_device_is_not_auto_selected():
    """MQTT-only devices are added by the operator, never grabbed automatically."""

    plan = _plan({}, proposals=[_proposal()], priority=MQTT_FIRST)

    assert plan["operations"]["select_mqtt_proposals"] == []


# --- composition: every pairwise decision comes from the canonical planner ----
def test_batch_groups_carry_the_canonical_pairwise_action():
    """The group's action is the pairwise planner's answer, verbatim."""

    draft = _draft()
    proposal = _proposal()
    plan = _plan(
        {"draft_items": [draft]},
        observations=[_observation()],
        proposals=[proposal],
        priority=MQTT_FIRST,
    )

    assert plan["groups"][0]["action"] == plan_connection_change(
        current_device=draft,
        candidate=proposal,
        intent=INTENT_SWITCH_CONNECTION,
        identity_token_key=KEY,
    ).to_dict()
    assert plan["groups"][0]["action"]["same_physical_device"] is True


def test_setup_and_maintenance_agree_on_the_same_pairwise_input():
    current = {"name": "INV_1", "sn": SERIAL_A, "ip": "10.0.0.11", "port": 8080}
    candidate = _proposal()

    maintenance = plan_trusted_selection(
        {"devices": [current]}, current, candidate, identity_token_key=KEY
    )
    setup = plan_setup_connection_switch(
        current_device=current,
        candidate=candidate,
        identity_token_key=KEY,
    )

    assert setup.to_dict() == maintenance.to_dict()


def test_setup_switch_is_the_canonical_planner_call():
    current = {"name": "INV_1", "sn": SERIAL_A, "ip": "10.0.0.11", "port": 8080}
    candidate = _proposal()

    assert plan_setup_connection_switch(
        current_device=current, candidate=candidate, identity_token_key=KEY
    ).to_dict() == plan_connection_change(
        current_device=current,
        candidate=candidate,
        intent=INTENT_SWITCH_CONNECTION,
        identity_token_key=KEY,
    ).to_dict()


def test_setup_switch_blocks_an_identity_conflict():
    current = {"name": "INV_1", "sn": SERIAL_A, "mqtt": {
        "source": "zendure_cloud_mqtt", "broker_ref": "zendure_cloud",
        "device_id": "DEV1", "product_key": "PK1",
    }}
    candidate = _proposal(serial=SERIAL_B)

    plan = plan_setup_connection_switch(
        current_device=current, candidate=candidate, identity_token_key=KEY
    )

    assert plan.action == ACTION_BLOCK_IDENTITY_CONFLICT
    assert plan.blocked is True


def test_setup_has_no_second_identity_or_capability_matrix():
    """The orchestrator composes; it never re-decides identity or capability."""

    import admin.setup_planner as planner

    source = open(planner.__file__, encoding="utf-8").read()
    assert "plan_connection_change(" in source
    for forbidden in (
        "def same_physical",
        "def compare_physical_identity",
        "def _identity_conflict",
        "power_capability",
        "write_output_limit",
    ):
        assert forbidden not in source, (
            f"admin/setup_planner.py re-implements {forbidden!r}; that answer "
            "belongs to ems/device_identity.py or the connection planner"
        )


# --- stale plans -------------------------------------------------------------
def test_plan_id_changes_when_the_candidate_generation_changes():
    state = {"draft_items": [_draft()]}
    first = _plan(state, observations=[_observation()])
    second = _plan(state, observations=[_observation()], proposals=[_proposal()])

    assert first["generation"] != second["generation"]
    assert first["plan_id"] != second["plan_id"]


def test_generation_is_stable_for_the_same_candidates():
    first = _plan({}, observations=[_observation()], proposals=[_proposal()])
    second = _plan({}, observations=[_observation()], proposals=[_proposal()])

    assert first["generation"] == second["generation"]


# --- batch grouping across enrichment and broker scope ------------------------
def _cloud(proposal_id, *, serial=None, device_id="DEV1", product_key=None, broker="zendure_cloud"):
    mqtt = {
        "source": "zendure_cloud_mqtt",
        "broker_ref": broker,
        "device_id": device_id,
    }
    if product_key:
        mqtt["product_key"] = product_key
    proposal = {
        "id": proposal_id,
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": broker,
        "device_id": device_id,
        "config_fragment": {"mqtt": dict(mqtt)},
    }
    if product_key:
        proposal["product_key"] = product_key
    if serial:
        proposal["serial_number"] = serial
    return proposal


def test_a_route_only_selection_and_its_enriched_proposal_are_one_group():
    """Enrichment adds a serial to a known route; that is one inverter, not two."""

    plan = _plan(
        {"mqtt_selections": [_selection("cloud-route", origin="manual")]},
        proposals=[_cloud("cloud-route", device_id="DEV1")],
        observations=[],
    )
    assert len(plan["groups"]) == 1

    # What the browser actually stored: the id plus the issued tokens that
    # response carried. Both sides of the remap are therefore server-issued.
    stored = _selection("cloud-route", origin="manual")
    stored["physical_identity_token"] = plan["proposals"][0]["physical_device_id"]
    enriched = _plan(
        {"mqtt_selections": [stored]},
        proposals=[_cloud("cloud-enriched", serial=SERIAL_A, device_id="DEV1")],
    )
    # The stored id predates the enrichment: exactly one selection survives, and
    # it is the current proposal.
    assert len(enriched["groups"]) == 1
    assert enriched["operations"]["drop_mqtt_selections"] == ["cloud-route"]
    assert [op["id"] for op in enriched["operations"]["select_mqtt_proposals"]] == [
        "cloud-enriched"
    ]
    assert enriched["operations"]["select_mqtt_proposals"][0]["selection_origin"] == "manual"


def test_one_route_seen_in_two_broker_scopes_stays_two_groups():
    plan = _plan(
        {},
        proposals=[
            _cloud("local-1", device_id="DEV1", broker="house-broker"),
            _cloud("cloud-1", device_id="DEV1", broker="zendure_cloud"),
        ],
    )
    assert len(plan["groups"]) == 2


def test_a_transitive_chain_is_one_group():
    """Serial↔route and route↔route bridges unite the whole chain."""

    plan = _plan(
        {},
        observations=[_observation(serial=SERIAL_A)],
        proposals=[
            _cloud("cloud-a", serial=SERIAL_A, device_id="DEV1"),
            _cloud("cloud-b", device_id="DEV1", product_key="PK1"),
        ],
    )
    assert len(plan["groups"]) == 1


def test_a_transitive_chain_with_contradictory_serials_never_merges():
    plan = _plan(
        {},
        proposals=[
            _cloud("cloud-a", serial=SERIAL_A, device_id="DEV1"),
            _cloud("cloud-b", serial=SERIAL_B, device_id="DEV1"),
        ],
    )
    assert len(plan["groups"]) == 2


@pytest.mark.parametrize("placeholder", ["••••1111", "…1111", "<redacted>", "your_serial"])
def test_a_redaction_placeholder_never_groups_two_transports(placeholder):
    plan = _plan(
        {
            "draft_items": [
                _draft(serial=placeholder, ip="10.0.0.11", item_id="item-1"),
                _draft(serial=placeholder, ip="10.0.0.12", item_id="item-2"),
            ]
        }
    )
    assert len(plan["groups"]) == 2
    assert all(group["physical_device_id"] is None for group in plan["groups"])


def test_a_dismissal_survives_serial_enrichment():
    """Dismissing the route-only device also dismisses it once a serial appears."""

    route_only = _plan({}, proposals=[_cloud("cloud-1", device_id="DEV1")])
    dismissed = route_only["groups"][0]["physical_device_id"]
    assert dismissed

    enriched = _plan(
        {"physical_dismissals": [dismissed]},
        proposals=[_cloud("cloud-1", serial=SERIAL_A, device_id="DEV1")],
        observations=[_observation(serial=SERIAL_A)],
    )
    assert enriched["groups"][0]["selected_source"] is None
    assert enriched["operations"]["adopt_observations"] == []


# --- what a discovered connection offers ------------------------------------
def _candidates(plan):
    return {entry["id"]: entry for entry in plan["candidates"]}


def test_a_candidate_for_no_configured_device_is_new():
    plan = _plan({}, proposals=[_proposal()])
    assert _candidates(plan)["cloud-1"]["state"] == "new"


def test_the_configured_connection_itself_is_active():
    observation = _observation()
    plan = _plan({"draft_items": [_draft()]}, observations=[observation])
    candidate = next(iter(_candidates(plan).values()))
    assert candidate["state"] == "active"
    assert candidate["current_ref"] == "item-1"
    assert candidate["current_source"] == "local_api"


def test_another_connection_for_a_configured_device_is_an_alternative():
    plan = _plan(
        {"draft_items": [_draft()]},
        observations=[_observation()],
        proposals=[_proposal()],
    )
    candidate = _candidates(plan)["cloud-1"]
    assert candidate["state"] == "alternative"
    assert candidate["current_ref"] == "item-1"
    assert candidate["current_source"] == "local_api"


def test_a_contradicting_candidate_is_fail_closed():
    """A shared route claiming a different serial never becomes actionable."""

    configured = dict(
        _draft(serial=SERIAL_A),
        connection_source="zendure_cloud_mqtt",
        mqtt={
            "source": "zendure_cloud_mqtt",
            "broker_ref": "zendure_cloud",
            "device_id": "DEV1",
            "product_key": "PK1",
        },
    )
    plan = _plan({"draft_items": [configured]}, proposals=[_proposal(serial=SERIAL_B)])

    assert _candidates(plan)["cloud-1"]["state"] == "identity_conflict"
    assert _candidates(plan)["cloud-1"]["current_ref"] is None


def test_a_route_only_candidate_is_recognized_through_its_scoped_identity():
    configured = {
        "draft_item_id": "item-1",
        "role": "inverter",
        "connection_source": "zendure_cloud_mqtt",
        "mqtt": {
            "source": "zendure_cloud_mqtt",
            "broker_ref": "zendure_cloud",
            "device_id": "DEV1",
        },
    }
    plan = _plan(
        {"draft_items": [configured]},
        proposals=[_cloud("cloud-enriched", serial=SERIAL_A, device_id="DEV1")],
    )

    candidate = _candidates(plan)["cloud-enriched"]
    # Enrichment, not a second device: the candidate is recognized as the
    # configured inverter's own connection rather than offered as new.
    assert candidate["state"] == "active"
    assert candidate["current_ref"] == "item-1"


def test_another_broker_scope_is_an_alternative_not_the_active_connection():
    plan = _plan(
        {"mqtt_selections": [_selection("local-a", origin="manual")]},
        proposals=[
            _cloud("local-a", device_id="DEV1", broker="broker-a"),
            _cloud("local-b", device_id="DEV1", broker="broker-b"),
        ],
    )

    assert _candidates(plan)["local-a"]["state"] == "active"
    # A different broker scope is a different route: never silently "already
    # configured", and never merged into the active one.
    assert _candidates(plan)["local-b"]["state"] in ("alternative", "new")


# --- the caller's own handle for a card --------------------------------------
def test_an_observation_without_an_issued_id_is_still_addressable():
    """Operations must name something the caller can resolve.

    A discovery payload that arrived without an ``observation_id`` still has to
    be adoptable and classifiable, or the plan would describe cards the browser
    cannot find and both would silently do nothing.
    """

    plan = _plan(
        {},
        observations=[dict(_observation(), observation_ref="card-7")],
    )

    adopted = plan["operations"]["adopt_observations"]
    assert [entry["observation_ref"] for entry in adopted] == ["card-7"]
    assert [entry["observation_ref"] for entry in plan["observations"]] == ["card-7"]
    assert [entry["id"] for entry in plan["candidates"]] == ["card-7"]
    # The issued id is still there — it is simply not what names the card.
    assert plan["observations"][0]["observation_id"].startswith("obs:v1:")


def test_a_manual_broker_choice_survives_the_next_plan():
    """Two brokers on one source: the selection's own proposal wins."""

    first = _cloud("local-b1", serial=SERIAL_A, device_id="DEV1", broker="broker-a")
    second = _cloud("local-b2", serial=SERIAL_A, device_id="DEV1", broker="broker-b")
    plan = _plan(
        {"mqtt_selections": [_selection("local-b2", origin="manual")]},
        observations=[_observation()],
        proposals=[first, second],
        priority=MQTT_FIRST,
    )

    assert plan["operations"]["drop_mqtt_selections"] == []
    assert plan["operations"]["select_mqtt_proposals"] == []


def test_a_server_served_observation_is_addressed_by_its_issued_id():
    """A caller that supplies no handle keys its cards on the issued id."""

    plan = _plan({}, observations=[_observation()])

    issued = plan["observations"][0]["observation_id"]
    assert plan["observations"][0]["observation_ref"] == issued
    assert [op["observation_ref"] for op in plan["operations"]["adopt_observations"]] == [
        issued
    ]
