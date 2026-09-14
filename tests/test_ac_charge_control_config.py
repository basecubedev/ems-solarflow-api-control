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


def test_the_feature_is_on_everywhere_by_default():
    """On by default like winter mode and full-charge assist.

    Four places carry this default and they must agree, or an installation
    behaves differently depending on whether its config.json spells the key out.
    """

    assert AC_CHARGE_CONTROL_DEFAULTS["enabled"] is True
    assert build_default_template()["ac_charge_control"]["enabled"] is True
    assert default_safe_config()["ac_charge_control"]["enabled"] is True
    assert cfg.ac_charge_control_enabled() is True


def test_devices_may_charge_and_discharge_by_default():
    device = build_default_template()["devices"][0]

    assert device["ac_discharge_enabled"] is True
    assert device["ac_charge_enabled"] is True
    # 0 means "ask the device for its own ceiling", not "no charging".
    assert device["max_charge_power_w"] == 0


def test_an_existing_config_gains_charging_on_upgrade():
    """This one does change behaviour, and the name says so.

    A config.json written before the feature existed has neither key. The
    upgrade fills both in as enabled, so after it the installation charges from
    surplus without a further step. That is the owner's decision; it is pinned
    here because it is exactly the kind of default that must never move by
    accident.
    """

    old = {
        "config_schema_version": 3,
        "system": {"enabled": True},
        "devices": [{"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "max_power": 800}],
    }

    upgraded = build_config_upgrade_plan(old)["upgraded_config"]

    assert upgraded["ac_charge_control"]["enabled"] is True
    device = upgraded["devices"][0]
    assert device["ac_discharge_enabled"] is True
    assert device["ac_charge_enabled"] is True
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


def test_the_per_device_config_key_reaches_the_device():
    """It did not, and the key looked live the whole time.

    `devices[].ac_charge_enabled` is documented, schema-validated and editable in
    the Admin console, but nothing passed it to the device object, so the
    controller's fallback decided and the config value was inert. Both transports
    carry it now, the same way they already carried `ac_discharge_enabled`.
    """

    from ems.clients import ZendureClient
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

    http_off = ZendureClient(
        "WR1", "192.0.2.10", "SN", None, 0, 0, 1, None, ac_charge_enabled=False
    )
    http_default = ZendureClient("WR2", "192.0.2.11", "SN", None, 0, 0, 1, None)

    assert http_off.ac_charge_enabled is False
    assert http_default.ac_charge_enabled is True
    assert http_default.ac_discharge_enabled is True

    import inspect

    signature = inspect.signature(ZendureMqttDeviceClient.__init__)
    assert signature.parameters["ac_charge_enabled"].default is True


def test_a_collapsed_charge_band_is_named_at_load():
    """Entry and exit at the same threshold defeats the anti-flutter design.

    The derivation prevents an *inverted* pair; it cannot prevent a *collapsed*
    one. Measured on the closed loop against a steady surplus: the shipped band
    gives one direction change in 200 cycles, a collapsed one gives 24 and pins
    the hourly cap. Relays move on each, which is the wear the whole asymmetric
    entry/exit design exists to prevent -- and the owner's founding requirement.
    """

    from ems.config import normalize_ac_charge_control_config

    def warnings_for(settings):
        collected = []
        normalize_ac_charge_control_config(settings, emit_warning=collected.append)
        return collected

    assert warnings_for({"enabled": True, "charge_start_w": 150, "charge_hysteresis_w": 50}) == []
    # A wide band is fine: the lower edge simply clamps at zero.
    assert warnings_for({"enabled": True, "charge_start_w": 150, "charge_hysteresis_w": 400}) == []
    # Off is off; the thresholds decide nothing.
    assert warnings_for({"enabled": False, "charge_hysteresis_w": 0}) == []

    assert len(warnings_for({"enabled": True, "charge_start_w": 150, "charge_hysteresis_w": 0})) == 1
    # The other way to collapse it, which a check that only looks at the
    # hysteresis misses.
    assert len(warnings_for({"enabled": True, "charge_start_w": 0})) == 1


def test_the_operator_numbers_survive_the_warning():
    """Warned about, never silently rewritten: clamping a value an operator set
    would make this a second authority for it."""

    from ems.config import normalize_ac_charge_control_config

    merged = normalize_ac_charge_control_config(
        {"enabled": True, "charge_start_w": 150, "charge_hysteresis_w": 0},
        emit_warning=lambda message: None,
    )

    assert merged["charge_start_w"] == 150
    assert merged["charge_hysteresis_w"] == 0


def test_a_runtime_charge_power_alone_never_starts_a_charge():
    """`ac_charge_power_w` and `max_charge_power_w` are one word apart and
    unrelated, so the dangerous reading of the near-miss is worth pinning.

    Setting the runtime charge power is meant to *prepare* a value for a device
    that is later switched into AC input mode. If it were enough on its own, an
    operator reaching for what they thought was a surplus-charging cap would
    have put the device on the grid instead.
    """

    from ems.controller import EMSController
    from tests.test_write_gates import RuntimeStateStub, device, state

    dev = device("WR1")
    item = state(soc=50, solar=0, output=0, soc_limit=0, pack_num=2)

    controller = EMSController.__new__(EMSController)
    controller.runtime_state = RuntimeStateStub(
        devices={"WR1": {"ac_charge_power_w": 600}}
    )
    intent = controller.get_device_runtime_intent(dev, item)

    assert intent.role.value == "ac_output"
    assert intent.desired_ac_mode == 2
    # No setpoint means the reconciler writes no inputLimit at all.
    assert intent.setpoint_w is None

    # It takes effect only once the role says the device is an AC input.
    controller.runtime_state = RuntimeStateStub(
        devices={"WR1": {"runtime_role": "ac_input", "ac_charge_power_w": 600}}
    )
    prepared = controller.get_device_runtime_intent(dev, item)

    assert prepared.role.value == "ac_input"
    assert prepared.setpoint_w == 600


@pytest.mark.parametrize(
    "label,override,expected",
    [
        # Negative thresholds clamp to zero, which the band check then names.
        ("negative start", {"charge_start_w": -500}, {"start_w": 0}),
        # Junk falls back rather than propagating into the control loop.
        ("text threshold", {"charge_start_w": "viel"}, {"start_w": 150}),
        ("null everywhere", {"charge_start_w": None, "entry_confirm_cycles": None}, {"start_w": 150, "entry_confirm_cycles": 5}),
        # A rate cap of zero would refuse every entry forever; one is the floor.
        ("zero rate cap", {"max_charge_entries_per_hour": 0}, {"max_entries_per_hour": 1}),
    ],
)
def test_hostile_settings_fall_back_instead_of_reaching_the_loop(label, override, expected):
    from ems.controller import EMSController

    merged = {**AC_CHARGE_CONTROL_DEFAULTS, **override, "enabled": True}
    previous = cfg.AC_CHARGE_CONTROL_CONFIG
    cfg.AC_CHARGE_CONTROL_CONFIG = merged
    try:
        controller = EMSController.__new__(EMSController)
        controller.runtime_state = None
        settings = controller.charge_settings()
    finally:
        cfg.AC_CHARGE_CONTROL_CONFIG = previous

    for field, value in expected.items():
        assert getattr(settings, field) == value, (label, field)


def test_a_negative_system_maximum_charges_nothing():
    """Fail-closed: a nonsense ceiling must not become an unbounded one."""

    from ems.ac_charge_control import ChargeDirectionState
    from ems.controller import EMSController

    merged = {
        **AC_CHARGE_CONTROL_DEFAULTS,
        "enabled": True,
        "max_total_charge_power_w": -1200,
    }
    previous = cfg.AC_CHARGE_CONTROL_CONFIG
    cfg.AC_CHARGE_CONTROL_CONFIG = merged
    try:
        controller = EMSController.__new__(EMSController)
        controller.runtime_state = None
        controller.charge_direction = ChargeDirectionState(charging=True)
        controller.charge_capacity_w = 2000
        controller.device_charge_limits = {"WR1": 1000}
        assert controller.commanded_total_floor_w() == 0
    finally:
        cfg.AC_CHARGE_CONTROL_CONFIG = previous
