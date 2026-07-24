# SPDX-License-Identifier: AGPL-3.0-or-later
"""MQTT broker discovery tests without real multicast or network probes."""

import pytest

from admin.models import MqttBrokerCandidate
from admin.mqtt_discovery import (
    MQTT_MDNS_SERVICE_TYPE,
    MQTT_PORTS,
    MqttBrokerDiscovery,
    MqttBrokerStore,
    build_mqtt_mdns_candidate,
)

pytestmark = pytest.mark.simulation


def _mdns_candidate(port=1883):
    return build_mqtt_mdns_candidate(
        "mosquitto._mqtt._tcp.local.",
        "mqtt.local.",
        ["192.168.178.10"],
        port,
        {b"version": b"2.0", b"flag": None},
    )


def test_mqtt_mdns_service_parsing_and_candidate_creation():
    candidate = _mdns_candidate()
    assert MQTT_MDNS_SERVICE_TYPE == "_mqtt._tcp.local."
    assert candidate["id"] == "mqtt:192.168.178.10:1883"
    assert candidate["host"] == "192.168.178.10"
    assert candidate["hostname"] == "mqtt.local."
    assert candidate["service_name"] == "mosquitto._mqtt._tcp.local."
    assert candidate["source"] == "mdns"
    assert candidate["reachable"] is True
    assert candidate["details"]["txt"] == {"version": "2.0", "flag": ""}


def test_mqtt_candidates_deduplicate_by_host_and_port():
    store = MqttBrokerStore()
    store.merge(_mdns_candidate())
    duplicate = dict(_mdns_candidate())
    duplicate["last_seen"] = "2026-07-01T15:00:00Z"
    store.merge(duplicate)
    assert len(store.to_list()) == 1
    assert store.to_list()[0]["last_seen"] == "2026-07-01T15:00:00Z"


def test_probe_creates_candidates_for_open_1883_and_8883_only():
    calls = []

    def connector(host, port, timeout_s):
        calls.append((host, port, timeout_s))
        return port in MQTT_PORTS

    discovery = MqttBrokerDiscovery(connector=connector)
    result = discovery.probe("192.168.178.10/32")

    assert {item["port"] for item in result["candidates"]} == {1883, 8883}
    assert all(item["source"] == "network_probe" for item in result["candidates"])
    assert all(item["status"] == "tcp_open" for item in result["candidates"])
    assert {port for _, port, _ in calls} == {1883, 8883}


def test_probe_reports_transport_and_tested_combinations():
    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: port in MQTT_PORTS
    )
    result = discovery.probe("192.168.178.10/32")

    by_port = {item["port"]: item for item in result["candidates"]}
    assert by_port[1883]["transport"] == "plaintext"
    assert by_port[8883]["transport"] == "tls"
    assert all(item["auth_mode"] == "anonymous" for item in result["candidates"])
    assert all(
        item["mqtt_connect_status"] == "tcp_open_only" for item in result["candidates"]
    )

    combos = {combo["port"]: combo for combo in result["tested_combinations"]}
    assert combos[1883]["transport"] == "plaintext"
    assert combos[8883]["transport"] == "tls"
    assert combos[1883]["open_endpoints"] == 1
    assert combos[8883]["open_endpoints"] == 1
    assert combos[1883]["checked_hosts"] == 1


def test_pool_credentials_passed_transiently_only():
    from types import SimpleNamespace

    seen = {}

    def topic_discoverer(broker):
        # Records the last attempt (the credential attempt after anonymous).
        seen.update(
            {
                "username": broker.get("username"),
                "password": broker.get("password"),
                "tls": broker.get("tls"),
                "tls_mode": broker.get("tls_mode"),
            }
        )
        return []

    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=topic_discoverer,
        credential_lookup=lambda ref: SimpleNamespace(
            username="mqtt-user", password="mqtt-secret", label="HA"
        ),
        credential_refs_provider=lambda: ["ha"],
    )
    # A TLS endpoint (from scan/mDNS); credentials come from the shared pool, not
    # from any broker-specific connection entry.
    discovery.store.merge(
        {
            "host": "192.168.1.20",
            "port": 8883,
            "tls": True,
            "tls_mode": "insecure_no_verify",
            "source": "mdns",
        }
    )
    result = discovery.refresh()

    assert seen["username"] == "mqtt-user"
    assert seen["password"] == "mqtt-secret"
    assert seen["tls"] is True
    assert seen["tls_mode"] == "insecure_no_verify"

    broker = result["candidates"][0]
    assert "username" not in broker
    assert "password" not in broker
    assert broker["transport"] == "tls"


def test_failed_or_closed_port_does_not_create_candidate():
    discovery = MqttBrokerDiscovery(connector=lambda host, port, timeout: False)
    result = discovery.probe("192.168.178.10/32")
    assert result["found"] == 0
    assert result["candidates"] == []


def test_mdns_and_network_probe_same_endpoint_remain_one_broker():
    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: port == 1883
    )
    discovery.add_mdns_candidate(_mdns_candidate())
    discovery.probe("192.168.178.10/32")
    candidates = discovery.candidates()
    assert len(candidates) == 1
    assert candidates[0]["source"] == "mdns"
    assert set(candidates[0]["sources"]) == {"mdns", "network_probe"}


def _hardware_candidate(broker):
    return {
        "id": f"mqtt-device:{broker['id']}:zensdk_ha_scalar:EOD123",
        "broker_id": broker["id"],
        "broker_host": broker["host"],
        "broker_port": broker["port"],
        "topic_family": "zensdk_ha_scalar",
        "device_id": "EOD123",
        "serial_number": "EOD123",
        "metrics_seen": ["packInputPower"],
        "topics_seen": ["Zendure/sensor/EOD123/packInputPower"],
        "confidence": 0.75,
    }


def test_refresh_attaches_hardware_candidates_to_reachable_broker():
    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=lambda broker: [_hardware_candidate(broker)],
    )
    discovery.add_mdns_candidate(_mdns_candidate())
    result = discovery.refresh()

    assert result["devices_found"] == 1
    broker = result["candidates"][0]
    assert broker["devices"][0]["serial_number"] == "EOD123"
    assert broker["devices"][0]["broker_id"] == broker["id"]


def test_refresh_keeps_each_brokers_devices_separated():
    def topic_discoverer(broker):
        # Same serial/device on both brokers must stay attributed per broker.
        return [_hardware_candidate(broker)]

    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=topic_discoverer,
    )
    discovery.add_mdns_candidate(_mdns_candidate())
    discovery.store.merge(
        MqttBrokerCandidate(
            host="192.168.178.30", port=1883, source="network_probe"
        ).to_dict()
    )
    result = discovery.refresh()

    assert result["devices_found"] == 2
    by_id = {item["id"]: item for item in result["candidates"]}
    for broker in by_id.values():
        assert len(broker["devices"]) == 1
        assert broker["devices"][0]["broker_id"] == broker["id"]
    ids = {broker["devices"][0]["id"] for broker in by_id.values()}
    assert len(ids) == 2  # same serial, different broker -> distinct ids


def test_refresh_without_topic_discoverer_reports_no_devices():
    discovery = MqttBrokerDiscovery(connector=lambda host, port, timeout: True)
    discovery.add_mdns_candidate(_mdns_candidate())
    result = discovery.refresh()
    assert result["devices_found"] == 0
    assert result["candidates"][0]["devices"] == []
