# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assembly of write-capable MQTT control devices from config."""

import json

import pytest

from ems.zendure_mqtt.control_runtime import (
    MqttControlStartupError,
    build_zendure_mqtt_control_runtime,
    build_zendure_mqtt_control_runtime_or_abort,
)


class FakeService:
    def __init__(self, broker_config):
        self.broker_config = broker_config
        self.started = False
        self.stopped = False
        self.connected = False
        self.published = []

    def start(self):
        self.started = True
        self.connected = True

    def stop(self):
        self.stopped = True
        self.connected = False

    def snapshots(self):
        return {}

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _config():
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "local_a": {"enabled": True, "source": "local_mqtt", "host": "a", "port": 1883},
                "local_b": {"enabled": True, "source": "local_mqtt", "host": "b", "port": 1883},
                "cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "c",
                    "port": 8883,
                    "credentials_ref": "cloud-token",
                    # Unit-test runtime credentials; production resolves these
                    # fields from the external credential record.
                    "username": "cloud-user",
                    "password": "cloud-password",
                    "client_id": "cloud-client",
                    "app_key": "cloud-app",
                },
            },
        },
        "devices": [
            {"name": "HTTP", "ip": "1.2.3.4", "sn": "SNHTTP"},
            {
                "type": "zendure_mqtt",
                "name": "Cloud",
                "mqtt": {
                    "broker_ref": "cloud",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEVC",
                    "product_key": "PKC",
                    "hardware_profile": "solarflow_800_pro_2",
                },
                "capabilities": {"write_output_limit": True},
            },
            {
                "type": "zendure_mqtt",
                "name": "LocalA",
                "mqtt": {
                    "broker_ref": "local_a",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEVA",
                    "product_key": "PKA",
                    "hardware_profile": "solarflow_800_pro_2",
                },
                "capabilities": {"write_output_limit": True},
            },
            {
                "type": "zendure_mqtt",
                "name": "LocalB",
                "mqtt": {
                    "broker_ref": "local_b",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEVB",
                    "product_key": "PKB",
                    "hardware_profile": "solarflow_800_pro_2",
                },
                "capabilities": {"write_output_limit": True},
            },
        ],
    }


def test_builds_one_device_per_control_entry_with_correct_gates():
    runtime = build_zendure_mqtt_control_runtime(
        _config(), service_factory=FakeService
    )
    by_name = {d.name: d for d in runtime.devices}
    assert set(by_name) == {"Cloud", "LocalA", "LocalB"}
    assert by_name["Cloud"].control_gate == "mqtt_zendure"
    assert by_name["LocalA"].control_gate == "mqtt_local"
    assert by_name["LocalB"].control_gate == "mqtt_local"


def test_legacy_top_level_enabled_false_does_not_block_control_devices():
    config = _config()
    config["zendure_mqtt"]["enabled"] = False
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    assert {d.name for d in runtime.devices} == {"Cloud", "LocalA", "LocalB"}
    assert runtime.rejected == []


def test_one_service_per_broker_started_and_stopped():
    runtime = build_zendure_mqtt_control_runtime(
        _config(), service_factory=FakeService
    )
    assert len(runtime.services) == 3  # local_a, local_b, cloud
    runtime.start()
    assert all(s.started for s in runtime.services)
    runtime.stop()
    assert all(s.stopped for s in runtime.services)


def test_devices_publish_through_their_own_broker_service():
    runtime = build_zendure_mqtt_control_runtime(
        _config(), service_factory=FakeService
    )
    by_name = {d.name: d for d in runtime.devices}
    # Each device shares the service of its broker only.
    service_a = by_name["LocalA"]._service
    service_b = by_name["LocalB"]._service
    assert service_a is not service_b

    by_name["LocalA"].write_output_limit(200)
    assert len(service_a.published) == 1
    topic, payload = service_a.published[0]
    assert topic == "iot/PKA/DEVA/properties/write"
    assert json.loads(payload)["properties"] == {"outputLimit": 200}
    assert service_b.published == []


def test_no_control_devices_yields_empty_runtime():
    runtime = build_zendure_mqtt_control_runtime(
        {"devices": [{"name": "HTTP", "ip": "1.2.3.4", "sn": "SN"}]},
        service_factory=FakeService,
    )
    assert runtime.devices == []
    assert runtime.services == []
    assert runtime.rejected == []


def _control_device(name, broker_ref, **mqtt):
    base = {
        "broker_ref": broker_ref,
        "topic_family": "legacy_zendure_json",
        "device_id": "DEV" + name,
        "product_key": "PK" + name,
    }
    base.update(mqtt)
    # A legacy control device pins a concrete registry hardware profile; a scalar
    # family is intentionally left without one so it stays rejected.
    if base.get("topic_family") in ("legacy_zendure_json", "legacy_zendure_json_alt") and (
        "write_protocol" not in base and "hardware_profile" not in base
    ):
        base["hardware_profile"] = "solarflow_800_pro_2"
    return {
        "type": "zendure_mqtt",
        "name": name,
        "mqtt": base,
        "capabilities": {"write_output_limit": True},
    }


def test_unknown_broker_ref_is_rejected_not_skipped():
    config = _config()
    config["devices"].append(_control_device("Ghost", "does_not_exist"))
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    assert "Ghost" not in {d.name for d in runtime.devices}
    rejected = {r.name for r in runtime.rejected}
    assert "Ghost" in rejected


def test_cloud_broker_service_subscribes_per_device_topics():
    # Cloud sessions are ACL-scoped: broad local wildcards are never delivered,
    # so the shared cloud service must subscribe the per-device trees plus the
    # account tree, while local brokers keep the default local families.
    from ems.zendure_mqtt import DEFAULT_LOCAL_SUBSCRIPTIONS

    runtime = build_zendure_mqtt_control_runtime(
        _config(), service_factory=FakeService
    )
    cloud = runtime.services_by_ref["cloud"].broker_config
    assert cloud.client_config().resolved_subscriptions() == (
        "/PKC/DEVC/#",
        "iot/PKC/DEVC/#",
        "cloud-app/#",
    )
    local = runtime.services_by_ref["local_a"].broker_config
    assert (
        local.client_config().resolved_subscriptions()
        == DEFAULT_LOCAL_SUBSCRIPTIONS
    )


def test_cloud_subscriptions_include_telemetry_only_devices():
    # Telemetry-only devices share the cloud service with control devices; their
    # per-device trees must be part of the same subscription set.
    config = _config()
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "CloudTele",
            "mqtt": {
                "broker_ref": "cloud",
                "topic_family": "legacy_zendure_json",
                "device_id": "DEVT",
                "product_key": "PKC",
            },
            "capabilities": {"read_power": True, "write_output_limit": False},
        }
    )
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    subs = runtime.services_by_ref["cloud"].broker_config.client_config().resolved_subscriptions()
    assert "/PKC/DEVT/#" in subs
    assert "iot/PKC/DEVT/#" in subs


def test_cloud_control_device_with_missing_runtime_credential_is_rejected():
    from ems.mqtt_credentials import MqttCredentialError

    class MissingResolver:
        def resolve(self, _ref):
            raise MqttCredentialError("record missing")

    config = _config()
    cloud = config["zendure_mqtt"]["brokers"]["cloud"]
    for key in ("username", "password", "client_id", "app_key"):
        cloud.pop(key, None)

    runtime = build_zendure_mqtt_control_runtime(
        config,
        service_factory=FakeService,
        credential_resolver=MissingResolver(),
    )
    rejected = next(entry for entry in runtime.rejected if entry.name == "Cloud")
    assert "broker_auth_missing" in {issue["code"] for issue in rejected.issues}


def test_scalar_family_control_device_is_rejected():
    config = _config()
    config["devices"].append(
        _control_device("Scalar", "local_a", topic_family="zensdk_ha_scalar")
    )
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    scalar = next(r for r in runtime.rejected if r.name == "Scalar")
    assert "write_protocol_unsupported" in {i["code"] for i in scalar.issues}


def test_missing_identifier_control_device_is_rejected():
    config = _config()
    bad = _control_device("NoId", "local_a")
    del bad["mqtt"]["device_id"]
    config["devices"].append(bad)
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    assert "NoId" in {r.name for r in runtime.rejected}


def test_disabled_control_device_is_neither_active_nor_rejected():
    config = _config()
    disabled = _control_device("Off", "does_not_exist")
    disabled["enabled"] = False
    config["devices"].append(disabled)
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    assert "Off" not in {d.name for d in runtime.devices}
    assert "Off" not in {r.name for r in runtime.rejected}


def test_active_count_matches_validated_config():
    runtime = build_zendure_mqtt_control_runtime(_config(), service_factory=FakeService)
    assert len(runtime.devices) == 3
    assert runtime.rejected == []


def test_cloud_broker_with_local_device_source_is_rejected():
    config = _config()
    config["devices"].append(
        _control_device("Mismatch", "cloud", source="local_mqtt")
    )
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    assert "Mismatch" not in {d.name for d in runtime.devices}
    entry = next(r for r in runtime.rejected if r.name == "Mismatch")
    assert "mqtt_source_mismatch" in {i["code"] for i in entry.issues}


def test_local_broker_with_cloud_device_source_is_rejected():
    config = _config()
    config["devices"].append(
        _control_device("Mismatch", "local_a", source="zendure_cloud_mqtt")
    )
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    entry = next(r for r in runtime.rejected if r.name == "Mismatch")
    assert "mqtt_source_mismatch" in {i["code"] for i in entry.issues}


def test_missing_device_source_inherits_broker_source():
    # The base Cloud/LocalA devices carry no mqtt.source; the broker profile is
    # authoritative for the transport source.
    runtime = build_zendure_mqtt_control_runtime(_config(), service_factory=FakeService)
    by_name = {d.name: d for d in runtime.devices}
    assert by_name["Cloud"].source == "zendure_cloud_mqtt"
    assert by_name["LocalA"].source == "local_mqtt"


def test_gate_selection_always_follows_broker_profile_not_device():
    # A matching device source is accepted but never changes the outcome: the
    # gate follows the broker profile. (A mismatching source is rejected above.)
    config = _config()
    config["devices"].append(
        _control_device("CloudExplicit", "cloud", source="zendure_cloud_mqtt")
    )
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    by_name = {d.name: d for d in runtime.devices}
    assert by_name["CloudExplicit"].source == "zendure_cloud_mqtt"
    assert by_name["CloudExplicit"].control_gate == "mqtt_zendure"


def test_status_lists_control_devices_without_credentials():
    config = _config()
    config["devices"].append(_control_device("Scalar", "local_a", topic_family="zensdk_ha_scalar"))
    runtime = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    status = runtime.status()

    assert status["accepted_control_devices"] == 3
    assert status["rejected_control_devices"] == 1
    cloud = next(d for d in status["devices"] if d["name"] == "Cloud")
    assert cloud["broker_ref"] == "cloud"
    assert cloud["source"] == "zendure_cloud_mqtt"
    assert cloud["write_gate"] == "allow_mqtt_zendure_control_writes"
    # MQTT control is a normal transport; status must not label it experimental.
    assert "experimental" not in cloud
    assert cloud["control_enabled"] is True
    assert cloud["state"] == "unseen"
    # The write method now comes from the pinned profile, not an explicit protocol.
    assert cloud["write_protocol"] is None
    # No credential-bearing fields leak into status.
    flat = json.dumps(status)
    for secret in ("password", "credentials_ref", "token", "app_key"):
        assert secret not in flat


def _boom_factory(_broker_config):
    raise RuntimeError("service creation failed")


def test_build_or_abort_raises_when_control_configured_and_build_fails():
    # An enabled control device is configured but service creation blows up:
    # startup must not silently continue without it.
    with pytest.raises(MqttControlStartupError):
        build_zendure_mqtt_control_runtime_or_abort(
            _config(), service_factory=_boom_factory
        )


def test_build_or_abort_returns_none_without_control_devices(monkeypatch):
    # Force the build itself to raise. Without any enabled control device the
    # failure is not fatal, so the helper degrades to None and startup continues.
    import ems.zendure_mqtt.control_runtime as cr

    def _boom(*_a, **_k):
        raise RuntimeError("build blew up")

    monkeypatch.setattr(cr, "build_zendure_mqtt_control_runtime", _boom)
    config = {
        "zendure_mqtt": {"enabled": True, "brokers": {}},
        "devices": [{"name": "HTTP", "ip": "1.2.3.4", "sn": "SN"}],
    }
    assert build_zendure_mqtt_control_runtime_or_abort(config) is None


def test_build_or_abort_returns_runtime_on_success():
    runtime = build_zendure_mqtt_control_runtime_or_abort(
        _config(), service_factory=FakeService
    )
    assert len(runtime.devices) == 3
