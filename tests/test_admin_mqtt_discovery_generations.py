# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from admin.mqtt_discovery import MqttBrokerStore
from admin.zendure_mqtt_config_proposals import proposals_from_brokers

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
]


def _broker(serial="SERIAL"):
    return {
        "id": "mqtt:broker.local:1883",
        "host": "broker.local",
        "port": 1883,
        "devices": [
            {
                "broker_host": "broker.local",
                "broker_port": 1883,
                "source_type": "local_mqtt",
                "topic_family": "zensdk_ha_scalar",
                "device_id": serial,
                "serial_number": serial,
                "metrics_seen": ["electricLevel"],
                "topics_seen": [f"Zendure/sensor/{serial}/electricLevel"],
            }
        ],
    }


def test_successive_generations_invalidate_old_proposal_ids():
    now = [100.0]
    store = MqttBrokerStore(clock=lambda: now[0])
    first = store.begin_refresh()
    store.complete_refresh(first, [_broker()], success=True)
    old = proposals_from_brokers(store.to_list())

    second = store.begin_refresh()
    assert proposals_from_brokers(store.to_list()) == []
    store.complete_refresh(second, [_broker()], success=True)
    current = proposals_from_brokers(store.to_list())

    assert old[0]["id"] != current[0]["id"]
    assert current[0]["discovery_generation"] == second


def test_failed_refresh_and_expiry_expose_no_selectable_devices():
    now = [100.0]
    store = MqttBrokerStore(clock=lambda: now[0], proposal_ttl_seconds=10)
    generation = store.begin_refresh()
    store.complete_refresh(generation, [_broker()], success=True)
    assert proposals_from_brokers(store.to_list())

    now[0] = 111.0
    assert proposals_from_brokers(store.to_list()) == []

    failed = store.begin_refresh()
    store.complete_refresh(failed, [_broker()], success=False)
    assert proposals_from_brokers(store.to_list()) == []
