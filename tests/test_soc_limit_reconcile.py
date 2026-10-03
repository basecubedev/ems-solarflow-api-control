# SPDX-License-Identifier: AGPL-3.0-or-later
"""SoC-limit reconciliation writes only the bounds the operator manages.

``min_soc``/``max_soc`` of ``0`` mean "leave this value to the device". A
payload that always carried both properties wrote ``minSoc=0`` or ``socSet=0``
to the inverter whenever only one bound was managed.
"""

import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.controller import EMSController

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]


def _controller():
    return EMSController(devices=[], shelly=None, sleep_enabled=False)


def _device(min_soc, max_soc):
    return SimpleNamespace(name="WR1", min_soc=min_soc, max_soc=max_soc)


def _state(min_soc=15, max_soc=95, soc=50):
    return SimpleNamespace(min_soc=min_soc, max_soc=max_soc, soc=soc, pack_num=1)


@contextlib.contextmanager
def _captured_writes():
    writes = []

    def capture(device, payload, **kwargs):
        writes.append(dict(payload))
        return True

    with patch("ems.controller.write_device_properties", capture), patch.object(
        cfg, "state_reconciliation_writes_allowed", lambda gate: True
    ):
        yield writes


def _reconcile(dev, state):
    with _captured_writes() as writes:
        result = _controller().apply_soc_limits(dev, state)
    return result, writes


@pytest.mark.parametrize(
    "min_soc, max_soc, expected",
    [
        (20, 90, {"minSoc": 200, "socSet": 900}),
        (0, 90, {"socSet": 900}),
        (20, 0, {"minSoc": 200}),
    ],
)
def test_only_managed_bounds_are_written(min_soc, max_soc, expected):
    result, writes = _reconcile(_device(min_soc, max_soc), _state())
    assert result is True
    assert writes == [expected]


def test_both_bounds_unmanaged_writes_nothing():
    assert _reconcile(_device(0, 0), _state()) == (True, [])


@pytest.mark.parametrize("min_soc, max_soc", [(0, 95), (15, 0)])
def test_an_unmanaged_bound_never_counts_as_a_difference(min_soc, max_soc):
    result, writes = _reconcile(_device(min_soc, max_soc), _state(15, 95))
    assert result is True
    assert writes == []


class _AssistStore:
    def __init__(self, active):
        self.active = active

    def get_device_state(self, device, now=None):
        return {"full_charge_assist_active": self.active}


@pytest.mark.parametrize("active, socset", [(True, 1000), (False, 900)])
def test_periodic_reconcile_leaves_an_active_full_charge_assist_at_100(
    active, socset
):
    controller = EMSController(
        devices=[],
        shelly=None,
        sleep_enabled=False,
        battery_full_charge_store=_AssistStore(active),
    )
    dev = _device(15, 90)

    with _captured_writes() as writes, patch.object(
        controller, "apply_device_modes", lambda dev, state: None
    ):
        controller.reconcile_device_state_limits(
            dev, _state(15, 50), winter_active=False, winter_adjust_today=False
        )

    assert writes == [{"minSoc": 150, "socSet": socset}]


@pytest.mark.parametrize(
    "device_min_soc, expected", [(30, 30), (10, 15), (70, 40)]
)
def test_a_restart_in_winter_keeps_the_ramped_min_soc(device_min_soc, expected):
    """No in-memory ramp target after a restart; the device's value is adopted."""

    controller = _controller()
    dev = _device(15, 95)
    with patch.object(cfg, "winter_feature_enabled", lambda runtime: True):
        target, adjusted = controller.winter_reconciliation_target(
            dev,
            _state(device_min_soc, 95),
            winter_active=True,
            adjust_today=False,
        )

    assert (target, adjusted) == (expected, False)
    assert controller.winter_min_soc_targets["WR1"] == expected


def _winter_target(controller, state, adjust_today, dev=None, *, enabled=True, winter_active=True):
    with patch.object(cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)), patch.object(
        cfg, "winter_feature_enabled", lambda runtime: enabled
    ):
        return controller.winter_reconciliation_target(
            dev or _device(15, 95), state, winter_active=winter_active, adjust_today=adjust_today
        )


def _min_soc_writes(controller, dev, state, *, winter_active=True, adjust_today=False):
    with _captured_writes() as writes, patch.object(
        cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)
    ), patch.object(cfg, "winter_feature_enabled", lambda runtime: True), patch.object(
        controller, "apply_device_modes", lambda dev, state: None
    ):
        controller.reconcile_device_state_limits(
            dev, state, winter_active=winter_active, winter_adjust_today=adjust_today
        )

    return [payload["minSoc"] for payload in writes if "minSoc" in payload]


def test_the_daily_adjustment_writes_no_min_soc_above_the_battery():
    """A raise five points above the SoC made the firmware charge from the grid.

    The inverter ignores the 200 W winter inputLimit while it is in output mode.
    On 2026-10-03 the adjustment raised 25 to 30 at a SoC of 25, and each of two
    devices drew 1.2 to 1.4 kW from the grid until it had reached it.
    """

    assert _min_soc_writes(
        _controller(), _device(15, 0), _state(25, 0, soc=25), adjust_today=True
    ) == []


def _waits(caplog):
    return [
        record.levelno
        for record in caplog.records
        if "event=winter_raise_waits_for_battery" in record.getMessage()
    ]


def test_the_raise_follows_once_the_battery_holds_it(caplog):
    controller = _controller()
    caplog.set_level(logging.DEBUG)

    assert _winter_target(controller, _state(25, soc=25), adjust_today=True) == (25, True)
    assert _winter_target(controller, _state(25, soc=29), adjust_today=False) == (25, False)
    assert _winter_target(controller, _state(25, soc=30), adjust_today=False) == (30, False)
    assert _waits(caplog) == [logging.INFO, logging.DEBUG]


def test_a_raise_that_starts_waiting_outside_the_adjustment_says_so_once(caplog):
    """Switched on again, a remembered target waits without a daily adjustment to announce it."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _state(30, soc=36), adjust_today=True)
    _winter_target(controller, _state(36, soc=36), adjust_today=False, enabled=False)
    for _ in range(2):
        _winter_target(controller, _state(15, soc=18), adjust_today=False)

    assert _waits(caplog) == [logging.INFO, logging.DEBUG]


def test_a_raise_still_waiting_when_winter_comes_back_says_so_again(caplog):
    controller = _controller()
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _state(25, soc=25), adjust_today=True)
    _winter_target(controller, _state(25, soc=25), adjust_today=False, enabled=False)
    _winter_target(controller, _state(25, soc=25), adjust_today=False)

    assert _waits(caplog) == [logging.INFO, logging.INFO]


def test_a_raise_that_waits_again_after_its_write_says_so_again(caplog):
    """The battery held the target and the write went out but did not land; it waits anew."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _state(25, soc=25), adjust_today=True)
    assert _winter_target(controller, _state(25, soc=30), adjust_today=False) == (30, False)
    _winter_target(controller, _state(25, soc=28), adjust_today=False)

    assert _waits(caplog) == [logging.INFO, logging.INFO]


def test_a_battery_gone_for_a_report_does_not_announce_the_raise_again(caplog):
    """A transient ``packNum: 0`` leaves the ramp alone, and so its waiting raise."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _state(25, soc=25), adjust_today=True)
    absent = SimpleNamespace(min_soc=25, max_soc=95, soc=25, pack_num=0)
    assert _winter_target(controller, absent, adjust_today=False) == (None, False)
    _winter_target(controller, _state(25, soc=25), adjust_today=False)

    assert _waits(caplog) == [logging.INFO, logging.DEBUG]


def test_a_new_target_that_waits_is_announced_although_another_waited(caplog):
    controller = _controller()
    dev = _device(5, 95)
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _state(5, soc=7), adjust_today=True, dev=dev)
    _winter_target(controller, _state(5, soc=7), adjust_today=False, dev=dev, winter_active=False)

    assert _waits(caplog) == [logging.INFO, logging.INFO]


def test_a_device_without_state_writes_logs_no_waiting_raise(caplog):
    """MQTT control devices are output-only; nothing is waiting to be written there."""

    dev = SimpleNamespace(name="WR1", min_soc=15, max_soc=95, supports_state_reconciliation=False)
    caplog.set_level(logging.DEBUG)

    _winter_target(_controller(), _state(25, soc=25), adjust_today=True, dev=dev)

    assert _waits(caplog) == []


def test_a_target_remembered_while_winter_was_off_is_not_written_above_the_battery():
    """Switching winter mode off writes the configured minimum and keeps the target.

    The battery discharges meanwhile; switched on again, the remembered target
    must not come back above it.
    """

    controller = _controller()

    assert _winter_target(controller, _state(30, soc=36), adjust_today=True) == (36, True)
    assert _winter_target(controller, _state(36, soc=36), adjust_today=False, enabled=False) == (None, False)
    assert controller.winter_min_soc_targets["WR1"] == 36
    assert _winter_target(controller, _state(15, soc=18), adjust_today=False) == (15, False)


def test_a_target_whose_write_failed_is_not_written_once_the_battery_fell_below_it():
    controller = _controller()

    assert _winter_target(controller, _state(25, soc=33), adjust_today=True) == (33, True)
    assert _winter_target(controller, _state(25, soc=28), adjust_today=False) == (25, False)


@pytest.mark.parametrize(
    "remembered_at, adjust_today, soc",
    [(None, False, 5), ((27, 32), False, 25), ((27, 32), True, 22)],
    ids=["configured-floor", "remembered-target", "adjustment"],
)
def test_a_report_without_min_soc_raises_nothing_the_battery_does_not_hold(remembered_at, adjust_today, soc):
    """``parse_device`` reads a missing ``minSoc`` as 0.

    Without the battery rule the remembered target or the configured floor was
    written whatever the battery held: a minSoc of 15 % at a SoC of 5 %, 32 % at
    25 %, 37 % at 22 %. After a restart the configured floor is adopted, once
    the battery holds it; adopting the device's own minSoc needs
    ``parse_device`` to tell a missing minSoc from 0 %.
    """

    controller = _controller()
    dev = _device(15, 0)
    if remembered_at:
        min_soc, start_soc = remembered_at
        assert _min_soc_writes(controller, dev, _state(min_soc, 0, soc=start_soc), adjust_today=True) == [320]

    assert _min_soc_writes(controller, dev, _state(0, 0, soc=soc), adjust_today=adjust_today) == []


def test_a_report_without_min_soc_keeps_the_remembered_target_the_battery_holds():
    controller = _controller()
    dev = _device(15, 0)
    assert _min_soc_writes(controller, dev, _state(27, 0, soc=32), adjust_today=True) == [320]

    assert _min_soc_writes(controller, dev, _state(0, 0, soc=35)) == [320]


def test_the_summer_reset_waits_for_the_battery_too(caplog):
    """A device left below the summer reserve is not raised above its SoC.

    The change out of winter is logged at info, the repeats while the raise
    waits at debug.
    """

    controller = _controller()
    dev = _device(15, 0)
    caplog.set_level(logging.DEBUG)
    _min_soc_writes(controller, dev, _state(10, 0, soc=12), adjust_today=True)

    for _ in range(3):
        assert _min_soc_writes(controller, dev, _state(10, 0, soc=12), winter_active=False) == []
    resets = [
        record.levelno for record in caplog.records if "event=winter_summer_reset" in record.getMessage()
    ]
    assert resets == [logging.INFO, logging.DEBUG, logging.DEBUG]


@pytest.mark.parametrize("soc", [0, float("nan")])
def test_without_a_soc_reading_the_adjustment_raises_nothing(soc):
    """A report without ``electricLevel`` parses as SoC 0; it is no licence to raise."""

    controller = _controller()

    assert _winter_target(controller, _state(35, soc=soc), adjust_today=True) == (35, True)


@pytest.mark.parametrize("soc", [None, "n/a", float("nan"), True])
def test_a_remembered_raise_without_a_soc_reading_waits(soc):
    """Anything that is not a number is no reading; it neither raises nor fails."""

    controller = _controller()
    _winter_target(controller, _state(25, soc=25), adjust_today=True)

    assert _winter_target(controller, _state(25, soc=soc), adjust_today=False) == (25, False)


@pytest.mark.parametrize("configured_min_soc", [15, 20])
def test_a_min_soc_above_the_battery_is_held(configured_min_soc):
    """The adjustment plans a raise. Lowering to the SoC instead would let one
    wrong SoC report empty the reserve, and below the configured minimum."""

    controller = _controller()

    target, _ = _winter_target(
        controller, _state(25, soc=17), adjust_today=True, dev=_device(configured_min_soc, 95)
    )

    assert target == 25


def test_the_adjustment_steps_up_from_the_remembered_target_below_the_device():
    controller = _controller()
    controller.winter_min_soc_targets["WR1"] = 30

    assert _winter_target(controller, _state(35, soc=38), adjust_today=True) == (38, True)


def test_an_adjustment_without_a_reported_min_soc_steps_up_from_the_floor():
    """Stepping up from the 0 read for a missing value wrote a minSoc below the floor."""

    controller = _controller()

    assert _winter_target(controller, _state(0, soc=10), adjust_today=True) == (0, True)
    assert controller.winter_min_soc_targets["WR1"] == 20


def test_a_summer_reset_the_battery_holds_is_logged_each_time(caplog):
    controller = _controller()
    caplog.set_level(logging.DEBUG)

    for _ in range(2):
        assert _winter_target(controller, _state(10, soc=50), adjust_today=False, winter_active=False) == (15, False)
    resets = [
        record.levelno for record in caplog.records if "event=winter_summer_reset" in record.getMessage()
    ]

    assert resets == [logging.INFO, logging.INFO]


@pytest.mark.parametrize("soc", [True, float("inf"), float("-inf"), float("nan"), 10**400])
def test_only_a_finite_number_is_a_soc_reading(soc):
    assert cfg.winter_min_soc_the_battery_holds(1, 0, soc) == 0


def test_a_summer_reset_that_does_not_wait_is_logged_each_time(caplog):
    """Its write has not landed; a reset that goes on failing stays in sight."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)

    for _ in range(3):
        _winter_target(controller, _state(25, soc=30), adjust_today=False, winter_active=False)
    resets = [
        record.levelno for record in caplog.records if "event=winter_summer_reset" in record.getMessage()
    ]

    assert resets == [logging.INFO, logging.INFO, logging.INFO]


def test_the_configured_floor_is_not_raised_above_the_battery():
    controller = _controller()

    assert _winter_target(controller, _state(12, soc=10), adjust_today=False) == (12, False)


def test_the_next_day_ramps_from_the_min_soc_the_battery_holds(caplog):
    """An unreached target is not stepped up again; the ramp starts from the device."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _state(25, soc=25), adjust_today=True)
    assert _winter_target(controller, _state(25, soc=27), adjust_today=True) == (25, True)
    assert controller.winter_min_soc_targets["WR1"] == 30
    assert _waits(caplog) == [logging.INFO, logging.INFO]


@pytest.mark.parametrize(
    "min_soc, max_soc, expected",
    [
        (150, 90, {"socSet": 900}),
        (20, 250, {"minSoc": 200}),
        (-5, 90, {"socSet": 900}),
        (float("nan"), 90, {"socSet": 900}),
        (float("inf"), 90, {"socSet": 900}),
        ("abc", 90, {"socSet": 900}),
    ],
)
def test_an_out_of_range_soc_bound_is_never_written(min_soc, max_soc, expected):
    result, writes = _reconcile(_device(min_soc, max_soc), _state())
    assert result is True
    assert writes == [expected]


@pytest.mark.parametrize("value", ["inf", float("inf"), "-inf", "nan", float("nan")])
def test_safe_number_parsers_never_return_or_raise_on_non_finite_input(value):
    assert cfg.safe_int(value, 7) == 7
    assert cfg.safe_float(value, 7.0) == 7.0


@pytest.mark.parametrize(
    "reported, expected",
    [(25, 30), (0, 35)],
    ids=["device-below-the-remembered-target", "device-reports-no-min-soc"],
)
def test_home_assistant_shows_the_target_the_adjustment_would_set(reported, expected):
    """The sensor and the controller step up from the same base.

    A target the battery never reached is not stepped up again; a device that
    reports no minSoc keeps the remembered target.
    """

    class _Ha:
        def __init__(self):
            self.states = {}

        def set_state(self, entity, value, **kwargs):
            self.states[entity] = value

    ha = _Ha()
    dev = SimpleNamespace(name="WR1", min_soc=15, max_soc=95, enabled=True)
    controller = EMSController(devices=[dev], shelly=None, ha=ha, sleep_enabled=False)
    controller.winter_min_soc_targets["WR1"] = 30

    with patch.object(cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)), patch.object(
        cfg, "winter_feature_enabled", lambda runtime: True
    ), patch.object(cfg, "winter_month_active", lambda now: True), patch.object(
        controller, "device_ha_extra", lambda dev, extra=None: extra
    ):
        controller.publish_winter_to_ha([_state(reported, soc=27)])

    assert ha.states["sensor.ems_solarflow_wr1_winter_min_soc_target"] == expected
