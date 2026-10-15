# SPDX-License-Identifier: AGPL-3.0-or-later
"""A state field missing from a device report is unknown, not 0 (K20).

The owner decided for K10 on 2026-10-12 that the EMS does not write blindly
over a state it does not know. ``acMode`` and ``minSoc`` followed that rule;
``smartMode``, ``gridOffMode``, ``socSet`` and ``inputLimit`` were still read
as 0 when the report lacked them, and the reconciler that owns each wrote its
target over that 0 on every reconcile interval (``gridOffMode`` and
``inputLimit`` on every cycle), with nothing in the next report to say it took.
"""

import logging
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.charge_record import (
    CHARGE_EXIT_PENDING,
    EXIT_RESEND_SECONDS,
    FOUND_EXIT_ATTEMPTS,
    ChargeRecord,
)
from ems.clients import ZendureClient, parse_device, zero_device_state
from ems.controller import EMSController
from ems.mqtt_control import dispatch
from ems.runtime_intents import ac_input_intent
from ems.simulation import state_from_trace_device
from ems.state_store import BatteryFullChargeStateStore
from ems.winter_reserve import MIN_SOC_UNMANAGED
from ems.zendure_mqtt.device_client import DEFAULT_SAFETY_PREEMPT_MARGIN_W
from test_ac_charge_regulator import (
    CLOCKS,
    IDLE_EXIT,
    Harness,
    following_http_device,
    following_mqtt_device,
    pv_less_state,
)
from test_battery_full_charge_assist import (
    configure_assist,
    controller_for,
    event_types,
    run_controller_once,
    state,
)
from test_battery_full_charge_assist import device as assist_device
from test_shutdown_ends_the_full_charge_assist import POWER, Assist, FullReport, charging_item
from test_unknown_ac_mode import _Device
from test_write_gates import RuntimeStateStub, ShellyStub, device

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

REPORT = {
    "electricLevel": 60,
    "minSoc": 150,
    "socSet": 1000,
    "solarInputPower": 500,
    "outputLimit": 200,
    "packNum": 1,
    "acStatus": 1,
    "dcStatus": 1,
    "packState": 2,
    "acMode": 2,
    "smartMode": 1,
    "gridOffMode": 2,
    "inputLimit": 0,
}

FIELDS = [
    ("smartMode", "smart_mode_reported", "smart_mode", "smart_mode"),
    ("gridOffMode", "grid_off_mode_reported", "grid_off_mode", "grid_off_mode"),
    ("socSet", "max_soc_reported", "max_soc", "max_soc"),
    ("inputLimit", "input_limit_reported", "input_limit_w", "input_limit_w"),
]


def _report(**changes):
    properties = dict(REPORT)
    for name, value in changes.items():
        if value is None:
            properties.pop(name, None)
        else:
            properties[name] = value
    return parse_device({"properties": properties})


def _controller(runtime_devices=None):
    controlled = device("WR1")
    controlled.session.post.return_value = SimpleNamespace(status_code=200)
    controller = EMSController(
        devices=[controlled],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(devices=runtime_devices or {}),
    )
    return controller, controlled


def _written(controlled):
    return [call.kwargs["json"]["properties"] for call in controlled.session.post.call_args_list]


def _gate_open():
    return patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True)


@pytest.mark.parametrize(("name", "flag", "_attr", "_trace"), FIELDS)
def test_a_report_without_a_state_field_says_so(name, flag, _attr, _trace):
    assert getattr(_report(), flag) is True
    assert getattr(_report(**{name: 0}), flag) is True
    assert getattr(_report(**{name: None}), flag) is False


@pytest.mark.parametrize(("name", "flag", "attribute", "trace_key"), FIELDS)
def test_states_built_without_a_report_carry_none_of_them(name, flag, attribute, trace_key):
    assert getattr(zero_device_state(), flag) is False
    assert getattr(state_from_trace_device({"soc": 50}), flag) is False
    assert getattr(state_from_trace_device({"soc": 50, name: 0}), flag) is True
    assert getattr(state_from_trace_device({"soc": 50, trace_key: 0}), flag) is True


def test_smart_mode_is_not_written_over_a_missing_one():
    controller, controlled = _controller()

    with _gate_open():
        controller.apply_device_modes(controlled, _report(smartMode=None))

    assert _written(controlled) == []


def test_a_known_smart_mode_is_still_reconciled():
    controller, controlled = _controller()

    with _gate_open():
        controller.apply_device_modes(controlled, _report(smartMode=0))

    assert _written(controlled) == [{"smartMode": 1}]


def test_grid_off_mode_is_not_written_over_a_missing_one():
    controller, controlled = _controller({"WR1": {"offgrid_socket_mode": "off"}})

    with _gate_open():
        controller.apply_runtime_device_state(controlled, _report(gridOffMode=None))

    assert _written(controlled) == []


def test_a_known_grid_off_mode_is_still_reconciled():
    controller, controlled = _controller({"WR1": {"offgrid_socket_mode": "off"}})

    with _gate_open():
        controller.apply_runtime_device_state(controlled, _report(gridOffMode=0))

    assert _written(controlled) == [{"gridOffMode": 2}]


def test_soc_set_is_not_written_over_a_missing_one():
    controller, controlled = _controller()

    with _gate_open():
        controller.apply_soc_limits(controlled, _report(socSet=None))

    assert _written(controlled) == []


def test_min_soc_is_reconciled_alone_beside_a_missing_soc_set():
    controller, controlled = _controller()

    with _gate_open():
        controller.apply_soc_limits(controlled, _report(socSet=None, minSoc=100))

    assert _written(controlled) == [{"minSoc": 150}]


def test_a_known_soc_set_is_still_reconciled():
    controller, controlled = _controller()

    with _gate_open():
        controller.apply_soc_limits(controlled, _report(socSet=900))

    assert _written(controlled) == [{"socSet": 1000}]


def test_the_full_charge_assist_raise_is_not_written_over_a_missing_soc_set_and_stays_owed():
    """Nothing went out, so the assist's request must not read as done."""

    controller, controlled = _controller()

    with _gate_open():
        ok = controller.apply_soc_limits(
            controlled,
            _report(socSet=None),
            desired_min_soc=MIN_SOC_UNMANAGED,
            desired_max_soc=100,
            reason="battery_full_charge_assist",
        )

    assert ok is False
    assert _written(controlled) == []


def test_the_assist_charge_power_is_not_written_over_a_missing_input_limit():
    controller, controlled = _controller()
    intent = ac_input_intent("WR1", "battery_full_charge_assist", setpoint_w=600, priority=200)

    with _gate_open():
        ok = controller.reconcile_runtime_ac_charge_power(
            controlled, _report(acMode=1, inputLimit=None), intent
        )

    assert ok is False
    assert _written(controlled) == []


def test_the_operator_s_charge_power_is_written_over_a_missing_input_limit():
    """As for acMode (K10), a value the operator set is the one exception."""

    controller, controlled = _controller(
        {"WR1": {"runtime_role": "ac_input", "ac_charge_power_w": 500}}
    )
    current = _report(acMode=1, inputLimit=None)
    intent = controller.get_device_runtime_intent(controlled, current)

    with _gate_open():
        controller.reconcile_runtime_ac_charge_power(controlled, current, intent)

    assert intent.operator_role is True
    assert _written(controlled) == [{"inputLimit": 500}]


def test_a_known_input_limit_is_still_reconciled_for_the_assist():
    controller, controlled = _controller()
    intent = ac_input_intent("WR1", "battery_full_charge_assist", setpoint_w=600, priority=200)

    with _gate_open():
        controller.reconcile_runtime_ac_charge_power(controlled, _report(acMode=1), intent)

    assert _written(controlled) == [{"inputLimit": 600}]


def test_an_unknown_field_is_said_once_per_device_until_it_is_known_again(caplog):
    controller, controlled = _controller()

    with _gate_open(), caplog.at_level(logging.DEBUG):
        for current in (
            _report(smartMode=None),
            _report(smartMode=None),
            _report(smartMode=None),
            _report(),
            _report(smartMode=None),
        ):
            controller.apply_device_modes(controlled, current)

    said = [
        record for record in caplog.records
        if "event=unknown_state_field" in record.getMessage()
    ]
    assert [record.levelno for record in said] == [logging.WARNING, logging.WARNING]
    assert "field=smartMode" in said[0].getMessage()
    assert "device=WR1" in said[0].getMessage()
    assert _written(controlled) == []


def test_a_report_without_these_fields_is_written_none_of_them_every_cycle():
    """The reported defect, through the real fetch: smartMode=1 went out each reconcile."""

    hardware = _Device({
        name: value
        for name, value in REPORT.items()
        if name not in ("smartMode", "gridOffMode", "socSet", "inputLimit")
    })
    dev = ZendureClient(
        "WR1", "192.0.2.10", "WR1-SN", hardware, min_soc=15, max_soc=100,
        smart_mode=1, grid_off_mode=None, max_power=800,
        hardware_profile="solarflow_800_pro_2",
    )
    runtime = RuntimeStateStub(devices={"WR1": {"offgrid_socket_mode": "off"}})
    runtime.data = {}
    controller = EMSController(
        [dev], ShellyStub(300), sleep_enabled=False, runtime_state=runtime
    )
    controller.set_output_limit = Mock(return_value=True)

    with patch.multiple(
        "ems.controller.cfg",
        SYSTEM_ENABLED=True,
        MAX_TOTAL_POWER=800,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=0,
        DEADBAND=10,
        SOC_RECONCILE_INTERVAL=1,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_STATE_RECONCILIATION_WRITES=True,
        RECONCILE_SMART_MODE=True,
    ):
        for _ in range(3):
            assert controller.run_guarded() is True

    blind = {"smartMode", "gridOffMode", "socSet", "inputLimit"}
    assert [written for written in hardware.posts if blind.intersection(written)] == []
    assert controller.set_output_limit.call_count >= 1


NOW = datetime.now().astimezone()


def _assist_completed(tmp_path, *, ac_charge_mode=False):
    """The store of an assist that completed, its restore owed, not due again for weeks.

    With ``ac_charge_mode`` the assist's ``acMode=1`` went out, so it owes that restore too.
    """

    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(True, NOW)
    store.update_device_state("WR1", NOW, has_battery=True, next_due_at=NOW.isoformat())
    if ac_charge_mode:
        store.mark_assist_started("WR1", NOW, True)
        store.update_device_state("WR1", NOW, ac_mode_restore_pending=True)
    store.mark_assist_completed("WR1", NOW, 28)
    return store


class SocSetGap(Assist):
    """The assist rig whose report leaves out ``socSet`` while ``gap`` is set."""

    gap = False

    def fetch(self, _devices):
        self.dev.fetch()
        return [replace(self.item, max_soc_reported=False) if self.gap else self.item]


def _run_assist(rig, cycles, assist_enabled):
    with rig.loop(), configure_assist(enabled=assist_enabled):
        for _ in range(cycles):
            rig.controller.run_once()
            rig.clock.now += 5


@pytest.mark.parametrize("assist_enabled", [True, False], ids=["assist on", "assist off"])
def test_the_assist_s_soc_set_restore_waits_for_a_report_with_one_and_holds_no_control(
    tmp_path, caplog, assist_enabled
):
    """The restore over a report without socSet neither holds the device nor goes away.

    It waited for a socSet write K20 withholds, and its claim kept the device
    out of output control for good. Settled unwritten instead (R1a), socSet
    stayed at the assist's 100 % for good with soc_reconcile_interval 0, which
    disables the periodic reconcile meant to write it later (R2a).
    """

    rig = SocSetGap(
        tmp_path, item=state(soc=99, max_soc=100), store=_assist_completed(tmp_path)
    )
    rig.dev.max_soc = 90
    rig.gap = True

    with caplog.at_level(logging.INFO):
        _run_assist(rig, 3, assist_enabled)

    assert rig.store.get_device_state("WR1")["restore_pending"] is True
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert [written for written in rig.hardware.writes if "socSet" in written] == []
    assert "event=unknown_state_field" in caplog.text
    assert "field=socSet" in caplog.text

    rig.gap = False
    _run_assist(rig, 1, assist_enabled)

    assert {"socSet": 900} in rig.hardware.writes
    assert rig.store.get_device_state("WR1")["restore_pending"] is False
    assert "full_charge_assist_soc_restored" in event_types(rig.store)


def test_a_failed_soc_set_restore_over_a_known_soc_set_stays_owed(tmp_path):
    """Only a report without socSet lets the restore wait without holding control."""

    store = _assist_completed(tmp_path)
    dev = assist_device(max_soc=90)
    dev.session.post.side_effect = [SimpleNamespace(status_code=500)] + [
        SimpleNamespace(status_code=200)
    ] * 10
    controller = controller_for(dev, store)

    with configure_assist(assist_start_soc=80):
        run_controller_once(controller, state(soc=60, max_soc=100))
        assert store.get_device_state("WR1")["restore_pending"] is True
        assert controller.device_output_control_allowed(dev) is False
        run_controller_once(controller, state(soc=60, max_soc=100))

    assert store.get_device_state("WR1")["restore_pending"] is False
    assert controller.device_output_control_allowed(dev) is True


def test_a_known_soc_set_is_still_restored_after_the_assist(tmp_path):
    store = _assist_completed(tmp_path)
    dev = assist_device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    controller = controller_for(dev, store)

    with configure_assist():
        run_controller_once(controller, _report(socSet=1000))

    assert {"socSet": 900} in _written(dev)
    assert store.get_device_state("WR1")["restore_pending"] is False
    assert "full_charge_assist_soc_restored" in event_types(store)


class AcModeUnreported(Assist):
    """The assist rig over a report without ``acMode``, which parse_device reads as 0, unknown."""

    def fetch(self, _devices):
        self.dev.fetch()
        return [replace(self.item, ac_mode=0, ac_mode_reported=False)]


def _restoring_ac_mode(tmp_path, item):
    return AcModeUnreported(
        tmp_path, item=item, store=_assist_completed(tmp_path, ac_charge_mode=True)
    )


def _ac_restore_settled(rig):
    return rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is False


def test_the_assist_s_ac_mode_restore_over_a_report_without_ac_mode_ends_its_charge(
    tmp_path, caplog
):
    """The device may still be in the assist's AC input: it gets the assist's exit, then control.

    Over an unknown mode the reconciler writes no acMode=2 (K10), and the
    restore waited for a report of acMode 2 that never comes: the claim kept the
    device out of output control for good, charging from the grid or not.
    """

    rig = _restoring_ac_mode(tmp_path, charging_item())

    with caplog.at_level(logging.INFO):
        rig.run(cycles=4)

    assert rig.hardware.writes == [IDLE_EXIT]
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    assert _ac_restore_settled(rig)
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    settled = [
        line for line in caplog.text.splitlines()
        if "event=full_charge_assist_ac_mode_restore_settled" in line
    ]
    assert len(settled) == 1, caplog.text
    assert "reason=ac_mode_not_reported" in settled[0]
    assert "full_charge_assist_ac_mode_restore_settled" in event_types(rig.store)
    ended = [
        line for line in caplog.text.splitlines()
        if "event=ac_charge_ended_on_disable" in line
    ]
    assert len(ended) == 1, caplog.text
    assert "claim=battery_full_charge_assist_restore" in ended[0]


def test_a_device_that_keeps_the_assist_s_ac_input_gets_three_exits_then_control(
    tmp_path, caplog
):
    """Bounded as a switch-off is (OFF-1): once per resend window, three times at most."""

    rig = _restoring_ac_mode(tmp_path, charging_item())
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=10_000)
    window = int(EXIT_RESEND_SECONDS // 5)

    rig.run()
    assert rig.hardware.writes == [IDLE_EXIT]
    rig.run(cycles=window - 1)
    assert rig.hardware.writes == [IDLE_EXIT]

    with caplog.at_level(logging.INFO):
        rig.run(cycles=FOUND_EXIT_ATTEMPTS * window + 10)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    assert [written for written in rig.hardware.writes if written == {"acMode": 2}] == []
    assert rig.item.ac_mode == 1
    assert _ac_restore_settled(rig)
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert "event=full_charge_assist_ac_mode_restore_settled" in caplog.text
    assert "left_to=device" in caplog.text


class AcModeUnreportedOffline(AcModeUnreported):
    """The rig over a report without ``acMode``, the device unreachable while ``offline`` is set."""

    offline = False

    def fetch(self, devices):
        if self.offline:
            return [None]
        return super().fetch(devices)


def test_an_offline_spell_gives_the_restore_s_exits_no_fresh_count(tmp_path, caplog):
    """Each cycle the device did not answer set the count back: three exits again each time (K24)."""

    rig = AcModeUnreportedOffline(
        tmp_path, item=charging_item(), store=_assist_completed(tmp_path, ac_charge_mode=True)
    )
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=10_000)
    window = int(EXIT_RESEND_SECONDS // 5)

    with caplog.at_level(logging.INFO):
        for _ in range(FOUND_EXIT_ATTEMPTS * 3):
            rig.run(cycles=window)
            rig.offline = True
            rig.run(cycles=2)
            rig.offline = False
        rig.run(cycles=window)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    assert rig.item.ac_mode == 1
    assert _ac_restore_settled(rig)
    assert "left_to=device" in caplog.text
    assert rig.controller.device_output_control_allowed(rig.dev) is True


def _settled_lines(caplog):
    return [
        line for line in caplog.text.splitlines()
        if "event=full_charge_assist_ac_mode_restore_settled" in line
    ]


@pytest.mark.parametrize(
    "settling_polls", [0, 2, 5], ids=["at once", "settling", "settled as the window ends"]
)
def test_a_device_that_takes_the_third_exit_is_settled_as_out_of_the_charge(
    tmp_path, caplog, settling_polls
):
    """The settle read the record before the cycle's report: a taken exit read as ignored (R2a).

    A device still settling out of the charge in the next report has the
    third exit's resend window to show it left, the report that ends it
    included.
    """

    rig = _restoring_ac_mode(tmp_path, charging_item())
    rig.hardware.switch_polls = settling_polls
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=2)

    with caplog.at_level(logging.INFO):
        rig.run(cycles=FOUND_EXIT_ATTEMPTS * int(EXIT_RESEND_SECONDS // 5) + 10)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    assert _ac_restore_settled(rig)
    settled = _settled_lines(caplog)
    assert len(settled) == 1, caplog.text
    assert settled[0].startswith("INFO")
    assert "left_to=device" not in settled[0]


class AcModeAndStatusUnreported(Assist):
    """Neither ``acMode`` nor ``acStatus`` in the report: no direction shown at all."""

    def fetch(self, _devices):
        self.dev.fetch()
        return [replace(self.item, ac_mode=0, ac_mode_reported=False, ac_status=0)]


@pytest.mark.parametrize(
    ("ac_mode", "ac_mode_reported", "grid_input", "released"),
    [
        (0, False, POWER, False),
        (0, False, 0, True),
        (3, True, POWER, False),
        (2, True, POWER, True),
    ],
    ids=["no acMode, drawing", "no acMode, not drawing", "acMode 3, drawing", "acMode 2"],
)
def test_a_charge_record_reads_a_report_without_a_direction_by_its_grid_draw(
    ac_mode, ac_mode_reported, grid_input, released
):
    """The record every transport keeps reads an unknown acMode as no direction, not as out."""

    record = ChargeRecord()
    record.charge_sent(POWER)
    record.exit_sent(0.0)
    report = replace(
        state(ac_mode=ac_mode, ac_status=0, input_limit_w=POWER),
        ac_mode_reported=ac_mode_reported,
    )
    report.grid_input = grid_input

    record.observe(report, setpoint_reported=True)

    assert record.open is not released


def test_a_device_drawing_without_ac_mode_or_ac_status_is_not_read_out_of_the_charge(
    tmp_path, caplog
):
    """With no direction in the report, out of the charge needs no grid draw (R2a).

    Read out of the charge by its direction alone, the device was settled at
    INFO after one ignored exit while it drew 200 W from the grid.
    """

    rig = AcModeAndStatusUnreported(
        tmp_path, item=charging_item(), store=_assist_completed(tmp_path, ac_charge_mode=True)
    )
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=10_000)

    with caplog.at_level(logging.INFO):
        rig.run(cycles=FOUND_EXIT_ATTEMPTS * int(EXIT_RESEND_SECONDS // 5) + 10)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    assert rig.item.grid_input == POWER
    assert _ac_restore_settled(rig)
    settled = _settled_lines(caplog)
    assert len(settled) == 1, caplog.text
    assert settled[0].startswith("WARNING")
    assert "left_to=device" in settled[0]


class IdleDraw(FullReport):
    """A device that reports a few watts of grid draw once out of its charge, and can discharge."""

    idle_draw_w = 5

    def report(self):
        return {**super().report(), "dcStatus": 1}

    def _settle(self):
        super()._settle()
        if self.state.ac_status != 2:
            self.state.grid_input = self.idle_draw_w


class ReportHarness(Harness):
    """The loop reads what the device's client parsed from its report, as it does live."""

    def fetch(self, states):
        return [dev.fetch() for dev in self.devices]


TRANSPORTS = {"http": following_http_device, "mqtt": following_mqtt_device}
NO_DIRECTION = ("acMode", "inputLimit")


def _regulator_charging(transport, *, unreported=NO_DIRECTION, idle_draw_w=5, clock="stepped"):
    """The regulator's charge on a device whose report carries no ``acMode`` and no ``inputLimit``."""

    item = pv_less_state(soc=50)
    dev, hardware = TRANSPORTS[transport](item, double=IdleDraw, unreported=unreported)
    hardware.idle_draw_w = idle_draw_w
    runtime = RuntimeStateStub(devices={})
    harness = ReportHarness(
        [dev], load=-900, runtime_state=runtime, seconds_per_cycle=5, clock=CLOCKS[clock]()
    )
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1
    assert dev.charge_commanded is True
    return SimpleNamespace(
        item=item, dev=dev, hardware=hardware, runtime=runtime, harness=harness
    )


def _exits(writes):
    return [written for written in writes if written.get("acMode") == 2]


def _left_lines(caplog):
    return [
        line for line in caplog.text.splitlines()
        if "event=ac_charge_left_to_device" in line
    ]


def _exits_spent(transport):
    """Control off: three exits answered with grid draw alone, inside the last one's window."""

    rig = _regulator_charging(transport)
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False
    while len(_exits(rig.hardware.writes[before:])) < FOUND_EXIT_ATTEMPTS:
        rig.harness.run(cycles=1, states=[rig.item])
    rig.harness.run(cycles=2, states=[rig.item])
    assert rig.dev.charge_commanded is True
    assert rig.dev._charge.drawn_exits_spent()
    return rig


def _moving_load(cycle):
    return 300 + 40 * ((cycle * 7) % 5)


def _in_flight(dev):
    active = getattr(dev, "_active_command", None)
    return active is not None and active.is_active


def _exits_spent_with_a_command_in_flight():
    """Control on and a moving load: the exits are spent and the last MQTT command is in flight."""

    rig = _regulator_charging("mqtt")
    for cycle in range(30):
        if rig.dev._charge.drawn_exits_spent() and _in_flight(rig.dev):
            return rig
        rig.harness.run(cycles=1, states=[rig.item], load=_moving_load(cycle))
    pytest.fail("the exits were never spent with a command in flight")


@pytest.mark.parametrize("clock", sorted(CLOCKS))
@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
@pytest.mark.parametrize(("idle_draw_w", "exits"), [(5, FOUND_EXIT_ATTEMPTS), (0, 1)])
def test_switched_off_a_charge_only_grid_draw_shows_gets_three_exits_then_is_left(
    transport, clock, idle_draw_w, exits, caplog
):
    """A transport's record over a report without acMode closed for no idle draw at all (R3a).

    A device that reports a few watts of grid draw once idle kept the
    record open for good: the exit went out once per resend window, ten
    times in 300 s, the stop's release went to it and ``charges_held_by_ems``
    named it. On a clock that moves within a cycle the window can end between
    the controller's reading and the transport's, which must not send a fourth.
    """

    rig = _regulator_charging(transport, idle_draw_w=idle_draw_w, clock=clock)
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False

    with caplog.at_level(logging.INFO):
        rig.harness.run(cycles=60, states=[rig.item])

    written = rig.hardware.writes[before:]
    assert written == _exits(written)
    assert len(written) == exits
    assert rig.item.ac_mode == 2
    assert rig.dev.charge_commanded is False
    assert rig.harness.controller.charges_held_by_ems() == {}
    left = _left_lines(caplog)
    if idle_draw_w:
        assert len(left) == 1, caplog.text
        assert left[0].startswith("WARNING")
        assert f"exits={FOUND_EXIT_ATTEMPTS}" in left[0]
        assert f"grid_input_w={idle_draw_w}" in left[0]
    else:
        assert left == []


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_the_third_exit_s_window_is_the_device_s_to_show_it_left(transport, caplog):
    """A device whose draw is gone before the last window ends left the charge: no warning."""

    with caplog.at_level(logging.INFO):
        rig = _exits_spent(transport)
        before = len(rig.hardware.writes)
        rig.hardware.idle_draw_w = 0
        rig.item.grid_input = 0
        rig.harness.run(cycles=30, states=[rig.item])

    assert rig.hardware.writes[before:] == []
    assert rig.dev.charge_commanded is False
    assert _left_lines(caplog) == []


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_with_control_on_a_charge_only_grid_draw_shows_holds_the_output_no_longer(
    transport, caplog
):
    """With control on every output target waited for the exit's window, for good (R3a).

    On the local API each one went out as the exit, the atomic set with
    ``acMode``, once per resend window; after the third the target is the
    plain ``outputLimit`` again, written when it changes.
    """

    rig = _regulator_charging(transport)

    with caplog.at_level(logging.INFO):
        rig.harness.run(cycles=40, states=[rig.item], load=300)

    assert rig.dev.charge_commanded is False
    assert rig.harness.controller.charges_held_by_ems() == {}
    assert len(_left_lines(caplog)) == 1, caplog.text
    if transport == "http":
        writes = rig.hardware.writes
        assert len(_exits(writes)) == FOUND_EXIT_ATTEMPTS
        last_exit = max(i for i, written in enumerate(writes) if written.get("acMode") == 2)
        after = writes[last_exit + 1:]
        assert after and all(set(written) == {"outputLimit"} for written in after)


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_with_control_on_and_a_moving_load_the_charge_is_left_to_the_device(
    transport, caplog
):
    """A target that changes every cycle replaced the MQTT command still in
    flight, each publish a new exit, so the bound was never reached (R4a)."""

    rig = _regulator_charging(transport)

    with caplog.at_level(logging.INFO):
        for cycle in range(60):
            rig.harness.run(cycles=1, states=[rig.item], load=_moving_load(cycle))

    assert rig.dev.charge_commanded is False
    assert rig.harness.controller.charges_held_by_ems() == {}
    assert len(_left_lines(caplog)) == 1, caplog.text


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_a_stop_inside_the_last_exit_s_window_still_sends_its_release(transport):
    """The shutdown release is not held with the exits spent, an MQTT command in flight (R4a, R5a)."""

    rig = _exits_spent(transport)
    assert _in_flight(rig.dev) is (transport == "mqtt")
    before = len(rig.hardware.writes)

    with patch("time.monotonic", rig.harness.clock):
        rig.harness.controller.release_charging_devices()

    assert rig.hardware.writes[before:] == [IDLE_EXIT]


def test_a_charge_target_inside_the_hold_is_published():
    """A new charge is not held behind the spent exits' command in flight (R5a)."""

    rig = _exits_spent_with_a_command_in_flight()
    before = len(rig.hardware.writes)

    with patch("time.monotonic", rig.harness.clock):
        result = rig.dev.dispatch_output_limit(-300)

    assert result.status is dispatch.WriteDispatchStatus.PUBLISHED
    assert rig.hardware.writes[before:] == [
        {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": 300}
    ]
    assert rig.dev.charge_commanded is True
    assert not rig.dev._charge.drawn_exits_spent()


@pytest.mark.parametrize("reduction", ["stop", "cut"])
def test_a_safety_reduction_inside_the_hold_preempts_the_command_in_flight(reduction):
    """A stop or a cut by the safety margin does not wait for that command's timeout (R5a).

    It is no exit of its own: the last exit's resend window stands (K23).
    """

    rig = _exits_spent_with_a_command_in_flight()
    active = rig.dev._active_command
    exit_sent_at = rig.dev._charge.exit_sent_at
    target = 0 if reduction == "stop" else active.target_w - DEFAULT_SAFETY_PREEMPT_MARGIN_W
    assert target >= 0
    before = len(rig.hardware.writes)

    with patch("time.monotonic", rig.harness.clock):
        result = rig.dev.dispatch_output_limit(target)

    assert result.status is dispatch.WriteDispatchStatus.PUBLISHED
    assert rig.hardware.writes[before:] == [
        {"smartMode": 1, "acMode": 2, "outputLimit": target, "inputLimit": 0}
    ]
    assert rig.dev._active_command.target_w == target
    assert not active.is_active
    assert rig.dev._charge.exit_sent_at == exit_sent_at


def test_a_change_short_of_a_safety_reduction_inside_the_hold_is_held():
    """The hold keeps the spent exits' bound: a rise or a small cut does not replace the command."""

    rig = _exits_spent_with_a_command_in_flight()
    active = rig.dev._active_command
    before = len(rig.hardware.writes)

    with patch("time.monotonic", rig.harness.clock):
        results = [
            rig.dev.dispatch_output_limit(target)
            for target in (active.target_w + 40, active.target_w - 40)
        ]

    assert [(result.status, result.command_state) for result in results] == [
        (dispatch.WriteDispatchStatus.COALESCED_ACTIVE, CHARGE_EXIT_PENDING)
    ] * 2
    assert rig.hardware.writes[before:] == []
    assert rig.dev._active_command is active


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_a_charge_its_report_still_shows_by_ac_status_is_not_left_to_the_device(
    transport, caplog
):
    """Bounded by grid draw alone: a device that reports ``acStatus`` charging keeps getting its exit."""

    rig = _regulator_charging(transport)
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=10_000)
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False

    with caplog.at_level(logging.INFO):
        rig.harness.run(cycles=60, states=[rig.item])

    assert rig.item.ac_status == 2
    assert len(_exits(rig.hardware.writes[before:])) > FOUND_EXIT_ATTEMPTS
    assert rig.dev.charge_commanded is True
    assert _left_lines(caplog) == []


def _drawn(grid_input=5, **changes):
    report = replace(
        state(**{"ac_mode": 0, "ac_status": 1, "input_limit_w": 0, **changes}),
        ac_mode_reported=False,
    )
    report.grid_input = grid_input
    return report


def test_a_charge_record_counts_only_exits_the_device_answers_with_grid_draw_alone():
    record = ChargeRecord()
    record.charge_sent(POWER)
    now = 0.0
    for _ in range(FOUND_EXIT_ATTEMPTS):
        assert record.exit_due(now) is True
        record.exit_sent(now)
        record.observe(_drawn(), setpoint_reported=False)
        record.observe(_drawn(), setpoint_reported=False)
        now += EXIT_RESEND_SECONDS - 1
        assert record.exit_due(now) is False
        assert record.exit_exhausted(now) is False
        now += 1

    assert record.drawn_exits == FOUND_EXIT_ATTEMPTS
    assert record.exit_due(now) is False
    assert record.exit_exhausted(now) is True
    record.observe(_drawn(ac_status=2), setpoint_reported=False)
    assert record.exit_due(now) is True
    assert record.exit_exhausted(now) is False
    record.charge_sent(POWER)
    assert record.drawn_exits == 0


def test_the_full_charge_assist_s_record_leaves_its_exits_to_the_controller():
    """The assist's exits are capped per switch-off by the controller (OFF-1b)."""

    record = ChargeRecord(bounded_by_draw=False)
    record.charge_sent(POWER)
    for now in (0.0, 30.0, 60.0, 90.0):
        record.exit_sent(now)
        record.observe(_drawn(), setpoint_reported=False)

    assert record.drawn_exits == 0
    assert record.exit_due(120.0) is True
    assert record.exit_exhausted(120.0) is False


def test_the_ac_mode_restore_over_a_device_out_of_any_charge_returns_control(tmp_path):
    """The reported shape (R1a): no acMode, no charge; one exit to idle at most, then control."""

    rig = _restoring_ac_mode(
        tmp_path, state(soc=88, max_soc=100, ac_mode=2, ac_status=1, input_limit_w=0)
    )

    rig.run(cycles=4)

    assert rig.hardware.writes in ([], [IDLE_EXIT])
    assert _ac_restore_settled(rig)
    assert rig.controller.device_output_control_allowed(rig.dev) is True


class InputLimitUnreported(Assist):
    """The assist rig over a report without ``inputLimit``, which parse_device reads as 0, unknown."""

    def fetch(self, _devices):
        self.dev.fetch()
        return [replace(self.item, input_limit_w=0, input_limit_reported=False)]


def test_the_assist_enters_ac_input_only_with_its_charge_power(tmp_path, caplog):
    """acMode=1 without the inputLimit beside it charged from the grid at a power nobody knew."""

    rig = InputLimitUnreported(tmp_path)

    with caplog.at_level(logging.INFO):
        rig.run(cycles=3)

    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is True
    assert [written for written in rig.hardware.writes if written.get("acMode") == 1] == []
    assert [written for written in rig.hardware.writes if "inputLimit" in written] == []
    assert rig.item.ac_mode == 2
    assert "event=unknown_state_field" in caplog.text
    assert "field=inputLimit" in caplog.text


def _assist_discharging_without_input_limit(tmp_path):
    rig = InputLimitUnreported(tmp_path)
    rig.item.output_limit = 400
    rig.item.output = 400
    rig.run(cycles=3)
    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is True
    assert [w for w in rig.hardware.writes if "acMode" in w or "inputLimit" in w] == []
    assert rig.item.ac_mode == 2
    return rig


def test_an_assist_that_entered_no_ac_input_holds_no_charge_at_a_stop(tmp_path, caplog):
    """The assist's charge record opened on its claim, not on its acMode=1 (R2a).

    Over a report without inputLimit the assist writes no AC input, yet
    charges_held_by_ems named the device and a stop by itself (--once,
    --max-cycles, Ctrl-C) sent the exit to idle to a device discharging at
    400 W that the assist never charged.
    """

    rig = _assist_discharging_without_input_limit(tmp_path)

    assert rig.controller.charges_held_by_ems() == {}
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is False
    before = len(rig.hardware.writes)
    with caplog.at_level(logging.INFO):
        rig.release()

    assert rig.written_since(before) == []
    assert "event=ac_charge_released_on_shutdown" not in caplog.text


def test_switching_off_sends_no_exit_to_an_assist_that_entered_no_ac_input(tmp_path, caplog):
    rig = _assist_discharging_without_input_limit(tmp_path)
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False

    with caplog.at_level(logging.INFO):
        rig.run(cycles=2)

    assert IDLE_EXIT not in rig.written_since(before)
    assert "event=ac_charge_ended_on_disable" not in caplog.text


def _never_reports_ac_mode(tmp_path, charging):
    rig = AcModeUnreported(tmp_path)
    if charging:
        rig.item.ac_mode = 1
        rig.item.ac_status = 2
        rig.item.input_limit_w = 300
        rig.item.grid_input = 300
    rig.run(cycles=3)
    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is True
    assert [written for written in rig.hardware.writes if "acMode" in written] == []
    return rig


@pytest.mark.parametrize("charging", [False, True], ids=["idle", "someone else charges"])
def test_an_assist_that_never_saw_ac_mode_owes_no_ac_mode_restore(tmp_path, caplog, charging):
    """K10 kept the assist's acMode=1 off a device whose report never carries acMode.

    Its restore still wrote the exit to idle there, acMode=2 over a mode
    never known, from a claim no operator set.
    """

    rig = _never_reports_ac_mode(tmp_path, charging)
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is False
    assert rig.controller.full_charge_assist_charge_held(rig.dev) is False
    before = len(rig.hardware.writes)

    rig.item.soc = 100
    rig.item.soc_limit = 1
    with caplog.at_level(logging.INFO):
        rig.run(cycles=30)

    after = rig.written_since(before)
    assert [written for written in after if "acMode" in written] == []
    assert IDLE_EXIT not in after
    record = rig.store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["ac_mode_restore_pending"] is False
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert "event=ac_charge_ended_on_disable" not in caplog.text


def test_an_assist_switched_off_before_it_saw_ac_mode_owes_no_ac_mode_restore(tmp_path):
    rig = _never_reports_ac_mode(tmp_path, charging=False)
    before = len(rig.hardware.writes)

    with rig.loop(), configure_assist(enabled=False):
        for _ in range(10):
            rig.controller.run_once()
            rig.clock.now += 5

    assert IDLE_EXIT not in rig.written_since(before)
    record = rig.store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["ac_mode_restore_pending"] is False
    assert rig.controller.device_output_control_allowed(rig.dev) is True


def test_the_assist_owes_the_restore_once_its_ac_mode_went_out(tmp_path):
    rig = Assist(tmp_path)

    rig.run(state_writes=False)
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is False
    assert rig.controller.charges_held_by_ems() == {}

    rig.run(cycles=2)
    assert {"acMode": 1} in rig.hardware.writes
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is True
    assert rig.controller.charges_held_by_ems() == {"WR1": POWER}


class AcInputAnswerLost(Assist):
    """The assist's ``acMode=1`` reaches the device, and no answer says it took it (R3a).

    ``answer`` is a ``5xx`` or a ``timeout``; with ``applied`` false the device
    refuses the write as well. ``offline`` makes the device stop answering reads.
    """

    offline = False

    def __init__(self, tmp_path, answer="5xx", applied=True):
        super().__init__(tmp_path)
        if not applied:
            self.hardware.ignore(lambda properties: properties.get("acMode") == 1, times=10_000)
        post = self.hardware.post

        def post_without_an_answer(url, json=None, **kwargs):
            reply = post(url, json=json, **kwargs)
            if json["properties"].get("acMode") != 1:
                return reply
            if answer == "timeout":
                raise TimeoutError("read timed out")
            return SimpleNamespace(status_code=500, text="")

        self.hardware.post = post_without_an_answer

    def fetch(self, devices):
        return [None] if self.offline else super().fetch(devices)


ANSWERS = ["5xx", "timeout"]


def _charging_after_a_lost_answer(tmp_path, answer):
    rig = AcInputAnswerLost(tmp_path, answer)
    rig.run()
    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    return rig


@pytest.mark.parametrize("answer", ANSWERS)
def test_the_assist_owes_the_restore_of_an_ac_mode_whose_answer_was_lost(tmp_path, answer):
    rig = _charging_after_a_lost_answer(tmp_path, answer)

    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is True
    assert rig.controller.charges_held_by_ems() == {"WR1": POWER}


@pytest.mark.parametrize("answer", ANSWERS)
def test_switched_off_after_a_lost_answer_the_assist_s_charge_gets_its_exit(tmp_path, answer):
    rig = _charging_after_a_lost_answer(tmp_path, answer)
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False

    rig.run(cycles=30)

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.grid_input == 0


@pytest.mark.parametrize("answer", ANSWERS)
def test_a_stop_by_itself_after_a_lost_answer_releases_the_assist_s_charge(tmp_path, answer):
    rig = _charging_after_a_lost_answer(tmp_path, answer)
    before = len(rig.hardware.writes)

    rig.release()

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2


@pytest.mark.parametrize("answer", ANSWERS)
def test_switched_off_while_unreachable_after_a_lost_answer_the_charge_gets_its_exit(
    tmp_path, answer
):
    rig = _charging_after_a_lost_answer(tmp_path, answer)
    rig.offline = True
    rig.run(cycles=3)
    rig.runtime.system["enabled"] = False
    rig.run(cycles=2)
    rig.offline = False
    before = len(rig.hardware.writes)

    rig.run(cycles=30)

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.grid_input == 0


@pytest.mark.parametrize("answer", ANSWERS)
def test_the_assist_s_ac_mode_switched_off_after_a_lost_answer_takes_its_ac_input_back(
    tmp_path, answer
):
    """AR-1."""

    rig = _charging_after_a_lost_answer(tmp_path, answer)

    rig.run(cycles=10, ac_mode=False)

    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0


@pytest.mark.parametrize("answer", ANSWERS)
def test_an_ac_mode_the_device_refused_costs_one_exit_at_most(tmp_path, answer):
    """Owed on the write alone, a restore the device never needed is one exit to idle.

    The next report shows ``acMode`` 2, which closes the record that exit opened.
    """

    rig = AcInputAnswerLost(tmp_path, answer, applied=False)
    rig.run()
    assert rig.item.ac_mode == 2
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is True
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False

    rig.run(cycles=30)
    rig.release()

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.controller.charges_held_by_ems() == {}


@pytest.mark.parametrize("answer", ANSWERS)
def test_a_stop_by_itself_after_a_refused_ac_mode_sends_one_exit_at_most(tmp_path, answer):
    rig = AcInputAnswerLost(tmp_path, answer, applied=False)
    rig.run()
    before = len(rig.hardware.writes)

    rig.release()

    assert rig.written_since(before) == [IDLE_EXIT]


@pytest.mark.parametrize("answer", ANSWERS)
def test_a_refused_ac_mode_restored_by_the_report_costs_no_exit(tmp_path, answer):
    rig = AcInputAnswerLost(tmp_path, answer, applied=False)
    rig.run(cycles=3)
    rig.item.soc = 100
    rig.item.soc_limit = 1
    before = len(rig.hardware.writes)

    rig.run(cycles=5)

    assert IDLE_EXIT not in rig.written_since(before)
    assert [written for written in rig.written_since(before) if "acMode" in written] == []
    assert _ac_restore_settled(rig)
    assert rig.controller.device_output_control_allowed(rig.dev) is True


def test_the_reconciler_reports_an_ac_mode_write_it_sent_whatever_the_answer():
    """Its one consumer owes the restore of ``acMode`` from it (:meth:`update_full_charge_assist_ac_pending`)."""

    controller, controlled = _controller()
    intent = ac_input_intent("WR1", "battery_full_charge_assist", setpoint_w=POWER)
    current = _report(acMode=2)

    with _gate_open():
        controlled.session.post.return_value = SimpleNamespace(status_code=500, text="")
        failed = controller.reconcile_ac_mode_intent(controlled, current, intent)
        controlled.session.post.side_effect = TimeoutError("read timed out")
        unanswered = controller.reconcile_ac_mode_intent(controlled, current, intent)
        with patch(
            "ems.controller.write_device_properties",
            return_value=dispatch.rejected(None, reason="transport_property_writes_unsupported"),
        ):
            unsent = controller.reconcile_ac_mode_intent(controlled, current, intent)
    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=False):
        withheld = controller.reconcile_ac_mode_intent(controlled, current, intent)

    assert (failed, unanswered, unsent, withheld) == (True, True, False, False)
    assert controlled.session.post.call_count == 2


def test_the_assist_s_charge_power_waits_for_a_known_ac_mode(tmp_path):
    """The other half of together or not at all: no inputLimit while acMode=1 cannot go out."""

    rig = AcModeUnreported(tmp_path)

    rig.run(cycles=3)

    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is True
    assert [written for written in rig.hardware.writes if "inputLimit" in written] == []
    assert [written for written in rig.hardware.writes if "acMode" in written] == []


def test_the_assist_with_both_known_still_enters_its_charge(tmp_path):
    rig = Assist(tmp_path)

    rig.run(cycles=2)

    entered = [written for written in rig.hardware.writes if written != {"socSet": 1000}]
    assert entered[:2] == [{"acMode": 1}, {"inputLimit": POWER}]
    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER


def test_the_operator_s_ac_input_role_is_still_written_over_a_missing_input_limit():
    controller, controlled = _controller(
        {"WR1": {"runtime_role": "ac_input", "ac_charge_power_w": 500}}
    )
    current = _report(acMode=2, inputLimit=None)
    intent = controller.get_device_runtime_intent(controlled, current)

    with _gate_open():
        controller.reconcile_ac_mode_intent(controlled, current, intent)
        controller.reconcile_runtime_ac_charge_power(controlled, current, intent)

    assert _written(controlled) == [{"acMode": 1}, {"inputLimit": 500}]


def _mqtt_control_device():
    """An MQTT control device: output-only, no state reconciliation."""

    controlled = assist_device(max_soc=90)
    controlled.supports_state_reconciliation = False
    controlled.control_gate = "mqtt_local"
    controlled.session.post.return_value = SimpleNamespace(status_code=200)
    return controlled


def _report_of(ac_mode_reported, **changes):
    return replace(
        state(**{"soc": 60, "max_soc": 90, "ac_mode": 2, **changes}),
        ac_mode=2 if ac_mode_reported else 0,
        ac_mode_reported=ac_mode_reported,
    )


@pytest.mark.parametrize("ac_mode_reported", [True, False], ids=["acMode", "no acMode"])
def test_an_assist_on_a_transport_without_state_reconciliation_owes_no_ac_mode_restore(
    tmp_path, ac_mode_reported
):
    """No acMode goes out there, so the assist never put the device into AC input."""

    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(True, NOW)
    store.update_device_state("WR1", NOW, has_battery=True, next_due_at=NOW.isoformat())
    controlled = _mqtt_control_device()
    controller = controller_for(controlled, store)

    with configure_assist(assist_start_soc=80):
        for _ in range(3):
            run_controller_once(controller, _report_of(ac_mode_reported, soc=85))
        assert store.get_device_state("WR1")["full_charge_assist_active"] is True
        assert store.get_device_state("WR1")["ac_mode_restore_pending"] is False
        for _ in range(3):
            run_controller_once(
                controller, _report_of(ac_mode_reported, soc=100, soc_limit=1)
            )

    record = store.get_device_state("WR1")
    assert record["full_charge_assist_active"] is False
    assert record["ac_mode_restore_pending"] is False
    assert controller.device_output_control_allowed(controlled) is True
    assert _written(controlled) == []


@pytest.mark.parametrize("ac_mode_reported", [True, False], ids=["acMode", "no acMode"])
def test_an_ac_mode_restore_owed_on_a_transport_without_state_reconciliation_is_settled(
    tmp_path, caplog, ac_mode_reported
):
    """A restore owed from the local API, the device now on MQTT control (R2a).

    No record of the assist's charge is kept on such a transport, so over a
    report without acMode the restore waited for good, the device out of
    output control.
    """

    store = _assist_completed(tmp_path, ac_charge_mode=True)
    controlled = _mqtt_control_device()
    controller = controller_for(controlled, store)

    with configure_assist(assist_start_soc=80), caplog.at_level(logging.INFO):
        for _ in range(3):
            run_controller_once(controller, _report_of(ac_mode_reported))

    assert store.get_device_state("WR1")["ac_mode_restore_pending"] is False
    assert controller.device_output_control_allowed(controlled) is True
    assert [written for written in _written(controlled) if "acMode" in written] == []
    exits = [
        call for call in controller.set_output_limit.call_args_list
        if call.kwargs.get("charge_exit") is not None
    ]
    assert exits == []
    assert "event=full_charge_assist_ac_mode_restore" in caplog.text
