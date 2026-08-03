# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-broker Zendure MQTT runtime: per-broker scoping and safe status.

Broker-free: a recording fake-service factory stands in for the real read-only
service so per-broker snapshot scoping and credential-free status are exercised
without a network broker.
"""

import json

import pytest

from ems.zendure_mqtt import build_zendure_mqtt_runtime
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class FakeService:
    def __init__(self, config):
        self.config = config
        self.started = False
        self._snapshots = {}

    def set_snapshots(self, snapshots):
        self._snapshots = dict(snapshots)

    def start(self):
        if self.config.enabled:
            self.started = True

    def stop(self):
        self.started = False

    @property
    def running(self):
        return self.started

    @property
    def connected(self):
        return self.started

    def snapshots(self):
        return dict(self._snapshots)

    def status(self):
        return {
            "enabled": self.config.enabled,
            "running": self.running,
            "connected": self.connected,
            "host": self.config.host,
            "port": self.config.port,
            "snapshot_count": len(self._snapshots),
            "last_error": None,
        }


class RecordingFactory:
    def __init__(self):
        self.services = {}

    def __call__(self, config):
        service = FakeService(config)
        self.services[config.broker_ref] = service
        return service


def _snapshot(device_id, *, metric="electricLevel", last_seen_monotonic=100.0):
    return ZendureMqttSnapshot(
        device_id=device_id,
        metrics={metric: 42},
        capabilities={"battery_storage"},
        last_seen_epoch=1_700_000_000.0,
        last_seen_monotonic=last_seen_monotonic,
    )


def _two_broker_config():
    return {
        "zendure_mqtt": {
            "enabled": True,
            "stale_after_seconds": 60,
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "tls": True,
                    "tls_insecure": True,
                    "username": "cloud-user",
                    "password": "cloud-secret-pw",
                    "app_key": "cloud-app-key",
                },
                "local_mqtt": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "192.168.20.10",
                    "port": 1883,
                },
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "SolarFlow via cloud MQTT",
                "mqtt": {
                    "broker_ref": "zendure_cloud",
                    "source": "zendure_cloud_mqtt",
                    "topic_family": "zensdk_ha_scalar",
                    "device_id": "CLOUDDEV",
                },
            },
            {
                "type": "zendure_mqtt",
                "name": "SolarFlow via local MQTT",
                "mqtt": {
                    "broker_ref": "local_mqtt",
                    "source": "local_mqtt",
                    "topic_family": "zensdk_ha_scalar",
                    "device_id": "LOCALDEV",
                },
            },
        ],
    }


def test_two_devices_use_two_broker_services():
    factory = RecordingFactory()
    runtime = build_zendure_mqtt_runtime(
        _two_broker_config(), service_factory=factory
    )
    runtime.start()
    status = runtime.status()
    assert status["broker_count"] == 2
    refs = {broker["broker_ref"] for broker in status["brokers"]}
    assert refs == {"zendure_cloud", "local_mqtt"}
    assert set(factory.services) == {"zendure_cloud", "local_mqtt"}


def test_device_summary_only_from_its_own_broker():
    factory = RecordingFactory()
    runtime = build_zendure_mqtt_runtime(
        _two_broker_config(), service_factory=factory
    )
    runtime.start()
    # Publish the cloud device's telemetry on the *local* broker only.
    factory.services["local_mqtt"].set_snapshots({"CLOUDDEV": _snapshot("CLOUDDEV")})

    by_name = {d["name"]: d for d in runtime.device_summaries(now_monotonic=120.0)}
    # The cloud device must not be satisfied by the local broker snapshot.
    assert by_name["SolarFlow via cloud MQTT"]["status"] == "unseen"
    assert by_name["SolarFlow via local MQTT"]["status"] == "unseen"

    # Now publish it on the correct (cloud) broker.
    factory.services["zendure_cloud"].set_snapshots(
        {"CLOUDDEV": _snapshot("CLOUDDEV", last_seen_monotonic=118.0)}
    )
    by_name = {d["name"]: d for d in runtime.device_summaries(now_monotonic=120.0)}
    assert by_name["SolarFlow via cloud MQTT"]["status"] == "online"
    assert by_name["SolarFlow via cloud MQTT"]["broker_ref"] == "zendure_cloud"
    assert by_name["SolarFlow via local MQTT"]["status"] == "unseen"


def test_status_has_broker_refs_sources_endpoints_but_no_secrets():
    factory = RecordingFactory()
    runtime = build_zendure_mqtt_runtime(
        _two_broker_config(), service_factory=factory
    )
    runtime.start()
    status = runtime.status()
    brokers = {b["broker_ref"]: b for b in status["brokers"]}
    assert brokers["zendure_cloud"]["source"] == "zendure_cloud_mqtt"
    assert brokers["zendure_cloud"]["endpoint"] == "mqtteu.zen-iot.com:8883"
    assert brokers["local_mqtt"]["endpoint"] == "192.168.20.10:1883"
    devices = {d["name"]: d for d in status["devices"]}
    assert devices["SolarFlow via cloud MQTT"]["broker_ref"] == "zendure_cloud"
    assert devices["SolarFlow via cloud MQTT"]["source"] == "zendure_cloud_mqtt"

    flattened = json.dumps(status)
    for secret in ("cloud-user", "cloud-secret-pw", "cloud-app-key"):
        assert secret not in flattened


def test_no_publish_or_write_methods_are_exposed():
    factory = RecordingFactory()
    runtime = build_zendure_mqtt_runtime(
        _two_broker_config(), service_factory=factory
    )
    for forbidden in ("publish", "write", "set_output_limit", "invoke"):
        assert not hasattr(runtime, forbidden)
    for service in factory.services.values():
        for forbidden in ("publish", "write", "set_output_limit", "invoke"):
            assert not hasattr(service, forbidden)


def test_old_single_broker_config_starts_as_default_broker():
    factory = RecordingFactory()
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"enabled": True, "host": "broker.local", "port": 1883},
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Legacy Battery",
                    "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV1"},
                }
            ],
        },
        service_factory=factory,
    )
    runtime.start()
    status = runtime.status()
    assert status["broker_count"] == 1
    assert status["brokers"][0]["broker_ref"] == "default"
    assert status["brokers"][0]["endpoint"] == "broker.local:1883"
    assert status["configured_device_count"] == 1
    assert status["devices"][0]["broker_ref"] == "default"
    assert set(factory.services) == {"default"}


def test_api_only_config_starts_no_broker():
    factory = RecordingFactory()
    runtime = build_zendure_mqtt_runtime(
        {
            "devices": [
                {"name": "WR1", "type": "solarflow", "ip": "192.168.1.10", "sn": "SN1"}
            ]
        },
        service_factory=factory,
    )
    runtime.start()
    status = runtime.status()
    assert status["broker_count"] == 0
    assert status["configured_device_count"] == 0
    assert status["enabled"] is False
    assert factory.services == {}
