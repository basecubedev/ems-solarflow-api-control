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

SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"
DEFAULT_BROKER_REF = "default"
DEFAULT_CLOUD_ACCOUNT_SCOPE = "cloud-account:default"
PHYSICAL_IDENTITY_TOKEN_FIELD = "physical_identity_token"
PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD = "physical_identity_alias_tokens"

_MASK_MARKERS = ("•", "…")
_MQTT_SOURCES = frozenset({SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_CLOUD_MQTT})


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
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or any(marker in cleaned for marker in _MASK_MARKERS)
        or cleaned.casefold() in {"<redacted>", "[redacted]", "redacted"}
        or cleaned.casefold().startswith(("your_", "your-"))
    ):
        return None
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


def opaque_identity_token(identity: InverterIdentity, key: bytes) -> str:
    """Return a versioned keyed token safe only for browser equality."""

    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("identity token key must contain at least 32 bytes")
    payload = json.dumps(
        ["inverter-identity-v1", identity.kind, list(identity.normalized_components)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"opaque:v1:{encoded}"


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
    "InverterIdentity",
    "InverterIdentityEvidence",
    "PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD",
    "PHYSICAL_IDENTITY_TOKEN_FIELD",
    "SOURCE_LOCAL_MQTT",
    "SOURCE_ZENDURE_CLOUD_MQTT",
    "broker_sources_from_config",
    "identity_conflict",
    "identity_evidence_conflict",
    "legacy_route_folded_tokens",
    "mqtt_route_conflict",
    "normalize_mqtt_route_segment",
    "normalize_physical_serial",
    "opaque_identity_token",
    "resolve_inverter_identity",
    "resolve_inverter_identity_evidence",
    "same_inverter_evidence",
    "same_physical_inverter",
    "same_physical_inverter_evidence",
    "supplied_identity_token",
]
