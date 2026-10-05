# SPDX-License-Identifier: AGPL-3.0-or-later
"""SoC-limit reconciliation writes only the bounds the operator manages.

``min_soc``/``max_soc`` of ``0`` mean "leave this value to the device". A
payload that always carried both properties wrote ``minSoc=0`` or ``socSet=0``
to the inverter whenever only one bound was managed.
"""

import contextlib
import logging
from datetime import datetime
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
            dev, _state(15, 50), winter_active=False, now=datetime(2026, 7, 1, 9, 0)
        )

    assert writes == [{"minSoc": 150, "socSet": socset}]


DAY = datetime(2026, 11, 2, 9, 0)
NEXT_DAY = datetime(2026, 11, 3, 9, 0)


def _pv_state(min_soc=20, soc=20, pv=0, max_soc=95, pack_num=1):
    return SimpleNamespace(
        min_soc=min_soc,
        max_soc=max_soc,
        soc=soc,
        pack_num=pack_num,
        solar=pv,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
    )


@contextlib.contextmanager
def _devices_configured(entries=None):
    with patch.object(cfg, "ZENDURE_CONFIG", list(entries or [])):
        yield


def _winter_target(
    controller, state, today=DAY, dev=None, *, enabled=True, winter_active=True, config=None, entry=None
):
    dev = dev or _device(15, 95)
    with patch.object(cfg, "WINTER_CONFIG", {**cfg.WINTER_DEFAULTS, **(config or {})}), patch.object(
        cfg, "winter_feature_enabled", lambda runtime: enabled
    ), _devices_configured([{"name": dev.name, **(entry or {})}]):
        return controller.winter.reconciliation_target(
            dev, state, winter_active=winter_active, now=today
        )


def _min_soc_writes(controller, dev, state, *, winter_active=True, today=DAY, entry=None):
    with _captured_writes() as writes, patch.object(
        cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)
    ), patch.object(cfg, "winter_feature_enabled", lambda runtime: True), patch.object(
        controller, "apply_device_modes", lambda dev, state: None
    ), _devices_configured([{"name": dev.name, **(entry or {})}]):
        controller.reconcile_device_state_limits(dev, state, winter_active=winter_active, now=today)

    return [payload["minSoc"] for payload in writes if "minSoc" in payload]


def _remember(controller, target, step_date="2026-11-01"):
    """A target this winter's ramp reached, the day before the test's day."""

    item = controller.winter.device("WR1")
    item.target = target
    item.step_date = step_date


def _events(caplog, event):
    return [record.levelno for record in caplog.records if f"event={event}" in record.getMessage()]


def _morning(controller, min_soc=20, soc=20, **kwargs):
    """A dark reading arms the step; the first PV reading takes it."""

    _winter_target(controller, _pv_state(min_soc, soc, pv=0), **kwargs)
    return _winter_target(controller, _pv_state(min_soc, soc, pv=60), **kwargs)


@pytest.mark.parametrize(
    "device_min_soc, expected", [(30, 30), (10, 15), (70, 40)]
)
def test_a_restart_in_winter_keeps_the_raised_min_soc(device_min_soc, expected):
    """Nothing is remembered after a restart; the device's value is adopted."""

    controller = _controller()

    assert _winter_target(controller, _pv_state(device_min_soc, soc=50)) == (expected, False)
    assert controller.winter.device("WR1").target == expected


def test_the_morning_step_raises_min_soc_three_points_above_the_soc():
    controller = _controller()

    assert _winter_target(controller, _pv_state(20, soc=21, pv=0)) == (20, False)
    assert _winter_target(controller, _pv_state(20, soc=21, pv=60)) == (24, True)


def test_the_morning_step_writes_the_raise_and_the_winter_input_limit():
    controller = _controller()
    dev = _device(15, 0)
    _min_soc_writes(controller, dev, _pv_state(20, soc=20, pv=0, max_soc=0))

    with _captured_writes() as writes, patch.object(
        cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)
    ), patch.object(cfg, "winter_feature_enabled", lambda runtime: True), patch.object(
        controller, "apply_device_modes", lambda dev, state: None
    ), _devices_configured():
        controller.reconcile_device_state_limits(
            dev, _pv_state(20, soc=20, pv=60, max_soc=0), winter_active=True, now=DAY
        )

    assert writes == [{"minSoc": 230}, {"inputLimit": 200}]


def test_the_step_waits_while_the_battery_is_well_below_its_min_soc():
    """Two points below, the export hold refills it first; the step follows."""

    controller = _controller()

    assert _morning(controller, min_soc=20, soc=17) == (20, False)
    assert _winter_target(controller, _pv_state(20, soc=18, pv=60)) == (20, False)
    assert _winter_target(controller, _pv_state(20, soc=19, pv=80)) == (22, True)


def test_one_point_below_its_min_soc_the_step_is_still_taken():
    """Too close for the hold, waiting there would stall the ramp for days."""

    controller = _controller()

    assert _morning(controller, min_soc=24, soc=23) == (26, True)


def test_there_is_one_step_a_day():
    """PV that drops to nothing under cloud at noon does not arm a second step."""

    controller = _controller()
    _morning(controller)

    assert _winter_target(controller, _pv_state(23, soc=23, pv=0)) == (23, False)
    assert _winter_target(controller, _pv_state(23, soc=23, pv=50)) == (23, False)
    assert _morning(controller, min_soc=23, soc=23, today=NEXT_DAY) == (26, True)


def test_a_restart_in_daylight_takes_no_step_until_the_next_morning():
    controller = _controller()

    assert _winter_target(controller, _pv_state(20, soc=30, pv=300)) == (20, False)
    assert _morning(controller, min_soc=20, soc=30, today=NEXT_DAY) == (33, True)


@pytest.mark.parametrize(
    "min_soc, soc, expected", [(38, 39, 40), (40, 45, 40), (38, 38, 40)]
)
def test_the_step_stops_at_the_winter_min_soc(min_soc, soc, expected):
    controller = _controller()

    assert _morning(controller, min_soc=min_soc, soc=soc)[0] == expected


@pytest.mark.parametrize("configured, expected", [(5, 23), (3, 23), (2, 22), (1, 21)])
def test_the_step_is_never_more_than_three_points(configured, expected):
    """Five points above the SoC made the firmware charge from the grid on 2026-10-03."""

    controller = _controller()

    assert _morning(controller, config={"ramp_step_percent": configured}) == (expected, True)


def test_min_soc_follows_the_soc_pv_reaches_after_the_step(caplog):
    controller = _controller()
    caplog.set_level(logging.INFO)
    _morning(controller)

    assert _winter_target(controller, _pv_state(23, soc=22, pv=400)) == (23, False)
    assert _winter_target(controller, _pv_state(23, soc=27, pv=400)) == (27, False)
    assert _winter_target(controller, _pv_state(27, soc=26, pv=400)) == (27, False)
    assert _winter_target(controller, _pv_state(27, soc=55, pv=400)) == (40, False)
    assert _events(caplog, "winter_follow_soc") == [logging.INFO, logging.INFO]


def test_min_soc_does_not_follow_the_soc_before_the_step_or_at_night():
    """A charged battery in the evening keeps what it holds for the night."""

    controller = _controller()

    assert _winter_target(controller, _pv_state(20, soc=35, pv=300)) == (20, False)
    _morning(controller, min_soc=20, soc=20, today=NEXT_DAY)
    assert _winter_target(controller, _pv_state(23, soc=30, pv=0), today=NEXT_DAY) == (23, False)


def test_a_raise_far_above_the_battery_waits_whole(caplog):
    """Cut to three above the SoC and written again each reconcile, it climbed all day."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)
    _remember(controller, 30)

    assert _min_soc_writes(controller, _device(15, 0), _pv_state(20, soc=20, max_soc=0)) == []
    assert _min_soc_writes(controller, _device(15, 0), _pv_state(20, soc=27, max_soc=0)) == [300]
    assert _events(caplog, "winter_raise_waits_for_battery") == [logging.INFO]


def test_a_target_remembered_while_winter_was_off_waits_for_the_battery():
    controller = _controller()
    _morning(controller, min_soc=30, soc=36)
    assert controller.winter.device("WR1").target == 39

    assert _winter_target(controller, _pv_state(39, soc=39), enabled=False) == (None, False)
    assert _winter_target(controller, _pv_state(15, soc=18)) == (15, False)


@pytest.mark.parametrize("remembered", [None, 32])
def test_a_report_without_min_soc_raises_nothing_the_battery_does_not_hold(remembered):
    """``parse_device`` reads a missing ``minSoc`` as 0; no gap is opened on a guess."""

    controller = _controller()
    if remembered:
        _remember(controller, remembered)

    assert _min_soc_writes(controller, _device(15, 0), _pv_state(0, soc=12, max_soc=0)) == []


def test_a_report_without_min_soc_keeps_the_remembered_target_the_battery_holds():
    controller = _controller()
    _remember(controller, 32)

    assert _min_soc_writes(controller, _device(15, 0), _pv_state(0, soc=35, max_soc=0)) == [320]


@pytest.mark.parametrize("soc", [0, float("nan")])
def test_without_a_soc_reading_the_step_waits(soc):
    """A report without ``electricLevel`` parses as SoC 0; it is no licence to raise."""

    controller = _controller()

    assert _morning(controller, min_soc=35, soc=soc) == (35, False)


@pytest.mark.parametrize("soc", [None, "n/a", float("nan"), True])
def test_a_remembered_raise_without_a_soc_reading_waits(soc):
    controller = _controller()
    _remember(controller, 25)

    assert _winter_target(controller, _pv_state(20, soc=soc)) == (20, False)


@pytest.mark.parametrize("soc", [True, float("inf"), float("-inf"), float("nan"), 10**400])
def test_only_a_finite_number_is_a_soc_reading(soc):
    assert cfg.winter_min_soc_raise_limit(30, 20, soc, 3) == 20


def test_a_device_without_state_writes_logs_no_waiting_raise(caplog):
    """MQTT control devices are output-only; nothing is waiting to be written there."""

    dev = SimpleNamespace(name="WR1", min_soc=15, max_soc=95, supports_state_reconciliation=False)
    controller = _controller()
    _remember(controller, 30)
    caplog.set_level(logging.DEBUG)

    _winter_target(controller, _pv_state(20, soc=20), dev=dev)

    assert _events(caplog, "winter_raise_waits_for_battery") == []


def test_a_battery_gone_for_a_report_keeps_the_target():
    """A transient ``packNum: 0`` leaves the remembered target alone."""

    controller = _controller()
    _morning(controller)

    assert _winter_target(controller, _pv_state(23, soc=23, pack_num=0)) == (None, False)
    assert controller.winter.device("WR1").target == 23


def test_the_summer_reset_waits_until_it_leads_the_battery_by_three_at_most(caplog):
    """A device left below the summer reserve is not raised far above its SoC.

    The change out of winter is logged at info, the repeats while the raise
    waits at debug.
    """

    controller = _controller()
    dev = _device(15, 0)
    caplog.set_level(logging.DEBUG)
    _remember(controller, 10)

    for _ in range(3):
        assert _min_soc_writes(controller, dev, _pv_state(8, soc=10, max_soc=0), winter_active=False) == []
    assert _min_soc_writes(controller, dev, _pv_state(8, soc=12, max_soc=0), winter_active=False) == [150]

    assert _events(caplog, "winter_summer_reset")[:3] == [logging.INFO, logging.DEBUG, logging.DEBUG]


def test_a_summer_reset_the_battery_holds_is_logged_each_time(caplog):
    """Its write has not landed; a reset that goes on failing stays in sight."""

    controller = _controller()
    caplog.set_level(logging.DEBUG)

    for _ in range(2):
        assert _winter_target(controller, _pv_state(10, soc=50), winter_active=False) == (15, False)

    assert _events(caplog, "winter_summer_reset") == [logging.INFO, logging.INFO]


def test_leaving_winter_forgets_the_armed_step():
    controller = _controller()
    _winter_target(controller, _pv_state(20, soc=20, pv=0))
    _winter_target(controller, _pv_state(20, soc=20, pv=0), winter_active=False)

    assert _winter_target(controller, _pv_state(20, soc=20, pv=60)) == (20, False)


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


def test_home_assistant_shows_the_target_the_controller_holds():
    class _Ha:
        def __init__(self):
            self.states = {}

        def set_state(self, entity, value, **kwargs):
            self.states[entity] = value

    ha = _Ha()
    dev = SimpleNamespace(name="WR1", min_soc=15, max_soc=95, enabled=True)
    controller = EMSController(devices=[dev], shelly=None, ha=ha, sleep_enabled=False)
    _remember(controller, 23)
    controller.winter.device("WR1").step_date = DAY.date().isoformat()

    with patch.object(cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)), patch.object(
        cfg, "winter_feature_enabled", lambda runtime: True
    ), patch.object(cfg, "winter_month_active", lambda now: True), patch.object(
        controller, "device_ha_extra", lambda dev, extra=None: extra
    ):
        controller.publish_winter_to_ha([_pv_state(20, soc=21)])

    assert ha.states["sensor.ems_solarflow_wr1_winter_min_soc_target"] == 23
    assert ha.states["sensor.ems_solarflow_wr1_winter_estimated_ramp_days"] == 6
    assert ha.states["sensor.ems_solarflow_winter_ramp_step"] == 3
    assert ha.states["sensor.ems_solarflow_winter_last_adjust_date"] == DAY.date().isoformat()
    assert ha.states["binary_sensor.ems_solarflow_winter_adjust_window"] in ("on", "off")
