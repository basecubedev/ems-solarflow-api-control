# SPDX-License-Identifier: AGPL-3.0-or-later
"""The config projection defaults a controllable new device to controlling.

The browser decides activation, but it must not be the only thing standing
between a control-ready inverter and a silently telemetry-only config entry. A
draft entry that states no output-control choice therefore resolves from
capability when the entry is new or its transport just changed, and stays exactly
as stored for an untouched existing device (a no-op apply must not rewrite it).
"""

import pytest

from admin.zendure_mqtt_config_draft import apply_zendure_mqtt_draft_fields

pytestmark = pytest.mark.simulation


def _draft_item(**overrides):
    item = {
        "name": "INV_2",
        "serial_number": "SN2",
        "hardware_model": "solarflow_800_pro_2",
        "product_key": "PK2",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV2",
            "product_key": "PK2",
        },
    }
    item.update(overrides)
    return item


def _stored_control_device():
    device = {}
    apply_zendure_mqtt_draft_fields(device, _draft_item(output_control=True))
    return device


def test_new_controllable_device_without_a_stated_choice_controls():
    device = {}

    apply_zendure_mqtt_draft_fields(device, _draft_item())

    assert device["capabilities"]["write_output_limit"] is True


def test_new_device_keeps_an_explicit_opt_out():
    device = {}

    apply_zendure_mqtt_draft_fields(device, _draft_item(output_control=False))

    assert device["capabilities"]["write_output_limit"] is False


def test_new_addressable_device_needs_a_write_target_to_default_on():
    # Capability alone is not an address: without a product key (or explicit
    # write topic) the canonical write topic cannot be built, so the implicit
    # default stays telemetry-only instead of writing an invalid control entry.
    device = {}

    apply_zendure_mqtt_draft_fields(
        device,
        _draft_item(
            product_key="",
            mqtt={
                "broker_ref": "local_a",
                "topic_family": "legacy_zendure_json",
                "device_id": "DEV2",
            },
        ),
    )

    assert device["capabilities"]["write_output_limit"] is False


def test_new_device_without_a_route_id_does_not_default_on():
    device = {}

    apply_zendure_mqtt_draft_fields(
        device,
        _draft_item(
            mqtt={
                "broker_ref": "local_a",
                "topic_family": "legacy_zendure_json",
                "product_key": "PK2",
            }
        ),
    )

    assert device["capabilities"]["write_output_limit"] is False


def test_an_explicit_request_survives_a_missing_write_target():
    # An operator's explicit choice is preserved so validation reports the
    # actionable error instead of the projection silently unticking the box.
    device = {}

    apply_zendure_mqtt_draft_fields(
        device,
        _draft_item(
            output_control=True,
            product_key="",
            mqtt={
                "broker_ref": "local_a",
                "topic_family": "legacy_zendure_json",
                "device_id": "DEV2",
            },
        ),
    )

    assert device["capabilities"]["write_output_limit"] is True


def test_new_uncontrollable_device_stays_telemetry_only():
    device = {}

    apply_zendure_mqtt_draft_fields(
        device,
        _draft_item(
            hardware_model="",
            mqtt={
                "broker_ref": "local_a",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "DEV2",
            },
        ),
    )

    assert device["capabilities"]["write_output_limit"] is False


def test_switched_transport_device_without_a_stated_choice_controls():
    # A transport switch reaches the projection with the stale MQTT block
    # stripped, exactly like a new device.
    device = {"name": "INV_2", "enabled": True}

    apply_zendure_mqtt_draft_fields(device, _draft_item())

    assert device["capabilities"]["write_output_limit"] is True


def test_untouched_existing_control_device_is_unchanged():
    device = _stored_control_device()

    apply_zendure_mqtt_draft_fields(device, _draft_item())

    assert device["capabilities"]["write_output_limit"] is True


def test_untouched_existing_telemetry_only_device_is_not_silently_enabled():
    device = {}
    apply_zendure_mqtt_draft_fields(device, _draft_item(output_control=False))

    apply_zendure_mqtt_draft_fields(device, _draft_item())

    assert device["capabilities"]["write_output_limit"] is False
