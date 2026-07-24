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
from ems.config import parse_mqtt_port, require_json_bool, resolve_mqtt_tls_metadata
from ems.zendure_mqtt.config_entries import (
    normalized_broker_identity,
    stable_local_broker_ref,
)
from ems.zendure_mqtt.config_mapping import (
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


# Admin cloud discovery names its TLS modes itself (TLS is always on for the
# cloud broker; only certificate verification differs). The shared normalizer
# does not know those names, so they are translated to canonical modes before
# resolution — otherwise every real cloud candidate would be dropped as
# carrying an "unknown TLS mode" — and the canonical name is what a proposal
# endpoint carries, so no Admin-only mode string leaks downstream.
_ADMIN_TLS_MODE_ALIASES = {
    "encrypted_no_verify": "insecure_no_verify",
    "pinned_ca": "insecure_no_verify",
}


def _canonical_tls_mode(observation: Mapping[str, Any]) -> Any:
    mode = observation.get("tls_mode")
    key = str(mode or "").strip().lower()
    return _ADMIN_TLS_MODE_ALIASES.get(key, mode)


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

# Markers used when masking display-only ids (…abcd / ••••). A value carrying
# either is not a usable identifier and must never reach config.
_MASK_MARKERS = ("•", "…")


def is_masked_zendure_identifier(value: Any) -> bool:
    """True if ``value`` is a display-masked id unsafe to write into config."""

    return isinstance(value, str) and any(marker in value for marker in _MASK_MARKERS)


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
    source_type = str(observation.get("source_type") or SOURCE_LOCAL_MQTT)
    if source_type == SOURCE_ZENDURE_CLOUD_MQTT and serial is None:
        # A cloud route id may be an account-scoped deviceKey. Without a
        # physical serial there is no browser-safe identity for the proposal,
        # so keep the existing fail-closed behaviour even in the trusted view.
        return None
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


def proposals_from_sources(
    brokers: Iterable[Any], cloud_candidates: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    """Combine local broker devices and cloud candidates into one proposal set.

    This is the single trusted proposal source shared by the proposals endpoint
    and the config-preview trust resolve, so a selection made in the review UI
    always resolves against the same set it was rendered from. Local proposals
    keep their generation stamping (cloud candidates take no part in the local
    broker store's generation/TTL bookkeeping). A cloud candidate whose
    identifier already appears on a discovered local broker is dropped: the
    local connection wins, so one physical device is never offered twice.
    """

    proposals = proposals_from_brokers(brokers)
    local_tokens = {
        token
        for proposal in proposals
        for token in (proposal.get("serial_number"), proposal.get("device_id"))
        if token
    }
    for proposal in build_proposals(cloud_candidates):
        if proposal.get("serial_number") in local_tokens:
            continue
        if proposal.get("device_id") in local_tokens:
            continue
        proposals.append(proposal)
    return proposals


# --- Server-side proposal trust boundary ------------------------------------
#
# The browser is never authoritative for a proposal's identity, broker or
# capabilities. It receives a stable ``id`` (plus its ``broker_ref``) and, on
# submit, that pair is resolved back to the full proposal held in current
# discovery state. Every trusted field then comes from stored state; the browser
# may only add its selection (currently ``replace_grid_meter``). A submitted
# field that conflicts with the stored proposal — a forged serial, broker host,
# topic family, injected ``seen_topics`` — is rejected rather than trusted.

# Fields the browser legitimately supplies for a selected proposal; validated,
# never taken from stored state.
_SELECTION_FIELDS = frozenset({"id", "broker_ref", "replace_grid_meter"})
# Non-secret identity/connection fields a browser echo must match exactly. A
# divergent value means a tampered submission and is rejected.
_TRUSTED_CONFLICT_FIELDS = (
    "target",
    "serial_number",
    "topic_family",
    "connection_source",
    "broker_host",
    "broker_port",
    "broker_tls",
    "product_key",
)


def _proposal_issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


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


def _topic_multiset(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return []
    return sorted(str(t).strip() for t in value if isinstance(t, str) and t.strip())


def resolve_trusted_proposal(
    submitted: Any, trusted_index: Mapping[tuple, dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Resolve one browser-submitted selection to its trusted stored proposal.

    Returns ``(resolved_proposal, None)`` or ``(None, issue)``. ``resolved`` is a
    deep copy of the stored proposal with only the validated browser selection
    (``replace_grid_meter``) applied, so no browser-supplied identity, broker or
    capability value ever reaches the generated config.
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
        return None, _proposal_issue(
            "zendure_mqtt_proposal_unknown",
            f"MQTT proposal '{pid.strip()}' is not present in current discovery "
            "state. Re-run discovery and select it again.",
        )

    for field in _TRUSTED_CONFLICT_FIELDS:
        if field in submitted and submitted[field] is not None:
            if submitted[field] != trusted.get(field):
                return None, _proposal_issue(
                    "zendure_mqtt_proposal_conflict",
                    f"MQTT proposal field '{field}' does not match the discovered "
                    "proposal and was rejected.",
                )
    # Cloud public proposals deliberately expose the physical serial as their
    # display device id while the trusted proposal carries the observed MQTT
    # route id (deviceKey). Accept either trusted identity as an echoed browser
    # value, but always return the trusted proposal so neither can be injected.
    if "device_id" in submitted and submitted["device_id"] is not None:
        accepted_device_ids = {
            value
            for value in (trusted.get("device_id"), trusted.get("serial_number"))
            if value is not None
        }
        if submitted["device_id"] not in accepted_device_ids:
            return None, _proposal_issue(
                "zendure_mqtt_proposal_conflict",
                "MQTT proposal field 'device_id' does not match the discovered "
                "proposal and was rejected.",
            )
    submitted_topics = _topic_multiset(submitted.get("seen_topics"))
    if submitted_topics is not None and submitted_topics != _topic_multiset(
        trusted.get("seen_topics")
    ):
        return None, _proposal_issue(
            "zendure_mqtt_proposal_conflict",
            "MQTT proposal observed topics do not match the discovered proposal "
            "and were rejected.",
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
    submitted: Iterable[Any], trusted_proposals: Iterable[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve every submitted selection against the trusted proposal set.

    Returns ``(resolved, errors)``. Any unknown, stale or forged selection lands
    in ``errors`` and is dropped from ``resolved`` so it can never reach config.
    """

    trusted_index = index_trusted_proposals(trusted_proposals)
    resolved: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entry in submitted:
        proposal, issue = resolve_trusted_proposal(entry, trusted_index)
        if issue is not None:
            errors.append(issue)
            continue
        resolved.append(proposal)
    return resolved, errors
