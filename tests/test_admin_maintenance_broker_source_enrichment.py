# SPDX-License-Identifier: AGPL-3.0-or-later
"""The maintenance draft resolves a device's transport source server-side.

A valid config may omit ``devices[].mqtt.source`` — the broker profile is the
authority there. The browser must not have to guess it from whichever discovery
proposals happen to exist, so the draft carries a resolved ``effective_source``
next to the stored one. It is display-only: ``mqtt.source`` keeps whatever the
config states (including nothing), so applying an untouched draft stays a no-op.
"""

import pytest

from admin.maintenance_config import build_maintenance_draft

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def _mqtt_device(name, broker_ref, **mqtt):
    device = {
        "type": "zendure_mqtt",
        "name": name,
        "serial_number": "PHYS-" + name,
        "mqtt": {"broker_ref": broker_ref, "device_id": "ROUTE-" + name, **mqtt},
    }
    return device


def _config(brokers, devices):
    return {"zendure_mqtt": {"brokers": brokers}, "devices": devices}


def _device_draft(config, index=0):
    return build_maintenance_draft(config)["devices"][index]


def test_cloud_broker_profile_resolves_a_device_without_a_stated_source():
    config = _config(
        {
            "cloud_a": {
                "source": "zendure_cloud_mqtt",
                "host": "mqtt.zen-iot.com",
                "port": 8883,
                "username": "u",
                "password": "p",
            }
        },
        [_mqtt_device("INV_1", "cloud_a")],
    )
    draft = _device_draft(config)
    assert draft["mqtt"]["effective_source"] == "zendure_cloud_mqtt"
    # The stored value is untouched: the broker profile stays authoritative.
    assert draft["mqtt"]["source"] == ""


def test_local_broker_profile_resolves_a_device_without_a_stated_source():
    config = _config(
        {"local_b1": {"source": "local_mqtt", "host": "192.168.1.5", "port": 1883}},
        [_mqtt_device("INV_1", "local_b1")],
    )
    assert _device_draft(config)["mqtt"]["effective_source"] == "local_mqtt"


def test_stated_device_source_is_reported_unchanged():
    config = _config(
        {"local_b1": {"source": "local_mqtt", "host": "192.168.1.5", "port": 1883}},
        [_mqtt_device("INV_1", "local_b1", source="local_mqtt")],
    )
    draft = _device_draft(config)
    assert draft["mqtt"]["source"] == "local_mqtt"
    assert draft["mqtt"]["effective_source"] == "local_mqtt"


def test_unresolvable_source_stays_empty():
    config = _config(
        {"local_b1": {"host": "192.168.1.5", "port": 1883}},
        [_mqtt_device("INV_1", "unknown_ref")],
    )
    assert _device_draft(config)["mqtt"]["effective_source"] == ""


def test_broker_profile_without_a_source_resolves_to_nothing():
    config = _config(
        {"local_b1": {"host": "192.168.1.5", "port": 1883}},
        [_mqtt_device("INV_1", "local_b1")],
    )
    assert _device_draft(config)["mqtt"]["effective_source"] == ""


def test_legacy_top_level_broker_resolves_the_implicit_default_ref():
    config = {
        "zendure_mqtt": {"host": "192.168.1.5", "port": 1883, "source": "local_mqtt"},
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "INV_1",
                "serial_number": "PHYS-1",
                "mqtt": {"device_id": "ROUTE-1"},
            }
        ],
    }
    assert _device_draft(config)["mqtt"]["effective_source"] == "local_mqtt"


def test_two_devices_on_different_brokers_keep_their_own_sources():
    config = _config(
        {
            "cloud_a": {
                "source": "zendure_cloud_mqtt",
                "host": "mqtt.zen-iot.com",
                "port": 8883,
                "username": "u",
                "password": "p",
            },
            "local_b1": {"source": "local_mqtt", "host": "192.168.1.5", "port": 1883},
        },
        [_mqtt_device("INV_1", "cloud_a"), _mqtt_device("INV_2", "local_b1")],
    )
    drafts = build_maintenance_draft(config)["devices"]
    assert [d["mqtt"]["effective_source"] for d in drafts] == [
        "zendure_cloud_mqtt",
        "local_mqtt",
    ]


def test_local_api_device_draft_is_unchanged():
    config = _config({}, [{"name": "INV_1", "ip": "192.168.1.100", "sn": "PHYS-1"}])
    draft = _device_draft(config)
    assert draft["kind"] == "local_api"
    assert "mqtt" not in draft
