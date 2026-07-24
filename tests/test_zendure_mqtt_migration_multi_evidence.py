# SPDX-License-Identifier: AGPL-3.0-or-later
"""Legacy control migration resolves the model from ALL available signals.

The pre-flip config may carry more than one model hint (``product``, ``model``,
``mqtt.product``). Reading only the first can pin a writable model that another,
disagreeing signal contradicts. Migration therefore builds an evidence record
from every available signal and resolves them together: agreeing exacts pin the
model, but two exact signals for *different* models are a conflict that disables
control and never pins a writable profile.
"""

import pytest

from ems.zendure_mqtt.migration import (
    ACTION_DISABLE_CONTROL,
    ACTION_PIN_PROFILE,
    migrate_zendure_mqtt_control_configs,
    plan_zendure_mqtt_migration,
)

pytestmark = pytest.mark.simulation


def _control_device(**over):
    device = {
        "type": "zendure_mqtt",
        "name": "Legacy",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
        },
        "capabilities": {"write_output_limit": True},
    }
    device.update(over)
    return device


def _config(device):
    return {"devices": [device]}


def _plan_one(device):
    changes = plan_zendure_mqtt_migration(_config(device))
    assert len(changes) == 1
    return changes[0]


def test_matching_product_and_model_values_pin_profile():
    device = _control_device(product="Hyper 2000", model="Hyper 2000")
    change = _plan_one(device)
    assert change.action == ACTION_PIN_PROFILE
    assert change.hardware_profile == "hyper_2000"


def test_conflicting_migration_evidence_disables_control():
    # product and model disagree on the exact model: never pin a writable model.
    device = _control_device(product="Hyper 2000", model="AIO 2400")
    change = _plan_one(device)
    assert change.action == ACTION_DISABLE_CONTROL
    assert change.hardware_profile is None

    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert "hardware_profile" not in device
    assert any("conflict" in w["code"] or "unknown_model" in w["code"] for w in warnings)


def test_exact_plus_ambiguous_value_pins_exact():
    # A bare family word never overrides or conflicts with an exact model.
    device = _control_device(product="Hyper 2000", model="Hyper")
    change = _plan_one(device)
    assert change.action == ACTION_PIN_PROFILE
    assert change.hardware_profile == "hyper_2000"


def test_mqtt_product_evidence_participates_in_conflict():
    device = _control_device(product="Hyper 2000")
    device["mqtt"]["product"] = "AIO 2400"
    change = _plan_one(device)
    assert change.action == ACTION_DISABLE_CONTROL
    assert change.hardware_profile is None


def test_conflicting_migration_evidence_never_pins_writable_profile():
    device = _control_device(product="Hub 2000", model="Hyper 2000")
    _cfg, _warn = migrate_zendure_mqtt_control_configs(_config(device))
    assert "hardware_profile" not in device
    assert device["capabilities"]["write_output_limit"] is False


def test_unknown_values_disable_control():
    device = _control_device(product="Nonsense", model="Also Nonsense")
    change = _plan_one(device)
    assert change.action == ACTION_DISABLE_CONTROL


def test_telemetry_only_exact_model_disables_control():
    # ACE 1500 resolves exactly but is telemetry-only: control is disabled.
    device = _control_device(product="ACE 1500")
    change = _plan_one(device)
    assert change.action == ACTION_DISABLE_CONTROL
    assert device["capabilities"]["write_output_limit"] is True  # plan does not mutate
