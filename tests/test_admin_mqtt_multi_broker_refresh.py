# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-broker validity of a partial multi-broker discovery refresh.

Drives the production ``MqttBrokerDiscovery.refresh()`` across generations: a
broker that becomes unreachable or whose topic discovery fails must never keep
offering its previous generation's devices as trusted proposals, even when
another broker in the same refresh succeeds.
"""

from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.zendure_mqtt_config_proposals import proposals_from_brokers


def _device(host, serial):
    return {
        "broker_host": host,
        "broker_port": 1883,
        "source_type": "local_mqtt",
        "topic_family": "zensdk_ha_scalar",
        "device_id": serial,
        "serial_number": serial,
        "metrics_seen": ["electricLevel"],
        "topics_seen": [f"Zendure/sensor/{serial}/electricLevel"],
    }


def _make_discovery(clock, *, reachable, devices_by_host, topic_fail_hosts=()):
    def connector(host, port, timeout_s):
        return host in reachable[0]

    def discoverer(broker):
        host = broker["host"]
        if host in topic_fail_hosts[0]:
            raise RuntimeError("topic discovery failed")
        devices = devices_by_host.get(host, [])
        status = "topics_seen" if devices else "mqtt_listened_no_topics"
        return {"status": status, "devices": list(devices)}

    store = MqttBrokerStore(clock=clock, proposal_ttl_seconds=900)
    discovery = MqttBrokerDiscovery(
        store=store, connector=connector, topic_discoverer=discoverer
    )
    discovery.set_configured_brokers(
        [
            {"host": "broker-a", "port": 1883},
            {"host": "broker-b", "port": 1883},
        ]
    )
    return discovery


def _serials(discovery):
    proposals = proposals_from_brokers(discovery.candidates())
    return {p["serial_number"] for p in proposals}


def test_two_brokers_both_succeed():
    now = [100.0]
    reachable = [{"broker-a", "broker-b"}]
    fail = [set()]
    discovery = _make_discovery(
        lambda: now[0],
        reachable=reachable,
        devices_by_host={"broker-a": [_device("broker-a", "SN-A")],
                         "broker-b": [_device("broker-b", "SN-B")]},
        topic_fail_hosts=fail,
    )
    discovery.refresh()
    assert _serials(discovery) == {"SN-A", "SN-B"}


def test_one_succeeds_one_unreachable_drops_stale_devices():
    now = [100.0]
    reachable = [{"broker-a", "broker-b"}]
    fail = [set()]
    discovery = _make_discovery(
        lambda: now[0],
        reachable=reachable,
        devices_by_host={"broker-a": [_device("broker-a", "SN-A")],
                         "broker-b": [_device("broker-b", "SN-B")]},
        topic_fail_hosts=fail,
    )
    discovery.refresh()
    assert _serials(discovery) == {"SN-A", "SN-B"}

    # Generation 2: broker-b drops out; broker-a still succeeds.
    reachable[0] = {"broker-a"}
    discovery.refresh()
    assert _serials(discovery) == {"SN-A"}


def test_one_succeeds_one_topic_discovery_fails():
    now = [100.0]
    reachable = [{"broker-a", "broker-b"}]
    fail = [set()]
    discovery = _make_discovery(
        lambda: now[0],
        reachable=reachable,
        devices_by_host={"broker-a": [_device("broker-a", "SN-A")],
                         "broker-b": [_device("broker-b", "SN-B")]},
        topic_fail_hosts=fail,
    )
    discovery.refresh()
    assert _serials(discovery) == {"SN-A", "SN-B"}

    # Generation 2: broker-b reachable at TCP but topic discovery raises.
    fail[0] = {"broker-b"}
    discovery.refresh()
    assert _serials(discovery) == {"SN-A"}


def test_all_fail_exposes_no_proposals():
    now = [100.0]
    reachable = [set()]
    fail = [set()]
    discovery = _make_discovery(
        lambda: now[0],
        reachable=reachable,
        devices_by_host={"broker-a": [_device("broker-a", "SN-A")],
                         "broker-b": [_device("broker-b", "SN-B")]},
        topic_fail_hosts=fail,
    )
    discovery.refresh()
    assert _serials(discovery) == set()


def test_failed_broker_later_recovers():
    now = [100.0]
    reachable = [{"broker-a"}]
    fail = [set()]
    discovery = _make_discovery(
        lambda: now[0],
        reachable=reachable,
        devices_by_host={"broker-a": [_device("broker-a", "SN-A")],
                         "broker-b": [_device("broker-b", "SN-B")]},
        topic_fail_hosts=fail,
    )
    discovery.refresh()
    assert _serials(discovery) == {"SN-A"}

    # broker-b recovers in a later generation.
    reachable[0] = {"broker-a", "broker-b"}
    discovery.refresh()
    assert _serials(discovery) == {"SN-A", "SN-B"}


def test_stale_device_never_becomes_trusted_proposal():
    now = [100.0]
    reachable = [{"broker-b"}]
    fail = [set()]
    discovery = _make_discovery(
        lambda: now[0],
        reachable=reachable,
        devices_by_host={"broker-b": [_device("broker-b", "SN-B")]},
        topic_fail_hosts=fail,
    )
    discovery.refresh()
    assert _serials(discovery) == {"SN-B"}

    # broker-b vanishes; its stale device must never resurface as a proposal
    # even though broker-a now succeeds (making the generation globally
    # successful).
    reachable[0] = {"broker-a"}
    discovery.refresh()
    assert "SN-B" not in _serials(discovery)
