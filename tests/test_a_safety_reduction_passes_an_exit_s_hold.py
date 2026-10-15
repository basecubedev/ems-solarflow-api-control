# SPDX-License-Identifier: AGPL-3.0-or-later
"""A safety reduction is not held behind an exit from the EMS's own charge (K23).

With no command in flight, a transport held every non-negative output target
while an exit's resend window was open, and, once three exits went to a charge
only grid draw shows, until that charge was left to the device: up to one
window, about 30 s, in which a stop to 0 W or a large cut waited too. Over MQTT
a target that preempts the command in flight already went through that hold.

A safety reduction -- a stop to 0 W, or a cut by the safety margin below the
target the last exit carried -- now goes out at once on both transports. It is
no exit of its own: the last exit's resend window and its count stay as they
were, so the charge is still left to the device when it would have been.
"""

from unittest.mock import patch

import pytest

from ems.charge_record import CHARGE_EXIT_PENDING, EXIT_RESEND_SECONDS, FOUND_EXIT_ATTEMPTS
from ems.mqtt_control import dispatch
from ems.zendure_mqtt.device_client import DEFAULT_SAFETY_PREEMPT_MARGIN_W
from test_ac_charge_regulator import (
    SteppedClock,
    following_http_device,
    following_mqtt_device,
    pv_less_state,
)
from test_unknown_state_fields import NO_DIRECTION, IdleDraw

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

CHARGE_W = 600
EXIT_W = 600


def _http(item):
    return following_http_device(item, double=IdleDraw, unreported=NO_DIRECTION)


def _mqtt(item):
    """No acknowledgement and no telemetry confirmation: no command is ever in flight."""

    return following_mqtt_device(
        item,
        double=IdleDraw,
        unreported=NO_DIRECTION,
        client_options={"telemetry_confirmation_supported": False},
    )


def _mqtt_with_a_wide_tolerance(item):
    """A safety margin below the confirmation tolerance, as a misconfiguration may set it."""

    return following_mqtt_device(
        item,
        double=IdleDraw,
        unreported=NO_DIRECTION,
        client_options={
            "telemetry_confirmation_supported": False,
            "safety_preempt_margin_w": MARGIN_W,
            "confirmation_tolerance_w": TOLERANCE_W,
        },
    )


MARGIN_W = 5
TOLERANCE_W = 25
TRANSPORTS = {"http": _http, "mqtt": _mqtt}
RIGS = {**TRANSPORTS, "mqtt, margin below tolerance": _mqtt_with_a_wide_tolerance}
REDUCTIONS = {"stop": 0, "cut": EXIT_W - DEFAULT_SAFETY_PREEMPT_MARGIN_W}


class Rig:
    """One device in the EMS's charge, its exits answered with grid draw alone."""

    def __init__(self, transport, exits):
        self.item = pv_less_state(soc=50)
        self.dev, self.hardware = RIGS[transport](self.item)
        self.clock = SteppedClock()
        with self.ticking():
            assert self.dev.dispatch_output_limit(-CHARGE_W)
            self.dev.fetch()
            for exit_number in range(exits):
                if exit_number:
                    self.clock.now += EXIT_RESEND_SECONDS
                assert self.dev.dispatch_output_limit(EXIT_W)
                self.exit_sent_at = self.clock.now
                self.clock.now += 5
                self.dev.fetch()
        assert self.dev.charge_commanded is True
        assert not getattr(self.dev, "_active_command", None) or not (
            self.dev._active_command.is_active
        )

    def ticking(self):
        return patch("time.monotonic", self.clock)

    def dispatch(self, target):
        before = len(self.hardware.writes)
        with self.ticking():
            result = self.dev.dispatch_output_limit(target)
        return result, self.hardware.writes[before:]

    def exhausted(self):
        with self.ticking():
            return self.dev.charge_exit_exhausted()


def _exit(target):
    return {"smartMode": 1, "acMode": 2, "outputLimit": target, "inputLimit": 0}


@pytest.mark.parametrize("reduction", REDUCTIONS)
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_safety_reduction_inside_an_exit_s_window_goes_out(transport, reduction):
    rig = Rig(transport, exits=1)
    target = REDUCTIONS[reduction]

    result, written = rig.dispatch(target)

    assert result.status is dispatch.WriteDispatchStatus.PUBLISHED
    assert written == [_exit(target)]


@pytest.mark.parametrize("reduction", REDUCTIONS)
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_safety_reduction_after_the_draw_only_exits_are_spent_goes_out(
    transport, reduction
):
    rig = Rig(transport, exits=FOUND_EXIT_ATTEMPTS)
    assert rig.dev._charge.drawn_exits_spent()
    assert rig.exhausted() is False
    target = REDUCTIONS[reduction]

    result, written = rig.dispatch(target)

    assert result.status is dispatch.WriteDispatchStatus.PUBLISHED
    assert written == [_exit(target)]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_change_short_of_a_safety_reduction_is_still_held(transport):
    rig = Rig(transport, exits=1)

    results = [
        rig.dispatch(target)
        for target in (EXIT_W + 40, EXIT_W - 40, EXIT_W - DEFAULT_SAFETY_PREEMPT_MARGIN_W + 1)
    ]

    assert [written for _result, written in results] == [[], [], []]
    assert {result.command_state for result, _written in results} == {CHARGE_EXIT_PENDING}


def test_over_mqtt_a_cut_inside_the_confirmation_tolerance_is_still_held():
    """The margin alone lets no cut through: it must exceed the confirmation tolerance too.

    With no command in flight, as test_zendure_mqtt_safety_preemption.py's
    ``test_ack_profile_sub_tolerance_reduction_queues_zero_stop_preempts`` pins
    it with one in flight: a stale report of the exit's target within the
    tolerance would otherwise confirm the cut.
    """

    rig = Rig("mqtt, margin below tolerance", exits=1)

    held, held_written = rig.dispatch(EXIT_W - TOLERANCE_W)
    passed, passed_written = rig.dispatch(EXIT_W - TOLERANCE_W - 1)

    assert held_written == []
    assert held.command_state == CHARGE_EXIT_PENDING
    assert passed.status is dispatch.WriteDispatchStatus.PUBLISHED
    assert passed_written == [_exit(EXIT_W - TOLERANCE_W - 1)]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_reduction_is_measured_against_the_target_it_left_on_the_device(transport):
    """A second stop is no reduction of the first; it does not go out every cycle."""

    rig = Rig(transport, exits=1)
    rig.dispatch(0)

    result, written = rig.dispatch(0)

    assert written == []
    assert result.command_state == CHARGE_EXIT_PENDING


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_reductions_re_arm_no_exit_and_the_charge_is_left_when_it_would_have_been(
    transport,
):
    """The draw-only bound ends on the last exit's window, reductions or not."""

    rig = Rig(transport, exits=FOUND_EXIT_ATTEMPTS)
    drawn = rig.dev._charge.drawn_exits

    for target in (EXIT_W - DEFAULT_SAFETY_PREEMPT_MARGIN_W, 0):
        rig.clock.now += 5
        result, written = rig.dispatch(target)
        assert written == [_exit(target)]
        with rig.ticking():
            rig.dev.fetch()

    assert rig.dev._charge.drawn_exits == drawn
    rig.clock.now = rig.exit_sent_at + EXIT_RESEND_SECONDS - 1
    assert rig.exhausted() is False
    rig.clock.now = rig.exit_sent_at + EXIT_RESEND_SECONDS
    assert rig.exhausted() is True
