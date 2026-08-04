# SPDX-License-Identifier: AGPL-3.0-or-later
"""Central Zendure hardware / write-profile registry.

The single source of truth shared by discovery, proposal generation, config
validation, runtime device construction, write-adapter selection and
diagnostics. Hardware identity is resolved from an explicit product/model name,
never from a topic family and never from a substring guess.

A device may only accept a power write when its resolved hardware profile carries
an *implemented* ``power_write_profile`` and the requested operation is supported;
every other device — unknown, deferred (ACE 1500), or conditionally excluded
(SuperBase) — stays telemetry-only.
"""

import re
from dataclasses import dataclass

# Verified write-protocol families. ``telemetry_only`` is intentionally NOT an
# implemented write profile: a device that resolves to it can never publish.
WRITE_PROFILE_ZENSDK_PROPERTIES = "zensdk_properties_write"
WRITE_PROFILE_LEGACY_HUB = "legacy_hub_device_automation"
WRITE_PROFILE_LEGACY_OBJECT = "legacy_object_device_automation"
WRITE_PROFILE_TELEMETRY_ONLY = "telemetry_only"

IMPLEMENTED_WRITE_PROFILES = frozenset(
    {
        WRITE_PROFILE_ZENSDK_PROPERTIES,
        WRITE_PROFILE_LEGACY_HUB,
        WRITE_PROFILE_LEGACY_OBJECT,
    }
)

# Neutral per-device operations. Sign of the controller's target selects one.
OPERATION_DISCHARGE = "discharge"
OPERATION_IDLE = "idle"
OPERATION_CHARGE = "charge"

# Validation maturity of a profile, surfaced to operators (never a write gate).
VALIDATION_EXISTING_SUPPORT = "existing_support"
VALIDATION_COMMUNITY_REQUIRED = "community_hardware_validation_required"
VALIDATION_DEFERRED = "deferred"
VALIDATION_TELEMETRY_ONLY = "telemetry_only"


@dataclass(frozen=True)
class ZendureHardwareProfile:
    canonical_name: str
    display_label: str
    hardware_generation: str
    aliases: tuple[str, ...]
    power_write_profile: str
    supports_discharge: bool
    supports_idle: bool
    supports_charge: bool
    validation_status: str
    # Source-backed MQTT-writable properties; empty = no verified contract.
    state_property_writes: tuple[str, ...] = ()

    @property
    def writable(self) -> bool:
        return self.power_write_profile in IMPLEMENTED_WRITE_PROFILES

    def supports_property_write(self, name) -> bool:
        return name in self.state_property_writes

    @property
    def supported_operations(self) -> tuple[str, ...]:
        ops = []
        if self.supports_discharge:
            ops.append(OPERATION_DISCHARGE)
        if self.supports_idle:
            ops.append(OPERATION_IDLE)
        if self.supports_charge:
            ops.append(OPERATION_CHARGE)
        return tuple(ops)

    def supports_operation(self, operation: str) -> bool:
        return operation in self.supported_operations


_ZENSDK_STATE_PROPERTIES = ("smartMode", "acMode", "outputLimit", "inputLimit")

_HARDWARE_PROFILES: tuple[ZendureHardwareProfile, ...] = (
    ZendureHardwareProfile(
        canonical_name="solarflow_800",
        display_label="SolarFlow 800",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 800", "SolarFlow800"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_800_pro",
        display_label="SolarFlow 800 Pro",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 800 Pro", "SolarFlow800Pro"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_800_pro_2",
        display_label="SolarFlow 800 Pro 2",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 800 Pro 2", "SolarFlow800Pro2"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_800_plus",
        display_label="SolarFlow 800 Plus",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 800 Plus", "SolarFlow800Plus"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_1600_ac_plus",
        display_label="SolarFlow 1600 AC+",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 1600 AC+", "SolarFlow1600AC+"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_2400_ac",
        display_label="SolarFlow 2400 AC",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 2400 AC", "SolarFlow2400AC"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_2400_ac_plus",
        display_label="SolarFlow 2400 AC+",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 2400 AC+", "SolarFlow2400AC+"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_2400_pro",
        display_label="SolarFlow 2400 Pro",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 2400 Pro", "SolarFlow2400Pro"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="solarflow_4000_ac_plus",
        display_label="SolarFlow 4000 AC+",
        hardware_generation="solarflow_zensdk",
        aliases=("SolarFlow 4000 AC+", "SolarFlow4000AC+"),
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_EXISTING_SUPPORT,
        state_property_writes=_ZENSDK_STATE_PROPERTIES,
    ),
    ZendureHardwareProfile(
        canonical_name="hyper_2000",
        display_label="Hyper 2000",
        hardware_generation="hub_hyper_legacy",
        aliases=("Hyper 2000", "Hyper 2000 3.0", "Hyper2000"),
        power_write_profile=WRITE_PROFILE_LEGACY_OBJECT,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=True,
        validation_status=VALIDATION_COMMUNITY_REQUIRED,
    ),
    ZendureHardwareProfile(
        canonical_name="aio_2400",
        display_label="AIO 2400",
        hardware_generation="hub_hyper_legacy",
        aliases=("AIO 2400", "SolarFlow AIO ZY", "AIO2400"),
        power_write_profile=WRITE_PROFILE_LEGACY_OBJECT,
        supports_discharge=True,
        supports_idle=True,
        # AIO 2400 must reject negative (charge) targets.
        supports_charge=False,
        validation_status=VALIDATION_COMMUNITY_REQUIRED,
    ),
    ZendureHardwareProfile(
        canonical_name="hub_1200",
        display_label="Hub 1200",
        hardware_generation="hub_hyper_legacy",
        aliases=("Hub 1200", "SolarFlow 2.0", "Hub1200"),
        power_write_profile=WRITE_PROFILE_LEGACY_HUB,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_COMMUNITY_REQUIRED,
    ),
    ZendureHardwareProfile(
        canonical_name="hub_2000",
        display_label="Hub 2000",
        hardware_generation="hub_hyper_legacy",
        aliases=("Hub 2000", "SolarFlow Hub 2000", "Hub2000"),
        power_write_profile=WRITE_PROFILE_LEGACY_HUB,
        supports_discharge=True,
        supports_idle=True,
        supports_charge=False,
        validation_status=VALIDATION_COMMUNITY_REQUIRED,
    ),
    ZendureHardwareProfile(
        canonical_name="ace_1500",
        display_label="ACE 1500",
        hardware_generation="hub_hyper_legacy",
        aliases=("ACE 1500", "ACE1500"),
        power_write_profile=WRITE_PROFILE_TELEMETRY_ONLY,
        supports_discharge=False,
        supports_idle=False,
        supports_charge=False,
        # Paired and standalone modes need different semantics + flash-write
        # protection; deferred to a later release.
        validation_status=VALIDATION_DEFERRED,
    ),
    ZendureHardwareProfile(
        canonical_name="superbase_v4600",
        display_label="SuperBase V4600",
        hardware_generation="hub_hyper_legacy",
        aliases=("SuperBase V4600",),
        power_write_profile=WRITE_PROFILE_TELEMETRY_ONLY,
        supports_discharge=False,
        supports_idle=False,
        supports_charge=False,
        # The shared object contract is not verified against fixtures here.
        validation_status=VALIDATION_DEFERRED,
    ),
    ZendureHardwareProfile(
        canonical_name="superbase_v6400",
        display_label="SuperBase V6400",
        hardware_generation="hub_hyper_legacy",
        aliases=("SuperBase V6400",),
        power_write_profile=WRITE_PROFILE_TELEMETRY_ONLY,
        supports_discharge=False,
        supports_idle=False,
        supports_charge=False,
        validation_status=VALIDATION_DEFERRED,
    ),
)

HARDWARE_PROFILES: dict[str, ZendureHardwareProfile] = {
    prof.canonical_name: prof for prof in _HARDWARE_PROFILES
}


# Resolution confidence. Only ``exact``/``canonical`` identify a specific model
# and may therefore drive a hardware write; ``ambiguous``/``unknown`` stay
# telemetry-only. ``ambiguous`` means a Zendure family/brand word was recognized
# but the exact model was not, ``unknown`` means nothing matched. ``conflict``
# means two exact signals disagreed on the model — it is never writable.
CONFIDENCE_EXACT = "exact"
CONFIDENCE_CANONICAL = "canonical"
CONFIDENCE_AMBIGUOUS = "ambiguous"
CONFIDENCE_UNKNOWN = "unknown"
CONFIDENCE_CONFLICT = "conflict"

WRITABLE_CONFIDENCES = frozenset({CONFIDENCE_EXACT, CONFIDENCE_CANONICAL})

# Family/brand words that name a product line but not a specific model. A value
# made up entirely of these tokens is ambiguous, never a writable model: a bare
# "Hyper", "AIO" or "SolarFlow" must never inherit a write profile.
_FAMILY_BRAND_TOKENS = frozenset(
    {"hub", "hyper", "aio", "ace", "superbase", "solar", "flow", "solarflow"}
)


def _normalize(value) -> str:
    """Normalize a product string to lowercase space-separated model tokens.

    Real Zendure product strings are camelCase and glue letters to digits
    (``solarFlow800Pro``). Splitting camelCase and letter/digit boundaries before
    collapsing punctuation lets a glued string match the same alias as its
    spaced form, without dropping the numeric model identifier.
    """

    text = str(value or "")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)  # camelCase boundary
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)  # letter -> digit
    text = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", text)  # digit -> letter
    text = re.sub(r"[^A-Za-z0-9]+", " ", text.lower())
    return text.strip()


@dataclass(frozen=True)
class HardwareProfileResolution:
    """Structured result of resolving a product string to a hardware profile."""

    profile_id: str | None
    confidence: str
    evidence: str
    matched_alias: str | None
    source_value: str | None

    @property
    def writable(self) -> bool:
        """True only when this resolution can authorize a real hardware write.

        Confidence alone is not authority: a telemetry-only model (ACE 1500,
        SuperBase) resolves with exact confidence but carries no implemented
        write profile. Writability requires an exact/canonical match to a
        resolved profile that is implemented-writable with at least one
        supported operation.
        """

        if self.profile_id is None or self.confidence not in WRITABLE_CONFIDENCES:
            return False
        profile = HARDWARE_PROFILES.get(self.profile_id)
        return (
            profile is not None
            and profile.writable
            and bool(profile.supported_operations)
        )


# Exact canonical id (e.g. "hyper_2000" pinned in config), matched case-folded
# on the raw string so it stays distinct from the human alias "Hyper 2000".
_CANONICAL_RAW: dict[str, ZendureHardwareProfile] = {
    _prof.canonical_name.strip().lower(): _prof for _prof in _HARDWARE_PROFILES
}
_CANONICAL_INDEX: dict[str, ZendureHardwareProfile] = {
    _normalize(_prof.canonical_name): _prof for _prof in _HARDWARE_PROFILES
}
# Normalized alias -> (profile, original alias); the first-declared (primary)
# alias wins when two spellings normalize to the same key.
_ALIAS_INDEX: dict[str, tuple[ZendureHardwareProfile, str]] = {}
for _prof in _HARDWARE_PROFILES:
    for _alias in _prof.aliases:
        _ALIAS_INDEX.setdefault(_normalize(_alias), (_prof, _alias))


def _is_ambiguous_family(name: str) -> bool:
    tokens = [t for t in name.split(" ") if t]
    return bool(tokens) and all(t in _FAMILY_BRAND_TOKENS for t in tokens)


def resolve_hardware_profile_detail(product) -> HardwareProfileResolution:
    """Resolve a product/model string to structured resolution metadata.

    Identity is proven only by an exact (normalized) alias or canonical name.
    Substrings never match (``SuperHub`` is not ``hub``), a bare family/brand
    word is ``ambiguous``, and everything else is ``unknown``; none of the latter
    two produces a writable profile.
    """

    source = None if product is None else str(product)
    raw = str(product or "").strip().lower()
    canonical_raw = _CANONICAL_RAW.get(raw)
    if canonical_raw is not None:
        return HardwareProfileResolution(
            canonical_raw.canonical_name,
            CONFIDENCE_CANONICAL,
            f"canonical:{canonical_raw.canonical_name}",
            None,
            source,
        )
    name = _normalize(product)
    if not name:
        return HardwareProfileResolution(None, CONFIDENCE_UNKNOWN, "empty", None, source)
    alias_hit = _ALIAS_INDEX.get(name)
    if alias_hit is not None:
        profile, alias = alias_hit
        return HardwareProfileResolution(
            profile.canonical_name, CONFIDENCE_EXACT, f"alias:{alias}", alias, source
        )
    canonical = _CANONICAL_INDEX.get(name)
    if canonical is not None:
        return HardwareProfileResolution(
            canonical.canonical_name,
            CONFIDENCE_CANONICAL,
            f"canonical:{canonical.canonical_name}",
            None,
            source,
        )
    if _is_ambiguous_family(name):
        return HardwareProfileResolution(
            None, CONFIDENCE_AMBIGUOUS, f"ambiguous_family:{name}", None, source
        )
    return HardwareProfileResolution(None, CONFIDENCE_UNKNOWN, "no_match", None, source)


# Evidence sources for a device's hardware model, in precedence order. A reviewed
# user selection and an already-persisted profile are *decisive* (the operator
# has committed to them); the remaining discovery sources are corroborating —
# agreeing exacts stay exact, disagreeing exacts become a conflict.
EVIDENCE_USER_SELECTION = "user_selection"
EVIDENCE_EXISTING_CONFIG = "existing_config"
EVIDENCE_CLOUD_DEVICE_LIST = "cloud_device_list"
EVIDENCE_FULL_REPORT = "full_report"
EVIDENCE_RETAINED_METADATA = "retained_metadata"
EVIDENCE_PRODUCT_KEY = "product_key"

# Sources whose exact evidence overrides lower tiers instead of conflicting with
# them: the operator has reviewed/committed, so their choice is authoritative.
_DECISIVE_EVIDENCE_SOURCES = (EVIDENCE_USER_SELECTION, EVIDENCE_EXISTING_CONFIG)


@dataclass(frozen=True)
class HardwareProfileEvidence:
    """One observation of a device's hardware model from a named source."""

    source: str
    value: str
    resolved_profile: str | None
    confidence: str
    observed_at: float | None = None


def make_hardware_profile_evidence(
    source, value, *, observed_at=None
) -> HardwareProfileEvidence:
    """Build an evidence record by resolving ``value`` through the registry."""

    detail = resolve_hardware_profile_detail(value)
    return HardwareProfileEvidence(
        source=str(source),
        value="" if value is None else str(value),
        resolved_profile=detail.profile_id,
        confidence=detail.confidence,
        observed_at=observed_at,
    )


def resolve_hardware_profile_evidence(evidences) -> HardwareProfileResolution:
    """Resolve many model observations into one resolution, detecting conflicts.

    Only exact/canonical evidence identifies a model. A decisive source (a
    reviewed user selection or an already-persisted profile) wins outright.
    Otherwise the corroborating discovery evidence must agree: two exact signals
    for *different* models produce a ``conflict`` that is never writable, so a
    misidentified device can never authorize a write. Weaker (ambiguous/unknown)
    evidence never overrides and never causes a conflict.
    """

    identified = [
        ev
        for ev in evidences
        if ev.resolved_profile is not None and ev.confidence in WRITABLE_CONFIDENCES
    ]
    if not identified:
        return HardwareProfileResolution(None, CONFIDENCE_UNKNOWN, "no_evidence", None, None)

    for source in _DECISIVE_EVIDENCE_SOURCES:
        decisive = [ev for ev in identified if ev.source == source]
        if decisive:
            ev = decisive[0]
            return HardwareProfileResolution(
                ev.resolved_profile,
                ev.confidence,
                f"{ev.source}:{ev.resolved_profile}",
                None,
                ev.value,
            )

    distinct = {ev.resolved_profile for ev in identified}
    if len(distinct) > 1:
        return HardwareProfileResolution(
            None,
            CONFIDENCE_CONFLICT,
            "conflict:" + ",".join(sorted(distinct)),
            None,
            None,
        )
    ev = identified[0]
    return HardwareProfileResolution(
        ev.resolved_profile,
        ev.confidence,
        f"{ev.source}:{ev.resolved_profile}",
        None,
        ev.value,
    )


def resolve_hardware_profile(product) -> ZendureHardwareProfile | None:
    """Resolve a product/model name to a hardware profile, or ``None``.

    Only an exactly identified model (``exact``/``canonical`` confidence) returns
    a profile; a bare family word or an unknown name returns ``None`` so the
    device stays telemetry-only. See :func:`resolve_hardware_profile_detail` for
    the confidence and evidence behind the decision.
    """

    detail = resolve_hardware_profile_detail(product)
    if detail.profile_id is None:
        return None
    return HARDWARE_PROFILES[detail.profile_id]


def hardware_profile_by_name(canonical_name) -> ZendureHardwareProfile | None:
    """Look up a profile by its canonical name (used when config pins one).

    Matches the exact canonical id first (so glued letter/digit ids such as
    ``superbase_v4600`` survive normalization), then a normalized form.
    """

    raw = str(canonical_name or "").strip().lower()
    if raw in _CANONICAL_RAW:
        return _CANONICAL_RAW[raw]
    return HARDWARE_PROFILES.get(_normalize(canonical_name).replace(" ", "_"))


def hardware_profile_matches_generation(profile, hardware_generation) -> bool:
    """Whether a concrete model can be reviewed under a telemetry generation.

    The local generations describe both the observed telemetry layout and the
    hardware line. Zendure Cloud MQTT is transport/display grouping only, so an
    exact model from either hardware line may be selected there. Keeping this
    compatibility rule in Core prevents the browser from inventing authority.
    """

    if not isinstance(profile, ZendureHardwareProfile):
        return False
    generation = str(hardware_generation or "").strip()
    return not generation or generation in {
        profile.hardware_generation,
        "zendure_cloud",
    }


def hardware_profile_selector_options() -> list[dict]:
    """Registry-derived options for the concrete Admin hardware-model selector.

    An explicit telemetry-only unknown option comes first, followed by every
    concrete model in registry order. Compatibility aliases are retained for old
    Admin clients, while the normalized browser/API contract uses ``id``,
    ``generation``, ``control_supported`` and ``validation_maturity``.
    """

    options = [
        {
            "id": "",
            "value": "",
            "label": "Unknown / telemetry only",
            "generation": None,
            "control_supported": False,
            "auto": False,
            "telemetry_only": True,
            "supported_operations": [],
            "power_write_profile": None,
            "validation_maturity": VALIDATION_TELEMETRY_ONLY,
            "validation_status": None,
        }
    ]
    for prof in _HARDWARE_PROFILES:
        telemetry_only = not prof.writable
        options.append(
            {
                "id": prof.canonical_name,
                "value": prof.canonical_name,
                "label": (
                    f"{prof.display_label} — telemetry only"
                    if telemetry_only
                    else prof.display_label
                ),
                "generation": prof.hardware_generation,
                "compatible_generations": [
                    prof.hardware_generation,
                    "zendure_cloud",
                ],
                "control_supported": not telemetry_only,
                "auto": False,
                "telemetry_only": telemetry_only,
                "supported_operations": list(prof.supported_operations),
                "power_write_profile": prof.power_write_profile,
                "validation_maturity": prof.validation_status,
                "validation_status": prof.validation_status,
            }
        )
    return options


def operation_for_target(target_w: int) -> str:
    """Map a signed controller target to a neutral operation.

    ``> 0`` discharge / AC output, ``== 0`` idle / stop, ``< 0`` AC charging.
    The EMS sign convention stays internal; the write adapter converts a charge
    operation to a positive charging watt value.
    """

    if target_w > 0:
        return OPERATION_DISCHARGE
    if target_w < 0:
        return OPERATION_CHARGE
    return OPERATION_IDLE


__all__ = [
    "WRITE_PROFILE_ZENSDK_PROPERTIES",
    "WRITE_PROFILE_LEGACY_HUB",
    "WRITE_PROFILE_LEGACY_OBJECT",
    "WRITE_PROFILE_TELEMETRY_ONLY",
    "IMPLEMENTED_WRITE_PROFILES",
    "OPERATION_DISCHARGE",
    "OPERATION_IDLE",
    "OPERATION_CHARGE",
    "VALIDATION_EXISTING_SUPPORT",
    "VALIDATION_COMMUNITY_REQUIRED",
    "VALIDATION_DEFERRED",
    "VALIDATION_TELEMETRY_ONLY",
    "CONFIDENCE_EXACT",
    "CONFIDENCE_CANONICAL",
    "CONFIDENCE_AMBIGUOUS",
    "CONFIDENCE_UNKNOWN",
    "CONFIDENCE_CONFLICT",
    "WRITABLE_CONFIDENCES",
    "EVIDENCE_USER_SELECTION",
    "EVIDENCE_EXISTING_CONFIG",
    "EVIDENCE_CLOUD_DEVICE_LIST",
    "EVIDENCE_FULL_REPORT",
    "EVIDENCE_RETAINED_METADATA",
    "EVIDENCE_PRODUCT_KEY",
    "ZendureHardwareProfile",
    "HardwareProfileResolution",
    "HardwareProfileEvidence",
    "HARDWARE_PROFILES",
    "resolve_hardware_profile",
    "resolve_hardware_profile_detail",
    "resolve_hardware_profile_evidence",
    "make_hardware_profile_evidence",
    "hardware_profile_by_name",
    "hardware_profile_matches_generation",
    "hardware_profile_selector_options",
    "operation_for_target",
]
