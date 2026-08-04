# SPDX-License-Identifier: AGPL-3.0-or-later
"""Neutral MQTT topic-family identifiers (dependency-free primitives).

These string constants name the *observed telemetry transport* only. They live at
the bottom of the dependency graph — this module imports nothing from either the
:mod:`ems.mqtt_control` or :mod:`ems.zendure_mqtt` package bodies — so both the
capability authority (:mod:`ems.mqtt_control.power_capability`) and the transport
layer (:mod:`ems.zendure_mqtt.topics`) can share one source of truth without a
circular import.

The ``legacy_`` prefix is historical: it survives in stored configs but never
means a hardware generation. A topic family identifies a transport, never a write
capability.
"""

FAMILY_ZENSDK_HA_SCALAR = "zensdk_ha_scalar"
FAMILY_LEGACY_JSON = "legacy_zendure_json"
FAMILY_LEGACY_JSON_ALT = "legacy_zendure_json_alt"
FAMILY_ZENDURE_CLOUD_SCALAR = "zendure_cloud_scalar"
FAMILY_UNKNOWN = "unknown"

SCALAR_FAMILIES = frozenset({FAMILY_ZENSDK_HA_SCALAR, FAMILY_ZENDURE_CLOUD_SCALAR})
JSON_FAMILIES = frozenset({FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_ALT})

__all__ = [
    "FAMILY_ZENSDK_HA_SCALAR",
    "FAMILY_LEGACY_JSON",
    "FAMILY_LEGACY_JSON_ALT",
    "FAMILY_ZENDURE_CLOUD_SCALAR",
    "FAMILY_UNKNOWN",
    "SCALAR_FAMILIES",
    "JSON_FAMILIES",
]
