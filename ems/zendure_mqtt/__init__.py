# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure MQTT support: telemetry always on, output control per capability.

Topic classification, payload parsing, snapshot aggregation and the MQTT read
client provide telemetry. Output control is a first-class EMS control transport
(``capability``, ``control``, ``control_runtime``, ``device_client``,
``write_protocols``): a device is controllable when its topic family has a
verified write method (see :mod:`ems.zendure_mqtt.capability`). A control device
publishes ``outputLimit`` only behind its broker's MQTT write gate, with the EMS
controller remaining the source of truth.

The pure parsing/mapping modules (``topics``, ``payloads``, ``snapshot``,
``config_mapping``) are imported eagerly. The runtime client/service/config
modules are loaded lazily so that consumers which only need offline config
proposals (e.g. the Admin setup image) can import this package without shipping
the runtime modules or their MQTT client dependency.
"""

import importlib
from typing import TYPE_CHECKING

from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    ZENDURE_MQTT_TYPE,
    ZendureMqttBrokerProfileView,
    find_duplicate_zendure_device_identities,
    find_zendure_mqtt_broker_profile_issues,
    is_control_zendure_mqtt_device_config,
    is_telemetry_only_zendure_mqtt_device_config,
    is_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
    zendure_config_device_identity,
    zendure_mqtt_broker_profile_views,
    zendure_mqtt_broker_ref,
    zendure_mqtt_device_identifier,
    zendure_mqtt_product_key,
    zendure_mqtt_source,
    zendure_mqtt_write_topic,
)
from ems.zendure_mqtt.capability import (
    MqttOutputControlCapability,
    mqtt_output_control_capability,
    proposal_output_control,
)
from ems.zendure_mqtt.config_mapping import (
    ZendureMqttConfigProposal,
    map_snapshot_to_proposal,
    map_snapshots_to_proposals,
)
from ems.zendure_mqtt.payloads import (
    ParsedReport,
    coerce_scalar,
    parse_report_payload,
)
from ems.zendure_mqtt.snapshot import (
    ZendureMqttAggregator,
    ZendureMqttSnapshot,
    infer_capabilities,
)
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    TopicMatch,
    classify_topic,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from ems.zendure_mqtt.client import (
        ZendureMqttReadClient,
    )
    from ems.zendure_mqtt.config import (
        DEFAULT_LOCAL_SUBSCRIPTIONS,
        ZendureMqttClientConfig,
        ZendureMqttClientError,
    )
    from ems.zendure_mqtt.service import (
        ZendureMqttConfigError,
        ZendureMqttRuntimeConfig,
        ZendureMqttService,
    )
    from ems.zendure_mqtt.runtime import (
        InvalidZendureMqttDevice,
        ZendureMqttTelemetryDevice,
        ZendureMqttTelemetryRuntime,
        build_zendure_mqtt_runtime,
        classify_zendure_mqtt_devices,
        load_zendure_mqtt_broker_configs,
        load_zendure_mqtt_runtime_config,
        summarize_zendure_mqtt_devices,
    )

# name -> submodule providing it, resolved on first access via __getattr__.
_LAZY_EXPORTS = {
    "ZendureMqttReadClient": "client",
    # The error class lives in the paho-free config module so status-only
    # deployments (no client module) can import it from the package.
    "ZendureMqttClientError": "config",
    "ZendureMqttClientConfig": "config",
    "DEFAULT_LOCAL_SUBSCRIPTIONS": "config",
    "ZendureMqttService": "service",
    "ZendureMqttRuntimeConfig": "service",
    "ZendureMqttConfigError": "service",
    "ZendureMqttTelemetryRuntime": "runtime",
    "ZendureMqttTelemetryDevice": "runtime",
    "InvalidZendureMqttDevice": "runtime",
    "build_zendure_mqtt_runtime": "runtime",
    "classify_zendure_mqtt_devices": "runtime",
    "load_zendure_mqtt_broker_configs": "runtime",
    "load_zendure_mqtt_runtime_config": "runtime",
    "summarize_zendure_mqtt_devices": "runtime",
}


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, name)


__all__ = [
    "ZendureMqttReadClient",
    "ZendureMqttClientError",
    "ZendureMqttClientConfig",
    "DEFAULT_LOCAL_SUBSCRIPTIONS",
    "ZENDURE_MQTT_TYPE",
    "DEFAULT_BROKER_REF",
    "is_zendure_mqtt_device_config",
    "is_telemetry_only_zendure_mqtt_device_config",
    "is_control_zendure_mqtt_device_config",
    "validate_zendure_mqtt_device_config",
    "validate_zendure_mqtt_control_device_config",
    "zendure_mqtt_device_identifier",
    "zendure_mqtt_broker_ref",
    "zendure_mqtt_source",
    "zendure_mqtt_product_key",
    "zendure_mqtt_write_topic",
    "zendure_config_device_identity",
    "find_duplicate_zendure_device_identities",
    "find_zendure_mqtt_broker_profile_issues",
    "zendure_mqtt_broker_profile_views",
    "ZendureMqttBrokerProfileView",
    "ZendureMqttConfigProposal",
    "map_snapshot_to_proposal",
    "map_snapshots_to_proposals",
    "MqttOutputControlCapability",
    "mqtt_output_control_capability",
    "proposal_output_control",
    "ParsedReport",
    "coerce_scalar",
    "parse_report_payload",
    "ZendureMqttService",
    "ZendureMqttRuntimeConfig",
    "ZendureMqttConfigError",
    "ZendureMqttTelemetryRuntime",
    "ZendureMqttTelemetryDevice",
    "InvalidZendureMqttDevice",
    "build_zendure_mqtt_runtime",
    "classify_zendure_mqtt_devices",
    "load_zendure_mqtt_broker_configs",
    "load_zendure_mqtt_runtime_config",
    "summarize_zendure_mqtt_devices",
    "ZendureMqttAggregator",
    "ZendureMqttSnapshot",
    "infer_capabilities",
    "FAMILY_LEGACY_JSON",
    "FAMILY_LEGACY_JSON_ALT",
    "FAMILY_UNKNOWN",
    "FAMILY_ZENDURE_CLOUD_SCALAR",
    "FAMILY_ZENSDK_HA_SCALAR",
    "TopicMatch",
    "classify_topic",
]
