# SPDX-License-Identifier: AGPL-3.0-or-later
"""Central MQTT route masking for browser and support-export boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"

_DROP_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "authorization",
    "auth_code",
    "client_secret",
    "app_key",
    "apikey",
    "api_key",
    "credential",
)
_DROP_EXACT_KEYS = frozenset({"token", "access_token", "refresh_token", "username"})
_NON_SECRET_STATUS_KEYS = frozenset(
    {
        "credential_rollback",
        "has_password",
        "physical_identity_token",
        "physical_identity_alias_tokens",
    }
)
_ROUTE_KEYS = frozenset(
    {"device_id", "device_key", "identifier", "route_id", "product_key"}
)
_DEVICE_ROUTE_KEYS = _ROUTE_KEYS - {"product_key"}
_MQTT_SCOPE_KEYS = frozenset(
    {"topic_family", "base_topic", "write_topic", "product_key", "device_id"}
)
_NAMED_ENTRY_CONTAINERS = frozenset(
    {
        "brokers",
        "devices",
        "device_max_power",
        "diagnostic_by_route",
        "metrics",
        "rules",
        "series",
        "sources",
    }
)
_EMBEDDED_IOT_TOPIC = re.compile(
    r"(?<![A-Za-z0-9_])iot/[^/\s\"']+/[^/\s\"']+(?:/[A-Za-z0-9_+#.-]+)*",
    re.IGNORECASE,
)
_EMBEDDED_ROOT_TOPIC = re.compile(
    r"(?<![A-Za-z0-9_…/])/(?!/)[^/\s\"']+/[^/\s\"']+/"
    r"(?:properties|state|report|read|write|function|invoke|custom|#)"
    r"(?:/[A-Za-z0-9_+#.-]+)*",
    re.IGNORECASE,
)
_EMBEDDED_ROUTE_LABEL = re.compile(
    r"(?P<label>\b(?:device(?:_id|_key)?|identifier|route(?:_id)?|"
    r"product(?:_key)?)"
    r"\s*(?:=|:)\s*)(?P<quote>[\"']?)(?P<value>[^\s,;\"'}]+)(?P=quote)",
    re.IGNORECASE,
)
_EMBEDDED_SECRET_LABEL = re.compile(
    r"(?P<label>\b(?:password|passwd|secret|token|access_token|refresh_token|"
    r"authorization(?:_code)?|auth_code|client_secret|app_key|api_key|apikey|"
    r"username)\s*(?:=|:)\s*)(?P<quote>[\"']?)(?P<value>[^\s,;\"'}]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)
_EMBEDDED_CREDENTIAL_URL = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<user>[^/@\s:]+):"
    r"(?P<password>[^/@\s]+)@",
    re.IGNORECASE,
)
_ROOT_TOPIC_MARKERS = frozenset(
    {
        "properties",
        "state",
        "report",
        "read",
        "write",
        "function",
        "invoke",
        "custom",
        "#",
    }
)
_SOURCE_KEYS = ("source", "connection_source", "source_type")
_TRUSTED_IDENTITY_KEYS = frozenset(
    {"sn", "serial_number", "physical_identity_token"}
)


def mask_route_identifier(value: Any) -> Any:
    """Mask an account-scoped identifier while retaining a short display hint."""

    if value is None:
        return None
    if not isinstance(value, str):
        return "••••"
    text = value.strip()
    if not text:
        return text
    if (
        "•" in text
        or "…" in text
        or text.casefold() in {"<redacted>", "[redacted]", "redacted"}
    ):
        return text
    return "••••" if len(text) <= 4 else f"…{text[-4:]}"


def mask_mqtt_topic(topic: Any, *, cloud_scoped: bool = False) -> Any:
    """Mask the product/device route segments of a Zendure Cloud MQTT topic.

    The security concern is Zendure Cloud *account-scoped* route ids, so masking
    applies only in Cloud scope (``cloud_scoped``): an ``iot/<product>/<device>/
    <suffix...>`` or leading-slash ``/<product>/<device>/<suffix...>`` route is
    reduced to ``iot/…/…/...`` / ``/…/…/...`` for any recognized suffix
    (``properties``/``report``/``function``/``invoke``/``custom``/…). Local MQTT
    topics carry user-controlled local identifiers rather than Cloud routing
    secrets and are preserved so they stay useful diagnostics.
    """

    if not isinstance(topic, str) or not topic or not cloud_scoped:
        return topic
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0].lower() == "iot":
        parts[1] = "…"
        parts[2] = "…"
        return "/".join(parts)
    if (
        topic.startswith("/")
        and len(parts) >= 4
        and parts[3].strip().lower() in _ROOT_TOPIC_MARKERS
    ):
        parts[1] = "…"
        parts[2] = "…"
        return "/".join(parts)
    return topic


def _topic_route_segments(topic: Any) -> tuple[str, ...]:
    """Return the product/device segments of a recognized Cloud topic shape."""

    if not isinstance(topic, str) or not topic:
        return ()
    parts = topic.split("/")
    segments: tuple[str, ...] = ()
    if len(parts) >= 3 and parts[0].lower() == "iot":
        segments = (parts[1], parts[2])
    elif (
        topic.startswith("/")
        and len(parts) >= 4
        and parts[3].strip().lower() in _ROOT_TOPIC_MARKERS
    ):
        segments = (parts[1], parts[2])
    return tuple(
        segment
        for segment in segments
        if segment
        and "•" not in segment
        and "…" not in segment
        and segment.strip().casefold() not in {"<redacted>", "[redacted]", "redacted"}
    )


def _drop_key(key: Any) -> bool:
    lowered = str(key).lower()
    if lowered in _NON_SECRET_STATUS_KEYS:
        return False
    if lowered.endswith(("_ref", "_reference")):
        return False
    return lowered in _DROP_EXACT_KEYS or any(
        marker in lowered for marker in _DROP_KEY_MARKERS
    )


def _mapping_entry_container(key: str) -> bool:
    """Whether mapping keys here are identities rather than schema fields."""

    lowered = key.casefold()
    return (
        lowered in _NAMED_ENTRY_CONTAINERS
        or "_by_" in lowered
        or lowered.endswith(("_map", "_index"))
    )


def _node_source(node: Mapping[str, Any]) -> str:
    for key in _SOURCE_KEYS:
        raw = node.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    return ""


def _secret_values(value: Any) -> set[str]:
    """Collect credential values so free-form status/log text is also safe."""

    values: set[str] = set()

    def collect_scalar(node: Any) -> None:
        if isinstance(node, str):
            raw = node.strip()
            if (
                raw
                and raw.casefold()
                not in {"<redacted>", "[redacted]", "redacted"}
            ):
                values.add(raw)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                collect_scalar(child)

    def visit(node: Any, *, key: str = "") -> None:
        if isinstance(node, Mapping):
            entry_container = _mapping_entry_container(key)
            for child_key, child in node.items():
                if not entry_container and _drop_key(child_key):
                    collect_scalar(child)
                visit(child, key=str(child_key).lower())
        elif isinstance(node, list):
            # A list under ``devices``/``brokers`` contains record objects; its
            # children's keys are fields again, not named mapping entries.
            for child in node:
                visit(child)

    visit(value)
    return values


def _cloud_broker_refs(value: Any) -> set[str]:
    refs: set[str] = set()

    def visit(node: Any, *, allow_implicit_default: bool = True) -> None:
        if isinstance(node, Mapping):
            named = node.get("brokers")
            if isinstance(named, Mapping):
                for named_ref, profile in named.items():
                    source = _node_source(profile) if isinstance(profile, Mapping) else ""
                    if source == SOURCE_ZENDURE_CLOUD_MQTT:
                        refs.add(str(named_ref).strip())
                    visit(profile, allow_implicit_default=False)
            source = _node_source(node)
            ref = node.get("broker_ref")
            if source == SOURCE_ZENDURE_CLOUD_MQTT and isinstance(ref, str) and ref:
                refs.add(ref)
            elif source == SOURCE_ZENDURE_CLOUD_MQTT and allow_implicit_default:
                refs.add("default")
            for key, child in node.items():
                if key == "brokers" and isinstance(named, Mapping):
                    continue
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return refs


def _node_broker_scope(node: Mapping[str, Any]) -> str | None:
    ref = node.get("broker_ref")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    nested_mqtt = node.get("mqtt")
    if not isinstance(nested_mqtt, Mapping):
        fragment = node.get("config_fragment")
        if isinstance(fragment, Mapping):
            nested_mqtt = fragment.get("mqtt")
    if isinstance(nested_mqtt, Mapping):
        nested_ref = nested_mqtt.get("broker_ref")
        if isinstance(nested_ref, str) and nested_ref.strip():
            return nested_ref.strip()
    if str(node.get("type") or "").strip().lower() == "zendure_mqtt":
        return "default"
    if any(key in node for key in _MQTT_SCOPE_KEYS):
        return "default"
    return None


def _cloud_route_values(value: Any, cloud_refs: set[str]) -> set[str]:
    """Collect raw cloud product/route values before any field is masked."""

    values: set[str] = set()

    def visit(node: Any, *, cloud: bool = False) -> None:
        if isinstance(node, Mapping):
            source = _node_source(node)
            ref = _node_broker_scope(node)
            node_cloud = cloud or source == SOURCE_ZENDURE_CLOUD_MQTT or (
                ref is not None and ref in cloud_refs
            )
            if node_cloud:
                for key in _ROUTE_KEYS:
                    raw = node.get(key)
                    if (
                        isinstance(raw, str)
                        and raw.strip()
                        and "•" not in raw
                        and "…" not in raw
                        and raw.strip().casefold()
                        not in {"<redacted>", "[redacted]", "redacted"}
                    ):
                        values.add(raw.strip())
            for child in node.values():
                visit(child, cloud=node_cloud)
        elif isinstance(node, list):
            for child in node:
                visit(child, cloud=cloud)

    visit(value)
    return values


_TOPIC_KEYS = frozenset(
    {"topic", "write_topic", "base_topic", "effective_write_topic", "read_topic"}
)


def _cloud_topic_route_segments(value: Any, cloud_refs: set[str]) -> set[str]:
    """Collect product/device segments embedded in Cloud topic fields."""

    values: set[str] = set()

    def visit(node: Any, *, cloud: bool = False) -> None:
        if isinstance(node, Mapping):
            source = _node_source(node)
            ref = _node_broker_scope(node)
            node_cloud = cloud or source == SOURCE_ZENDURE_CLOUD_MQTT or (
                ref is not None and ref in cloud_refs
            )
            if node_cloud:
                for key, raw in node.items():
                    if str(key).casefold() in _TOPIC_KEYS:
                        values.update(_topic_route_segments(raw))
            for child in node.values():
                visit(child, cloud=node_cloud)
        elif isinstance(node, list):
            for child in node:
                visit(child, cloud=cloud)

    visit(value)
    return values


def _mapping_has_cloud_scope(
    node: Mapping[str, Any], cloud_refs: set[str]
) -> bool:
    source = _node_source(node)
    ref = _node_broker_scope(node)
    if source == SOURCE_ZENDURE_CLOUD_MQTT or (
        ref is not None and ref in cloud_refs
    ):
        return True
    for nested_key in ("mqtt", "config_fragment"):
        nested = node.get(nested_key)
        if isinstance(nested, Mapping):
            if nested_key == "config_fragment":
                nested = nested.get("mqtt")
            if isinstance(nested, Mapping):
                nested_source = _node_source(nested)
                nested_ref = _node_broker_scope(nested)
                if nested_source == SOURCE_ZENDURE_CLOUD_MQTT or (
                    nested_ref is not None and nested_ref in cloud_refs
                ):
                    return True
    return False


def _has_route_material(node: Any) -> bool:
    if isinstance(node, Mapping):
        for key, child in node.items():
            if (
                str(key).casefold() in _DEVICE_ROUTE_KEYS
                and isinstance(child, str)
                and child.strip()
                and "•" not in child
                and "…" not in child
                and child.strip().casefold()
                not in {"<redacted>", "[redacted]", "redacted"}
            ):
                return True
        return any(_has_route_material(child) for child in node.values())
    if isinstance(node, list):
        return any(_has_route_material(child) for child in node)
    return False


def _cloud_names_with_route(value: Any, cloud_refs: set[str]) -> set[str]:
    names: set[str] = set()

    def visit(node: Any, *, cloud: bool = False) -> None:
        if isinstance(node, Mapping):
            node_cloud = cloud or _mapping_has_cloud_scope(node, cloud_refs)
            if node_cloud and _has_route_material(node):
                for key in ("name", "device_name", "display_name"):
                    raw = node.get(key)
                    if isinstance(raw, str) and raw.strip():
                        names.add(raw.strip())
            for child in node.values():
                visit(child, cloud=node_cloud)
        elif isinstance(node, list):
            for child in node:
                visit(child, cloud=cloud)

    visit(value)
    return names


def _route_keyed_container(key: str) -> bool:
    lowered = key.casefold()
    return lowered == "devices" or "_by_route" in lowered


def _cloud_dynamic_route_values(
    value: Any,
    cloud_refs: set[str],
    *,
    trusted_names: frozenset[str],
) -> set[str]:
    values: set[str] = set()

    def remember(raw: Any, *, allow_trusted_name: bool) -> None:
        if (
            isinstance(raw, str)
            and raw.strip()
            and (not allow_trusted_name or raw.strip() not in trusted_names)
            and "•" not in raw
            and "…" not in raw
            and raw.strip().casefold()
            not in {"<redacted>", "[redacted]", "redacted"}
        ):
            values.add(raw.strip())

    def visit(node: Any, *, cloud: bool = False, key: str = "") -> None:
        if isinstance(node, Mapping):
            node_cloud = cloud or _mapping_has_cloud_scope(node, cloud_refs)
            if _route_keyed_container(key):
                allow_trusted_name = key.casefold() == "devices"
                for dynamic_key, child in node.items():
                    child_cloud = node_cloud or (
                        isinstance(child, Mapping)
                        and _mapping_has_cloud_scope(child, cloud_refs)
                    )
                    if child_cloud:
                        remember(
                            dynamic_key,
                            allow_trusted_name=allow_trusted_name,
                        )
            for child_key, child in node.items():
                visit(child, cloud=node_cloud, key=str(child_key))
        elif isinstance(node, list):
            for child in node:
                visit(child, cloud=cloud)

    visit(value)
    return values


def _cloud_fail_closed_values(
    value: Any,
    cloud_refs: set[str],
    *,
    trusted_names: frozenset[str] = frozenset(),
) -> set[str]:
    """Collect Cloud display labels when no authoritative route survives."""

    values: set[str] = set()

    def visit(node: Any, *, cloud: bool = False) -> None:
        if isinstance(node, Mapping):
            node_cloud = cloud or _mapping_has_cloud_scope(node, cloud_refs)
            if node_cloud and not _has_route_material(node):
                for key in ("name", "device_name", "display_name"):
                    raw = node.get(key)
                    if (
                        isinstance(raw, str)
                        and raw.strip()
                        and "•" not in raw
                        and "…" not in raw
                        and raw.strip() not in trusted_names
                    ):
                        values.add(raw.strip())
            for child in node.values():
                visit(child, cloud=node_cloud)
        elif isinstance(node, list):
            for child in node:
                visit(child, cloud=cloud)

    visit(value)
    return values


def mask_external_mqtt_string(
    value: str,
    *,
    sensitive_values: frozenset[str] = frozenset(),
    cloud_scoped: bool = False,
) -> str:
    """Mask known cloud identifiers and, in Cloud scope, cloud topic routes.

    Known Cloud route/product values in ``sensitive_values`` are masked in every
    string regardless of scope. Topic-shape masking (``iot/…/…`` and
    ``/…/…/<suffix>``) applies only when ``cloud_scoped`` so local MQTT topics,
    which are not Cloud account-scoped secrets, stay visible.
    """

    safe = value
    for raw in sorted(sensitive_values, key=len, reverse=True):
        replacement = str(mask_route_identifier(raw))
        if len(raw) >= 8:
            safe = safe.replace(raw, replacement)
        elif safe == raw:
            safe = replacement
        else:
            safe = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(raw)}(?![A-Za-z0-9])",
                replacement,
                safe,
            )
    safe = _EMBEDDED_CREDENTIAL_URL.sub(
        lambda match: f"{match.group('scheme')}<redacted>:<redacted>@",
        safe,
    )
    safe = _EMBEDDED_SECRET_LABEL.sub(
        lambda match: (
            f"{match.group('label')}{match.group('quote')}"
            f"<redacted>{match.group('quote')}"
        ),
        safe,
    )
    safe = _EMBEDDED_ROUTE_LABEL.sub(
        lambda match: (
            f"{match.group('label')}{match.group('quote')}"
            f"{mask_route_identifier(match.group('value'))}{match.group('quote')}"
        ),
        safe,
    )
    safe = _EMBEDDED_IOT_TOPIC.sub(
        lambda match: str(mask_mqtt_topic(match.group(), cloud_scoped=cloud_scoped)),
        safe,
    )
    safe = _EMBEDDED_ROOT_TOPIC.sub(
        lambda match: str(mask_mqtt_topic(match.group(), cloud_scoped=cloud_scoped)),
        safe,
    )
    return str(mask_mqtt_topic(safe, cloud_scoped=cloud_scoped))


def sanitize_external_mqtt_status(
    value: Any, *, sensitive_context: Any = None, drop_secrets: bool = True
) -> Any:
    """Deep-copy status data while masking cloud routes and dropping secrets."""

    cloud_refs = _cloud_broker_refs(value) | _cloud_broker_refs(sensitive_context)
    sensitive_values = _cloud_route_values(value, cloud_refs) | _cloud_route_values(
        sensitive_context, cloud_refs
    )
    trusted_cloud_names = _cloud_names_with_route(sensitive_context, cloud_refs)
    trusted_cloud_names |= {
        mask_external_mqtt_string(
            name, sensitive_values=frozenset(sensitive_values)
        )
        for name in trusted_cloud_names
    }
    trusted_cloud_names = frozenset(trusted_cloud_names)
    sensitive_values |= _cloud_dynamic_route_values(
        value, cloud_refs, trusted_names=trusted_cloud_names
    ) | _cloud_dynamic_route_values(
        sensitive_context, cloud_refs, trusted_names=trusted_cloud_names
    )
    sensitive_values |= _cloud_fail_closed_values(
        value, cloud_refs, trusted_names=trusted_cloud_names
    ) | _cloud_fail_closed_values(
        sensitive_context, cloud_refs, trusted_names=trusted_cloud_names
    )
    # A Cloud topic field encodes the product/device route; collect those segments
    # so the same route stays masked when it appears in free-form/global text.
    sensitive_values |= _cloud_topic_route_segments(
        value, cloud_refs
    ) | _cloud_topic_route_segments(sensitive_context, cloud_refs)
    secret_values = (
        _secret_values(value) | _secret_values(sensitive_context)
        if drop_secrets
        else set()
    )

    def replace_secrets(raw: str, *, mapping_key: bool = False) -> str:
        safe = raw
        for secret in sorted(secret_values, key=len, reverse=True):
            if len(secret) >= 8:
                safe = safe.replace(secret, "<redacted>")
            elif safe == secret:
                safe = "<redacted>"
            elif not mapping_key:
                safe = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])",
                    "<redacted>",
                    safe,
                )
        return safe

    def safe_string(raw: str, *, cloud_scoped: bool = False) -> str:
        return mask_external_mqtt_string(
            replace_secrets(raw),
            sensitive_values=frozenset(sensitive_values),
            cloud_scoped=cloud_scoped,
        )

    def safe_mapping_key(raw: str, *, cloud_scoped: bool = False) -> str:
        return mask_external_mqtt_string(
            replace_secrets(raw, mapping_key=True),
            sensitive_values=frozenset(sensitive_values),
            cloud_scoped=cloud_scoped,
        )

    def scrub(node: Any, *, cloud: bool = False, key: str = "") -> Any:
        if isinstance(node, Mapping):
            source = _node_source(node)
            ref = _node_broker_scope(node)
            node_cloud = cloud or source == SOURCE_ZENDURE_CLOUD_MQTT or (
                ref is not None and ref in cloud_refs
            )
            result = {}
            entry_container = _mapping_entry_container(key)
            for child_key, child in node.items():
                if (
                    drop_secrets
                    and not entry_container
                    and _drop_key(child_key)
                ):
                    continue
                lowered = str(child_key).lower()
                safe_key = (
                    safe_mapping_key(child_key, cloud_scoped=node_cloud)
                    if isinstance(child_key, str)
                    else child_key
                )
                if safe_key in result:
                    # Masked route keys can share the same display suffix.
                    # Retain every evidence entry with an ordinal that reveals
                    # nothing about either original identifier.
                    base_key = str(safe_key)
                    ordinal = 2
                    candidate = f"{base_key} [{ordinal}]"
                    while candidate in result:
                        ordinal += 1
                        candidate = f"{base_key} [{ordinal}]"
                    safe_key = candidate
                if lowered in _TRUSTED_IDENTITY_KEYS and isinstance(child, str):
                    result[safe_key] = replace_secrets(child)
                elif node_cloud and lowered in _ROUTE_KEYS:
                    result[safe_key] = mask_route_identifier(child)
                else:
                    result[safe_key] = scrub(
                        child, cloud=node_cloud, key=lowered
                    )
            return result
        if isinstance(node, list):
            # A named-entry exemption applies only when that container is a
            # mapping keyed by user labels. List elements are ordinary records,
            # so their credential fields must still be removed.
            return [scrub(child, cloud=cloud) for child in node]
        if isinstance(node, str):
            return safe_string(node, cloud_scoped=cloud)
        return node

    return scrub(value)


__all__ = [
    "mask_external_mqtt_string",
    "mask_mqtt_topic",
    "mask_route_identifier",
    "sanitize_external_mqtt_status",
]
