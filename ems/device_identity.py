# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dependency-light inverter identity resolution shared by Core and Admin."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

IdentityKind = Literal[
    "physical_serial",
    "scoped_mqtt_device_anchor",
    "scoped_mqtt_route",
    "local_api_endpoint",
]

IdentityStatus = Literal[
    "confirmed",
    "probable",
    "unresolved",
    "ambiguous",
    "conflict",
]

STATUS_CONFIRMED = "confirmed"
STATUS_PROBABLE = "probable"
STATUS_UNRESOLVED = "unresolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_CONFLICT = "conflict"

# The one ordered evidence policy. Everything that ranks, resolves or compares
# physical identity reads this tuple; a new evidence kind is declared here and
# nowhere else.
EVIDENCE_PRECEDENCE: tuple[IdentityKind, ...] = (
    "physical_serial",
    "scoped_mqtt_device_anchor",
    "scoped_mqtt_route",
    "local_api_endpoint",
)

# Evidence that can identify a *physical* device. A local endpoint is route
# evidence only: two inverters can trade IPs, so it never confirms hardware.
PHYSICAL_EVIDENCE_KINDS = frozenset(
    {"physical_serial", "scoped_mqtt_device_anchor", "scoped_mqtt_route"}
)

SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"
DEFAULT_BROKER_REF = "default"
DEFAULT_CLOUD_ACCOUNT_SCOPE = "cloud-account:default"
PHYSICAL_IDENTITY_TOKEN_FIELD = "physical_identity_token"
PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD = "physical_identity_alias_tokens"

_MASK_MARKERS = ("•", "…")
_REDACTED_WORDS = frozenset({"<redacted>", "[redacted]", "redacted"})
_PLACEHOLDER_PREFIXES = ("your_", "your-")
_MQTT_SOURCES = frozenset({SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_CLOUD_MQTT})


def is_masked_identity_value(value: Any) -> bool:
    """True when a value only *displays* an identifier and proves nothing.

    The single placeholder policy for the whole project: the mask markers a
    redacted view emits (``••••``, ``…abcd``), the ``redacted`` word forms, the
    template's ``your_…`` placeholders, and any string with no alphanumeric
    content at all (``****``, ``----``) — an identifier without a single
    alphanumeric character identifies nothing. Non-strings are masked by
    definition, because only a string can carry an identifier.
    """

    if not isinstance(value, str):
        return True
    cleaned = value.strip()
    if not cleaned:
        return True
    folded = cleaned.casefold()
    return (
        any(marker in cleaned for marker in _MASK_MARKERS)
        or folded in _REDACTED_WORDS
        or folded.startswith(_PLACEHOLDER_PREFIXES)
        or not any(char.isalnum() for char in cleaned)
    )


@dataclass(frozen=True)
class InverterIdentity:
    """Normalized backend identity plus an optional browser equality token."""

    kind: IdentityKind
    normalized_components: tuple[str, ...]
    confidence: str
    opaque_token: str | None = None

    @property
    def comparison_key(self) -> tuple[str, ...]:
        return (self.kind, *self.normalized_components)


@dataclass(frozen=True)
class InverterIdentityEvidence:
    """One physical inverter's trusted identity aliases, strongest first.

    ``primary`` is the strongest available identity (serial > scoped device
    anchor > precise scoped route > endpoint); ``aliases`` are the other trusted
    identities the same observation carries. Matching intersects the whole alias
    set, so an inverter is enriched, not duplicated, when a later observation adds
    a stronger identity to an alias already known.

    An MQTT observation carries two scoped identities: a *device anchor*
    (source/broker-scope/device_id) that stays stable while semantic metadata such
    as product key or topic family is enriched, and — when a product key is known —
    a *precise route* (source/broker-scope/product_key/device_id) that is the exact
    write address. Two observations that share an anchor but carry different known
    product keys prove two distinct routes and never merge (:meth:`route_conflict`).
    """

    primary: InverterIdentity
    aliases: tuple[InverterIdentity, ...] = ()
    confidence: str = ""

    @property
    def identities(self) -> tuple[InverterIdentity, ...]:
        return (self.primary, *self.aliases)

    @property
    def comparison_keys(self) -> frozenset[tuple[str, ...]]:
        return frozenset(identity.comparison_key for identity in self.identities)

    @property
    def opaque_tokens(self) -> tuple[str, ...]:
        seen: list[str] = []
        for identity in self.identities:
            token = identity.opaque_token
            if token is not None and token not in seen:
                seen.append(token)
        return tuple(seen)

    def serial_keys(self) -> frozenset[tuple[str, ...]]:
        return frozenset(
            identity.comparison_key
            for identity in self.identities
            if identity.kind == "physical_serial"
        )

    def alias_keys(self) -> frozenset[tuple[str, ...]]:
        return frozenset(
            identity.comparison_key
            for identity in self.identities
            if identity.kind != "physical_serial"
        )

    def _routes_by_anchor(self) -> dict[tuple[str, ...], set[str]]:
        by_anchor: dict[tuple[str, ...], set[str]] = {}
        for identity in self.identities:
            if identity.kind != "scoped_mqtt_route":
                continue
            source, scope, product_key, device_id = identity.normalized_components
            by_anchor.setdefault((source, scope, device_id), set()).add(product_key)
        return by_anchor

    def route_conflict(self, other: InverterIdentityEvidence) -> bool:
        """True when a shared device anchor carries two different known product keys.

        The anchor says "one device route", the differing product keys say "two"
        — a distinct precise write address on each side. Merging them would mix two
        routes onto one control target, so they are kept apart (fail closed).
        """

        mine = self._routes_by_anchor()
        theirs = other._routes_by_anchor()
        for anchor, my_keys in mine.items():
            other_keys = theirs.get(anchor)
            if other_keys and my_keys and my_keys.isdisjoint(other_keys):
                return True
        return False


def _clean(value: Any, *, fold_case: bool = False) -> str | None:
    if is_masked_identity_value(value):
        return None
    cleaned = value.strip()
    return cleaned.casefold() if fold_case else cleaned


def normalize_physical_serial(value: Any) -> str | None:
    """Case-folded, trimmed physical serial for equality; ``None`` if unusable.

    The shared serial match key used by Core, Admin discovery, Maintenance and the
    hardware probe, so serial comparison is identical everywhere. Masked,
    redacted and placeholder values return ``None``. It is a match key only —
    never a display or config value, as it lowercases.
    """

    return _clean(value, fold_case=True)


def normalize_mqtt_route_segment(value: Any) -> str | None:
    """Trimmed, case-sensitive MQTT route segment (product key or device id).

    MQTT topic segments are case-sensitive addresses, so — unlike a physical
    serial — they are compared exactly and their original case is preserved.
    Masked, redacted and placeholder values return ``None``. Never apply the
    serial normalizer to a route identifier: case-folding would collapse two
    distinct write addresses (``iot/PK/DEV`` vs ``iot/pk/dev``).
    """

    return _clean(value, fold_case=False)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fragment(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(item.get("config_fragment"))


def _first(*values: Any, fold_case: bool = False) -> str | None:
    for value in values:
        cleaned = _clean(value, fold_case=fold_case)
        if cleaned is not None:
            return cleaned
    return None


def _mqtt_view(item: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = _mapping(item.get("mqtt"))
    if direct:
        return direct
    return _mapping(_fragment(item).get("mqtt"))


def broker_sources_from_config(config: Any) -> dict[str, str]:
    """Return configured broker source by ref without reading credentials."""

    if not isinstance(config, Mapping):
        return {}
    raw = _mapping(config.get("zendure_mqtt"))
    sources: dict[str, str] = {}
    top_source = _clean(raw.get("source"), fold_case=True)
    if top_source in _MQTT_SOURCES:
        sources[DEFAULT_BROKER_REF] = top_source
    brokers = raw.get("brokers")
    if isinstance(brokers, Mapping):
        for ref, profile in brokers.items():
            source = _clean(_mapping(profile).get("source"), fold_case=True)
            if source in _MQTT_SOURCES:
                sources[str(ref).strip()] = source
    return sources


def _serial_identity(item: Mapping[str, Any], fragment: Mapping[str, Any]):
    serial = _first(
        item.get("sn"),
        item.get("serial_number"),
        fragment.get("sn"),
        fragment.get("serial_number"),
        fold_case=True,
    )
    if serial is None:
        return None
    return InverterIdentity("physical_serial", (serial,), "trusted")


def _scoped_route_identities(
    item: Mapping[str, Any],
    fragment: Mapping[str, Any],
    broker_sources: Mapping[str, str] | None,
) -> list[InverterIdentity]:
    """Return the scoped device anchor and, when a product key is known, its route.

    The anchor (``source``/``broker_scope``/``device_id``) is stable across
    product-key and topic-family enrichment; the precise route additionally pins
    the product key so distinct write addresses on one device id stay distinct.
    """

    mqtt = _mqtt_view(item)
    item_type = _first(item.get("kind"), item.get("type"), fold_case=True)
    connection_source = _first(
        item.get("connection_source"),
        mqtt.get("source"),
        fragment.get("connection_source"),
        fold_case=True,
    )
    is_mqtt = bool(mqtt) or item_type == "zendure_mqtt" or connection_source in _MQTT_SOURCES
    if not is_mqtt:
        return []
    broker_ref = _first(
        mqtt.get("broker_ref"),
        item.get("broker_ref"),
        _mapping(fragment.get("mqtt")).get("broker_ref"),
    ) or DEFAULT_BROKER_REF
    source = connection_source
    if source not in _MQTT_SOURCES and isinstance(broker_sources, Mapping):
        source = _clean(broker_sources.get(broker_ref), fold_case=True)
    if source not in _MQTT_SOURCES:
        source = "zendure_mqtt"
    broker_scope = broker_ref
    if source == SOURCE_ZENDURE_CLOUD_MQTT:
        configured_cloud_refs = {
            str(ref).strip()
            for ref, configured_source in (broker_sources or {}).items()
            if _clean(configured_source, fold_case=True) == SOURCE_ZENDURE_CLOUD_MQTT
        }
        # Admin supports one connected Zendure account. Discovery uses the
        # canonical ``zendure_cloud`` ref, while an installed config may give that
        # sole account any local alias. Normalize only when the account is
        # unambiguous. Multiple configured Cloud profiles retain their distinct
        # refs and therefore never merge automatically.
        if broker_ref == "zendure_cloud" or (
            len(configured_cloud_refs) == 1 and broker_ref in configured_cloud_refs
        ):
            broker_scope = DEFAULT_CLOUD_ACCOUNT_SCOPE
    device_id = _first(
        mqtt.get("device_id"),
        item.get("device_id"),
        _mapping(fragment.get("mqtt")).get("device_id"),
    )
    if device_id is None:
        return []
    anchor = InverterIdentity(
        "scoped_mqtt_device_anchor",
        (source, broker_scope, device_id),
        "scoped",
    )
    product_key = _first(
        mqtt.get("product_key"),
        item.get("product_key"),
        _mapping(fragment.get("mqtt")).get("product_key"),
    )
    if product_key is None:
        return [anchor]
    route = InverterIdentity(
        "scoped_mqtt_route",
        (source, broker_scope, product_key, device_id),
        "scoped",
    )
    return [anchor, route]


def _endpoint_identity(item: Mapping[str, Any], fragment: Mapping[str, Any]):
    endpoint = _first(item.get("ip"), item.get("host"), fold_case=True)
    if endpoint is None:
        endpoint = _first(fragment.get("ip"), fragment.get("host"), fold_case=True)
    if endpoint is None:
        return None
    port_value = item.get("port", fragment.get("port"))
    port = "" if port_value in (None, "") else str(port_value).strip()
    return InverterIdentity("local_api_endpoint", (endpoint, port), "endpoint_fallback")


def _resolved_identities(
    item: Mapping[str, Any], broker_sources: Mapping[str, str] | None
) -> list[InverterIdentity]:
    fragment = _fragment(item)
    candidates = [
        _serial_identity(item, fragment),
        *_scoped_route_identities(item, fragment, broker_sources),
        _endpoint_identity(item, fragment),
    ]
    return [identity for identity in candidates if identity is not None]


def resolve_inverter_identity(
    item: Any,
    *,
    broker_sources: Mapping[str, str] | None = None,
    token_key: bytes | None = None,
) -> InverterIdentity | None:
    """Resolve serial, scoped MQTT route, or local endpoint in that order."""

    if not isinstance(item, Mapping):
        return None
    identities = _resolved_identities(item, broker_sources)
    if not identities:
        return None
    return _with_token(identities[0], token_key)


def resolve_inverter_identity_evidence(
    item: Any,
    *,
    broker_sources: Mapping[str, str] | None = None,
    token_key: bytes | None = None,
) -> InverterIdentityEvidence | None:
    """Resolve every trusted identity alias an observation carries, strongest first.

    Unlike :func:`resolve_inverter_identity`, which returns only the strongest
    identity, this retains the scoped MQTT route (and endpoint) as aliases even
    once a physical serial is present, so a route-only device and a later
    serial-bearing observation of the same route still intersect.
    """

    if not isinstance(item, Mapping):
        return None
    identities = _resolved_identities(item, broker_sources)
    if not identities:
        return None
    identities = [_with_token(identity, token_key) for identity in identities]
    primary, *aliases = identities
    return InverterIdentityEvidence(
        primary=primary, aliases=tuple(aliases), confidence=primary.confidence
    )


def identity_evidence_conflict(
    left: InverterIdentityEvidence | None, right: InverterIdentityEvidence | None
) -> bool:
    """True when two evidences prove contradictory physical mappings.

    The only contradiction is a *shared* weaker alias (scoped route or endpoint)
    combined with *different* physical serials: the shared route says "one
    inverter" while the serials say "two". Sharing a serial is never a conflict —
    a serial may legitimately gain additional routes or account scopes.
    """

    if left is None or right is None:
        return False
    left_serials = left.serial_keys()
    right_serials = right.serial_keys()
    if not left_serials or not right_serials:
        return False
    if not left_serials.isdisjoint(right_serials):
        return False
    return not left.alias_keys().isdisjoint(right.alias_keys())


def same_physical_inverter_evidence(
    left: InverterIdentityEvidence | None, right: InverterIdentityEvidence | None
) -> bool:
    """True when two evidences describe one physical inverter.

    Physical identity and writable-route ambiguity are separate decisions. A
    shared physical serial is decisive: one inverter may gain routes, account
    scopes or product keys, so it holds even when the precise write routes differ
    (that ambiguity blocks control only; see :func:`mqtt_route_conflict`). Absent
    a shared serial, a shared weaker alias (device anchor, route, endpoint) unites
    them only when no identity conflict and no route conflict makes the shared
    anchor ambiguous.
    """

    if left is None or right is None:
        return False
    if identity_evidence_conflict(left, right):
        return False
    if not left.serial_keys().isdisjoint(right.serial_keys()):
        return True
    if left.route_conflict(right):
        return False
    return not left.comparison_keys.isdisjoint(right.comparison_keys)


# The stable public name of the physical-identity predicate. It answers "one
# inverter?" and never overloads control-address safety onto that boolean.
same_inverter_evidence = same_physical_inverter_evidence


def mqtt_route_conflict(
    left: InverterIdentityEvidence | None, right: InverterIdentityEvidence | None
) -> bool:
    """True when one shared device anchor carries two different known product keys.

    A pure control-address concern, independent of physical identity: the two
    evidences may still be one physical inverter
    (:func:`same_physical_inverter_evidence`), but the precise write address is
    ambiguous, so output control must be blocked.
    """

    if left is None or right is None:
        return False
    return left.route_conflict(right)


@dataclass(frozen=True)
class PhysicalIdentityResult:
    """The canonical answer to "which physical device is this observation?".

    ``evidence`` and ``physical_identity`` are server-side detail (they carry
    raw serials and route segments). Only ``public_identity_id``, ``status``,
    ``confidence``, ``conflict`` and ``reason`` are safe to hand to a browser.
    """

    status: IdentityStatus
    evidence: InverterIdentityEvidence | None = None
    physical_identity: InverterIdentity | None = None
    public_identity_id: str | None = None
    confidence: str = ""
    conflict: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        """True when the evidence identifies a physical device at all."""

        return self.status in (STATUS_CONFIRMED, STATUS_PROBABLE)

    def public_view(self) -> dict[str, Any]:
        """The browser-facing projection: never raw serials or route segments."""

        return {
            "status": self.status,
            "public_identity_id": self.public_identity_id,
            "confidence": self.confidence,
            "identity_conflict": self.conflict,
            "reason": self.reason,
        }


def resolve_physical_identity(
    item: Any,
    *,
    broker_sources: Mapping[str, str] | None = None,
    token_key: bytes | None = None,
) -> PhysicalIdentityResult:
    """Resolve one observation's physical identity, status included.

    A verified serial confirms; a verified scoped MQTT device identifier makes
    it probable; endpoint-only or masked-only evidence stays unresolved, because
    neither proves which hardware answered. An unresolved observation never
    receives a public identity id, so no caller can mistake "we do not know" for
    "this device".
    """

    evidence = resolve_inverter_identity_evidence(
        item, broker_sources=broker_sources, token_key=token_key
    )
    if evidence is None:
        return PhysicalIdentityResult(
            status=STATUS_UNRESOLVED, reason="no_identity_evidence"
        )
    physical = next(
        (
            identity
            for identity in evidence.identities
            if identity.kind in PHYSICAL_EVIDENCE_KINDS
        ),
        None,
    )
    if physical is None:
        return PhysicalIdentityResult(
            status=STATUS_UNRESOLVED,
            evidence=evidence,
            confidence=evidence.confidence,
            reason="endpoint_evidence_only",
        )
    if physical.kind == "physical_serial":
        status: IdentityStatus = STATUS_CONFIRMED
        reason = "verified_physical_serial"
    else:
        status = STATUS_PROBABLE
        reason = "scoped_mqtt_device_identifier"
    return PhysicalIdentityResult(
        status=status,
        evidence=evidence,
        physical_identity=physical,
        public_identity_id=physical.opaque_token,
        confidence=physical.confidence,
        reason=reason,
    )


@dataclass(frozen=True)
class IdentityComparison:
    """How two observations relate physically, and why.

    ``route_ambiguous`` is deliberately independent of ``same_physical_device``:
    one inverter observed on two precise product routes is still one inverter,
    its *write address* is what became ambiguous.
    """

    status: IdentityStatus
    same_physical_device: bool
    identity_conflict: bool
    reason: str
    route_ambiguous: bool = False


def compare_physical_identity(
    left: PhysicalIdentityResult | None, right: PhysicalIdentityResult | None
) -> IdentityComparison:
    """Compare two resolved observations under the one evidence policy.

    Ordered so that a strong contradiction always wins: contradictory serials
    conflict, and a shared weaker alias can never promote either back to "same
    device". Absent physical evidence on either side the answer is
    ``unresolved``, never ``different``: the caller must not treat "we cannot
    tell" as proof of two devices.

    An ambiguous write route is reported through ``route_ambiguous`` rather than
    denying identity, because a shared serial identifies the inverter even when
    two precise product routes make the write address ambiguous. Without a
    shared serial the route conflict does keep the two apart.
    """

    left_evidence = left.evidence if left is not None and left.resolved else None
    right_evidence = right.evidence if right is not None and right.resolved else None
    if left_evidence is None or right_evidence is None:
        return IdentityComparison(
            status=STATUS_UNRESOLVED,
            same_physical_device=False,
            identity_conflict=False,
            reason="insufficient_identity_evidence",
        )
    if identity_evidence_conflict(left_evidence, right_evidence):
        return IdentityComparison(
            status=STATUS_CONFLICT,
            same_physical_device=False,
            identity_conflict=True,
            reason="contradictory_physical_serials",
        )
    route_ambiguous = mqtt_route_conflict(left_evidence, right_evidence)
    if not same_physical_inverter_evidence(left_evidence, right_evidence):
        return IdentityComparison(
            status=STATUS_AMBIGUOUS if route_ambiguous else STATUS_UNRESOLVED,
            same_physical_device=False,
            identity_conflict=False,
            reason="ambiguous_mqtt_write_route"
            if route_ambiguous
            else "no_shared_identity_evidence",
            route_ambiguous=route_ambiguous,
        )
    shared_serial = not left_evidence.serial_keys().isdisjoint(
        right_evidence.serial_keys()
    )
    return IdentityComparison(
        status=STATUS_CONFIRMED if shared_serial else STATUS_PROBABLE,
        same_physical_device=True,
        identity_conflict=False,
        reason="shared_physical_serial" if shared_serial else "shared_scoped_identifier",
        route_ambiguous=route_ambiguous,
    )


def connection_coordinates(
    item: Any, *, broker_sources: Mapping[str, str] | None = None
) -> tuple[str, ...] | None:
    """The transport address of one observation: where it was reached, not who it is.

    Deliberately independent of physical identity, so two observations with the
    same masked serial still get distinct coordinates when they answer on
    different hosts or routes. Returns ``None`` when nothing addressable is
    known — the caller must then supply its own discriminator rather than let
    two unaddressable observations collapse.
    """

    if not isinstance(item, Mapping):
        return None
    fragment = _fragment(item)
    for identity in _scoped_route_identities(item, fragment, broker_sources):
        if identity.kind == "scoped_mqtt_route":
            return ("mqtt_route", *identity.normalized_components)
    for identity in _scoped_route_identities(item, fragment, broker_sources):
        if identity.kind == "scoped_mqtt_device_anchor":
            return ("mqtt_anchor", *identity.normalized_components)
    endpoint = _endpoint_identity(item, fragment)
    if endpoint is not None:
        return ("local_endpoint", *endpoint.normalized_components)
    return None


def legacy_route_folded_tokens(
    evidence: InverterIdentityEvidence | None, key: bytes
) -> tuple[str, ...]:
    """Server-only legacy tokens for an evidence's MQTT route identities.

    A prior release case-folded MQTT route segments, so a browser selection stored
    then may still carry a case-folded token. These tokens let such a stale
    selection remap to its current exact-case proposal. They are never sent to the
    browser: two case-distinct routes fold to one token, so exposing it would
    merge them again.
    """

    if evidence is None:
        return ()
    tokens: list[str] = []
    for identity in evidence.identities:
        if identity.kind == "scoped_mqtt_device_anchor":
            source, scope, device_id = identity.normalized_components
            folded = (source, scope, device_id.casefold())
        elif identity.kind == "scoped_mqtt_route":
            source, scope, product_key, device_id = identity.normalized_components
            folded = (source, scope, product_key.casefold(), device_id.casefold())
        else:
            continue
        if folded == identity.normalized_components:
            continue
        legacy = InverterIdentity(identity.kind, folded, identity.confidence)
        token = opaque_identity_token(legacy, key)
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _keyed_token(payload: Any, prefix: str, key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("identity token key must contain at least 32 bytes")
    encoded_payload = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hmac.new(key, encoded_payload, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{prefix}:{encoded}"


def opaque_identity_token(identity: InverterIdentity, key: bytes) -> str:
    """Return a versioned keyed token safe only for browser equality."""

    return _keyed_token(
        ["inverter-identity-v1", identity.kind, list(identity.normalized_components)],
        "opaque:v1",
        key,
    )


def opaque_observation_id(components: Any, key: bytes) -> str:
    """A stable browser-safe id for *one observation* of a device.

    Distinct from a physical identity: an observation is "this device, reached
    this way", so two observations that only display the same masked serial keep
    different ids. Keyed, so no host or route segment is reconstructable.
    """

    return _keyed_token(
        ["discovery-observation-v1", [str(part) for part in components]],
        "obs:v1",
        key,
    )


def opaque_connection_id(coordinates: Any, key: bytes) -> str:
    """A stable browser-safe id for one configured or proposed transport route."""

    return _keyed_token(
        ["connection-route-v1", [str(part) for part in coordinates]],
        "conn:v1",
        key,
    )


def opaque_plan_id(components: Any, key: bytes) -> str:
    """A stable browser-safe fingerprint of a computed plan and its inputs.

    Not an identity: it names *this answer, over these candidates*, so a browser
    can prove the plan it is about to apply is still the current one. Keyed like
    every other public id, so its inputs cannot be recovered or forged.
    """

    return _keyed_token(["setup-plan-v1", components], "plan:v1", key)


def _with_token(
    identity: InverterIdentity, token_key: bytes | None
) -> InverterIdentity:
    if token_key is None:
        return identity
    return replace(identity, opaque_token=opaque_identity_token(identity, token_key))


def same_physical_inverter(left: Any, right: Any) -> bool:
    """Compare two resolved identities; absent evidence never matches."""

    left_identity = (
        left if isinstance(left, InverterIdentity) else resolve_inverter_identity(left)
    )
    right_identity = (
        right if isinstance(right, InverterIdentity) else resolve_inverter_identity(right)
    )
    return bool(
        left_identity is not None
        and right_identity is not None
        and left_identity.comparison_key == right_identity.comparison_key
    )


def identity_conflict(left: Any, right: Any) -> bool:
    """True only when two concrete identity observations disagree."""

    left_identity = (
        left if isinstance(left, InverterIdentity) else resolve_inverter_identity(left)
    )
    right_identity = (
        right if isinstance(right, InverterIdentity) else resolve_inverter_identity(right)
    )
    return bool(
        left_identity is not None
        and right_identity is not None
        and left_identity.comparison_key != right_identity.comparison_key
    )


def supplied_identity_token(item: Any) -> str | None:
    if not isinstance(item, Mapping):
        return None
    value = item.get(PHYSICAL_IDENTITY_TOKEN_FIELD)
    if isinstance(value, str) and value.startswith("opaque:v1:"):
        return value
    return None


__all__ = [
    "DEFAULT_BROKER_REF",
    "DEFAULT_CLOUD_ACCOUNT_SCOPE",
    "EVIDENCE_PRECEDENCE",
    "PHYSICAL_EVIDENCE_KINDS",
    "PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD",
    "PHYSICAL_IDENTITY_TOKEN_FIELD",
    "SOURCE_LOCAL_MQTT",
    "SOURCE_ZENDURE_CLOUD_MQTT",
    "STATUS_AMBIGUOUS",
    "STATUS_CONFIRMED",
    "STATUS_CONFLICT",
    "STATUS_PROBABLE",
    "STATUS_UNRESOLVED",
    "IdentityComparison",
    "InverterIdentity",
    "InverterIdentityEvidence",
    "PhysicalIdentityResult",
    "broker_sources_from_config",
    "compare_physical_identity",
    "connection_coordinates",
    "identity_conflict",
    "identity_evidence_conflict",
    "is_masked_identity_value",
    "legacy_route_folded_tokens",
    "mqtt_route_conflict",
    "normalize_mqtt_route_segment",
    "normalize_physical_serial",
    "opaque_connection_id",
    "opaque_identity_token",
    "opaque_observation_id",
    "opaque_plan_id",
    "resolve_inverter_identity",
    "resolve_inverter_identity_evidence",
    "resolve_physical_identity",
    "same_inverter_evidence",
    "same_physical_inverter",
    "same_physical_inverter_evidence",
    "supplied_identity_token",
]
