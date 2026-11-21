# SPDX-FileCopyrightText: 2026 basecubedev
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
    the fields the Admin console builds (``frontend/admin-modern/src/lib/setupDraft.js``
    and ``state/setupDraft.svelte.js``) and the fields the config-producing
    modules read.

``zendure_mqtt_proposals[]``
    Not classified field by field, because nothing the browser sends survives:
    each selection is a *lookup key* resolved back to the trusted stored
    proposal, and the resolved record is a copy of that proposal. Only the
    validated ``replace_grid_meter`` decision is taken from the request, which
    is proven below rather than assumed.

See ``docs/developer/developer.md`` — "Setup draft field authority".
"""

import copy
import functools
import json
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
from tests.modern_frontend import js_export, js_run

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]

REPO = pathlib.Path(__file__).resolve().parents[1]
CONSOLE = REPO / "frontend" / "admin-modern" / "src"
SETUP_DRAFT_JS = CONSOLE / "lib" / "setupDraft.js"
SETUP_DRAFT_STATE = CONSOLE / "state" / "setupDraft.svelte.js"

_ITEM_LITERAL = re.compile(r"\{\s*\.\.\.(?:item|grid|entry|meter)\s*,|\{\s*\.\.\.itemFromDevice\(")
_ITEM_ASSIGNMENT = re.compile(r"\b(?:item|grid|meter)\.([a-z][a-z0-9_]*)\s*=(?!=)")
_EXPORTED_FUNCTION = re.compile(r"^export (?:async )?function (\w+)\(", re.MULTILINE)
_CLOSING = {"{": "}", "(": ")", "[": "]"}


def _draft_authority(field):
    """The planner's authority for a draft field, or ``None``."""

    return draft_field_authority(field)


def _all_classified():
    return dict(DRAFT_FIELD_AUTHORITY)


def _literal_parts(source, start):
    """The top-level comma-separated parts of the object literal opening at ``start``."""

    parts, depth, quote, part = [], 0, None, start + 1
    position = start
    while position < len(source):
        char = source[position]
        if quote:
            if char == "\\":
                position += 1
            elif char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in _CLOSING:
            depth += 1
        elif char in _CLOSING.values():
            depth -= 1
            if depth == 0:
                parts.append(source[part:position])
                return [chunk.strip() for chunk in parts if chunk.strip()]
        elif char == "," and depth == 1:
            parts.append(source[part:position])
            part = position + 1
        position += 1
    raise AssertionError("unbalanced object literal in the Setup draft state")


def _endpoint_keys():
    """The fields the console's components hand to ``setGridEndpoint`` by name."""

    components = "\n".join(path.read_text(encoding="utf-8") for path in sorted(CONSOLE.rglob("*.svelte")))
    keys = re.findall(r'setGridEndpoint\("([a-z_]+)"', components)
    assert keys and len(keys) == components.count("setGridEndpoint("), "setGridEndpoint is called with a key the walk cannot read"
    return set(keys)


def _state_fields():
    """Every field the Setup draft state adds to a draft entry, read from its source.

    A literal that starts from an entry (``{ ...item, ... }``, ``{ ...grid, ... }``,
    ``{ ...itemFromDevice(...), ... }``) or an assignment to one names its keys;
    a computed key is resolved only where the function that writes it is known.
    """

    bridge_keys = js_export(SETUP_DRAFT_JS, "BRIDGE_KEYS")
    if bridge_keys is None:
        pytest.skip("node is required to read the Setup draft's bridge keys")
    computed = {
        ("setGridFeature", "bridgeKey"): set(bridge_keys),
        ("setGridEndpoint", "key"): _endpoint_keys(),
    }
    source = SETUP_DRAFT_STATE.read_text(encoding="utf-8")
    functions = [(match.start(), match.group(1)) for match in _EXPORTED_FUNCTION.finditer(source)]
    fields = set(_ITEM_ASSIGNMENT.findall(source))
    for match in _ITEM_LITERAL.finditer(source):
        owner = next((name for start, name in reversed(functions) if start < match.start()), "")
        for part in _literal_parts(source, match.start()):
            if part.startswith("..."):
                continue
            if part.startswith("["):
                expression = part[1 : part.index("]")]
                assert (owner, expression) in computed, f"{owner} sets a draft key the walk cannot follow: [{expression}]"
                fields |= computed[(owner, expression)]
                continue
            key = part.split(":", 1)[0].strip().strip("\"'")
            assert re.fullmatch(r"[a-z][a-z0-9_]*", key), f"{owner} sets a draft key the walk cannot read: {part}"
            fields.add(key)
    return fields


def _constructed_fields():
    """Every field the console's own draft constructors put on an entry, run under Node."""

    from admin.setup_config import build_setup_catalog

    return set(
        js_run(
            SETUP_DRAFT_JS,
            f"""
const catalog = {json.dumps(build_setup_catalog())};
const built = new Set();
const take = (item) => {{ for (const key of Object.keys(item || {{}})) built.add(key); return item; }};
const device = {{ observation_id: "obs:v1:a", ip: "192.0.2.10", port: 80, serial_number: "SN1",
  physical_identity_token: "opaque:v1:a", device_type: "zendure", api_family: "zendure",
  display_name: "Inverter", sources: ["active_scan"] }};
const bridge = {{ observation_id: "obs:v1:b", ip: "192.0.2.20", port: 80, serial_number: "M1",
  device_type: m.BRIDGE_TYPE, api_family: m.BRIDGE_TYPE, credentials_ref: "bridge-1", node_id: 1,
  unit_id: 1, sources: ["mdns"] }};
const inverter = take(m.itemFromDevice(device, "inverter", {{ id: "item-1", items: [], configName: "INV_1" }}));
const grid = take(m.itemFromDevice(bridge, "grid_meter", {{ id: "item-2", items: [] }}));
take(m.manualItem(m.emptyDraft(), catalog, {{ role: "inverter", host: "192.0.2.30" }}, "item-3").item);
take(m.manualItem(m.emptyDraft(), catalog, {{ role: "grid_meter", host: "192.0.2.31" }}, "item-4").item);
const devices = (catalog.sections || []).find((section) => section.id === "devices");
for (const field of devices ? devices.fields : []) take(m.setDeviceField(inverter, field, 1));
for (const type of [m.D0_TYPE, m.BRIDGE_TYPE]) take(m.syncGridFeatures({{}}, {{ ...grid, grid_meter_type: type }}).item);
const draft = {{ ...m.emptyDraft(), items: [{{ ...inverter, auto_added: false }}, grid] }};
m.reassertPriority(draft).items.forEach(take);
m.withItemIds([{{ role: "inverter" }}]).forEach(take);
const plan = {{ plan_id: "plan:v1:p", generation: "g1", operations: {{ adopt_observations: [{{ observation_ref: "obs:v1:c" }}] }} }};
const available = new Map([["obs:v1:c", {{ ...device, observation_id: "obs:v1:c", ip: "192.0.2.40" }}]]);
const adopted = m.applyOperations(draft, plan, plan, {{ proposals: [], available, names: new Map(), newId: () => "item-5" }});
adopted.draft.items.forEach(take);
m.previewBody(adopted.draft, {{ workflowId: "w", planId: "plan:v1:p", supportedMeters: 1, catalog }}).devices.forEach(take);
process.stdout.write(JSON.stringify([...built]));
""",
        )
    )


@functools.cache
def _browser_draft_fields():
    return _constructed_fields() | _state_fields()


# --- the draft the browser owns ----------------------------------------------
def test_every_field_the_browser_builds_into_a_draft_is_classified():
    unclassified = sorted(
        field
        for field in _browser_draft_fields()
        if _draft_authority(field) is None
    )
    assert unclassified == [], (
        "these Setup draft fields belong to no authority model: "
        + ", ".join(unclassified)
    )


def test_the_classification_carries_no_field_the_browser_stopped_sending():
    """Drift in the other direction: a removed field must leave the map too."""

    stale = sorted(set(_all_classified()) - _browser_draft_fields())
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
        for field, authority in _all_classified().items()
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
    assert not set(common_device_value_fields()) & set(_all_classified())


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
        for field, authority in _all_classified().items()
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
    for field, authority in _all_classified().items():
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
