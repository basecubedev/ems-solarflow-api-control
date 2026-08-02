# SPDX-License-Identifier: AGPL-3.0-or-later
"""Legacy Setup state is rehydrated by the backend, never re-matched in the browser.

A Setup draft persisted by an earlier release carries no issued identity at all:
its ``source_id`` is the old ``<api_family>:<serial>`` key, its dismissals are
bare serials and its MQTT selections have no opaque tokens. Once the browser
stops comparing serials, hosts and route ids, nothing in the browser can relate
such an entry to a current observation — so the backend has to, from the fields
the entry already persists.

``admin.setup_planner`` is that boundary, and it treats those fields as *lookup
hints only*. A hint is matched against the current trusted candidates and, on
exactly one acceptable match, that candidate's already-issued ids are copied
onto the entry. A hint that matches nothing, or several things, never mints a
physical identity of its own: a serial a browser persisted is the browser's
word, and an HMAC over it would only make the browser's word unforgeable, not
true.
"""

import pytest

from admin.setup_planner import build_setup_plan

pytestmark = pytest.mark.simulation

KEY = b"setup-planner-identity-migration-key-0123456789"

SERIAL_A = "EOD1AAA111"
SERIAL_B = "EOD1BBB222"
MASKED_SERIAL = "••••1111"


def _observation(**overrides):
    device = {
        "role_suggestion": "inverter",
        "device_type": "solarflow",
        "api_family": "solarflow",
        "ip": "10.0.0.11",
        "port": 8080,
        "serial_number": SERIAL_A,
        "verified": True,
        "usable_for_config": True,
    }
    device.update(overrides)
    return device


def _cloud_proposal(**overrides):
    proposal = {
        "id": "cloud-1",
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "device_id": "DEV1",
        "product_key": "PK1",
        "config_fragment": {
            "mqtt": {
                "source": "zendure_cloud_mqtt",
                "broker_ref": "zendure_cloud",
                "device_id": "DEV1",
                "product_key": "PK1",
            }
        },
    }
    proposal.update(overrides)
    return proposal


def _plan(state, *, observations=None, proposals=None, priority=None, **kwargs):
    return build_setup_plan(
        state,
        observations=list(observations or []),
        proposals=list(proposals or []),
        priority=list(priority or ["local_api", "local_mqtt", "zendure_mqtt"]),
        enabled_sources=kwargs.pop(
            "enabled_sources",
            {"local_api": True, "local_mqtt": True, "zendure_mqtt": True},
        ),
        identity_token_key=KEY,
        **kwargs,
    )


def _draft_map(plan):
    return {entry["draft_item_id"]: entry for entry in plan["draft_items"]}


# --- legacy draft shapes ----------------------------------------------------
def test_legacy_serial_derived_draft_is_mapped_to_the_current_observation():
    """The old ``<api_family>:<serial>`` source id resolves to an issued id."""

    observation = _observation()
    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + SERIAL_A,
                    "role": "inverter",
                    "serial_number": SERIAL_A,
                    "ip": "10.0.0.11",
                    "port": 8080,
                    "config_name": "INV_1",
                }
            ]
        },
        observations=[observation],
    )

    entry = _draft_map(plan)["item-1"]
    assert entry["observation_id"] == plan["observations"][0]["observation_id"]
    assert entry["observation_id"].startswith("obs:v1:")
    assert entry["physical_device_id"].startswith("opaque:v1:")
    assert entry["connection_id"].startswith("conn:v1:")
    assert entry["identity_status"] == "confirmed"
    assert entry["unresolved"] is False
    # The legacy entry maps onto the live observation instead of appearing next
    # to it: exactly one inverter, not a duplicate.
    assert plan["operations"]["adopt_observations"] == []


def test_legacy_draft_never_merges_with_a_different_serial():
    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + SERIAL_A,
                    "role": "inverter",
                    "serial_number": SERIAL_A,
                    "ip": "10.0.0.11",
                }
            ]
        },
        observations=[_observation(serial_number=SERIAL_B, ip="10.0.0.12")],
    )

    entry = _draft_map(plan)["item-1"]
    live = plan["observations"][0]
    assert entry["physical_device_id"] != live["physical_device_id"]
    assert len(plan["groups"]) == 2


# --- legacy hints are hints ---------------------------------------------------
def test_a_legacy_hint_without_a_trusted_match_mints_no_physical_identity():
    """Nothing current confirms this serial, so nothing is concluded from it."""

    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + SERIAL_A,
                    "role": "inverter",
                    "serial_number": SERIAL_A,
                    "ip": "10.0.0.11",
                    "port": 8080,
                    "config_name": "INV_1",
                }
            ]
        }
    )

    entry = _draft_map(plan)["item-1"]
    assert entry["physical_device_id"] is None
    assert entry["connection_id"] is None
    assert entry["identity_status"] == "unresolved"
    assert entry["unresolved"] is True
    assert entry["legacy_match"] == "unmatched"
    # Preserved, not dropped and not merged into anything.
    assert entry["draft_item_id"] == "item-1"
    assert plan["operations"]["drop_draft_items"] == []
    assert any(
        warning["code"] == "legacy_state_unresolved" for warning in plan["warnings"]
    )


def test_a_legacy_hint_with_one_trusted_match_rehydrates_the_issued_ids():
    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + SERIAL_A,
                    "role": "inverter",
                    "serial_number": SERIAL_A,
                    "ip": "10.0.0.11",
                    "port": 8080,
                }
            ]
        },
        observations=[_observation()],
    )

    entry = _draft_map(plan)["item-1"]
    live = plan["observations"][0]
    assert entry["legacy_match"] == "matched"
    assert entry["physical_device_id"] == live["physical_device_id"]
    assert entry["connection_id"] == live["connection_id"]
    assert entry["observation_id"] == live["observation_id"]


def test_a_legacy_hint_matching_two_trusted_candidates_stays_ambiguous():
    """One serial, two current routes: the entry may not pick one of them."""

    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "serial_number": SERIAL_A,
                }
            ]
        },
        observations=[_observation(), _observation(ip="10.0.0.12")],
    )

    entry = _draft_map(plan)["item-1"]
    assert entry["legacy_match"] == "ambiguous"
    assert entry["physical_device_id"] is None
    assert entry["unresolved"] is True
    assert plan["operations"]["drop_draft_items"] == []
    assert any(
        warning["code"] == "legacy_state_ambiguous" for warning in plan["warnings"]
    )


def test_a_masked_legacy_hint_never_produces_physical_identity():
    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "serial_number": MASKED_SERIAL,
                    "ip": "10.0.0.11",
                    "port": 8080,
                }
            ]
        },
        observations=[_observation(serial_number=MASKED_SERIAL)],
    )

    entry = _draft_map(plan)["item-1"]
    # The endpoint still matches (a route is a route), but the trusted candidate
    # has no physical identity to hand over, so neither does the entry.
    assert entry["physical_device_id"] is None
    assert plan["observations"][0]["physical_device_id"] is None


def test_legacy_rehydration_is_idempotent_for_an_unmatched_hint():
    state = {
        "draft_items": [
            {
                "draft_item_id": "item-1",
                "role": "inverter",
                "serial_number": SERIAL_A,
                "ip": "10.0.0.11",
                "port": 8080,
            }
        ]
    }
    first = _plan(state)
    second = _plan(state)

    assert first["draft_items"] == second["draft_items"]
    assert first["operations"] == second["operations"]
    assert first["plan_id"] == second["plan_id"]


def test_legacy_masked_serial_draft_stays_unresolved_and_distinct():
    """A redaction placeholder is never physical identity, in any layer."""

    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + MASKED_SERIAL,
                    "role": "inverter",
                    "serial_number": MASKED_SERIAL,
                    "ip": "10.0.0.11",
                },
                {
                    "draft_item_id": "item-2",
                    "source_id": "solarflow:" + MASKED_SERIAL,
                    "role": "inverter",
                    "serial_number": MASKED_SERIAL,
                    "ip": "10.0.0.12",
                },
            ]
        }
    )

    first, second = _draft_map(plan)["item-1"], _draft_map(plan)["item-2"]
    assert first["physical_device_id"] is None
    assert second["physical_device_id"] is None
    assert first["identity_status"] == "unresolved"
    # Two rows stay two rows because they are two rows — never because a
    # placeholder serial was turned into two different identities.
    assert first["connection_id"] is None
    assert second["connection_id"] is None
    assert len(plan["groups"]) == 2
    assert plan["operations"]["drop_draft_items"] == []


def test_serialless_cloud_draft_rehydrates_from_its_trusted_scoped_route():
    """No serial anywhere: the scoped route is what the trusted proposal proves."""

    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "connection_source": "zendure_cloud_mqtt",
                    "mqtt": {
                        "source": "zendure_cloud_mqtt",
                        "broker_ref": "zendure_cloud",
                        "device_id": "DEV1",
                        "product_key": "PK1",
                    },
                }
            ]
        },
        proposals=[_cloud_proposal()],
    )

    entry = _draft_map(plan)["item-1"]
    assert entry["legacy_match"] == "matched"
    assert entry["physical_device_id"] == plan["proposals"][0]["physical_device_id"]
    assert entry["identity_status"] == "probable"
    assert entry["unresolved"] is False


def test_serialless_cloud_draft_without_a_trusted_route_stays_unresolved():
    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "connection_source": "zendure_cloud_mqtt",
                    "mqtt": {
                        "source": "zendure_cloud_mqtt",
                        "broker_ref": "zendure_cloud",
                        "device_id": "DEV1",
                        "product_key": "PK1",
                    },
                }
            ]
        }
    )

    entry = _draft_map(plan)["item-1"]
    assert entry["physical_device_id"] is None
    assert entry["legacy_match"] == "unmatched"
    assert entry["unresolved"] is True


def test_rehydration_is_idempotent():
    state = {
        "draft_items": [
            {
                "draft_item_id": "item-1",
                "source_id": "solarflow:" + SERIAL_A,
                "role": "inverter",
                "serial_number": SERIAL_A,
                "ip": "10.0.0.11",
                "port": 8080,
            }
        ]
    }
    first = _plan(state, observations=[_observation()])
    migrated = {
        "draft_items": [
            dict(state["draft_items"][0], **{
                key: first["draft_items"][0][key]
                for key in ("observation_id", "connection_id", "physical_device_id")
            })
        ]
    }
    second = _plan(migrated, observations=[_observation()])

    assert second["draft_items"] == first["draft_items"]
    assert second["operations"] == first["operations"]
    assert second["plan_id"] == first["plan_id"]


def test_plan_never_returns_raw_identity_evidence():
    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "serial_number": SERIAL_A,
                    "ip": "10.0.0.11",
                }
            ]
        },
        observations=[_observation()],
        proposals=[_cloud_proposal()],
    )

    encoded = repr(plan)
    for secret in (SERIAL_A, SERIAL_A.lower(), "10.0.0.11", "DEV1", "PK1"):
        assert secret not in encoded, f"{secret!r} leaked into the browser plan"


# --- legacy MQTT selections -------------------------------------------------
def test_legacy_mqtt_selection_without_tokens_keeps_its_connection():
    proposal = _cloud_proposal()
    plan = _plan(
        {
            "mqtt_selections": [
                {
                    "id": "cloud-1",
                    "connection_source": "zendure_cloud_mqtt",
                    "broker_ref": "zendure_cloud",
                    "selection_origin": "manual",
                }
            ]
        },
        proposals=[proposal],
    )

    entry = plan["mqtt_selections"][0]
    assert entry["id"] == "cloud-1"
    assert entry["connection_id"].startswith("conn:v1:")
    assert entry["physical_device_id"].startswith("opaque:v1:")
    assert plan["operations"]["drop_mqtt_selections"] == []


def test_legacy_mqtt_selection_for_a_vanished_proposal_is_reported_unresolved():
    plan = _plan(
        {
            "mqtt_selections": [
                {
                    "id": "cloud-gone",
                    "connection_source": "zendure_cloud_mqtt",
                    "broker_ref": "zendure_cloud",
                    "selection_origin": "manual",
                }
            ]
        },
        proposals=[_cloud_proposal()],
    )

    entry = plan["mqtt_selections"][0]
    assert entry["unresolved"] is True
    # Fail closed: an entry the backend cannot place is preserved, never dropped.
    assert plan["operations"]["drop_mqtt_selections"] == []
    assert plan["warnings"]


# --- legacy dismissals ------------------------------------------------------
def test_legacy_bare_serial_dismissal_becomes_an_issued_physical_dismissal():
    plan = _plan(
        {"physical_dismissals": [SERIAL_A]},
        observations=[_observation()],
    )

    mapping = plan["dismissals"]["physical"]
    assert len(mapping) == 1
    assert mapping[0]["physical_device_id"].startswith("opaque:v1:")
    assert "serial" not in repr(mapping)
    assert SERIAL_A not in repr(mapping)


def test_legacy_dismissal_does_not_dismiss_an_unrelated_same_name_device():
    plan = _plan(
        {"physical_dismissals": [SERIAL_A]},
        observations=[
            _observation(display_name="Solarflow"),
            _observation(serial_number=SERIAL_B, ip="10.0.0.12", display_name="Solarflow"),
        ],
    )

    dismissed = {entry["physical_device_id"] for entry in plan["dismissals"]["physical"]}
    other = next(
        obs
        for obs in plan["observations"]
        if obs["observation_id"] != plan["observations"][0]["observation_id"]
    )
    assert other["physical_device_id"] not in dismissed


def test_legacy_observation_dismissal_maps_only_to_its_own_observation():
    plan = _plan(
        {"observation_dismissals": ["solarflow:" + SERIAL_A]},
        observations=[_observation(), _observation(serial_number=SERIAL_B, ip="10.0.0.12")],
    )

    mapped = plan["dismissals"]["observations"]
    assert len(mapped) == 1
    assert mapped[0]["observation_id"] == plan["observations"][0]["observation_id"]


def test_unmappable_dismissal_is_preserved_as_unresolved():
    plan = _plan({"physical_dismissals": [MASKED_SERIAL]})

    assert plan["dismissals"]["physical"] == []
    assert MASKED_SERIAL in [entry["value"] for entry in plan["dismissals"]["unresolved"]]


def test_a_bare_serial_dismissal_without_a_trusted_match_hides_nothing():
    """An arbitrary serial is a hint; it may not become a device-wide dismissal."""

    plan = _plan(
        {"physical_dismissals": [SERIAL_A]},
        observations=[_observation(serial_number=SERIAL_B, ip="10.0.0.12")],
    )

    assert plan["dismissals"]["physical"] == []
    unresolved = plan["dismissals"]["unresolved"]
    assert {"value": SERIAL_A, "scope": "physical"} in unresolved
    # The unrelated current device stays offered.
    assert len(plan["operations"]["adopt_observations"]) == 1


def test_a_bare_serial_dismissal_matching_two_trusted_candidates_stays_unresolved():
    plan = _plan(
        {"physical_dismissals": [SERIAL_A]},
        observations=[_observation(), _observation(ip="10.0.0.12")],
    )

    assert plan["dismissals"]["physical"] == []
    assert {"value": SERIAL_A, "scope": "physical"} in plan["dismissals"]["unresolved"]


def test_an_issued_physical_dismissal_survives_without_a_current_candidate():
    """Already-issued tokens are the browser's migrated store, not a hint."""

    issued = _plan({}, observations=[_observation()])["observations"][0][
        "physical_device_id"
    ]
    plan = _plan({"physical_dismissals": [issued]})

    assert plan["dismissals"]["physical"] == [{"physical_device_id": issued}]


# --- schema versioning ------------------------------------------------------
def test_plan_declares_the_identity_schema_version_it_speaks():
    plan = _plan({})
    assert plan["identity_schema_version"] >= 1


# --- serial-less and unresolved --------------------------------------------
def test_a_cloud_device_with_an_incomplete_route_stays_unresolved():
    """Without a device id there is no scoped identity — and none is invented."""

    plan = _plan(
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "connection_source": "zendure_cloud_mqtt",
                    "mqtt": {"source": "zendure_cloud_mqtt", "broker_ref": "zendure_cloud"},
                }
            ]
        }
    )

    entry = _draft_map(plan)["item-1"]
    assert entry["physical_device_id"] is None
    assert entry["identity_status"] == "unresolved"
    assert entry["unresolved"] is True
    assert plan["operations"]["drop_draft_items"] == []


def test_a_legacy_draft_without_identity_evidence_is_preserved_and_distinct():
    plan = _plan(
        {
            "draft_items": [
                {"draft_item_id": "item-1", "role": "inverter", "config_name": "INV_1"},
                {"draft_item_id": "item-2", "role": "inverter", "config_name": "INV_2"},
            ]
        },
        observations=[_observation()],
    )

    entries = _draft_map(plan)
    assert entries["item-1"]["unresolved"] is True
    assert entries["item-2"]["unresolved"] is True
    # Nothing merges them into each other or into the live observation, and
    # nothing is dropped: unknown identity is non-destructive.
    assert len(plan["groups"]) == 3
    assert plan["operations"]["drop_draft_items"] == []


def test_two_unresolved_observations_from_different_endpoints_stay_apart():
    plan = _plan(
        {},
        observations=[
            _observation(serial_number=MASKED_SERIAL, ip="10.0.0.21"),
            _observation(serial_number=MASKED_SERIAL, ip="10.0.0.22"),
        ],
    )

    first, second = plan["observations"]
    assert first["observation_id"] != second["observation_id"]
    assert first["physical_device_id"] is None
    assert second["physical_device_id"] is None
    assert len(plan["groups"]) == 2
    # Both are still offered: unresolved identity is not a reason to hide a card.
    assert len(plan["operations"]["adopt_observations"]) == 2
