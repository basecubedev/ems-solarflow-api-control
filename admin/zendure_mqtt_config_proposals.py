# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert Admin MQTT discovery observations into Zendure config proposals.

Thin adapter only: it reshapes Admin discovery candidates into the EMS Zendure
MQTT snapshot model and delegates every Zendure-specific decision (capability
inference, role hints, confidence, config fragments) to
``ems.zendure_mqtt.config_mapping``. It adds no capability rules of its own.

Preview-only and secret-free: it reads a fixed allowlist of non-secret candidate
fields, so broker credentials, tokens and the cloud app key can never leak into
a proposal. Output control is decided by the EMS mapper's shared capability rule
(supported write method plus an addressable write target); this adapter adds no
capability rules of its own. Nothing here writes config, files or MQTT.
"""

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from admin.models import SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_CLOUD_MQTT
from admin.zendure_mqtt_config_draft import (
    generation_label,
    resolve_hardware_generation,
    telemetry_schema_for_topic_family,
)
from ems.config import (
    canonical_mqtt_tls_mode,
    parse_mqtt_port,
    require_json_bool,
    resolve_mqtt_tls_metadata,
)
from ems.device_identity import (
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
    PHYSICAL_IDENTITY_TOKEN_FIELD,
    connection_coordinates,
    is_masked_identity_value,
    legacy_route_folded_tokens,
    normalize_mqtt_route_segment,
    opaque_connection_id,
    resolve_inverter_identity_evidence,
    resolve_physical_identity,
)
from ems.zendure_mqtt.config_entries import (
    normalized_broker_identity,
    stable_local_broker_ref,
)
from ems.zendure_mqtt.config_mapping import (
    WARN_IDENTITY_CONFLICT,
    WARN_ROUTE_PRODUCT_CONFLICT,
    ZendureMqttConfigProposal,
    map_snapshots_to_proposals,
)
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot, infer_capabilities
from ems.zendure_mqtt.topics import FAMILY_UNKNOWN

# Stable broker profile refs. One Zendure cloud account maps to a single cloud
# ref. Every discovered local broker gets a deterministic, endpoint-derived
# ``local_mqtt_<slug>_<hash>`` ref (see :func:`_local_broker_identity` and the
# shared ``stable_local_broker_ref``), so a broker's ref never changes merely
# because another broker is (or is not) discovered alongside it, and two brokers
# never collapse into one profile. The bare ``local_mqtt`` ref is only reused
# downstream in config preview when it already names a matching endpoint.
BROKER_REF_ZENDURE_CLOUD = "zendure_cloud"
BROKER_REF_LOCAL_MQTT = "local_mqtt"


def _canonical_tls_mode(observation: Mapping[str, Any]) -> Any:
    """Canonical TLS mode of an observation's broker.

    Cloud discovery names its own TLS modes (TLS is always on for the cloud
    broker; only certificate verification differs). Translating them is Core's
    job — this only says *which* value to translate, so no Admin-only mode
    string leaks into a proposal endpoint.
    """

    return canonical_mqtt_tls_mode(observation.get("tls_mode"))


def _observation_tls(observation: Mapping[str, Any]) -> tuple[bool, bool]:
    """Effective ``(tls, tls_insecure)`` for an observation's broker.

    Invalid or contradictory metadata raises so it can never become a plaintext
    proposal.
    """

    return resolve_mqtt_tls_metadata(
        tls_mode=_canonical_tls_mode(observation),
        tls=observation.get("tls"),
        tls_insecure=observation.get("tls_insecure"),
    )


def _local_broker_identity(observation: Mapping[str, Any]) -> tuple:
    """Credential-free endpoint identity for a local broker.

    Derived from source, host, port and TLS mode via the shared Core
    normalizer, never from ``broker_id`` alone, so two brokers that share a
    broker id but differ in host/port/TLS are never merged onto one profile.
    Credentials are never part of the identity, so a generated ref can never
    leak a secret. Falls back to a raw host/port/tls tuple when no host is known.
    """

    tls, tls_insecure = _observation_tls(observation)
    identity = normalized_broker_identity(
        {
            "source": SOURCE_LOCAL_MQTT,
            "host": observation.get("broker_host"),
            "port": observation.get("broker_port"),
            "tls": tls,
            "tls_insecure": tls_insecure,
            "credentials_ref": observation.get("credentials_ref"),
        }
    )
    if identity is not None:
        return identity
    host = str(observation.get("broker_host") or "").strip().lower()
    return (SOURCE_LOCAL_MQTT, host, None, tls, tls_insecure)

# Non-secret candidate fields consumed from a discovery observation. Broker
# host/port, credentials and auth modes are deliberately excluded.
_DEVICE_ID_KEYS = ("device_id", "serial_number")

# A cloud deviceList candidate seen before any MQTT telemetry. Its ``topic_family``
# carries this marker, not a real observed topic family.
_DEVICE_LIST_ONLY_FAMILY = "device_list_only"
WAITING_FOR_MQTT_TELEMETRY = "waiting_for_mqtt_telemetry"

def is_masked_zendure_identifier(value: Any) -> bool:
    """True if ``value`` is a display-masked id unsafe to write into config.

    The placeholder vocabulary is Core's. An absent or empty value is not a
    mask here — it carries no placeholder to write back.
    """

    return (
        isinstance(value, str)
        and value.strip() != ""
        and is_masked_identity_value(value)
    )


def _unmasked(value: Any) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed and not is_masked_zendure_identifier(trimmed):
            return trimmed
    return None


def _as_observation(candidate: Any) -> Mapping[str, Any] | None:
    if isinstance(candidate, Mapping):
        return candidate
    to_dict = getattr(candidate, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return result if isinstance(result, Mapping) else None
    return None


def _metric_keys(observation: Mapping[str, Any]) -> list[str]:
    metrics = observation.get("metrics_seen")
    if not isinstance(metrics, (list, tuple, set)):
        return []
    return [key for key in metrics if isinstance(key, str) and key]


# Only the fixed, non-secret local ZenSDK/HA prefix is safe to carry into a
# snapshot. A cloud topic is prefixed with the account app key (a secret), so it
# is deliberately dropped and can never reach a proposal or config.
_SAFE_LOCAL_TOPIC_PREFIX = "Zendure/"


def _safe_local_topics(observation: Mapping[str, Any]) -> set[str]:
    source_type = str(observation.get("source_type") or SOURCE_LOCAL_MQTT)
    if source_type != SOURCE_LOCAL_MQTT:
        return set()
    topics = observation.get("topics_seen")
    if not isinstance(topics, (list, tuple, set)):
        return set()
    return {
        topic
        for topic in topics
        if isinstance(topic, str) and topic.startswith(_SAFE_LOCAL_TOPIC_PREFIX)
    }


def _observation_to_snapshot(
    observation: Mapping[str, Any],
) -> ZendureMqttSnapshot | None:
    family = observation.get("topic_family")
    if not isinstance(family, str) or not family:
        return None
    serial = _unmasked(observation.get("serial_number"))
    device_id = _unmasked(observation.get("device_id"))
    if serial is None and device_id is None:
        # A masked-only candidate (e.g. a cloud deviceList entry without a serial)
        # has no identifier safe to persist, so it never becomes a proposal.
        return None

    product = observation.get("model_hint")
    metric_keys = _metric_keys(observation)
    metrics = {key: None for key in metric_keys}
    capabilities = infer_capabilities(metrics, [])
    # A device-list-only candidate has no observed topic yet, so it must not
    # claim a real topic family.
    topic_families: set = set() if family == _DEVICE_LIST_ONLY_FAMILY else {family}
    return ZendureMqttSnapshot(
        device_id=device_id,
        serial_number=serial,
        product_key=_unmasked(observation.get("product_key")),
        product=product if isinstance(product, str) else None,
        topic_families=topic_families,
        metrics=metrics,
        capabilities=capabilities,
        seen_topics=_safe_local_topics(observation),
    )


def _proposal_to_dict(proposal: ZendureMqttConfigProposal) -> dict[str, Any]:
    # The raw topic family stays out of the UI copy: it resolves here to a
    # friendly hardware generation id/label the frontend renders instead.
    # Generation and telemetry schema are separate derivations: the product
    # model identifies the generation (a ZenSDK device on the cloud broker may
    # publish the leading-slash JSON report), while the topic family only names
    # the observed schema and flags the alternative layout.
    hardware_generation, alternative_layout = resolve_hardware_generation(
        proposal.topic_family, model_hint=proposal.product
    )
    from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

    model_profile = hardware_profile_by_name(proposal.hardware_profile)
    return {
        "id": proposal.proposal_id,
        "source": proposal.source,
        "broker_ref": proposal.broker_ref,
        "connection_source": proposal.connection_source,
        "device_id": proposal.device_id,
        "serial_number": proposal.serial_number,
        "product_key": proposal.product_key,
        "product": proposal.product,
        "topic_family": proposal.topic_family,
        "telemetry_schema": telemetry_schema_for_topic_family(proposal.topic_family),
        "hardware_generation": hardware_generation,
        "hardware_generation_label": generation_label(hardware_generation),
        "hardware_model": proposal.hardware_profile,
        "power_write_profile": (
            model_profile.power_write_profile if model_profile is not None else None
        ),
        "alternative_layout": alternative_layout,
        "base_topic": proposal.base_topic,
        "display_name": proposal.display_name,
        "role_hint": proposal.role_hint,
        "confidence": proposal.confidence,
        "output_control_supported": proposal.output_control_supported,
        "output_control_reason": proposal.output_control_reason,
        # Why the exact model may be blocked. The model itself is the normalized
        # hardware_model above; config_fragment keeps Core's canonical
        # hardware_profile field for runtime output.
        "hardware_profile_confidence": proposal.hardware_profile_confidence,
        "hardware_profile_evidence": proposal.hardware_profile_evidence,
        "hardware_profile_evidence_sources": [
            dict(entry) for entry in proposal.hardware_profile_evidence_sources
        ],
        "control_block_reason": proposal.control_block_reason,
        "capabilities": list(proposal.capabilities),
        "metrics": list(proposal.metrics),
        "config_fragment": dict(proposal.config_fragment),
        "warnings": list(proposal.warnings),
        "target": proposal.target,
        "grid_meter_fragment": (
            dict(proposal.grid_meter_fragment)
            if proposal.grid_meter_fragment is not None
            else None
        ),
        "seen_topics": list(proposal.seen_topics),
    }


def build_proposals(observations: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert discovery observations into preview-only proposal dicts.

    Accepts Admin ``MqttHardwareCandidate`` objects or their dict form.
    Unusable observations are skipped rather than raising. Deduplication across
    observations for one logical device is handled by the EMS mapper.
    """

    # Group by concrete broker identity, not just source type: a physical device
    # seen on two local brokers (or on a local and the cloud broker) yields one
    # proposal per broker, each with its own endpoint and broker ref, so two
    # brokers are never collapsed into a single ambiguous profile.
    groups: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for candidate in observations:
        observation = _as_observation(candidate)
        if observation is None:
            continue
        source_type = str(observation.get("source_type") or SOURCE_LOCAL_MQTT)
        try:
            tls_metadata = _observation_tls(observation)
            if source_type == SOURCE_LOCAL_MQTT:
                parse_mqtt_port(observation.get("broker_port"))
        except ValueError:
            continue
        snapshot = _observation_to_snapshot(observation)
        if snapshot is None:
            continue
        if source_type == SOURCE_ZENDURE_CLOUD_MQTT:
            identity: tuple = ("cloud",)
        else:
            identity = _local_broker_identity(observation)
        key = (source_type, identity)
        group = groups.get(key)
        if group is None:
            group = {
                "source_type": source_type,
                "identity": identity,
                "snapshots": [],
                "waiting": set(),
                "endpoint": {},
            }
            groups[key] = group
            order.append(key)
        _record_broker_endpoint(group["endpoint"], observation, tls_metadata)
        if observation.get("topic_family") == _DEVICE_LIST_ONLY_FAMILY:
            group["waiting"].update(
                token
                for token in (snapshot.serial_number, snapshot.device_id)
                if token
            )
        group["snapshots"].append(snapshot)

    broker_refs = _assign_broker_refs(order, groups)

    results = []
    for key in order:
        group = groups[key]
        broker_ref = broker_refs[key]
        proposals = map_snapshots_to_proposals(
            group["snapshots"], source=group["source_type"], broker_ref=broker_ref
        )
        for proposal in proposals:
            data = _proposal_to_dict(proposal)
            # The broker endpoint (host/port/tls) is not a secret and lets the
            # preview provision a usable broker profile instead of a blank stub.
            data.update(group["endpoint"])
            if proposal.topic_family == FAMILY_UNKNOWN and _is_waiting(
                proposal, group["waiting"]
            ):
                data["warnings"] = [*data["warnings"], WAITING_FOR_MQTT_TELEMETRY]
            results.append(data)
    return results


def _assign_broker_refs(order: list[tuple], groups: dict[tuple, dict[str, Any]]) -> dict[tuple, str]:
    """Assign a deterministic, collision-free broker ref to each group.

    Cloud groups share the single ``zendure_cloud`` ref. Every local broker gets
    a stable ``local_mqtt_<slug>_<hash>`` ref derived from its secret-free
    connection-profile identity, so the same broker keeps the same ref whether it was
    discovered alone or alongside others, and independent of discovery order.
    """

    refs: dict[tuple, str] = {}
    used: set[str] = set()
    for key in order:
        group = groups[key]
        if group["source_type"] == SOURCE_ZENDURE_CLOUD_MQTT:
            ref = BROKER_REF_ZENDURE_CLOUD
        else:
            ref = stable_local_broker_ref(group["identity"])
        base = ref
        counter = 2
        while ref in used:
            ref = f"{base}_{counter}"
            counter += 1
        used.add(ref)
        refs[key] = ref
    return refs


def _record_broker_endpoint(
    endpoint: dict[str, Any],
    observation: Mapping[str, Any],
    tls_metadata: tuple[bool, bool] | None = None,
):
    # The first observation that carries a usable host/port fixes the group's
    # broker endpoint; later ones do not override it. The effective TLS mode,
    # tls_insecure and non-secret credentials_ref are preserved so a discovered
    # TLS/authenticated broker keeps its connection metadata into the proposal.
    if endpoint.get("broker_host"):
        return
    host = observation.get("broker_host")
    if not isinstance(host, str) or not host.strip():
        return
    endpoint["broker_host"] = host.strip()
    endpoint["broker_port"] = parse_mqtt_port(observation.get("broker_port"))
    tls, tls_insecure = tls_metadata or _observation_tls(observation)
    endpoint["broker_tls"] = tls
    endpoint["broker_tls_insecure"] = tls_insecure
    tls_mode = _canonical_tls_mode(observation)
    if isinstance(tls_mode, str) and tls_mode.strip():
        endpoint["broker_tls_mode"] = tls_mode.strip()
    ref = observation.get("credentials_ref")
    if isinstance(ref, str) and ref.strip():
        endpoint["credentials_ref"] = ref.strip()


def _is_waiting(
    proposal: ZendureMqttConfigProposal, waiting_tokens: set[str]
) -> bool:
    return bool(waiting_tokens) and (
        proposal.serial_number in waiting_tokens
        or proposal.device_id in waiting_tokens
    )


def proposals_from_brokers(brokers: Iterable[Any]) -> list[dict[str, Any]]:
    """Flatten discovered broker candidates' ``devices`` into proposals."""

    observations: list[Any] = []
    generations: set[int] = set()
    for broker in brokers:
        if not isinstance(broker, Mapping):
            continue
        devices = broker.get("devices")
        if isinstance(devices, (list, tuple)):
            observations.extend(devices)
            generation = broker.get("discovery_generation")
            if isinstance(generation, int) and not isinstance(generation, bool):
                generations.add(generation)
    proposals = build_proposals(observations)
    if len(generations) == 1:
        generation = next(iter(generations))
        for proposal in proposals:
            proposal["discovery_generation"] = generation
            proposal["id"] = f"{proposal['id']}:g{generation}"
    return proposals


def connection_dedup_key(proposal: Mapping[str, Any]) -> tuple:
    """Concrete connection a proposal describes, for cross-source deduplication.

    Everything that makes a connection reachable in its own right: transport
    source, broker/account scope, telemetry family and the route within it. A
    physical serial deliberately takes no part — one inverter may be reachable
    over Local MQTT and over the Zendure account at the same time, and those are
    two selectable alternatives, not one observation seen twice.
    """

    fragment = proposal.get("config_fragment")
    mqtt = fragment.get("mqtt") if isinstance(fragment, Mapping) else None
    mqtt = mqtt if isinstance(mqtt, Mapping) else {}
    source = str(
        proposal.get("connection_source") or mqtt.get("source") or SOURCE_LOCAL_MQTT
    ).strip().lower()
    broker_ref = str(
        proposal.get("broker_ref") or mqtt.get("broker_ref") or ""
    ).strip()
    return (
        source,
        broker_ref,
        str(proposal.get("broker_host") or "").strip().lower(),
        parse_mqtt_port(proposal.get("broker_port"))
        if proposal.get("broker_port") not in (None, "")
        else None,
        str(mqtt.get("topic_family") or proposal.get("topic_family") or "").strip(),
        normalize_mqtt_route_segment(mqtt.get("device_id") or proposal.get("device_id")),
        normalize_mqtt_route_segment(
            mqtt.get("product_key") or proposal.get("product_key")
        ),
    )


def proposals_from_sources(
    brokers: Iterable[Any], cloud_candidates: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    """Combine local broker devices and cloud candidates into one proposal set.

    This is the single trusted proposal source shared by the proposals endpoint
    and the config-preview trust resolve, so a selection made in the review UI
    always resolves against the same set it was rendered from. Local proposals
    keep their generation stamping (cloud candidates take no part in the local
    broker store's generation/TTL bookkeeping).

    Proposals are kept apart by concrete connection scope, never by physical
    identity: one inverter on Local MQTT and on the Zendure account yields both
    connections so either direction of a Maintenance switch stays offerable. A
    shared serial groups them as alternatives for one logical inverter
    downstream (see :func:`annotate_identity_tokens`). Only an observation of
    the very same connection collapses.
    """

    proposals = proposals_from_brokers(brokers)
    seen = {connection_dedup_key(proposal) for proposal in proposals}
    for proposal in build_proposals(cloud_candidates):
        key = connection_dedup_key(proposal)
        if key in seen:
            continue
        seen.add(key)
        proposals.append(proposal)
    return proposals


def _identity_of_kind(evidence, kind):
    return next(
        (
            identity
            for identity in evidence.identities
            if identity.kind == kind and identity.opaque_token is not None
        ),
        None,
    )


def _anchored_selection_id(token: str, broker_ref: Any, generation: Any) -> str:
    stable_id = f"zendure-mqtt:{token}:{broker_ref or 'default'}"
    if isinstance(generation, int) and not isinstance(generation, bool):
        stable_id = f"{stable_id}:g{generation}"
    return stable_id


def annotate_identity_tokens(
    proposals: list[dict[str, Any]], token_key: bytes | None
) -> list[dict[str, Any]]:
    """Attach browser-safe identity tokens and a stable selection id in place.

    The selection ``id`` is anchored to the stable device-anchor token (not the
    physical serial and not the mutable precise-route token) for a Cloud proposal
    or any anchor-primary proposal, so it survives product-key, topic-family and
    serial enrichment of one scoped device. Local serial-bearing proposals keep
    their serial-based id (a stale local route-only selection is recovered by the
    alias-token remap). Opaque tokens are keyed HMACs, never reversible ids.

    Conflicts fail closed. Contested serials keep their distinct serial-based id
    and full alias set (the serial guard separates them in the browser). Two known
    product keys on one device id expose only their distinct precise-route token —
    the shared device anchor is withheld so the routes never share a browser
    identity — and each id is anchored to that route token.
    """

    if token_key is None:
        return proposals
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        # The transport route this proposal offers, as an issued id: what a
        # replacement plan keeps or replaces, and the only thing the browser may
        # compare to decide "this is the same connection".
        coordinates = connection_coordinates(proposal.get("config_fragment"))
        if coordinates is not None:
            proposal["connection_id"] = opaque_connection_id(coordinates, token_key)
        # Why this identity is what it is: "confirmed" means a verified physical
        # serial, and only two confirmed identities can contradict each other.
        proposal["identity_status"] = resolve_physical_identity(
            proposal.get("config_fragment"), token_key=token_key
        ).status
        evidence = resolve_inverter_identity_evidence(
            proposal.get("config_fragment"), token_key=token_key
        )
        if evidence is None or evidence.primary.opaque_token is None:
            continue
        warnings = proposal.get("warnings") or []
        broker_ref = proposal.get("broker_ref")
        generation = proposal.get("discovery_generation")
        route_identity = _identity_of_kind(evidence, "scoped_mqtt_route")

        if WARN_ROUTE_PRODUCT_CONFLICT in warnings and route_identity is not None:
            proposal[PHYSICAL_IDENTITY_TOKEN_FIELD] = route_identity.opaque_token
            proposal[PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD] = [route_identity.opaque_token]
            proposal["id"] = _anchored_selection_id(
                route_identity.opaque_token, broker_ref, generation
            )
            continue

        proposal[PHYSICAL_IDENTITY_TOKEN_FIELD] = evidence.primary.opaque_token
        alias_tokens = list(evidence.opaque_tokens)
        if alias_tokens:
            proposal[PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD] = alias_tokens

        if WARN_IDENTITY_CONFLICT in warnings:
            continue

        anchor_identity = _identity_of_kind(evidence, "scoped_mqtt_device_anchor")
        anchor_is_primary = evidence.primary.kind == "scoped_mqtt_device_anchor"
        is_cloud = proposal.get("connection_source") == SOURCE_ZENDURE_CLOUD_MQTT
        if anchor_identity is not None and (anchor_is_primary or is_cloud):
            proposal["id"] = _anchored_selection_id(
                anchor_identity.opaque_token, broker_ref, generation
            )
    return proposals


# --- Server-side proposal trust boundary ------------------------------------
#
# The browser is never authoritative for a proposal's identity, broker,
# capabilities or discovery evidence. The trust contract is deliberately
# compatible (not token-mandatory):
#
#   * an exact ``(id, broker_ref)`` hit against current discovery state is
#     sufficient to select the current proposal directly — a token is not
#     required for that path;
#   * a server-issued opaque identity token is required only to remap a stale or
#     alias id (a stored ``id`` from before a serial/route enrichment, or a
#     case-folded legacy token), and is validated whenever it is supplied: a
#     token belonging to no trusted identity in scope is rejected.
#
# Once resolved, every trusted field comes from current stored state; the browser
# may only add its own selection state (``replace_grid_meter``). Mutable discovery
# evidence a browser echoes — topic family, product key, serial, device id,
# ``seen_topics``, model evidence, capabilities, broker endpoint — is deliberately
# ignored (the stored value wins), so a stable selection never fails merely
# because it carries a stale echo after enrichment.

# Fields the browser legitimately supplies for a selected proposal; validated,
# never taken from stored state.
_SELECTION_FIELDS = frozenset({"id", "broker_ref", "replace_grid_meter"})
# The only echoed field validated for conflict: ``target`` selects the workflow
# (device vs grid meter), so a mismatch is rejected rather than silently applied.
# Every other discovery field is mutable trusted evidence and is taken from the
# stored proposal, never compared to the browser echo. The physical identity
# token is validated separately (:func:`_identity_token_matches`).
_TRUSTED_CONFLICT_FIELDS = ("target",)


def _proposal_issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _is_opaque_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("opaque:v1:") and len(value) > 10


def _identity_tokens(source: Any) -> set[str]:
    """Collect the validated opaque identity tokens carried by a proposal/selection."""

    if not isinstance(source, Mapping):
        return set()
    tokens: set[str] = set()
    primary = source.get(PHYSICAL_IDENTITY_TOKEN_FIELD)
    if _is_opaque_token(primary):
        tokens.add(primary)
    aliases = source.get(PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD)
    if isinstance(aliases, (list, tuple)):
        for value in aliases:
            if _is_opaque_token(value):
                tokens.add(value)
    return tokens


def _identity_token_matches(
    submitted: Mapping[str, Any],
    trusted: Mapping[str, Any],
    token_key: bytes | None = None,
) -> bool:
    """True unless the browser echoes a physical identity token the device never had.

    A submitted token is accepted when it is any of the trusted proposal's
    server-derived tokens (primary or alias), so a route-only selection stays
    valid after the device gains a serial. When ``token_key`` is supplied a
    stored case-folded token from a prior release is also accepted (that is how
    the stale selection was remapped). A token belonging to no trusted identity is
    a tampered/forged value and is rejected. Opaque tokens are keyed HMACs, so
    this can never be forged for another physical device.
    """

    submitted_token = submitted.get(PHYSICAL_IDENTITY_TOKEN_FIELD)
    if not _is_opaque_token(submitted_token):
        return True
    if submitted_token in _identity_tokens(trusted):
        return True
    return submitted_token in _legacy_folded_tokens(trusted, token_key)


def _legacy_folded_tokens(
    proposal: Mapping[str, Any], token_key: bytes | None
) -> set[str]:
    """Server-only case-folded route tokens for a trusted proposal, or ``set()``."""

    if token_key is None:
        return set()
    evidence = resolve_inverter_identity_evidence(
        proposal.get("config_fragment"), token_key=token_key
    )
    return set(legacy_route_folded_tokens(evidence, token_key))


def _identity_token_ambiguous(
    submitted: Mapping[str, Any],
    trusted_index: Mapping[tuple, dict[str, Any]],
    token_key: bytes | None,
) -> bool:
    """True when the submitted primary token identifies more than one route in scope.

    A submitted case-folded token can coincide with both one route's legacy token
    and a case-distinct route's current token (e.g. ``ROUTE-UP`` folds to
    ``route-up``, which is another current route). Counting current *and* legacy
    tokens across the broker scope makes that collision ambiguous, so a stale
    selection never silently binds to the wrong physical device — it fails closed.
    """

    token = submitted.get(PHYSICAL_IDENTITY_TOKEN_FIELD)
    if not _is_opaque_token(token):
        return False
    broker_ref = submitted.get("broker_ref")
    hits = 0
    for (_pid, ref), proposal in trusted_index.items():
        if ref != broker_ref:
            continue
        if token in _identity_tokens(proposal) or token in _legacy_folded_tokens(
            proposal, token_key
        ):
            hits += 1
            if hits > 1:
                return True
    return False


def _remap_by_identity_alias(
    submitted: Mapping[str, Any],
    trusted_index: Mapping[tuple, dict[str, Any]],
    token_key: bytes | None = None,
) -> dict[str, Any] | None:
    """Resolve a stale proposal id to the current proposal via shared alias tokens.

    Used only when the exact ``(id, broker_ref)`` lookup misses — e.g. a stored
    route-only selection whose id predates a serial enrichment. Matching is
    scoped to the submitted ``broker_ref`` (never crossing account/broker scopes)
    and must be unambiguous: zero or multiple candidates fail closed. The browser
    token is used only as an equality key against server-derived tokens; the
    returned proposal is always the trusted stored one.

    When no current token matches and ``token_key`` is supplied, a stored
    case-folded token from a prior release is tried against each proposal's
    server-only legacy tokens; it also must resolve to exactly one candidate, so
    two case-distinct routes that fold to one token fail closed.
    """

    submitted_tokens = _identity_tokens(submitted)
    if not submitted_tokens:
        return None
    broker_ref = submitted.get("broker_ref")
    scoped = [
        proposal
        for (_pid, ref), proposal in trusted_index.items()
        if ref == broker_ref
    ]
    matches = [p for p in scoped if _identity_tokens(p) & submitted_tokens]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    legacy = [p for p in scoped if _legacy_folded_tokens(p, token_key) & submitted_tokens]
    if len(legacy) == 1:
        return legacy[0]
    return None


def index_trusted_proposals(proposals: Iterable[Any]) -> dict[tuple, dict[str, Any]]:
    """Index trusted discovery proposals by their ``(id, broker_ref)`` pair.

    A physical device seen on two brokers shares one ``id`` but differs by
    ``broker_ref``, so the pair — not the id alone — is the stable lookup key.
    """

    index: dict[tuple, dict[str, Any]] = {}
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            continue
        pid = proposal.get("id")
        if not (isinstance(pid, str) and pid.strip()):
            continue
        index[(pid.strip(), proposal.get("broker_ref"))] = dict(proposal)
    return index


def resolve_trusted_proposal(
    submitted: Any,
    trusted_index: Mapping[tuple, dict[str, Any]],
    token_key: bytes | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Resolve one browser-submitted selection to its trusted stored proposal.

    Returns ``(resolved_proposal, None)`` or ``(None, issue)``. ``resolved`` is a
    deep copy of the stored proposal with only the validated browser selection
    (``replace_grid_meter``) applied. The contract is compatible: an exact
    ``(id, broker_ref)`` hit selects the current proposal directly (no token
    required), while an opaque identity token is required only to remap a stale or
    alias id — and is validated whenever supplied (a token matching no trusted
    identity in scope is rejected). Mutable discovery evidence the browser echoes
    (topic family, product key, serial, device id, ``seen_topics``, …) is ignored:
    the trusted value always wins, so a valid selection never fails merely because
    it carries a stale echo. Unknown/ambiguous ids, forged tokens, wrong broker
    scopes and target-type mismatches still fail closed.
    """

    if not isinstance(submitted, Mapping):
        return None, _proposal_issue(
            "zendure_mqtt_proposal_invalid", "MQTT proposal selection must be an object."
        )
    pid = submitted.get("id")
    if not (isinstance(pid, str) and pid.strip()):
        return None, _proposal_issue(
            "zendure_mqtt_proposal_id_missing",
            "MQTT proposal selection is missing its proposal id.",
        )
    key = (pid.strip(), submitted.get("broker_ref"))
    trusted = trusted_index.get(key)
    if trusted is None:
        # A stored selection may carry an id from before a serial enrichment
        # changed the proposal's primary identity, or a case-folded token from a
        # prior release. Fall back to the trusted alias tokens (scoped to the same
        # broker); unknown/ambiguous still fails closed.
        trusted = _remap_by_identity_alias(submitted, trusted_index, token_key)
    if trusted is None:
        return None, _proposal_issue(
            "zendure_mqtt_proposal_unknown",
            f"MQTT proposal '{pid.strip()}' is not present in current discovery "
            "state. Re-run discovery and select it again.",
        )
    if _identity_token_ambiguous(submitted, trusted_index, token_key):
        # A submitted token that identifies two case-distinct routes (one current,
        # one legacy-folded) is ambiguous even on a direct id hit — fail closed.
        return None, _proposal_issue(
            "zendure_mqtt_proposal_unknown",
            f"MQTT proposal '{pid.strip()}' is ambiguous across case-distinct "
            "routes. Re-run discovery and select it again.",
        )

    for field in _TRUSTED_CONFLICT_FIELDS:
        if field in submitted and submitted[field] is not None:
            if submitted[field] != trusted.get(field):
                return None, _proposal_issue(
                    "zendure_mqtt_proposal_conflict",
                    f"MQTT proposal field '{field}' does not match the discovered "
                    "proposal and was rejected.",
                )
    if not _identity_token_matches(submitted, trusted, token_key):
        return None, _proposal_issue(
            "zendure_mqtt_proposal_conflict",
            "MQTT proposal field 'physical_identity_token' does not match the "
            "discovered proposal and was rejected.",
        )

    resolved = copy.deepcopy(trusted)
    if "replace_grid_meter" in submitted:
        try:
            resolved["replace_grid_meter"] = require_json_bool(
                submitted["replace_grid_meter"], "replace_grid_meter"
            )
        except ValueError as exc:
            return None, _proposal_issue("zendure_mqtt_replace_invalid", str(exc))
    return resolved, None


def resolve_selected_proposals(
    submitted: Iterable[Any],
    trusted_proposals: Iterable[Any],
    token_key: bytes | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve every submitted selection against the trusted proposal set.

    Returns ``(resolved, errors)``. Any unknown, stale or forged selection lands
    in ``errors`` and is dropped from ``resolved`` so it can never reach config.
    ``token_key`` enables the server-only legacy case-folded token remap for
    selections stored before route identifiers were compared case-sensitively.
    """

    trusted_index = index_trusted_proposals(trusted_proposals)
    resolved: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entry in submitted:
        proposal, issue = resolve_trusted_proposal(entry, trusted_index, token_key)
        if issue is not None:
            errors.append(issue)
            continue
        resolved.append(proposal)
    return resolved, errors
