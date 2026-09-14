# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config surface for AC charging from surplus.

The feature lets the EMS draw from the grid, so its default state and the shape
of its thresholds are safety properties, not preferences: off unless switched
on, off in simulation, and a charge band whose edges cannot be inverted.
"""

import pytest

from ems import config as cfg
from ems.config import (
    AC_CHARGE_CONTROL_DEFAULTS,
    build_config_upgrade_plan,
    default_safe_config,
)
from ems.config_catalog import build_default_template

pytestmark = [
    pytest.mark.config,
    pytest.mark.power_control,
    pytest.mark.contract,
]


class RuntimeStateStub:
    def __init__(self, data):
        self.data = data


def test_the_feature_is_off_everywhere_by_default():
    assert AC_CHARGE_CONTROL_DEFAULTS["enabled"] is False
    assert build_default_template()["ac_charge_control"]["enabled"] is False
    assert default_safe_config()["ac_charge_control"]["enabled"] is False
    assert cfg.ac_charge_control_enabled() is False


def test_devices_keep_discharging_and_do_not_start_charging_by_default():
    device = build_default_template()["devices"][0]

    # Today's behaviour made explicit; nothing changes for an existing install.
    assert device["ac_discharge_enabled"] is True
    assert device["ac_charge_enabled"] is False
    # 0 means "derive from the device output limit", not "no charging".
    assert device["max_charge_power_w"] == 0


def test_an_existing_config_gains_the_defaults_without_changing_behaviour():
    old = {
        "config_schema_version": 3,
        "system": {"enabled": True},
        "devices": [{"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "max_power": 800}],
    }

    upgraded = build_config_upgrade_plan(old)["upgraded_config"]

    assert upgraded["ac_charge_control"]["enabled"] is False
    device = upgraded["devices"][0]
    assert device["ac_discharge_enabled"] is True
    assert device["ac_charge_enabled"] is False
    # The operator's own values survive untouched.
    assert device["max_power"] == 800


def test_the_lower_band_edge_is_derived_and_can_never_invert(monkeypatch):
    """Configuring both edges would let an operator express start < stop.

    Configuring the start and the hysteresis cannot, and a hysteresis wider than
    the start floors at zero instead of going negative.
    """

    monkeypatch.setattr(
        cfg, "AC_CHARGE_CONTROL_CONFIG",
        {**AC_CHARGE_CONTROL_DEFAULTS, "charge_start_w": 150, "charge_hysteresis_w": 50},
    )
    assert cfg.ac_charge_stop_w() == 100

    monkeypatch.setattr(
        cfg, "AC_CHARGE_CONTROL_CONFIG",
        {**AC_CHARGE_CONTROL_DEFAULTS, "charge_start_w": 100, "charge_hysteresis_w": 400},
    )
    assert cfg.ac_charge_stop_w() == 0

    monkeypatch.setattr(
        cfg, "AC_CHARGE_CONTROL_CONFIG",
        {**AC_CHARGE_CONTROL_DEFAULTS, "charge_start_w": 0, "charge_hysteresis_w": 0},
    )
    assert cfg.ac_charge_stop_w() == 0


def test_runtime_state_can_switch_the_feature_off_without_a_restart(monkeypatch):
    monkeypatch.setattr(
        cfg, "AC_CHARGE_CONTROL_CONFIG", {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": True}
    )
    assert cfg.ac_charge_control_enabled() is True

    off = RuntimeStateStub({"ac_charge_control": {"enabled": False}})
    assert cfg.ac_charge_control_enabled(off) is False

    # An absent runtime section leaves config in charge.
    assert cfg.ac_charge_control_enabled(RuntimeStateStub({})) is True
