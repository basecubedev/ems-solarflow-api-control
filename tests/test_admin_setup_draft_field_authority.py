# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every editable Setup field belongs to exactly one authority model.

The device plan decides which hardware is configured and how it is reached. It
deliberately does *not* decide what the operator calls it, whether it is
included, or which catalog values it carries — those are other decisions, with
other owners. That split is only safe while it is total: a field with two owners
is one neither can actually hold, and a field with none is a browser value that
reaches ``config/config.json`` without ever being authorized.

Setup submits two editable collections, and they are authorized differently:

``devices[]``
    The draft the browser owns between plans. Classified field by field in
    :data:`admin.setup_planner.DRAFT_FIELD_AUTHORITY`, pinned below against both
    the fields the browser builds and the fields the config-producing modules
    read.

``zendure_mqtt_proposals[]``
    Not classified field by field, because nothing the browser sends survives:
    each selection is a *lookup key* resolved back to the trusted stored
    proposal, and the resolved record is a copy of that proposal. Only the
    validated ``replace_grid_meter`` decision is taken from the request, which
    is proven below rather than assumed.

See ``docs/developer/developer.md`` — "Setup draft field authority".
"""

import copy
import pathlib
import re

import pytest

from admin.setup_planner import (
    AUTHORITY_CATALOG_MUTATION,
    AUTHORITY_DEVICE_PLAN,
    AUTHORITY_EXACT_PREVIEW,
    AUTHORITY_PRESENTATION,
    DRAFT_FIELD_AUTHORITY,
    _DRAFT_IDENTITY_FIELDS,
    draft_field_authority,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]

REPO = pathlib.Path(__file__).resolve().parents[1]
ADMIN_JS = REPO / "admin" / "static" / "admin.js"

# Every place the browser builds or mutates an entry of ``configDraftItems``.
# Named rather than discovered: a new construction site has to be added here,
# which is exactly the review this contract exists to force.
DRAFT_CONSTRUCTION_SITES = (
    "function draftItemFromDevice(",
    "function addDeviceToDraft(",
    "function selectGridMeter(",
    "function applyConnectionSwitch(",
    "function applySetupPlanOperations(",
    "function autoSelectGridMeter(",
    "function addManualDevice(",
    "function updateDraftDeviceField(",
)

_LITERAL_KEY = re.compile(r"^\s{4,}([a-z][a-z0-9_]*):", re.MULTILINE)
# Draft entries are built and mutated as ``item``; an MQTT selection in the same
# function is ``entry`` and is authorized by server-side re-resolution instead.
_ASSIGNED_KEY = re.compile(r"\bitem\.([a-z][a-z0-9_]*)\s*=[^=]")

# Names that appear inside those functions but are not draft-entry fields:
# arguments of neighbouring calls and keys of other payloads.
_NOT_DRAFT_FIELDS = frozenset({"target"})


def _function_body(source, anchor):
    start = source.index(anchor)
    end = source.find("\nfunction ", start + len(anchor))
    return source[start : end if end > 0 else len(source)]


def _browser_draft_fields():
    source = ADMIN_JS.read_text(encoding="utf-8")
    fields = set()
    for anchor in DRAFT_CONSTRUCTION_SITES:
        assert anchor in source, f"draft construction site renamed: {anchor}"
        body = _function_body(source, anchor)
        fields.update(_LITERAL_KEY.findall(body))
        fields.update(_ASSIGNED_KEY.findall(body))
    return fields - _NOT_DRAFT_FIELDS


# --- the draft the browser owns ----------------------------------------------
def test_every_field_the_browser_builds_into_a_draft_is_classified():
    unclassified = sorted(
        field
        for field in _browser_draft_fields()
        if draft_field_authority(field) is None
    )
    assert unclassified == [], (
        "these Setup draft fields belong to no authority model: "
        + ", ".join(unclassified)
    )


def test_the_classification_carries_no_field_the_browser_stopped_sending():
    """Drift in the other direction: a removed field must leave the map too."""

    stale = sorted(set(DRAFT_FIELD_AUTHORITY) - _browser_draft_fields())
    assert stale == [], "these classified fields are no longer built: " + ", ".join(
        stale
    )


def test_the_device_plan_owns_exactly_what_it_compares():
    """The plan's authority is its fingerprint, plus the handle it addresses."""

    owned = {
        field
        for field, authority in DRAFT_FIELD_AUTHORITY.items()
        if authority == AUTHORITY_DEVICE_PLAN
    }
    assert owned == set(_DRAFT_IDENTITY_FIELDS) | {"draft_item_id"}


def test_every_field_has_exactly_one_known_authority():
    known = {
        AUTHORITY_DEVICE_PLAN,
        AUTHORITY_CATALOG_MUTATION,
        AUTHORITY_EXACT_PREVIEW,
        AUTHORITY_PRESENTATION,
    }
    unknown = sorted(
        f"{field}={authority}"
        for field, authority in DRAFT_FIELD_AUTHORITY.items()
        if authority not in known
    )
    assert unknown == []


def test_catalog_values_travel_only_inside_config_values():
    """No catalog device value is also a top-level draft field.

    Otherwise the same value would have two writers — the shared catalog
    mutation and a per-field draft path — and only one of them would be coerced.
    """

    from admin.device_common_fields import common_device_value_fields

    assert DRAFT_FIELD_AUTHORITY["config_values"] == AUTHORITY_CATALOG_MUTATION
    assert not set(common_device_value_fields()) & set(DRAFT_FIELD_AUTHORITY)


def test_presentation_fields_never_reach_the_generated_config():
    """"Presentation" is only an honest category while it is provably inert."""

    producers = (
        REPO / "admin" / "config_preview.py",
        REPO / "admin" / "config_export.py",
        REPO / "ems" / "config_mutation.py",
    )
    sources = {path.name: path.read_text(encoding="utf-8") for path in producers}
    offenders = [
        f"{name}:{field}"
        for field, authority in DRAFT_FIELD_AUTHORITY.items()
        if authority == AUTHORITY_PRESENTATION
        for name, source in sources.items()
        if f'"{field}"' in source
    ]
    assert offenders == [], (
        "these fields are classified as presentation but are read where the "
        "config is produced: " + ", ".join(sorted(offenders))
    )


def test_exact_preview_fields_are_bound_from_review_to_apply():
    """Category B fields that are not catalog values still change the bytes.

    They are authorized by the exact preview fingerprint, which covers the whole
    submitted draft — so changing one between review and apply is a preview
    mismatch, not a silently accepted edit.
    """

    from admin.guided_setup_workflow import setup_mutation_fingerprint

    def fingerprint(draft):
        return setup_mutation_fingerprint(
            draft=draft,
            supported_grid_meter_count=0,
            features=None,
            zendure_mqtt_proposals=None,
            zendure_mqtt_broker=None,
            zendure_mqtt_manual_devices=None,
            device_plan_id="plan:v1:a",
        )

    base = {
        "role": "inverter",
        "config_name": "WR1",
        "display_name": "Balcony",
        "enabled": True,
        "config_values": {"max_power": 800},
        "ip": "10.0.0.11",
    }
    reviewed = fingerprint([base])
    for field, authority in DRAFT_FIELD_AUTHORITY.items():
        if authority not in (AUTHORITY_EXACT_PREVIEW, AUTHORITY_CATALOG_MUTATION):
            continue
        moved = dict(base)
        moved[field] = False if field == "enabled" else "moved"
        assert fingerprint([moved]) != reviewed, field


# --- the MQTT selections the server re-resolves -------------------------------
def _trusted_proposal():
    return {
        "id": "zendure_mqtt:AAA111",
        "broker_ref": "broker-a",
        "target": "device",
        "connection_source": "local_mqtt",
        "serial_number": "AAA111",
        "config_fragment": {"type": "zendure_mqtt", "mqtt": {"device_id": "AAA111"}},
    }


def test_a_selection_carries_no_authority_of_its_own():
    """Only the resolved trusted proposal reaches config, plus one decision.

    A selection is a lookup key. Every operator-facing and evidence-bearing
    field the browser puts beside it is dropped, so an unclassified selection
    field cannot exist: there is nothing for it to fall through into.
    """

    from admin.zendure_mqtt_config_proposals import resolve_selected_proposals

    trusted = _trusted_proposal()
    submitted = {
        "id": trusted["id"],
        "broker_ref": trusted["broker_ref"],
        "target": "device",
        "replace_grid_meter": True,
        # None of these may survive: operator intent, browser bookkeeping and
        # echoed discovery evidence alike.
        "config_name": "attacker",
        "display_name": "attacker",
        "enabled": False,
        "selection_origin": "manual",
        "serial_number": "BBB222",
        "connection_source": "zendure_cloud_mqtt",
        "config_fragment": {"type": "zendure_mqtt", "mqtt": {"device_id": "BBB222"}},
    }

    resolved, errors = resolve_selected_proposals([submitted], [copy.deepcopy(trusted)])

    assert errors == []
    assert resolved == [dict(copy.deepcopy(trusted), replace_grid_meter=True)]


def test_a_selection_the_server_does_not_offer_is_refused():
    """The lookup itself is the authority: an unknown key resolves to nothing."""

    from admin.zendure_mqtt_config_proposals import resolve_selected_proposals

    resolved, errors = resolve_selected_proposals(
        [{"id": "zendure_mqtt:FORGED", "broker_ref": "broker-a"}],
        [_trusted_proposal()],
    )

    assert resolved == []
    assert errors and errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_the_plan_binds_the_connection_a_selection_names():
    """``id``/``broker_ref`` are the device plan's half of a selection."""

    from admin.setup_planner import selection_projection

    trusted = _trusted_proposal()
    projection = selection_projection(trusted)
    assert projection == "zendure_mqtt:AAA111|broker-a"
    assert selection_projection(dict(trusted, broker_ref="broker-b")) != projection
    # Everything else about a selection is the server's own record, so it
    # cannot move the plan's view of which connection was chosen.
    assert selection_projection(dict(trusted, serial_number="BBB222")) == projection
