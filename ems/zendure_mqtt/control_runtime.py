# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemble write-capable MQTT control devices for the EMS control loop.

Every enabled control entry is either accepted as an active control device or
rejected with an explicit configuration error; an invalid enabled device is
never silently skipped. Accepted devices share one
:class:`ZendureMqttControlService` per referenced broker profile.
"""

from dataclasses import dataclass, field, replace

from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    SOURCE_ZENDURE_CLOUD_MQTT,
    config_entry_enabled,
    has_enabled_mqtt_control_device,
    is_control_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    zendure_cloud_device_subscriptions,
    zendure_mqtt_broker_profile_views,
    zendure_mqtt_broker_ref,
    zendure_mqtt_hardware_profile,
    zendure_mqtt_power_write_profile,
    zendure_mqtt_product_key,
    zendure_mqtt_route_device_id,
    zendure_mqtt_topic_family,
    zendure_mqtt_write_protocol,
    zendure_mqtt_write_topic,
)
from ems.mqtt_credentials import missing_mqtt_credential_fields
from ems.zendure_mqtt.control import ZendureMqttControlService
from ems.mqtt_control.confirmation import DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
from ems.zendure_mqtt.device_client import (
    DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS,
    DEFAULT_SAFETY_PREEMPT_MARGIN_W,
    ZendureMqttDeviceClient,
)
from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs


class MqttControlStartupError(RuntimeError):
    """MQTT control is configured but its runtime could not be built.

    Startup must treat this as fatal: continuing would silently drop configured
    control devices from the control loop.
    """


@dataclass(frozen=True)
class RejectedControlDevice:
    """An enabled control entry that failed validation; issues carry no secrets."""

    index: int
    name: str
    issues: tuple


@dataclass
class ZendureMqttControlRuntime:
    """Owns the control services and exposes the control-loop device adapters."""

    devices: list = field(default_factory=list)
    services: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    # One shared control service per referenced broker profile, keyed by
    # broker_ref. Reused by the telemetry runtime so a broker opens one
    # connection and keeps a single snapshot cache across telemetry and control.
    services_by_ref: dict = field(default_factory=dict)

    @property
    def has_rejections(self) -> bool:
        return bool(self.rejected)

    def start(self):
        for service in self.services:
            service.start()

    def stop(self):
        for service in self.services:
            service.stop()

    def status(self, *, now_monotonic=None) -> dict:
        """Credential-free control status: per-device freshness, gate, protocol."""

        return {
            "accepted_control_devices": len(self.devices),
            "rejected_control_devices": len(self.rejected),
            "service_count": len(self.services),
            "devices": [dev.describe(now_monotonic=now_monotonic) for dev in self.devices],
            "rejected": [
                {
                    "name": entry.name,
                    "issues": sorted(
                        {i["code"] for i in entry.issues if i.get("code")}
                    ),
                }
                for entry in self.rejected
            ],
        }


def _default_service_factory(broker_config):
    return ZendureMqttControlService(broker_config)


def _issue(code, message):
    return {"severity": "error", "code": code, "message": message}


def _broker_issues(ref, brokers, views):
    broker = brokers.get(ref)
    if broker is None:
        return [_issue("broker_unavailable", f"broker '{ref}' is not configured")]
    if broker.source == SOURCE_ZENDURE_CLOUD_MQTT and missing_mqtt_credential_fields(
        broker, source=SOURCE_ZENDURE_CLOUD_MQTT
    ):
        return [
            _issue(
                "broker_auth_missing",
                f"broker '{ref}' has no complete runtime credential",
            )
        ]
    if ref == DEFAULT_BROKER_REF:
        return []
    view = views.get(ref)
    code = view.usability_issue() if view is not None else "broker_unavailable"
    if code is not None:
        return [_issue(code, f"broker '{ref}' is not usable ({code})")]
    return []


def build_zendure_mqtt_control_runtime(
    config, *, service_factory=None, credential_resolver=None
):
    """Build control device adapters + their shared per-broker services.

    Enabled control entries that do not validate are returned in ``rejected``
    rather than being dropped; startup must treat any rejection as fatal.
    """

    service_factory = service_factory or _default_service_factory
    if not isinstance(config, dict):
        return ZendureMqttControlRuntime()
    devices = config.get("devices")
    raw = config.get("zendure_mqtt")

    brokers, _errors, _stale = load_zendure_mqtt_broker_configs(
        raw, credential_resolver=credential_resolver
    )
    known_refs = set(brokers) | {DEFAULT_BROKER_REF}
    broker_sources = {ref: broker.source for ref, broker in brokers.items()}
    brokers_defined = (
        isinstance(raw, dict)
        and isinstance(raw.get("brokers"), dict)
        and bool(raw.get("brokers"))
    )
    views = zendure_mqtt_broker_profile_views(raw)

    services_by_ref = {}
    device_clients = []
    rejected = []
    for index, item in enumerate(devices or []):
        if not is_control_zendure_mqtt_device_config(item):
            continue
        if not config_entry_enabled(item):
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else f"device-{index}"
        ref = zendure_mqtt_broker_ref(item)
        issues = [
            issue
            for issue in validate_zendure_mqtt_control_device_config(
                item,
                known_broker_refs=known_refs,
                brokers_defined=brokers_defined,
                broker_sources=broker_sources,
            )
            if issue.get("severity") == "error"
        ]
        if not issues:
            issues = _broker_issues(ref, brokers, views)
        if issues:
            rejected.append(RejectedControlDevice(index, name, tuple(issues)))
            continue

        service = services_by_ref.get(ref)
        if service is None:
            broker_config = brokers[ref]
            # Cloud sessions are ACL-scoped: the broad local wildcard families
            # are never delivered, so the shared cloud service subscribes the
            # per-device trees of every entry bound to this broker instead. An
            # explicit profile subscription list always wins.
            if (
                broker_config.source == SOURCE_ZENDURE_CLOUD_MQTT
                and broker_config.subscriptions is None
            ):
                derived = zendure_cloud_device_subscriptions(devices, ref)
                if derived:
                    broker_config = replace(broker_config, subscriptions=derived)
            service = service_factory(broker_config)
            services_by_ref[ref] = service
        device = ZendureMqttDeviceClient(
                name,
                service,
                # The MQTT route/payload device id is the explicit mqtt.device_id
                # only; validation above rejected any control entry lacking it, so a
                # physical serial is never routed here.
                device_id=zendure_mqtt_route_device_id(item),
                topic_family=zendure_mqtt_topic_family(item),
                # The broker profile is authoritative for the transport source
                # (and thus the write gate); a device may not override it. A
                # mismatching device source is rejected during validation above.
                source=brokers[ref].source,
                broker_ref=ref,
                product_key=zendure_mqtt_product_key(item),
                write_topic=zendure_mqtt_write_topic(item),
                write_protocol=zendure_mqtt_write_protocol(item),
                hardware_profile=zendure_mqtt_hardware_profile(item),
                power_write_profile=zendure_mqtt_power_write_profile(item),
                serial_number=item.get("serial_number") or item.get("sn"),
                min_soc=item.get("min_soc", 0),
                max_soc=item.get("max_soc", 0),
                smart_mode=item.get("smart_mode", 1),
                grid_off_mode=item.get("grid_off_mode"),
                max_power=item.get("max_power"),
                pv_kwp=item.get("pv_kwp", 1.0),
                battery_kwh=item.get("battery_kwh", 1.0),
                pv_priority_factor=item.get("pv_priority_factor", 1.0),
                command_ack_timeout_seconds=item.get(
                    "command_ack_timeout_seconds",
                    DEFAULT_COMMAND_ACK_TIMEOUT_SECONDS,
                ),
                confirmation_timeout_seconds=item.get(
                    "confirmation_timeout_seconds",
                    DEFAULT_CONFIRMATION_TIMEOUT_SECONDS,
                ),
                confirmation_tolerance_w=item.get("confirmation_tolerance_w"),
                safety_preempt_margin_w=item.get(
                    "safety_preempt_margin_w", DEFAULT_SAFETY_PREEMPT_MARGIN_W
                ),
            )
        # Route this device's command replies back to its command records. The
        # service (re)applies the registration across reconnects.
        register = getattr(service, "register_reply_handler", None)
        reply_topics = device.reply_topics()
        if callable(register) and reply_topics:
            register(reply_topics, device.handle_reply)
        device_clients.append(device)

    return ZendureMqttControlRuntime(
        device_clients, list(services_by_ref.values()), rejected, services_by_ref
    )


def build_zendure_mqtt_control_runtime_or_abort(
    config, *, service_factory=None, credential_resolver=None
):
    """Build the control runtime, raising when a configured runtime can't be made.

    Validation-level problems are still returned as ``rejected`` devices (startup
    handles those). This wrapper only concerns unexpected build failures: if any
    enabled MQTT control device is configured and the build raises, the failure
    is re-raised as :class:`MqttControlStartupError` so startup aborts instead of
    silently running without those devices. Without any enabled control device a
    build failure yields ``None`` and startup may continue.
    """

    try:
        return build_zendure_mqtt_control_runtime(
            config,
            service_factory=service_factory,
            credential_resolver=credential_resolver,
        )
    except Exception as exc:
        if has_enabled_mqtt_control_device(config):
            raise MqttControlStartupError(
                "MQTT control is configured but the control runtime could not be "
                "built"
            ) from exc
        return None
