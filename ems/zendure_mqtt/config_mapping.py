# SPDX-License-Identifier: AGPL-3.0-or-later
"""Map read-only Zendure MQTT snapshots to safe config proposals.

Pure and read-only: it derives display/preview proposal objects from already
parsed telemetry snapshots. It never writes files, config, MQTT or hardware, and
never emits credentials (broker passwords, tokens, or the cloud app key). The
generated ``config_fragment`` is a preview draft only — it is never applied to
``config.json`` here. Output control is proposed for a device whose concrete
model and telemetry transport have a verified write method and whose write
target is addressable (see :mod:`ems.zendure_mqtt.capability`); every other
device stays telemetry-only.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ems.mqtt_control.zendure_profiles import (
    CONFIDENCE_CONFLICT,
    EVIDENCE_FULL_REPORT,
    make_hardware_profile_evidence,
    resolve_hardware_profile_evidence,
)
from ems.zendure_mqtt.capability import (
    mqtt_output_control_capability,
    proposal_output_control,
)
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
)

SOURCE = "zendure_mqtt"

# Model-evidence sources that may be seeded alongside telemetry, in addition to
# the observed full report. Keeping the real source (never re-labelling
# everything ``full_report``) is what lets a reviewed selection be decisive and
# lets conflicts be explained.
_SEEDABLE_EVIDENCE_SOURCES = frozenset(
    {
        "user_selection",
        "existing_config",
        "cloud_device_list",
        "retained_metadata",
        "product_key",
    }
)

ROLE_BATTERY_INVERTER = "battery_inverter_candidate"
ROLE_GRID_METER = "grid_meter_candidate"
ROLE_TELEMETRY_ONLY = "telemetry_only_candidate"
ROLE_UNKNOWN = "unknown_candidate"

# Lower rank wins when a device carries several families. The cloud family is
# last because its base topic is the account app key, which must stay secret.
_FAMILY_RANK = {
    FAMILY_ZENSDK_HA_SCALAR: 0,
    FAMILY_LEGACY_JSON: 1,
    FAMILY_LEGACY_JSON_ALT: 2,
    FAMILY_ZENDURE_CLOUD_SCALAR: 3,
}

# Only families with a fixed, non-secret prefix expose a base topic. The legacy
# "alt" prefix is empty and the cloud prefix is a secret app key.
_FAMILY_BASE_TOPIC = {
    FAMILY_ZENSDK_HA_SCALAR: "Zendure",
    FAMILY_LEGACY_JSON: "iot",
}

_SOC_METRICS = frozenset(
    {"electricLevel", "socLevel", "soc_set_percent", "min_soc_percent"}
)

_POWER_METRICS = frozenset(
    {
        "outputLimit",
        "inputLimit",
        "solarInputPower",
        "gridInputPower",
        "outputHomePower",
        "outputPackPower",
        "packInputPower",
        "inverseMaxPower",
    }
    | {f"solarPower{n}" for n in range(1, 7)}
)

_GRID_METRICS = frozenset(
    {
        "total_power",
        "totalPower",
        "grid_power",
        "gridPower",
        "gridInputPower",
        "gridImportPower",
        "gridExportPower",
    }
)

# EMS/Core owns the canonical D0 grid-meter type and topic rule. Only an exact
# ``Zendure/sensor/<non-empty-device>/totalPower`` observed on a local broker
# maps to a config-ready D0 grid-meter fragment.
ZENDURE_SMARTMETER_D0_GRID_METER_TYPE = "zendure_smartmeter_d0"
_SOURCE_LOCAL_MQTT = "local_mqtt"

TARGET_DEVICE = "device"
TARGET_GRID_METER = "grid_meter"

# Topic prefixes that are safe to echo back on a proposal. A cloud scalar topic
# is prefixed with the secret account app key, so it never matches and is never
# exposed. Only the fixed non-secret local prefixes are carried.
_SAFE_TOPIC_PREFIXES = ("Zendure/", "iot/")

# Emitted when a grid-power metric is seen but no exact safe local totalPower
# topic is available, so no auto-applicable D0 grid-meter fragment is produced.
WARN_GRID_METRIC_WITHOUT_TOPIC = "grid_power_metric_seen_but_topic_unavailable"


@dataclass(frozen=True)
class ZendureMqttConfigProposal:
    proposal_id: str
    source: str
    device_id: str | None
    serial_number: str | None
    product_key: str | None
    product: str | None
    topic_family: str
    base_topic: str | None
    display_name: str
    role_hint: str
    confidence: str
    capabilities: tuple[str, ...]
    metrics: tuple[str, ...]
    config_fragment: Mapping[str, Any]
    warnings: tuple[str, ...] = ()
    broker_ref: str | None = None
    connection_source: str | None = None
    # "device" (a devices[] telemetry entry) or "grid_meter" (a central grid
    # meter). A grid-meter proposal also carries a read-only grid_meter_fragment.
    target: str = TARGET_DEVICE
    grid_meter_fragment: Mapping[str, Any] | None = None
    seen_topics: tuple[str, ...] = ()
    # Whether this device is proposed as an output-controllable inverter, and the
    # machine-readable capability reason (write protocol name when supported).
    output_control_supported: bool = False
    output_control_reason: str = ""
    # Resolved hardware identity for Admin review. ``hardware_profile`` is the
    # concrete registry model (persisted even when read-only), ``confidence`` is
    # exact/canonical/ambiguous/unknown/conflict, ``evidence`` names the winning
    # or conflicting source(s), and ``control_block_reason`` says why control is
    # unavailable (e.g. ``hardware_profile_conflict``).
    hardware_profile: str | None = None
    hardware_profile_confidence: str = "unknown"
    hardware_profile_evidence: str = ""
    # Sanitized ``{source, value}`` provenance of every model observation, in
    # observed order — never a raw product key.
    hardware_profile_evidence_sources: tuple = ()
    control_block_reason: str | None = None


@dataclass
class _DeviceView:
    """Read-only accumulation of one logical device across snapshots."""

    device_id: str | None = None
    serial_number: str | None = None
    product_key: str | None = None
    product: str | None = None
    # Every distinct product/model string observed for this logical device. Kept
    # in full (not collapsed to the first) so conflicting model evidence from two
    # sources is detectable rather than silently resolved to whichever arrived
    # first.
    products: set = field(default_factory=set)
    # Source-tagged model observations ``(source, value)`` in insertion order.
    # Telemetry products are ``full_report``; other sources may be seeded so the
    # real provenance is preserved rather than collapsed to ``full_report``.
    model_evidence: list = field(default_factory=list)
    topic_families: set = field(default_factory=set)
    capabilities: set = field(default_factory=set)
    metric_keys: set = field(default_factory=set)
    seen_topics: set = field(default_factory=set)

    def merge(self, snap: ZendureMqttSnapshot) -> None:
        self.device_id = self.device_id or snap.device_id
        self.serial_number = self.serial_number or snap.serial_number
        self.product_key = self.product_key or snap.product_key
        self.product = self.product or snap.product
        if isinstance(snap.product, str) and snap.product.strip():
            self.products.add(snap.product.strip())
            self.add_model_evidence(EVIDENCE_FULL_REPORT, snap.product.strip())
        self.topic_families |= set(snap.topic_families)
        self.capabilities |= set(snap.capabilities)
        self.metric_keys |= set(snap.metrics)
        self.seen_topics |= {t for t in snap.seen_topics if isinstance(t, str) and t}

    def add_model_evidence(self, source: str, value) -> None:
        """Record one source-tagged model observation, de-duplicated."""

        if not (isinstance(value, str) and value.strip()):
            return
        entry = (str(source), value.strip())
        if entry not in self.model_evidence:
            self.model_evidence.append(entry)


def _logical_key(snap: ZendureMqttSnapshot):
    if snap.serial_number:
        return ("sn", snap.serial_number)
    if snap.device_id:
        return ("dev", snap.device_id)
    if snap.product_key:
        return ("pk", snap.product_key)
    return ("topic", _primary_family(snap.topic_families))


def _primary_family(topic_families) -> str:
    known = [family for family in topic_families if family in _FAMILY_RANK]
    if not known:
        return FAMILY_UNKNOWN
    return min(known, key=lambda family: _FAMILY_RANK[family])


def _has_power(metric_keys: set) -> bool:
    # A recognized grid-power metric (e.g. totalPower) counts as power evidence,
    # so a D0 that only reports totalPower is not left at low confidence.
    return bool(metric_keys & (_POWER_METRICS | _GRID_METRICS))


def _role_hint(capabilities: set, metric_keys: set) -> str:
    if "battery_storage" in capabilities and "output_control" in capabilities:
        return ROLE_BATTERY_INVERTER
    grid_like = bool(metric_keys & _GRID_METRICS)
    non_battery = not capabilities & {"battery_storage", "output_control", "pv_input"}
    if grid_like and non_battery:
        return ROLE_GRID_METER
    if metric_keys and "output_control" not in capabilities:
        return ROLE_TELEMETRY_ONLY
    return ROLE_UNKNOWN


def _d0_total_power_topic(view: _DeviceView) -> str | None:
    """Return the exact observed ``Zendure/sensor/<device>/totalPower`` topic.

    Prefers real observed evidence over a constructed topic. The exact canonical
    shape (number/write channel, extra path segments, custom or cloud prefixes,
    an empty device segment and MQTT wildcards are all rejected) is enforced by
    the EMS-owned validator. Returns ``None`` when no exact safe topic was seen.
    """

    from ems.config import is_zendure_smartmeter_d0_topic

    for topic in view.seen_topics:
        if isinstance(topic, str) and is_zendure_smartmeter_d0_topic(topic):
            return topic
    return None


def _confidence(topic_family: str, role_hint: str, has_power: bool, has_soc: bool) -> str:
    if topic_family == FAMILY_UNKNOWN or role_hint == ROLE_UNKNOWN:
        return "low"
    if has_power and has_soc:
        return "high"
    if has_power or has_soc:
        return "medium"
    return "low"


def _display_label(view: _DeviceView) -> str:
    return view.product or view.serial_number or view.device_id or "device"


def _proposal_id(view: _DeviceView, topic_family: str) -> str:
    token = (
        view.serial_number
        or view.device_id
        or view.product_key
        or topic_family
    )
    return f"zendure-mqtt:{token}"


def _config_fragment(
    view: _DeviceView,
    topic_family: str,
    base_topic: str | None,
    has_power: bool,
    has_soc: bool,
    *,
    source: str | None = None,
    broker_ref: str | None = None,
    output_control: bool = False,
    write_protocol: str | None = None,
    hardware_profile: str | None = None,
    power_write_profile: str | None = None,
    display_label: str | None = None,
) -> dict[str, Any]:
    mqtt: dict[str, Any] = {}
    # The broker profile identity leads so the connection method is explicit.
    if broker_ref:
        mqtt["broker_ref"] = broker_ref
    if source:
        mqtt["source"] = source
    mqtt["topic_family"] = topic_family
    mqtt["base_topic"] = base_topic
    # A control device pins its resolved write method into config so it is never
    # silently re-inferred at runtime.
    if output_control and write_protocol:
        mqtt["write_protocol"] = write_protocol
    if view.device_id:
        mqtt["device_id"] = view.device_id
    if view.product_key:
        mqtt["product_key"] = view.product_key
    # The cloud app key is a secret and is never proposed as config.
    mqtt["app_key"] = None

    fragment: dict[str, Any] = {"type": SOURCE}
    # A known physical serial is the strongest cross-adapter identity, so it is
    # promoted to the top level where duplicate detection reads it.
    if view.serial_number:
        fragment["serial_number"] = view.serial_number
    # The resolved hardware identity is pinned into config so the runtime write
    # adapter is chosen from the verified model, never re-inferred from telemetry.
    # It is persisted whenever a concrete model was identified — including a
    # read-only model (ACE 1500, SuperBase) — so a future firmware/support upgrade
    # never has to rediscover it. power_write_profile is informational and
    # re-validated from the registry (``telemetry_only`` for a read-only model).
    if hardware_profile:
        fragment["hardware_profile"] = hardware_profile
        if power_write_profile:
            fragment["power_write_profile"] = power_write_profile
    fragment["enabled"] = True
    fragment["name"] = f"Zendure MQTT {display_label or _display_label(view)}"
    fragment["mqtt"] = mqtt
    fragment["capabilities"] = {
        "read_power": has_power,
        "read_soc": has_soc,
        # Output control is enabled for a supported, write-addressable device.
        "write_output_limit": bool(output_control),
    }
    return fragment


def _sanitized_evidence_sources(model_evidence, product_key) -> tuple:
    """Sanitized ``{source, value}`` provenance, never exposing a raw product key.

    The values are observed product/model strings (not secrets), but an entry that
    happens to equal the device's secret product key is dropped defensively.
    """

    secret = product_key if isinstance(product_key, str) and product_key.strip() else None
    entries = []
    for src, value in model_evidence:
        if secret is not None and value == secret:
            continue
        entries.append({"source": str(src), "value": value})
    return tuple(entries)


def _build_proposal(
    view: _DeviceView,
    *,
    source: str | None = None,
    broker_ref: str | None = None,
    display_label: str | None = None,
) -> ZendureMqttConfigProposal:
    topic_family = _primary_family(view.topic_families)
    base_topic = _FAMILY_BASE_TOPIC.get(topic_family)
    has_power = _has_power(view.metric_keys)
    has_soc = bool(view.metric_keys & _SOC_METRICS)
    role_hint = _role_hint(view.capabilities, view.metric_keys)
    confidence = _confidence(topic_family, role_hint, has_power, has_soc)

    # Hardware identity is resolved from all product/model evidence, never from
    # the topic family, and each observation keeps its real source (telemetry
    # full report, a reviewed user selection, an existing config, ...). Two exact
    # signals for different models resolve to a read-only conflict unless a
    # decisive reviewed source resolves it; output control additionally needs a
    # known writable model that is transport-compatible and addressable.
    model_evidence = view.model_evidence or [
        (EVIDENCE_FULL_REPORT, product) for product in sorted(view.products)
    ]
    resolution = resolve_hardware_profile_evidence(
        [make_hardware_profile_evidence(src, value) for src, value in model_evidence]
    )
    evidence_sources = _sanitized_evidence_sources(model_evidence, view.product_key)
    is_conflict = resolution.confidence == CONFIDENCE_CONFLICT
    hardware_profile = None if is_conflict else resolution.profile_id
    capability = mqtt_output_control_capability(
        topic_family=topic_family,
        hardware_profile=hardware_profile,
        observed_capabilities=view.capabilities,
    )
    output_control, control_reason = proposal_output_control(capability)
    # Capability alone cannot derive a concrete write topic. Discovery normally
    # supplies the product key; without one the proposal remains telemetry-only
    # and reports the missing target instead of generating an invalid config.
    if output_control and not view.product_key:
        output_control = False
        control_reason = "write_target_missing"
    control_block_reason = (
        "hardware_profile_conflict"
        if is_conflict
        else (
            "write_target_missing"
            if control_reason == "write_target_missing"
            else capability.block_reason
        )
    )

    warnings = []
    if is_conflict:
        warnings.append("hardware_profile_conflict")
    if role_hint == ROLE_UNKNOWN:
        warnings.append("insufficient_telemetry")
    if topic_family == FAMILY_ZENDURE_CLOUD_SCALAR:
        warnings.append("cloud_base_topic_hidden")
    if topic_family == FAMILY_UNKNOWN:
        warnings.append("unknown_topic_family")

    target = TARGET_DEVICE
    grid_meter_fragment = None
    if role_hint == ROLE_GRID_METER:
        # A D0 grid-meter target is only auto-applicable from a safe local
        # zensdk_ha_scalar topic. Cloud sources and weak evidence stay a device
        # proposal (with a warning) so a secret cloud topic is never mapped.
        d0_topic = None
        if source == _SOURCE_LOCAL_MQTT and topic_family == FAMILY_ZENSDK_HA_SCALAR:
            d0_topic = _d0_total_power_topic(view)
        if d0_topic is not None:
            target = TARGET_GRID_METER
            grid_meter_fragment = _grid_meter_fragment(d0_topic, broker_ref)
        else:
            warnings.append(WARN_GRID_METRIC_WITHOUT_TOPIC)

    # A grid-meter target is never an output-controlled device.
    if target == TARGET_GRID_METER:
        output_control = False

    return ZendureMqttConfigProposal(
        proposal_id=_proposal_id(view, topic_family),
        source=SOURCE,
        device_id=view.device_id,
        serial_number=view.serial_number,
        product_key=view.product_key,
        product=view.product,
        topic_family=topic_family,
        base_topic=base_topic,
        display_name=f"Zendure MQTT {display_label or _display_label(view)}",
        role_hint=role_hint,
        confidence=confidence,
        capabilities=tuple(sorted(view.capabilities)),
        metrics=tuple(sorted(view.metric_keys)),
        config_fragment=_config_fragment(
            view,
            topic_family,
            base_topic,
            has_power,
            has_soc,
            source=source,
            broker_ref=broker_ref,
            display_label=display_label,
            output_control=output_control,
            write_protocol=capability.write_protocol,
            hardware_profile=capability.hardware_profile,
            power_write_profile=capability.power_write_profile,
        ),
        warnings=tuple(warnings),
        broker_ref=broker_ref,
        connection_source=source,
        target=target,
        grid_meter_fragment=grid_meter_fragment,
        seen_topics=_safe_seen_topics(view.seen_topics),
        output_control_supported=output_control,
        output_control_reason=control_reason,
        hardware_profile=hardware_profile,
        hardware_profile_confidence=resolution.confidence,
        hardware_profile_evidence=resolution.evidence,
        hardware_profile_evidence_sources=evidence_sources,
        control_block_reason=control_block_reason,
    )


def _safe_seen_topics(seen_topics: set) -> tuple[str, ...]:
    return tuple(
        sorted(
            topic
            for topic in seen_topics
            if isinstance(topic, str)
            and topic.startswith(_SAFE_TOPIC_PREFIXES)
        )
    )


def _grid_meter_fragment(topic: str, broker_ref: str | None) -> dict[str, Any]:
    """Build the read-only D0 grid-meter fragment for a selected proposal.

    It references a named broker profile (never inlines credentials) and forces
    the number payload format. It never advertises write/control capability.
    """

    mqtt: dict[str, Any] = {}
    if broker_ref:
        mqtt["broker_ref"] = broker_ref
    mqtt["topic"] = topic
    mqtt["payload_format"] = "number"
    mqtt["max_age_seconds"] = 15
    return {
        "type": ZENDURE_SMARTMETER_D0_GRID_METER_TYPE,
        "mqtt": mqtt,
    }


def _seed_view_evidence(view: _DeviceView, seed_evidence) -> None:
    """Seed non-telemetry model evidence (existing config, user selection, ...).

    ``seed_evidence`` maps a device identity (serial number or device id) to a
    list of ``(source, value)`` observations. Only recognized non-telemetry
    sources are accepted; the real source is preserved so a reviewed selection can
    be decisive and a conflict is explainable.
    """

    if not isinstance(seed_evidence, dict):
        return
    for identity in (view.serial_number, view.device_id):
        entries = seed_evidence.get(identity)
        if not entries:
            continue
        for source, value in entries:
            if source in _SEEDABLE_EVIDENCE_SOURCES:
                view.add_model_evidence(source, value)
        break


def map_snapshots_to_proposals(
    snapshots: Iterable[ZendureMqttSnapshot],
    *,
    source: str | None = None,
    broker_ref: str | None = None,
    seed_evidence: dict | None = None,
) -> tuple[ZendureMqttConfigProposal, ...]:
    """Convert read-only snapshots into deduplicated config proposals.

    Snapshots referring to the same logical device (by serial, else device id,
    else product key) are merged into a single proposal. When ``source``/
    ``broker_ref`` are given they are recorded on the proposal and its config
    fragment so the connection method stays explicit. ``seed_evidence`` (keyed by
    serial/device id) adds non-telemetry model evidence — an existing config, a
    reviewed user selection — with its real source preserved. Input snapshots are
    never mutated.
    """

    views: dict[Any, _DeviceView] = {}
    order: list[Any] = []
    for snap in snapshots:
        key = _logical_key(snap)
        view = views.get(key)
        if view is None:
            view = _DeviceView()
            views[key] = view
            order.append(key)
        view.merge(snap)
    for key in order:
        _seed_view_evidence(views[key], seed_evidence)
    # Device names are the EMS runtime identity key, so two units of the same
    # model must not propose the identical name: a shared product label gets the
    # per-device identity (serial, else device id, else product key) appended.
    labels: dict[Any, str] = {}
    label_counts: dict[str, int] = {}
    for key in order:
        label = _display_label(views[key])
        labels[key] = label
        label_counts[label] = label_counts.get(label, 0) + 1
    for key in order:
        view = views[key]
        if label_counts[labels[key]] > 1:
            identity = view.serial_number or view.device_id or view.product_key
            if identity:
                labels[key] = f"{labels[key]} ({identity})"
    return tuple(
        _build_proposal(
            views[key],
            source=source,
            broker_ref=broker_ref,
            display_label=labels[key],
        )
        for key in order
    )


def map_snapshot_to_proposal(
    snapshot: ZendureMqttSnapshot,
    *,
    source: str | None = None,
    broker_ref: str | None = None,
) -> ZendureMqttConfigProposal:
    """Convenience wrapper for a single snapshot."""

    return map_snapshots_to_proposals(
        [snapshot], source=source, broker_ref=broker_ref
    )[0]
