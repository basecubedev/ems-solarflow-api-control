# SPDX-License-Identifier: AGPL-3.0-or-later
"""MQTT broker discovery tests without real multicast or network probes."""

import pytest

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
