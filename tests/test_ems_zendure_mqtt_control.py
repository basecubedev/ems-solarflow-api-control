# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Zendure MQTT control stack (write path).

Broker-free: a fake paho-style client is injected so publish behaviour is
asserted without a network broker. The write gates themselves are tested in
tests/test_control_write_gates.py; here we exercise the mechanism.
"""

import json
from types import SimpleNamespace

from ems.zendure_mqtt.config import ZendureMqttClientConfig
from ems.zendure_mqtt.control import (
    ZendureMqttControlClient,
    ZendureMqttControlService,
)
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.service import ZendureMqttRuntimeConfig
from ems.zendure_mqtt.topics import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENSDK_HA_SCALAR,
)
from ems.zendure_mqtt.write_protocols import (
    PROTOCOL_CUSTOM_PROPERTIES_WRITE,
    PROTOCOL_LEGACY_PROPERTIES_WRITE,
    build_output_limit_message,
    next_message_id,
    resolve_write_protocol,
)


class FakeMqttClient:
    """Minimal paho-style stand-in recording publishes and delivering messages."""

    def __init__(self):
        self.subscriptions = []
        self.publish_calls = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def tls_set(self, *a, **k):
        pass

    def tls_insecure_set(self, v):
        pass

    def username_pw_set(self, u, p=None):
        pass

    def connect(self, host, port, keepalive=0):
        pass

    def loop_start(self):
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.publish_calls.append((topic, payload, qos, retain))
        return None


# --- write protocol resolution ---------------------------------------------


def test_legacy_families_have_no_inferred_write_protocol():
    # Topic-family write inference was removed: a bare legacy family never
    # resolves a write protocol on its own.
    assert resolve_write_protocol(FAMILY_LEGACY_JSON) is None
    assert resolve_write_protocol(FAMILY_LEGACY_JSON_ALT) is None


def test_scalar_and_unknown_families_have_no_inferred_protocol():
    assert resolve_write_protocol(FAMILY_ZENSDK_HA_SCALAR) is None
    assert resolve_write_protocol(FAMILY_UNKNOWN) is None


def test_explicit_protocol_must_be_supported():
    # Only the isolated custom escape hatch config-authorizes a no-profile write.
    assert (
        resolve_write_protocol(FAMILY_ZENSDK_HA_SCALAR, "custom_properties_write")
        == PROTOCOL_CUSTOM_PROPERTIES_WRITE
    )
    assert resolve_write_protocol(FAMILY_LEGACY_JSON, "made_up_protocol") is None


def test_built_in_legacy_protocol_does_not_config_authorize():
    # The built-in legacy_properties_write shape never authorizes from config; it
    # is reachable only through a concrete ZenSDK hardware profile.
    assert resolve_write_protocol(FAMILY_LEGACY_JSON, "legacy_properties_write") is None
    assert (
        resolve_write_protocol(FAMILY_ZENSDK_HA_SCALAR, "legacy_properties_write")
        is None
    )


# --- write message builder -------------------------------------------------


def test_legacy_message_topic_and_payload_fields():
    message = build_output_limit_message(
        PROTOCOL_LEGACY_PROPERTIES_WRITE,
        topic_family=FAMILY_LEGACY_JSON,
        product_key="PK",
        device_id="DEV",
        output_limit_w=321.6,
        message_id=7,
        timestamp=1700000000,
    )
    assert message.topic == "iot/PK/DEV/properties/write"
    body = json.loads(message.payload)
    assert body["deviceId"] == "DEV"
    assert body["messageId"] == 7
    assert body["timestamp"] == 1700000000
    assert body["properties"] == {"outputLimit": 321}


def test_legacy_slash_report_family_still_writes_to_iot_topic():
    # Devices on the leading-slash report family accept commands on iot/… only
    # (live cloud capture + reference implementation); a leading-slash write
    # topic is silently undelivered and must never be built.
    message = build_output_limit_message(
        PROTOCOL_LEGACY_PROPERTIES_WRITE,
        topic_family=FAMILY_LEGACY_JSON_ALT,
        product_key="PK",
        device_id="DEV",
        output_limit_w=100,
    )
    assert message.topic == "iot/PK/DEV/properties/write"


def test_message_none_when_unaddressable():
    assert (
        build_output_limit_message(
            PROTOCOL_LEGACY_PROPERTIES_WRITE,
            topic_family=FAMILY_LEGACY_JSON,
            product_key=None,
            device_id="DEV",
            output_limit_w=100,
        )
        is None
    )


def test_unsupported_protocol_yields_no_message():
    assert (
        build_output_limit_message(
            None,
            topic_family=FAMILY_ZENSDK_HA_SCALAR,
            product_key="PK",
            device_id="DEV",
            output_limit_w=100,
        )
        is None
    )


def test_custom_protocol_requires_explicit_topic():
    assert (
        build_output_limit_message(
            PROTOCOL_CUSTOM_PROPERTIES_WRITE,
            topic_family=FAMILY_UNKNOWN,
            device_id="DEV",
            output_limit_w=100,
        )
        is None
    )
    message = build_output_limit_message(
        PROTOCOL_CUSTOM_PROPERTIES_WRITE,
        topic_family=FAMILY_UNKNOWN,
        device_id="DEV",
        output_limit_w=100,
        write_topic="custom/topic/write",
    )
    assert message.topic == "custom/topic/write"


def test_message_ids_progress_monotonically():
    first = next_message_id()
    second = next_message_id()
    assert second > first


# --- control client + service ----------------------------------------------


def _service(**config):
    fake = FakeMqttClient()
    runtime = ZendureMqttRuntimeConfig(enabled=True, host="broker.local", **config)
    service = ZendureMqttControlService(
        runtime, read_client_factory=lambda _cfg: ZendureMqttControlClient(
            _cfg, client_factory=lambda _c: fake
        )
    )
    return service, fake


def test_control_client_publishes_when_connected():
    config = ZendureMqttClientConfig(host="broker.local")
    fake = FakeMqttClient()
    client = ZendureMqttControlClient(config, client_factory=lambda _c: fake)
    client.start()
    assert client.publish("iot/PK/DEV/properties/write", "{}") is True
    assert fake.publish_calls == [("iot/PK/DEV/properties/write", "{}", 0, False)]


def test_control_client_does_not_publish_when_down():
    config = ZendureMqttClientConfig(host="broker.local")
    client = ZendureMqttControlClient(config, client_factory=lambda _c: FakeMqttClient())
    # Never started -> no connection -> publish is a no-op returning False.
    assert client.publish("t", "{}") is False


def test_service_publish_output_limit_roundtrip():
    service, fake = _service()
    service.start()
    assert service.publish_output_limit("iot/PK/DEV/properties/write", "{}") is True
    assert fake.publish_calls[0][0] == "iot/PK/DEV/properties/write"


def test_service_publish_returns_false_when_stopped():
    service, _fake = _service()
    assert service.publish_output_limit("t", "{}") is False


# --- device adapter --------------------------------------------------------


class FakeService:
    def __init__(self, metrics=None):
        self._metrics = metrics
        self.published = []

    def snapshots(self):
        if self._metrics is None:
            return {}
        return {"DEV": SimpleNamespace(metrics=self._metrics)}

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(
            self.snapshots().get(device_id), 60.0, now_monotonic=now_monotonic or 0.0
        )

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(service, **kwargs):
    return ZendureMqttDeviceClient(
        "WR-MQTT",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        max_power=800,
        **kwargs,
    )


def test_device_fetch_maps_snapshot_metrics_to_state():
    service = FakeService(metrics={"electricLevel": 55, "outputLimit": 300})
    dev = _device(service)
    state = dev.fetch()
    assert state is not None
    assert state.soc == 55
    assert state.output_limit == 300


def test_device_fetch_returns_none_without_snapshot():
    dev = _device(FakeService(metrics=None))
    assert dev.fetch() is None


def test_device_write_output_limit_publishes_and_gate_is_local():
    service = FakeService(metrics={})
    # A concrete ZenSDK profile selects the properties/write shape (its transport
    # is compatible with the JSON-report family); no explicit write_protocol.
    dev = _device(service, hardware_profile="solarflow_800_pro_2")
    assert dev.control_gate == "mqtt_local"
    assert dev.write_protocol is None
    assert dev.write_output_limit(250) is True
    topic, payload = service.published[0]
    assert topic == "iot/PK/DEV/properties/write"
    assert json.loads(payload)["properties"] == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 250,
        "inputLimit": 0,
    }


def test_device_cloud_source_maps_to_zendure_gate():
    dev = ZendureMqttDeviceClient(
        "WR-CLOUD",
        FakeService(metrics={}),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK",
    )
    assert dev.control_gate == "mqtt_zendure"


def test_device_without_write_topic_fails_closed():
    dev = ZendureMqttDeviceClient(
        "WR-NOKEY",
        FakeService(metrics={}),
        device_id="DEV",
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        source="local_mqtt",
        product_key=None,
    )
    assert dev.write_output_limit(100) is False


def test_device_is_excluded_from_state_reconciliation():
    dev = _device(FakeService(metrics={}))
    assert dev.supports_state_reconciliation is False


def test_reconciliation_skip_is_capability_based_not_type_based():
    # HTTP devices keep reconciliation (no opt-out attribute -> default True);
    # any transport can opt out via the same capability flag, not a type check.
    from ems.clients import ZendureClient

    assert getattr(ZendureClient, "supports_state_reconciliation", True) is True
    mqtt_dev = _device(FakeService(metrics={}))
    future_transport = SimpleNamespace(supports_state_reconciliation=False)
    assert getattr(mqtt_dev, "supports_state_reconciliation", True) is False
    assert getattr(future_transport, "supports_state_reconciliation", True) is False


# --- config predicate + control validator ----------------------------------


def _control_entry(**mqtt):
    base = {"topic_family": "legacy_zendure_json", "device_id": "DEV", "product_key": "PK"}
    base.update(mqtt)
    return {
        "type": "zendure_mqtt",
        "name": "Ctrl",
        "mqtt": base,
        "capabilities": {"write_output_limit": True},
    }


def test_control_predicate_distinguishes_control_from_telemetry():
    from ems.zendure_mqtt.config_entries import (
        is_control_zendure_mqtt_device_config,
        is_telemetry_only_zendure_mqtt_device_config,
    )

    control = _control_entry()
    telemetry = {
        "type": "zendure_mqtt",
        "name": "Tele",
        "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV"},
    }
    assert is_control_zendure_mqtt_device_config(control) is True
    assert is_control_zendure_mqtt_device_config(telemetry) is False
    assert is_telemetry_only_zendure_mqtt_device_config(control) is False


def test_control_validator_accepts_addressable_entry():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    # A pinned, writable, transport-compatible model makes the entry valid.
    assert (
        validate_zendure_mqtt_control_device_config(
            _control_entry(hardware_profile="hyper_2000")
        )
        == []
    )


def test_control_validator_requires_write_target_and_opt_in():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    no_target = _control_entry(product_key=None)
    codes = {i["code"] for i in validate_zendure_mqtt_control_device_config(no_target)}
    assert "write_target_missing" in codes

    not_requested = {
        "type": "zendure_mqtt",
        "name": "Ctrl",
        "mqtt": {"topic_family": "legacy_zendure_json", "device_id": "DEV", "product_key": "PK"},
    }
    codes = {
        i["code"]
        for i in validate_zendure_mqtt_control_device_config(not_requested)
    }
    assert "control_not_requested" in codes


def test_control_validator_rejects_scalar_family_write():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    scalar = _control_entry(topic_family="zensdk_ha_scalar")
    codes = {i["code"] for i in validate_zendure_mqtt_control_device_config(scalar)}
    assert "write_protocol_unsupported" in codes


def test_control_validator_allows_custom_escape_hatch_on_scalar_family():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    # The isolated custom escape hatch (explicit custom_properties_write + an
    # explicit write_topic) is the only no-profile method that validates.
    explicit = _control_entry(
        topic_family="zensdk_ha_scalar",
        write_protocol="custom_properties_write",
        write_topic="Zendure/number/DEV/outputLimit",
    )
    assert validate_zendure_mqtt_control_device_config(explicit) == []


def test_control_validator_rejects_built_in_legacy_protocol_without_profile():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    explicit = _control_entry(
        topic_family="zensdk_ha_scalar", write_protocol="legacy_properties_write"
    )
    codes = {i["code"] for i in validate_zendure_mqtt_control_device_config(explicit)}
    assert "write_protocol_unsupported" in codes
