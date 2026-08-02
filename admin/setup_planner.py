# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Setup's batch connection planning and legacy-state rehydration.

Maintenance edits one configured device at a time, so a single pairwise
question — may this candidate take over that device? — answers it, and
:mod:`admin.connection_planner` owns that answer. Setup asks a second,
*batch* question: given every observation and proposal from all three discovery
sources, which one connection per physical device is configured, and what has to
be dropped or selected to get there? That orchestration lives here.

Orchestration is all that lives here. Every domain answer is borrowed:

physical identity, conflict, route ambiguity
    :func:`admin.connection_planner.plan_connection_change`, which composes
    :mod:`ems.device_identity` — grouping asks the planner "are these two the
    same device?" rather than comparing evidence itself

public observation / connection / physical ids
    :mod:`admin.observation_identity`

keep / replace / add / block for one pair
    :func:`admin.connection_planner.plan_connection_change`

output-control capability
    resolved by the caller and passed in, never re-derived here

Two rules shape everything below.

**Trusted candidates are the only evidence.** ``observations`` and ``proposals``
are the server's own current discovery state. Persisted browser state — draft
items, MQTT selections, dismissals — is a *lookup hint*: it says which device an
entry believes it is about, and the answer is whatever the trusted candidates
say. A hint that matches exactly one of them inherits that candidate's already
issued ids; a hint that matches nothing or several things stays unresolved and
is preserved untouched. No issued id is ever minted from a hint, because signing
a value the browser supplied only makes the browser's word unforgeable.

**The verdict decides which operations are executable.** A transport switch is
emitted as an executable operation only when the canonical pairwise planner
allows it outright. A switch it wants confirmed is returned as a *proposal* with
a confirmation token; a switch it blocks is returned as nothing at all.

See ``docs/developer/developer.md``.
"""

from ems.device_identity import (
    STATUS_UNRESOLVED,
    is_masked_identity_value,
    opaque_plan_id,
    resolve_physical_identity,
)
from admin.connection_planner import (
    ACTION_ADD_AS_NEW_DEVICE,
    ACTION_KEEP_CURRENT,
    ACTION_REPLACE_WITH_CONFIRMATION,
    ACTION_USE_CANDIDATE,
    INTENT_REVIEW,
    INTENT_SWITCH_CONNECTION,
    ConnectionPlan,
    plan_connection_change,
)
from admin.observation_identity import resolve_identity_fields

# The identity contract the returned mappings speak. The browser stores it with
# its draft so a store written by an older release is recognizable without
# guessing at its field shapes.
IDENTITY_SCHEMA_VERSION = 1

SOURCE_LOCAL_API = "local_api"
SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_MQTT = "zendure_mqtt"
MQTT_SOURCES = (SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_MQTT)

ORIGIN_MANUAL = "manual"
ORIGIN_AUTOMATIC = "automatic"
ORIGIN_PRIORITY = "priority"
ORIGIN_NONE = "none"

# How a persisted entry related to the current trusted candidates.
MATCH_NONE = "none"
MATCH_MATCHED = "matched"
MATCH_UNMATCHED = "unmatched"
MATCH_AMBIGUOUS = "ambiguous"

# What may be done with a group's transition.
_MODE_EXECUTE = "execute"
_MODE_PROPOSE = "propose"
_MODE_REFUSE = "refuse"

_INVERTER_ROLE = "inverter"

# The caller's local handle for one discovered card. Not an identity: it names
# which card an operation is about, so a payload that arrived without an issued
# observation id is still addressable.
OBSERVATION_REF_FIELD = "observation_ref"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return str(value if value is not None else "").strip()


def mqtt_source_of(connection_source):
    """The user-facing transport of an MQTT connection source."""

    return (
        SOURCE_ZENDURE_MQTT
        if _text(connection_source) == "zendure_cloud_mqtt"
        else SOURCE_LOCAL_MQTT
    )


def legacy_observation_key(device):
    """The pre-``obs:v1`` browser collection key for one observation.

    Reproduced verbatim from the release that wrote it, so a persisted legacy
    dismissal or draft id can be matched against a current observation by
    recomputing the key rather than by parsing it. It is a *compatibility
    decoder* and never an identity: two devices that once collided under it
    still resolve to two different physical identities below.
    """

    if not isinstance(device, dict):
        return ""
    serial = _text(device.get("serial_number"))
    if serial:
        return _text(device.get("api_family") or "device") + ":" + serial
    identifier = _text(device.get("id"))
    if identifier:
        return identifier
    return ":".join(
        [
            _text(device.get("source") or "unknown"),
            _text(device.get("ip") or "unknown"),
            _text(device.get("port") or 80) or "80",
        ]
    )


class _Entry:
    """One thing that can belong to a physical device group.

    A *trusted* entry (observation, proposal) carries the server's own record
    and resolves its own identity. A *hint* entry (draft item, MQTT selection)
    carries what the browser persisted until it is anchored to a trusted entry,
    at which point it adopts that entry's payload and issued ids wholesale.
    """

    __slots__ = (
        "kind",
        "ref",
        "payload",
        "source",
        "origin",
        "identity",
        "identity_tokens",
        "connection_id",
        "observation_id",
        "legacy_match",
    )

    def __init__(self, kind, ref, payload, source, origin=ORIGIN_AUTOMATIC):
        self.kind = kind
        self.ref = ref
        self.payload = payload
        self.source = source
        self.origin = origin
        self.identity = None
        self.identity_tokens = ()
        self.connection_id = None
        self.observation_id = None
        self.legacy_match = MATCH_NONE

    @property
    def unresolved(self):
        return self.identity is None or self.identity.public_identity_id is None


def _resolve_trusted(entry, *, key, broker_sources, fallback):
    """Issue the public identity of a server-owned record."""

    entry.identity = resolve_physical_identity(
        entry.payload, broker_sources=broker_sources, token_key=key
    )
    issued = resolve_identity_fields(
        entry.payload, key=key, broker_sources=broker_sources, fallback=fallback
    )
    entry.connection_id = issued["connection_id"]
    # The observation id is a *collection label*: which card this is, not what
    # hardware it is. A payload that already carries a well-formed one keeps it,
    # so the operations reference the cards the browser is actually rendering.
    supplied = _text(entry.payload.get("observation_id"))
    entry.observation_id = (
        supplied if supplied.startswith("obs:v1:") else issued["observation_id"]
    )
    entry.identity_tokens = tuple(issued["physical_identity_alias_tokens"] or ())
    return entry


def _adopt(entry, anchor):
    """Give a hint the trusted candidate's payload and issued ids."""

    entry.payload = anchor.payload
    entry.identity = anchor.identity
    entry.identity_tokens = anchor.identity_tokens
    entry.connection_id = anchor.connection_id
    entry.observation_id = anchor.observation_id
    entry.legacy_match = MATCH_MATCHED
    return entry


# --- physical grouping -------------------------------------------------------
#
# Grouping never compares identity evidence itself: it asks the canonical
# pairwise planner "are these two the same physical device?" and unions on that
# answer. An entry that would bridge two mutually contradictory groups keeps its
# own group instead of uniting them (fail closed).
class _Relations:
    """Memoized pairwise planner verdicts for one planning pass."""

    def __init__(self, *, identity_token_key, broker_sources):
        self._key = identity_token_key
        self._sources = broker_sources
        self._cache = {}

    def plan(self, left, right):
        cache_key = (id(left), id(right))
        plan = self._cache.get(cache_key)
        if plan is None:
            plan = plan_connection_change(
                current_device=left.payload,
                candidate=right.payload,
                intent=INTENT_REVIEW,
                identity_token_key=self._key,
                broker_sources=self._sources,
            )
            self._cache[cache_key] = plan
        return plan

    def same_device(self, left, right):
        return self.plan(left, right).same_physical_device

    def conflict(self, left, right):
        return self.plan(left, right).identity_conflict

    def same_route(self, left, right):
        plan = self.plan(left, right)
        return (
            plan.current_connection_id is not None
            and plan.current_connection_id == plan.candidate_connection_id
        )


def _group_entries(entries, relations):
    groups = []
    for entry in entries:
        matches = [
            group
            for group in groups
            if any(relations.same_device(member, entry) for member in group)
        ]
        if not matches:
            groups.append([entry])
            continue
        bridged = any(
            any(
                relations.conflict(member, other)
                for member in matches[i]
                for other in matches[j]
            )
            for i in range(len(matches))
            for j in range(i + 1, len(matches))
        )
        if bridged:
            groups.append([entry])
            continue
        primary = matches[0]
        primary.append(entry)
        for absorbed in matches[1:]:
            primary.extend(absorbed)
            groups.remove(absorbed)
    return groups


# --- legacy hints ------------------------------------------------------------
def _anchor_hint(entry, trusted, relations):
    """The one trusted candidate a persisted entry is about, or none.

    The persisted fields say which device the entry believes it is; the trusted
    candidates say what is actually there. Only an unambiguous answer resolves:
    one physical identity among the matches, and one connection within it —
    otherwise the entry stays exactly as the browser stored it.
    """

    matches = [
        candidate for candidate in trusted if relations.same_device(entry, candidate)
    ]
    if not matches:
        entry.legacy_match = MATCH_UNMATCHED
        return None
    identities = {
        candidate.identity.public_identity_id
        for candidate in matches
        if candidate.identity is not None
        and candidate.identity.public_identity_id is not None
    }
    if len(identities) > 1:
        entry.legacy_match = MATCH_AMBIGUOUS
        return None
    exact = [
        candidate for candidate in matches if relations.same_route(entry, candidate)
    ]
    anchor = _only(exact) or _only(matches)
    if anchor is None:
        anchor = _only([c for c in matches if c.source == entry.source])
    if anchor is None:
        entry.legacy_match = MATCH_AMBIGUOUS
        return None
    return _adopt(entry, anchor)


def _only(items):
    return items[0] if len(items) == 1 else None


def _dismissed_identity(value, *, key, broker_sources, trusted, relations):
    """The issued physical identity a stored dismissal key refers to.

    Accepts the three shapes a dismissal store has ever held: an already-issued
    opaque token, the ``serial:`` sentinel of the previous release, and a bare
    serial from the one before that. The first is the browser's migrated store
    and stands on its own; the other two are hints and resolve only through a
    unique current trusted candidate, so an arbitrary serial can never hide a
    device. A masked or placeholder value resolves to nothing either way.
    """

    raw = _text(value)
    if not raw:
        return None
    if raw.startswith("opaque:v1:"):
        return raw
    serial = raw[len("serial:"):] if raw.startswith("serial:") else raw
    if is_masked_identity_value(serial):
        return None
    hint = _Entry("dismissal", raw, {"sn": serial}, SOURCE_LOCAL_API)
    identities = {
        candidate.identity.public_identity_id
        for candidate in trusted
        if relations.same_device(hint, candidate)
        and candidate.identity is not None
        and candidate.identity.public_identity_id is not None
    }
    return identities.pop() if len(identities) == 1 else None


# --- per-group source selection ---------------------------------------------
def resolve_selected_source(available, priority, previous):
    """Which transport a physical device is configured over.

    A manual choice is never overridden — it surfaces as unavailable when its
    source disappears; otherwise the highest-priority available source wins.
    """

    present = [source for source in available if source]
    if previous and previous.get("origin") == ORIGIN_MANUAL and previous.get("source"):
        return {
            "source": previous["source"],
            "origin": ORIGIN_MANUAL,
            "available": previous["source"] in present,
        }
    ranked = [source for source in priority if source in present]
    selected = ranked[0] if ranked else (present[0] if present else None)
    if selected is None:
        origin = ORIGIN_NONE
    elif len(present) > 1:
        origin = ORIGIN_PRIORITY
    else:
        origin = ORIGIN_AUTOMATIC
    return {"source": selected, "origin": origin, "available": selected is not None}


def _previous_selection(group):
    manual_draft = next(
        (e for e in group if e.kind == "draft" and e.origin == ORIGIN_MANUAL), None
    )
    if manual_draft is not None:
        return {"source": SOURCE_LOCAL_API, "origin": ORIGIN_MANUAL}
    manual_selection = next(
        (e for e in group if e.kind == "selection" and e.origin == ORIGIN_MANUAL), None
    )
    if manual_selection is not None:
        return {"source": manual_selection.source, "origin": ORIGIN_MANUAL}
    selection = next((e for e in group if e.kind == "selection"), None)
    if selection is not None:
        return {"source": selection.source, "origin": ORIGIN_AUTOMATIC}
    draft = next((e for e in group if e.kind == "draft"), None)
    if draft is not None:
        return {"source": SOURCE_LOCAL_API, "origin": ORIGIN_AUTOMATIC}
    return None


def _auto_config_ready(device):
    ready = device.get("usable_for_config")
    if ready is None:
        ready = device.get("config_ready")
    return device.get("verified") is not False and bool(ready)


def plan_setup_connection_switch(
    *,
    current_device,
    candidate,
    identity_token_key,
    broker_sources=None,
    current_control_supported=None,
    candidate_control_supported=None,
    control_required=False,
    operator_confirmed=False,
):
    """Setup's one pairwise decision — the canonical planner, nothing added.

    Setup adapts its own inputs (a draft item or a stored selection as the
    current connection, an observation or proposal as the candidate) and hands
    them to the same function Maintenance calls, so the two workflows cannot
    answer the same pair differently.
    """

    return plan_connection_change(
        current_device=current_device,
        candidate=candidate,
        intent=INTENT_SWITCH_CONNECTION,
        identity_token_key=identity_token_key,
        broker_sources=broker_sources,
        current_control_supported=current_control_supported,
        candidate_control_supported=candidate_control_supported,
        control_required=control_required,
        operator_confirmed=operator_confirmed,
    )


def _empty_operations():
    return {
        "drop_draft_items": [],
        "drop_mqtt_selections": [],
        "select_mqtt_proposals": [],
        "adopt_observations": [],
    }


def setup_candidate_generation(
    *, observations, proposals, priority, enabled_sources, identity_token_key
):
    """The opaque generation of one candidate set.

    Derived from server-owned state only — the issued observation ids, the
    current proposal ids and the operator's source preference — so any holder of
    the key can recompute it later and prove a device plan still describes the
    world it was planned in.
    """

    return opaque_plan_id(
        [
            "setup-candidates-v1",
            sorted(observations),
            sorted(proposals),
            list(priority or []),
            sorted(
                f"{name}={bool(value)}"
                for name, value in dict(enabled_sources or {}).items()
            ),
        ],
        identity_token_key,
    )


def build_setup_plan(
    state,
    *,
    observations,
    proposals,
    priority,
    enabled_sources,
    identity_token_key,
    broker_sources=None,
    control_supported=None,
    confirmed_switches=(),
    declined_switches=(),
    unresolved_references=(),
):
    """Rehydrate persisted Setup state and plan one connection per device.

    ``state`` is what the browser persisted (draft items, MQTT selections,
    dismissals) and is treated as hints; ``observations`` and ``proposals`` are
    the current trusted candidates, read server-side. The result carries only
    issued ids, typed operations and stable reason codes — never a serial, host
    or route segment.
    """

    key = identity_token_key
    sources = dict(broker_sources or {})
    state = _mapping(state)
    priority = [source for source in (priority or []) if source]
    enabled = dict(enabled_sources or {})
    capability = control_supported if callable(control_supported) else lambda _e: None
    confirmed = {_text(token) for token in confirmed_switches or () if _text(token)}
    declined = {_text(token) for token in declined_switches or () if _text(token)}

    warnings = []
    relations = _Relations(identity_token_key=key, broker_sources=sources)

    # --- trusted candidates ---------------------------------------------------
    observation_entries = []
    observation_views = []
    for index, raw in enumerate(observations or []):
        device = _mapping(raw)
        # The caller's own handle for this card. Like `draft_item_id` it is local
        # and carries no evidence; it exists so the returned operations name
        # something the caller can resolve, whatever id the record arrived with.
        supplied = _text(device.get(OBSERVATION_REF_FIELD))
        entry = _Entry("observation", supplied, device, SOURCE_LOCAL_API)
        _resolve_trusted(entry, key=key, broker_sources=sources, fallback=str(index))
        if not supplied:
            entry.ref = entry.observation_id or f"observation:{index}"
        observation_views.append(
            dict(_entry_view(entry, OBSERVATION_REF_FIELD), source=SOURCE_LOCAL_API)
        )
        if _text(device.get("role_suggestion")) == _INVERTER_ROLE:
            observation_entries.append(entry)

    proposal_by_id = {}
    for raw in proposals or []:
        proposal = _mapping(raw)
        identifier = _text(proposal.get("id"))
        if identifier:
            proposal_by_id[identifier] = proposal

    proposal_entries = []
    for identifier, proposal in proposal_by_id.items():
        entry = _Entry(
            "proposal",
            identifier,
            proposal,
            mqtt_source_of(proposal.get("connection_source")),
        )
        proposal_entries.append(
            _resolve_trusted(entry, key=key, broker_sources=sources, fallback=identifier)
        )
    proposal_tokens = {
        entry.ref: set(entry.identity_tokens) for entry in proposal_entries
    }
    trusted = observation_entries + proposal_entries

    # --- persisted hints ------------------------------------------------------
    draft_entries = []
    for index, raw in enumerate(state.get("draft_items") or []):
        item = _mapping(raw)
        if _text(item.get("role") or _INVERTER_ROLE) != _INVERTER_ROLE:
            continue
        ref = _text(item.get("draft_item_id")) or f"draft:{index}"
        entry = _Entry(
            "draft",
            ref,
            item,
            SOURCE_LOCAL_API,
            ORIGIN_AUTOMATIC if item.get("auto_added") is True else ORIGIN_MANUAL,
        )
        _anchor_hint(entry, trusted, relations)
        draft_entries.append(entry)

    selection_entries = []
    for index, raw in enumerate(state.get("mqtt_selections") or []):
        item = _mapping(raw)
        identifier = _text(item.get("id"))
        if _text(item.get("target") or "device").lower() == "grid_meter":
            continue
        # The stored selection names a proposal id and carries the issued tokens
        # that response gave it. Both sides of the lookup are therefore
        # server-issued: the current proposal with that id, or — when the id
        # predates a serial or route enrichment — the one whose issued alias set
        # it still shares.
        anchor = _proposal_entry(proposal_entries, identifier) or _remap_stale_selection(
            item, proposal_entries, proposal_tokens
        )
        entry = _Entry(
            "selection",
            identifier or f"selection:{index}",
            item,
            mqtt_source_of(
                (anchor.payload if anchor is not None else item).get("connection_source")
            ),
            ORIGIN_MANUAL
            if _text(item.get("selection_origin")) == ORIGIN_MANUAL
            else ORIGIN_AUTOMATIC,
        )
        if anchor is not None:
            _adopt(entry, anchor)
        else:
            entry.legacy_match = MATCH_UNMATCHED
            warnings.append(
                {
                    "code": "mqtt_selection_not_offered",
                    "id": entry.ref,
                    "message": "the selected connection is no longer offered",
                }
            )
        selection_entries.append(entry)

    for entry in draft_entries + selection_entries:
        if entry.legacy_match == MATCH_UNMATCHED:
            warnings.append(
                {
                    "code": "legacy_state_unresolved",
                    "id": entry.ref,
                    "kind": entry.kind,
                    "message": "no current connection matches this stored entry",
                }
            )
        elif entry.legacy_match == MATCH_AMBIGUOUS:
            warnings.append(
                {
                    "code": "legacy_state_ambiguous",
                    "id": entry.ref,
                    "kind": entry.kind,
                    "message": "several current connections match this stored entry",
                }
            )

    # An anchored hint sits in its anchor's group; an unresolved one keeps its
    # own, so nothing it failed to identify is merged, replaced or dropped.
    groups = _group_entries(trusted, relations)
    for entry in draft_entries + selection_entries:
        host = _host_group(entry, groups)
        if host is None:
            groups.append([entry])
        else:
            # Appended in persisted order: which stored entry counts as the
            # current connection is decided by that order, not by grouping.
            host.append(entry)

    # --- dismissals -----------------------------------------------------------
    dismissed_physical = set()
    unresolved_dismissals = []
    for value in state.get("physical_dismissals") or []:
        identity = _dismissed_identity(
            value, key=key, broker_sources=sources, trusted=trusted, relations=relations
        )
        if identity is None:
            unresolved_dismissals.append({"value": _text(value), "scope": "physical"})
            continue
        dismissed_physical.add(identity)

    # A dismissal may name a card by either reference: the handle the caller
    # keys its own state on, or the issued id an earlier response gave it.
    known_observations = {}
    for view in observation_views:
        for name in (view[OBSERVATION_REF_FIELD], view["observation_id"]):
            if name:
                known_observations.setdefault(name, view)
    for index, raw in enumerate(observations or []):
        legacy = legacy_observation_key(_mapping(raw))
        if legacy:
            known_observations.setdefault(legacy, observation_views[index])
    dismissed_observations = []
    for value in state.get("observation_dismissals") or []:
        raw = _text(value)
        if not raw:
            continue
        view = known_observations.get(raw)
        if view is None:
            unresolved_dismissals.append({"value": raw, "scope": "observation"})
            continue
        dismissed_observations.append(
            {
                OBSERVATION_REF_FIELD: view[OBSERVATION_REF_FIELD],
                "observation_id": view["observation_id"],
            }
        )
    dismissed_observation_ids = {
        entry[OBSERVATION_REF_FIELD] for entry in dismissed_observations
    }

    generation = setup_candidate_generation(
        observations=[view["observation_id"] or "" for view in observation_views],
        proposals=list(proposal_by_id),
        priority=priority,
        enabled_sources=enabled,
        identity_token_key=key,
    )

    # --- one connection per physical device -----------------------------------
    operations = _empty_operations()
    proposed = _empty_operations()
    confirmations = []
    group_views = []

    for group in groups:
        drafts = [entry for entry in group if entry.kind == "draft"]
        selections = [entry for entry in group if entry.kind == "selection"]
        candidates = [entry for entry in group if entry.kind in ("observation", "proposal")]
        identities = [
            _public_identity(entry) for entry in group if _public_identity(entry)
        ]
        physical_device_id = identities[0] if identities else None
        available_sources = sorted({entry.source for entry in group})
        group_tokens = {token for entry in group for token in entry.identity_tokens}

        if group_tokens & dismissed_physical:
            operations["drop_draft_items"].extend(entry.ref for entry in drafts)
            operations["drop_mqtt_selections"].extend(entry.ref for entry in selections)
            group_views.append(
                _group_view(
                    group, physical_device_id, available_sources, None, ORIGIN_NONE, False, None
                )
            )
            continue

        previous = _previous_selection(group)
        resolved = resolve_selected_source(
            [source for source in available_sources if enabled.get(source) is not False],
            priority,
            previous,
        )
        selected = resolved["source"]
        decision = _decide_transition(
            group,
            previous,
            selected,
            generation=generation,
            identity_token_key=key,
            broker_sources=sources,
            capability=capability,
            confirmed=confirmed,
            declined=declined,
        )
        if decision["declined"]:
            selected = previous["source"] if previous else selected
            resolved = {"source": selected, "origin": ORIGIN_MANUAL, "available": True}
        if decision["confirmation"] is not None:
            confirmations.append(decision["confirmation"])

        target = (
            operations
            if decision["mode"] == _MODE_EXECUTE
            else proposed
            if decision["mode"] == _MODE_PROPOSE
            else None
        )
        if target is not None:
            _emit_group_operations(
                target,
                selected=selected,
                drafts=drafts,
                selections=selections,
                candidates=candidates,
                available_sources=available_sources,
                origin=resolved["origin"],
                dismissed_observation_ids=dismissed_observation_ids,
            )

        group_views.append(
            _group_view(
                group,
                physical_device_id,
                available_sources,
                selected,
                resolved["origin"],
                resolved["available"],
                decision["action"],
            )
        )

    candidate_views = _candidate_views(
        groups,
        draft_entries + selection_entries,
        observation_entries + proposal_entries,
        relations,
    )
    proposal_views = [
        dict(_entry_view(entry, "id"), source=entry.source) for entry in proposal_entries
    ]
    draft_views = [_entry_view(entry, "draft_item_id") for entry in draft_entries]
    selection_views = [
        dict(
            _entry_view(entry, "id"),
            unresolved=entry.ref not in proposal_by_id,
        )
        for entry in selection_entries
    ]

    plan_id = opaque_plan_id(
        [
            "setup-plan-v1",
            generation,
            draft_views,
            selection_views,
            sorted(dismissed_physical),
            sorted(entry[OBSERVATION_REF_FIELD] for entry in dismissed_observations),
            operations,
            proposed,
            sorted(entry["token"] for entry in confirmations),
        ],
        key,
    )

    return {
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "plan_id": plan_id,
        "generation": generation,
        "observations": observation_views,
        "proposals": proposal_views,
        "candidates": candidate_views,
        "draft_items": draft_views,
        "mqtt_selections": selection_views,
        "dismissals": {
            "physical": [
                {"physical_device_id": identity} for identity in sorted(dismissed_physical)
            ],
            "observations": dismissed_observations,
            "unresolved": unresolved_dismissals,
        },
        "groups": group_views,
        "operations": operations,
        "proposed_operations": proposed,
        "confirmations": confirmations,
        "confirmation_required": bool(confirmations),
        "unresolved_references": [dict(entry) for entry in unresolved_references or ()],
        "warnings": warnings,
    }


def _host_group(entry, groups):
    """The trusted group an anchored hint belongs to."""

    if entry.payload is None:
        return None
    for group in groups:
        for member in group:
            if member.payload is entry.payload and member is not entry:
                return group
    return None


def _proposal_entry(entries, identifier):
    if not identifier:
        return None
    return next((entry for entry in entries if entry.ref == identifier), None)


def _trusted_route_record(
    item, *, observations, proposals, identity_token_key, broker_sources
):
    """The trusted record that *is* this persisted entry's connection.

    Route equality only. A pairwise switch asks about the connection the entry
    names, so a device merely identified as the same hardware on a different
    address must stay a different connection — otherwise the question would
    silently become one about a route the operator never chose.
    """

    if identity_token_key is None:
        return None
    sources = dict(broker_sources or {})
    relations = _Relations(
        identity_token_key=identity_token_key, broker_sources=sources
    )
    trusted = []
    for index, raw in enumerate(observations or []):
        device = _mapping(raw)
        if _text(device.get("role_suggestion")) != _INVERTER_ROLE:
            continue
        entry = _Entry("observation", str(index), device, SOURCE_LOCAL_API)
        trusted.append(
            _resolve_trusted(
                entry, key=identity_token_key, broker_sources=sources, fallback=str(index)
            )
        )
    for raw in proposals or []:
        proposal = _mapping(raw)
        identifier = _text(proposal.get("id"))
        if not identifier:
            continue
        entry = _Entry(
            "proposal",
            identifier,
            proposal,
            mqtt_source_of(proposal.get("connection_source")),
        )
        trusted.append(
            _resolve_trusted(
                entry,
                key=identity_token_key,
                broker_sources=sources,
                fallback=identifier,
            )
        )
    hint = _Entry("draft", "current", item, SOURCE_LOCAL_API)
    matches = [
        candidate for candidate in trusted if relations.same_route(hint, candidate)
    ]
    anchor = _only(matches)
    return anchor.payload if anchor is not None else None


def _emit_group_operations(
    target,
    *,
    selected,
    drafts,
    selections,
    candidates,
    available_sources,
    origin,
    dismissed_observation_ids,
):
    if selected == SOURCE_LOCAL_API:
        target["drop_mqtt_selections"].extend(entry.ref for entry in selections)
        if not drafts:
            adoptable = [
                entry
                for entry in candidates
                if entry.kind == "observation"
                and _auto_config_ready(entry.payload)
                and entry.ref not in dismissed_observation_ids
            ]
            for entry in adoptable[:1]:
                target["adopt_observations"].append(
                    {
                        OBSERVATION_REF_FIELD: entry.ref,
                        "observation_id": entry.observation_id,
                        "connection_id": entry.connection_id,
                        "physical_device_id": _public_identity(entry),
                        "role": _INVERTER_ROLE,
                    }
                )
        return
    if selected not in MQTT_SOURCES:
        # Nothing resolved: drop auto-added drafts only, keep manual entries.
        target["drop_draft_items"].extend(
            entry.ref for entry in drafts if entry.origin == ORIGIN_AUTOMATIC
        )
        return

    target["drop_draft_items"].extend(entry.ref for entry in drafts)
    target["drop_mqtt_selections"].extend(
        entry.ref for entry in selections if entry.source != selected
    )
    same_source = [entry for entry in selections if entry.source == selected]
    offered_entries = [
        entry for entry in candidates if entry.kind == "proposal" and entry.source == selected
    ]
    # One source can offer several brokers for one device. The proposal a
    # current selection already names wins, or the operator's choice would be
    # replaced by whichever other broker happened to come first.
    selected_ids = {entry.ref for entry in same_source}
    offered = next(
        (entry for entry in offered_entries if entry.ref in selected_ids),
        offered_entries[0] if offered_entries else None,
    )
    if offered is None:
        return
    stale = [entry for entry in same_source if entry.ref != offered.ref]
    if stale:
        # A stored selection predates the current proposal id (a route-only
        # selection since enriched). Replace it so exactly one selected entry
        # remains, preserving a manual choice.
        target["drop_mqtt_selections"].extend(entry.ref for entry in stale)
        target["select_mqtt_proposals"].append(
            {
                "id": offered.ref,
                "selection_origin": ORIGIN_MANUAL
                if any(entry.origin == ORIGIN_MANUAL for entry in stale)
                else origin,
                "connection_id": offered.connection_id,
                "physical_device_id": _public_identity(offered),
            }
        )
    elif not same_source and SOURCE_LOCAL_API in available_sources:
        # Auto-select only when Local API also offers this device; an MQTT-only
        # device is added by the operator.
        target["select_mqtt_proposals"].append(
            {
                "id": offered.ref,
                "selection_origin": origin,
                "connection_id": offered.connection_id,
                "physical_device_id": _public_identity(offered),
            }
        )


def _decide_transition(
    group,
    previous,
    selected,
    *,
    generation,
    identity_token_key,
    broker_sources,
    capability,
    confirmed,
    declined,
):
    """What this group's transition is, and what may be done about it.

    A group whose recorded source is already the selected one is not switching:
    its operations only remove the duplicates that switch left behind. A group
    that *is* switching gets the canonical pairwise verdict, and only
    ``use_candidate``/``add_as_new_device`` — or the candidate turning out to be
    the current connection under a new id — may be executed.
    """

    blank = {"mode": _MODE_EXECUTE, "action": None, "confirmation": None, "declined": False}
    if selected is None:
        return blank
    previous_source = previous.get("source") if previous else None
    current = _entry_for_source(group, previous_source)
    candidate = _entry_for_source(group, selected, prefer_candidate=True)
    if candidate is None:
        return blank
    if current is not None and current.ref == candidate.ref:
        return blank

    plan = plan_setup_connection_switch(
        current_device=current.payload if current is not None else None,
        candidate=candidate.payload,
        identity_token_key=identity_token_key,
        broker_sources=broker_sources,
        current_control_supported=capability(current) if current is not None else None,
        candidate_control_supported=capability(candidate),
    )
    if previous_source is None or previous_source == selected:
        return {
            "mode": _MODE_EXECUTE,
            "action": plan.to_dict(),
            "confirmation": None,
            "declined": False,
        }

    if plan.action in (ACTION_USE_CANDIDATE, ACTION_ADD_AS_NEW_DEVICE):
        return {
            "mode": _MODE_EXECUTE,
            "action": plan.to_dict(),
            "confirmation": None,
            "declined": False,
        }
    if plan.action == ACTION_KEEP_CURRENT:
        # The candidate turned out to be the current connection under another
        # reference; adopting that reference is not a replacement.
        same_connection = (
            plan.current_connection_id is not None
            and plan.current_connection_id == plan.candidate_connection_id
        )
        return {
            "mode": _MODE_EXECUTE if same_connection else _MODE_REFUSE,
            "action": plan.to_dict(),
            "confirmation": None,
            "declined": False,
        }
    if plan.action != ACTION_REPLACE_WITH_CONFIRMATION:
        return {
            "mode": _MODE_REFUSE,
            "action": plan.to_dict(),
            "confirmation": None,
            "declined": False,
        }

    token = opaque_plan_id(
        [
            "setup-confirmation-v1",
            generation,
            plan.physical_device_id or "",
            plan.current_connection_id or "",
            plan.candidate_connection_id or "",
            current.ref if current is not None else "",
            candidate.ref,
            selected,
        ],
        identity_token_key,
    )
    if token in declined:
        kept = ConnectionPlan(
            action=ACTION_KEEP_CURRENT,
            same_physical_device=plan.same_physical_device,
            identity_status=plan.identity_status,
            control_continuity=plan.control_continuity,
            reason="operator_declined_replacement",
            current_connection_id=plan.current_connection_id,
            candidate_connection_id=plan.candidate_connection_id,
            physical_device_id=plan.physical_device_id,
            notes=plan.notes,
        )
        return {
            "mode": _MODE_EXECUTE,
            "action": kept.to_dict(),
            "confirmation": None,
            "declined": True,
        }
    if token in confirmed:
        accepted = plan_setup_connection_switch(
            current_device=current.payload if current is not None else None,
            candidate=candidate.payload,
            identity_token_key=identity_token_key,
            broker_sources=broker_sources,
            current_control_supported=capability(current) if current is not None else None,
            candidate_control_supported=capability(candidate),
            operator_confirmed=True,
        )
        return {
            "mode": _MODE_EXECUTE if not accepted.blocked else _MODE_REFUSE,
            "action": accepted.to_dict(),
            "confirmation": None,
            "declined": False,
        }
    return {
        "mode": _MODE_PROPOSE,
        "action": plan.to_dict(),
        "confirmation": {
            "token": token,
            "physical_device_id": plan.physical_device_id,
            "current_ref": current.ref if current is not None else None,
            "current_source": current.source if current is not None else None,
            "candidate_ref": candidate.ref,
            "candidate_source": selected,
            "current_connection_id": plan.current_connection_id,
            "candidate_connection_id": plan.candidate_connection_id,
            "action": plan.action,
            "reason": plan.reason,
            "control_continuity": plan.control_continuity,
        },
        "declined": False,
    }


def _issued_tokens(payload):
    """The issued alias tokens a payload carries, validated."""

    tokens = set()
    if not isinstance(payload, dict):
        return tokens
    values = [payload.get("physical_identity_token"), payload.get("physical_device_id")]
    aliases = payload.get("physical_identity_alias_tokens")
    if isinstance(aliases, list):
        values.extend(aliases)
    for value in values:
        token = _text(value)
        if token.startswith("opaque:v1:"):
            tokens.add(token)
    return tokens


def _remap_stale_selection(item, proposal_entries, proposal_tokens):
    """The current proposal a stored selection still shares an issued alias with.

    Both sides are server-issued tokens: the browser stored what an earlier
    response gave it, and the match is made against what the current response
    proves. An ambiguous set is never resolved by picking one.
    """

    stored = _issued_tokens(item)
    if not stored:
        return None
    matches = [
        identifier for identifier, tokens in proposal_tokens.items() if tokens & stored
    ]
    return _proposal_entry(proposal_entries, matches[0]) if len(matches) == 1 else None


# --- what one discovered connection offers -----------------------------------
#
# The classification a candidate card renders. It is decided here, from the
# grouping above, so the browser never asks "is this the inverter I already
# configured?" of a serial, host or route id.
CANDIDATE_NEW = "new"
CANDIDATE_ACTIVE = "active"
CANDIDATE_ALTERNATIVE = "alternative"
CANDIDATE_IDENTITY_CONFLICT = "identity_conflict"


def _candidate_views(groups, configured, candidates, relations):
    views = []
    group_of = {}
    for group in groups:
        for entry in group:
            group_of[id(entry)] = group
    for candidate in candidates:
        view = {
            "kind": candidate.kind,
            "id": candidate.ref,
            "observation_id": candidate.observation_id
            if candidate.kind == "observation"
            else None,
            "source": candidate.source,
            "connection_id": candidate.connection_id,
            "physical_device_id": _public_identity(candidate),
            "identity_status": candidate.identity.status if candidate.identity else STATUS_UNRESOLVED,
            "state": CANDIDATE_NEW,
            "current_ref": None,
            "current_source": None,
        }
        conflicting = any(relations.conflict(entry, candidate) for entry in configured)
        if conflicting:
            view["state"] = CANDIDATE_IDENTITY_CONFLICT
            views.append(view)
            continue
        group = group_of.get(id(candidate)) or []
        current = next(
            (entry for entry in group if entry.kind in ("draft", "selection")), None
        )
        if current is not None:
            same_connection = (
                current.connection_id is not None
                and current.connection_id == candidate.connection_id
            ) or (
                current.kind == "draft"
                and current.observation_id == candidate.observation_id
            )
            view["state"] = CANDIDATE_ACTIVE if same_connection else CANDIDATE_ALTERNATIVE
            view["current_ref"] = current.ref
            view["current_source"] = current.source
        views.append(view)
    return views


def _public_identity(entry):
    """The issued physical id an entry carries, or ``None`` when unresolved."""

    return entry.identity.public_identity_id if entry.identity is not None else None


def _entry_view(entry, ref_field):
    identity = entry.identity
    view = {
        ref_field: entry.ref,
        "observation_id": entry.observation_id,
        "connection_id": entry.connection_id,
        "physical_device_id": identity.public_identity_id if identity else None,
        "identity_status": identity.status if identity else STATUS_UNRESOLVED,
        "identity_reason": identity.reason if identity else "no_identity_evidence",
        "unresolved": entry.unresolved,
    }
    if entry.kind in ("draft", "selection"):
        view["legacy_match"] = entry.legacy_match
    return view


def _group_view(group, physical_device_id, sources, selected, origin, available, action):
    return {
        "physical_device_id": physical_device_id,
        "sources": list(sources),
        "selected_source": selected,
        "selection_origin": origin,
        "available": bool(available),
        "connection_ids": sorted(
            {entry.connection_id for entry in group if entry.connection_id}
        ),
        "action": action,
    }


def _entry_for_source(group, source, *, prefer_candidate=False):
    if source is None:
        return None
    order = (
        ("observation", "proposal", "draft", "selection")
        if prefer_candidate
        else ("draft", "selection", "observation", "proposal")
    )
    for kind in order:
        for entry in group:
            if entry.kind == kind and entry.source == source:
                return entry
    return None


def resolve_current_connection(
    state,
    current_ref,
    proposals,
    *,
    observations=None,
    identity_token_key=None,
    broker_sources=None,
):
    """The connection a switch is replacing, read from trusted state.

    A stored MQTT selection is a browser echo: it names an id and carries the
    tokens it was given, but no evidence. Resolving it against the current
    proposals — by id, or by the issued alias set when the id predates an
    enrichment — is what makes it comparable at all.

    A draft item is resolved the same way when it can be: its persisted fields
    are matched against the current trusted candidates and, on a unique hit,
    that record is what the switch compares against. A draft nothing currently
    observes — a hand-entered device, or one that went offline — falls back to
    what it persists, which is the only description of it that exists.
    """

    ref = _text(current_ref)
    if not ref:
        return None
    state = _mapping(state)
    for item in state.get("draft_items") or []:
        entry = _mapping(item)
        if _text(entry.get("draft_item_id")) != ref:
            continue
        anchor = _trusted_route_record(
            entry,
            observations=observations,
            proposals=proposals,
            identity_token_key=identity_token_key,
            broker_sources=broker_sources,
        )
        return anchor if anchor is not None else entry
    proposal_by_id = {}
    for raw in proposals or []:
        proposal = _mapping(raw)
        identifier = _text(proposal.get("id"))
        if identifier:
            proposal_by_id[identifier] = proposal
    proposal_tokens = {
        identifier: _issued_tokens(proposal)
        for identifier, proposal in proposal_by_id.items()
    }
    for item in state.get("mqtt_selections") or []:
        entry = _mapping(item)
        if _text(entry.get("id")) != ref:
            continue
        if ref in proposal_by_id:
            return proposal_by_id[ref]
        stored = _issued_tokens(entry)
        matches = [
            identifier
            for identifier, tokens in proposal_tokens.items()
            if tokens & stored
        ] if stored else []
        return proposal_by_id[matches[0]] if len(matches) == 1 else entry
    return None


__all__ = [
    "IDENTITY_SCHEMA_VERSION",
    "OBSERVATION_REF_FIELD",
    "build_setup_plan",
    "legacy_observation_key",
    "mqtt_source_of",
    "plan_setup_connection_switch",
    "resolve_current_connection",
    "resolve_selected_source",
    "setup_candidate_generation",
]
