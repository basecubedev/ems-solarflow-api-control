# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unified discovery aggregation: identity grouping + priority selection."""

import pytest

from admin.discovery_preparation import (
    SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_MQTT,
)
from admin.discovery_unify import build_unified_devices

pytestmark = pytest.mark.simulation

DEFAULT_PRIORITY = [SOURCE_LOCAL_API, SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_MQTT]


def _local_api_device(serial=None, ip="192.168.1.10", model="SolarFlow 800 Pro 2"):
    return {
        "id": f"zendure_local_http:{serial or ip}",
        "serial_number": serial,
        "model": model,
        "display_name": f"Zendure {model}" if model else "Zendure device",
        "ip": ip,
        "api_family": "zendure_local_http",
        "device_type": "zendure_solarflow_800_pro_2",
    }


def _mqtt_candidate(source_id, serial=None, device_id=None, model="SolarFlow 800 Pro 2"):
    return {
        "id": source_id,
        "serial_number": serial,
        "device_id": device_id,
        "model_hint": model,
        "display_name": model or "Zendure MQTT device",
    }


def _by_id(devices):
    return {d["id"]: d for d in devices}


def test_duplicate_serial_across_sources_selects_local_api_by_default():
    devices = build_unified_devices(
        {
            SOURCE_LOCAL_API: [_local_api_device(serial="ABC123")],
            SOURCE_ZENDURE_MQTT: [
                _mqtt_candidate("mqtt-device:zendure:ABC123", serial="ABC123")
            ],
        },
        DEFAULT_PRIORITY,
    )
    assert len(devices) == 1
    device = devices[0]
    # The grouping id carries Core's normalized serial match key, so it no
    # longer depends on which source happened to win. The displayed serial keeps
    # its original case.
    assert device["id"] == "serial:abc123"
    assert device["serial_number"] == "ABC123"
    assert device["selected_source"] == SOURCE_LOCAL_API
    assert set(device["sources"]) == {SOURCE_LOCAL_API, SOURCE_ZENDURE_MQTT}
    assert device["confidence"] == "high"
    assert device["selected_reason"] == "Selected by discovery priority"
    # Hardware facts come from the selected source so the setup card can show
    # the same IP / API family / type grid as the maintenance discovery card.
    assert device["ip"] == "192.168.1.10"
    assert device["api_family"] == "zendure_local_http"
    assert device["device_type"] == "zendure_solarflow_800_pro_2"


def test_selected_mqtt_source_does_not_leak_local_api_facts():
    # MQTT is selected by priority and carries no IP/api_family/type. Connection
    # facts classify a device by its transport, so they must come from the
    # winning source only and must NOT be filled from the lower-priority
    # local-API view (which would make the device read as an API device).
    devices = build_unified_devices(
        {
            SOURCE_ZENDURE_MQTT: [
                _mqtt_candidate("mqtt-device:zendure:ABC123", serial="ABC123")
            ],
            SOURCE_LOCAL_API: [_local_api_device(serial="ABC123")],
        },
        [SOURCE_ZENDURE_MQTT, SOURCE_LOCAL_API, SOURCE_LOCAL_MQTT],
    )
    assert len(devices) == 1
    device = devices[0]
    assert device["selected_source"] == SOURCE_ZENDURE_MQTT
    assert device["ip"] is None
    assert device["api_family"] is None
    assert device["device_type"] is None
    # It is still recorded as also seen via the local API.
    assert set(device["sources"]) == {SOURCE_ZENDURE_MQTT, SOURCE_LOCAL_API}


def test_mqtt_only_device_has_no_hardware_facts():
    devices = build_unified_devices(
        {
            SOURCE_LOCAL_MQTT: [
                _mqtt_candidate("mqtt-device:local:XYZ", serial="XYZ")
            ],
        },
        DEFAULT_PRIORITY,
    )
    assert len(devices) == 1
    device = devices[0]
    assert device["ip"] is None
    assert device["api_family"] is None
    assert device["device_type"] is None


def test_changed_priority_selects_zendure_first():
    devices = build_unified_devices(
        {
            SOURCE_LOCAL_API: [_local_api_device(serial="ABC123")],
            SOURCE_ZENDURE_MQTT: [
                _mqtt_candidate("mqtt-device:zendure:ABC123", serial="ABC123")
            ],
        },
        [SOURCE_ZENDURE_MQTT, SOURCE_LOCAL_MQTT, SOURCE_LOCAL_API],
    )
    assert len(devices) == 1
    assert devices[0]["selected_source"] == SOURCE_ZENDURE_MQTT
    # The source list is ordered by the configured priority.
    assert devices[0]["sources"] == [SOURCE_ZENDURE_MQTT, SOURCE_LOCAL_API]


def test_same_serial_on_multiple_brokers_stays_one_device_but_keeps_candidates():
    devices = build_unified_devices(
        {
            SOURCE_LOCAL_MQTT: [
                _mqtt_candidate("mqtt-device:local_mqtt:brokerA:ABC123", serial="ABC123"),
                _mqtt_candidate("mqtt-device:local_mqtt:brokerB:ABC123", serial="ABC123"),
            ]
        },
        DEFAULT_PRIORITY,
    )
    assert len(devices) == 1
    device = devices[0]
    assert device["selected_source"] == SOURCE_LOCAL_MQTT
    # Both broker-specific candidates are preserved inside the unified device.
    assert len(device["candidates"]) == 2
    assert device["sources"] == [SOURCE_LOCAL_MQTT]


def test_weak_identity_without_serial_is_never_merged():
    devices = build_unified_devices(
        {
            SOURCE_LOCAL_MQTT: [
                _mqtt_candidate("mqtt-device:local_mqtt:brokerA:dk1", device_id="dk1"),
            ],
            SOURCE_ZENDURE_MQTT: [
                _mqtt_candidate("mqtt-device:zendure:dk2", device_id="dk2"),
            ],
        },
        DEFAULT_PRIORITY,
    )
    assert len(devices) == 2
    for device in devices:
        assert device["serial_number"] is None
        assert device["confidence"] == "low"
        assert len(device["sources"]) == 1


def test_single_source_device_reports_only_source_reason():
    devices = build_unified_devices(
        {SOURCE_LOCAL_API: [_local_api_device(serial="ONLY1")]},
        DEFAULT_PRIORITY,
    )
    assert devices[0]["selected_reason"] == "Only source"
    assert devices[0]["selected_source"] == SOURCE_LOCAL_API


def test_serial_match_is_case_insensitive_and_high_confidence_sorts_first():
    devices = build_unified_devices(
        {
            SOURCE_LOCAL_API: [
                _local_api_device(serial="abc123"),
                _local_api_device(serial=None, ip="192.168.1.50", model="Smart Meter 3CT"),
            ],
            SOURCE_ZENDURE_MQTT: [
                _mqtt_candidate("mqtt-device:zendure:ABC123", serial="ABC123")
            ],
        },
        DEFAULT_PRIORITY,
    )
    by_id = _by_id(devices)
    # Case-insensitive serial match folds the two ABC123 rows into one device.
    assert "serial:abc123" in by_id
    assert len(by_id["serial:abc123"]["sources"]) == 2
    # High-confidence (serial) device sorts before the low-confidence meter row.
    assert devices[0]["confidence"] == "high"


def test_disabled_source_absent_from_input_is_not_aggregated():
    # The server only passes enabled sources; a device found solely via a
    # disabled source therefore never appears in the unified list.
    devices = build_unified_devices(
        {SOURCE_LOCAL_API: [_local_api_device(serial="ABC123")]},
        DEFAULT_PRIORITY,
    )
    assert [d["selected_source"] for d in devices] == [SOURCE_LOCAL_API]
