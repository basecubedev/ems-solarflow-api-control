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

The second reason this module exists is that the browser may hold Setup state
persisted by an earlier release, whose entries carry no issued identity at all:
a ``<api_family>:<serial>`` source id, bare-serial dismissals, MQTT selections
without opaque tokens. Once the browser stops comparing serials, hosts and route
ids, nothing there can relate such an entry to a current observation. So this
module does, from the fields the entry already persists, and returns explicit
per-entry mappings. Unmappable state stays unresolved and is preserved — never
silently merged, dropped or re-homed.

See ``docs/developer/developer.md``.
"""

from ems.device_identity import (
    STATUS_UNRESOLVED,
    is_masked_identity_value,
    opaque_plan_id,
    resolve_physical_identity,
)
from admin.connection_planner import (
    INTENT_REVIEW,
    INTENT_SWITCH_CONNECTION,
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
    """One thing that can belong to a physical device group."""

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

    @property
    def unresolved(self):
        return self.identity is None or self.identity.public_identity_id is None


def _resolve(entry, *, key, broker_sources, fallback):
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
    # Everything that decides anything — physical identity, connection route,
    # status — is recomputed below regardless of what the payload claimed.
    supplied = _text(entry.payload.get("observation_id"))
    entry.observation_id = (
        supplied if supplied.startswith("obs:v1:") else issued["observation_id"]
    )
    entry.identity_tokens = tuple(issued["physical_identity_alias_tokens"] or ())
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

    def _plan(self, left, right):
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
        return self._plan(left, right).same_physical_device

    def conflict(self, left, right):
        return self._plan(left, right).identity_conflict


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


def _dismissal_identity(value, *, key, broker_sources):
    """The issued physical identity a stored dismissal key refers to.

    Accepts the three shapes a dismissal store has ever held: an already-issued
    opaque token, the ``serial:`` sentinel of the previous release, and a bare
    serial from the one before that. A masked or placeholder value resolves to
    nothing, so a redaction can never dismiss a device.
    """

    raw = _text(value)
    if not raw:
        return None
    if raw.startswith("opaque:v1:"):
        return raw
    serial = raw[len("serial:"):] if raw.startswith("serial:") else raw
    if is_masked_identity_value(serial):
        return None
    identity = resolve_physical_identity(
        {"sn": serial}, broker_sources=broker_sources, token_key=key
    )
    return identity.public_identity_id


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
):
    """Rehydrate persisted Setup state and plan one connection per device.

    ``state`` is what the browser persisted (draft items, MQTT selections,
    dismissals); ``observations`` and ``proposals`` are the current candidates,
    read server-side. The result carries only issued ids, typed operations and
    stable reason codes — never a serial, host or route segment.
    """

    key = identity_token_key
    sources = dict(broker_sources or {})
    state = _mapping(state)
    priority = [source for source in (priority or []) if source]
    enabled = dict(enabled_sources or {})
    capability = control_supported if callable(control_supported) else lambda _entry: None

    warnings = []
    entries = []

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
        draft_entries.append(_resolve(entry, key=key, broker_sources=sources, fallback=ref))

    proposal_by_id = {}
    for raw in proposals or []:
        proposal = _mapping(raw)
        identifier = _text(proposal.get("id"))
        if identifier:
            proposal_by_id[identifier] = proposal

    # Every current proposal is a candidate, including the one a stored
    # selection already points at: that is how a card renders as "active" and
    # how a stale stored id finds the proposal that superseded it. Their alias
    # sets are derived here, never taken from what the browser echoed back.
    proposal_entries = []
    for identifier, proposal in proposal_by_id.items():
        entry = _Entry(
            "proposal",
            identifier,
            proposal,
            mqtt_source_of(proposal.get("connection_source")),
        )
        proposal_entries.append(
            _resolve(entry, key=key, broker_sources=sources, fallback=identifier)
        )
    proposal_tokens = {
        entry.ref: set(entry.identity_tokens) for entry in proposal_entries
    }

    selection_entries = []
    for index, raw in enumerate(state.get("mqtt_selections") or []):
        item = _mapping(raw)
        identifier = _text(item.get("id"))
        if _text(item.get("target") or "device").lower() == "grid_meter":
            continue
        # The stored selection is a browser echo; its trusted twin is the
        # current proposal with that id, or — when the id predates a serial or
        # route enrichment — the proposal whose issued alias set it still shares.
        # Identity is always resolved from the trusted side.
        trusted = proposal_by_id.get(identifier) or _remap_stale_selection(
            item, proposal_by_id, proposal_tokens
        )
        payload = trusted if trusted is not None else item
        entry = _Entry(
            "selection",
            identifier or f"selection:{index}",
            payload,
            mqtt_source_of(payload.get("connection_source") or item.get("connection_source")),
            ORIGIN_MANUAL
            if _text(item.get("selection_origin")) == ORIGIN_MANUAL
            else ORIGIN_AUTOMATIC,
        )
        entry.payload = payload
        _resolve(entry, key=key, broker_sources=sources, fallback=entry.ref)
        if trusted is None:
            warnings.append(
                {
                    "code": "mqtt_selection_not_offered",
                    "id": entry.ref,
                    "message": "the selected connection is no longer offered",
                }
            )
        entry.origin = (
            ORIGIN_MANUAL
            if _text(item.get("selection_origin")) == ORIGIN_MANUAL
            else ORIGIN_AUTOMATIC
        )
        selection_entries.append(entry)

    observation_entries = []
    observation_views = []
    for index, raw in enumerate(observations or []):
        device = _mapping(raw)
        # The caller's own handle for this card. Like `draft_item_id` it is local
        # and carries no evidence; it exists so the returned operations name
        # something the caller can resolve, whatever id the payload did or did
        # not arrive with.
        supplied = _text(device.get(OBSERVATION_REF_FIELD))
        entry = _Entry("observation", supplied, device, SOURCE_LOCAL_API)
        _resolve(entry, key=key, broker_sources=sources, fallback=str(index))
        # A caller that supplied no handle keys its cards on the issued id, which
        # is what discovery stamped onto this payload. Only a card that has
        # neither falls back to its position in the response.
        if not supplied:
            entry.ref = entry.observation_id or f"observation:{index}"
        observation_views.append(
            dict(_entry_view(entry, OBSERVATION_REF_FIELD), source=SOURCE_LOCAL_API)
        )
        if _text(device.get("role_suggestion")) != _INVERTER_ROLE:
            continue
        entry.payload = device
        observation_entries.append(entry)

    relations = _Relations(identity_token_key=key, broker_sources=sources)
    for entry in draft_entries:
        _adopt_live_observation(entry, observation_entries, relations)

    entries = draft_entries + selection_entries + observation_entries + proposal_entries
    groups = _group_entries(entries, relations)

    dismissed_physical = set()
    unresolved_dismissals = []
    for value in state.get("physical_dismissals") or []:
        identity = _dismissal_identity(value, key=key, broker_sources=sources)
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

    operations = {
        "drop_draft_items": [],
        "drop_mqtt_selections": [],
        "select_mqtt_proposals": [],
        "adopt_observations": [],
    }
    dismissed_observation_ids = {
        entry[OBSERVATION_REF_FIELD] for entry in dismissed_observations
    }
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
        dismissed = bool(group_tokens & dismissed_physical)

        if dismissed:
            operations["drop_draft_items"].extend(entry.ref for entry in drafts)
            operations["drop_mqtt_selections"].extend(entry.ref for entry in selections)
            group_views.append(
                _group_view(group, physical_device_id, available_sources, None, ORIGIN_NONE, False, None)
            )
            continue

        previous = _previous_selection(group)
        resolved = resolve_selected_source(
            [source for source in available_sources if enabled.get(source) is not False],
            priority,
            previous,
        )
        selected = resolved["source"]

        if selected == SOURCE_LOCAL_API:
            operations["drop_mqtt_selections"].extend(entry.ref for entry in selections)
            if not drafts:
                adoptable = [
                    entry
                    for entry in candidates
                    if entry.kind == "observation"
                    and _auto_config_ready(entry.payload)
                    and entry.ref not in dismissed_observation_ids
                ]
                for entry in adoptable[:1]:
                    operations["adopt_observations"].append(
                        {
                            OBSERVATION_REF_FIELD: entry.ref,
                            "observation_id": entry.observation_id,
                            "connection_id": entry.connection_id,
                            "physical_device_id": _public_identity(entry),
                            "role": _INVERTER_ROLE,
                        }
                    )
        elif selected in MQTT_SOURCES:
            operations["drop_draft_items"].extend(entry.ref for entry in drafts)
            operations["drop_mqtt_selections"].extend(
                entry.ref for entry in selections if entry.source != selected
            )
            same_source = [entry for entry in selections if entry.source == selected]
            offered_entries = [
                entry
                for entry in candidates
                if entry.kind == "proposal" and entry.source == selected
            ]
            # One source can offer several brokers for one device. The proposal a
            # current selection already names wins, or the operator's choice
            # would be replaced by whichever other broker happened to come first.
            selected_ids = {entry.ref for entry in same_source}
            offered = next(
                (entry for entry in offered_entries if entry.ref in selected_ids),
                offered_entries[0] if offered_entries else None,
            )
            if offered is not None:
                stale = [entry for entry in same_source if entry.ref != offered.ref]
                if stale:
                    # A stored selection predates the current proposal id (a
                    # route-only selection since enriched). Replace it so exactly
                    # one selected entry remains, preserving a manual choice.
                    operations["drop_mqtt_selections"].extend(entry.ref for entry in stale)
                    operations["select_mqtt_proposals"].append(
                        {
                            "id": offered.ref,
                            "selection_origin": ORIGIN_MANUAL
                            if any(entry.origin == ORIGIN_MANUAL for entry in stale)
                            else resolved["origin"],
                            "connection_id": offered.connection_id,
                            "physical_device_id": _public_identity(offered),
                        }
                    )
                elif not same_source and SOURCE_LOCAL_API in available_sources:
                    # Auto-select only when Local API also offers this device;
                    # an MQTT-only device is added by the operator.
                    operations["select_mqtt_proposals"].append(
                        {
                            "id": offered.ref,
                            "selection_origin": resolved["origin"],
                            "connection_id": offered.connection_id,
                            "physical_device_id": _public_identity(offered),
                        }
                    )
        else:
            # Nothing resolved: drop auto-added drafts only, keep manual entries.
            operations["drop_draft_items"].extend(
                entry.ref for entry in drafts if entry.origin == ORIGIN_AUTOMATIC
            )

        action = _group_action(
            group,
            previous,
            selected,
            identity_token_key=key,
            broker_sources=sources,
            capability=capability,
        )
        group_views.append(
            _group_view(
                group,
                physical_device_id,
                available_sources,
                selected,
                resolved["origin"],
                resolved["available"],
                action,
            )
        )

    candidate_views = _candidate_views(
        groups,
        draft_entries + selection_entries,
        observation_entries + proposal_entries,
        relations,
    )
    proposal_views = [
        dict(
            _entry_view(entry, "id"),
            source=entry.source,
        )
        for entry in _proposal_views(proposal_by_id, key, sources)
    ]
    draft_views = [_entry_view(entry, "draft_item_id") for entry in draft_entries]
    selection_views = [
        dict(
            _entry_view(entry, "id"),
            unresolved=entry.ref not in proposal_by_id,
        )
        for entry in selection_entries
    ]

    generation = opaque_plan_id(
        [
            "setup-candidates-v1",
            sorted(known_observations),
            sorted(proposal_by_id),
            list(priority),
            sorted(f"{name}={bool(value)}" for name, value in enabled.items()),
        ],
        key,
    )
    plan_id = opaque_plan_id(
        [
            "setup-plan-v1",
            generation,
            draft_views,
            selection_views,
            sorted(dismissed_physical),
            sorted(entry[OBSERVATION_REF_FIELD] for entry in dismissed_observations),
            operations,
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
        "warnings": warnings,
    }


def _adopt_live_observation(entry, observations, relations):
    """Give a persisted entry the issued id of the observation it *is*.

    A store written before observation ids existed derives its own id from the
    fields it kept, which is not the id the current discovery response carries
    for the same device. The match is made here, from Core's identity answer and
    the transport coordinates — never in the browser, and never on a display
    value: an exact connection wins, and otherwise only an unambiguous single
    physical match counts, so two candidates leave the entry as it is.
    """

    if entry.connection_id is not None:
        exact = [
            observation
            for observation in observations
            if observation.connection_id == entry.connection_id
        ]
        if len(exact) == 1:
            entry.observation_id = exact[0].observation_id
            return
    physical = [
        observation
        for observation in observations
        if relations.same_device(entry, observation)
    ]
    if len(physical) == 1:
        entry.observation_id = physical[0].observation_id
        if entry.connection_id is None:
            entry.connection_id = physical[0].connection_id


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


def _remap_stale_selection(item, proposal_by_id, proposal_tokens):
    """The current proposal a stored selection still shares an issued alias with.

    Both sides are server-issued tokens: the browser stored what an earlier
    response gave it, and the match is made against what the current response
    proves. An ambiguous set is never resolved by picking one.
    """

    stored = _issued_tokens(item)
    if not stored:
        return None
    matches = [
        identifier
        for identifier, tokens in proposal_tokens.items()
        if tokens & stored
    ]
    return None if len(matches) != 1 else proposal_by_id[matches[0]]


def _proposal_views(proposal_by_id, key, broker_sources):
    """Every current proposal as a resolved entry, selected or not."""

    views = []
    for identifier, proposal in proposal_by_id.items():
        entry = _Entry(
            "proposal", identifier, proposal, mqtt_source_of(proposal.get("connection_source"))
        )
        views.append(_resolve(entry, key=key, broker_sources=broker_sources, fallback=identifier))
    return views


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
        conflicting = any(
            relations.conflict(entry, candidate) for entry in configured
        )
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
    return {
        ref_field: entry.ref,
        "observation_id": entry.observation_id,
        "connection_id": entry.connection_id,
        "physical_device_id": identity.public_identity_id if identity else None,
        "identity_status": identity.status if identity else STATUS_UNRESOLVED,
        "identity_reason": identity.reason if identity else "no_identity_evidence",
        "unresolved": entry.unresolved,
    }


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


def _group_action(group, previous, selected, *, identity_token_key, broker_sources, capability):
    """The pairwise planner's verdict for this group's actual transition."""

    if selected is None:
        return None
    current = _entry_for_source(group, previous.get("source") if previous else None)
    candidate = _entry_for_source(group, selected, prefer_candidate=True)
    if candidate is None:
        return None
    if current is not None and current.ref == candidate.ref:
        return None
    plan = plan_setup_connection_switch(
        current_device=current.payload if current is not None else None,
        candidate=candidate.payload,
        identity_token_key=identity_token_key,
        broker_sources=broker_sources,
        current_control_supported=capability(current) if current is not None else None,
        candidate_control_supported=capability(candidate),
    )
    return plan.to_dict()


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


def resolve_current_connection(state, current_ref, proposals):
    """The connection a switch is replacing, read from trusted state.

    A stored MQTT selection is a browser echo: it names an id and carries the
    tokens it was given, but no evidence. Resolving it against the current
    proposals — by id, or by the issued alias set when the id predates an
    enrichment — is what makes it comparable at all. A draft item persists its
    own connection fields and is usable as it stands.
    """

    ref = _text(current_ref)
    if not ref:
        return None
    state = _mapping(state)
    for item in state.get("draft_items") or []:
        entry = _mapping(item)
        if _text(entry.get("draft_item_id")) == ref:
            return entry
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
        return (
            proposal_by_id.get(ref)
            or _remap_stale_selection(entry, proposal_by_id, proposal_tokens)
            or entry
        )
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
]
