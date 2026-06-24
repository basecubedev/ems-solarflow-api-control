# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import types

from ems.controller import EMSController


def make_controller():
    return EMSController(devices=[], shelly=None, sleep_enabled=False)


def make_states():
    return [
        types.SimpleNamespace(output_limit=0, output=0),
        types.SimpleNamespace(output_limit=0, output=0),
    ]


def _events(caplog, event, level=None):
    return [
        record
        for record in caplog.records
        if f"event={event}" in record.getMessage()
        and (level is None or record.levelno == level)
    ]


def test_initial_output_control_state_logs_info(caplog):
    controller = make_controller()
    caplog.set_level(logging.DEBUG)

    controller.stabilized_total_target(raw_load=0, states=make_states(), max_power=2400)

    initial = [
        r
        for r in _events(caplog, "output_control_state")
        if "initialized=True" in r.getMessage()
    ]
    assert initial
    assert all(r.levelno == logging.INFO for r in initial)


def test_repeated_output_control_state_logs_debug(caplog):
    controller = make_controller()
    controller.stabilized_total_target(raw_load=0, states=make_states(), max_power=2400)

    caplog.set_level(logging.DEBUG)
    controller.stabilized_total_target(raw_load=0, states=make_states(), max_power=2400)

    repeated = [
        r
        for r in _events(caplog, "output_control_state")
        if "initialized=False" in r.getMessage()
    ]
    assert repeated
    assert all(r.levelno == logging.DEBUG for r in repeated)
    assert not _events(caplog, "output_control_state", logging.INFO)


def test_deadband_hold_logs_debug(caplog):
    controller = make_controller()
    controller.stabilized_total_target(raw_load=0, states=make_states(), max_power=2400)

    caplog.set_level(logging.DEBUG)
    controller.stabilized_total_target(raw_load=0, states=make_states(), max_power=2400)

    holds = _events(caplog, "output_control_deadband_hold")
    assert holds
    assert all(r.levelno == logging.DEBUG for r in holds)
    assert not _events(caplog, "output_control_deadband_hold", logging.INFO)


def make_night_idle_controller(output_limit):
    devices = [types.SimpleNamespace(name="WR1", max_power=800)]
    controller = EMSController(devices=devices, shelly=None, sleep_enabled=False)
    states = [types.SimpleNamespace(output_limit=output_limit, output=0)]
    return controller, states


def test_night_idle_park_write_logs_info(caplog):
    controller, states = make_night_idle_controller(output_limit=0)
    caplog.set_level(logging.DEBUG)

    controller.apply_night_min_soc_idle_control(states, [0], min_output_limit=30)

    park = _events(caplog, "night_min_soc_idle_park_write")
    assert park
    assert all(r.levelno == logging.INFO for r in park)
    assert not _events(caplog, "night_min_soc_idle_hold_skip_write")


def test_night_idle_hold_skip_logs_debug(caplog):
    controller, states = make_night_idle_controller(output_limit=30)
    caplog.set_level(logging.DEBUG)

    # First pass records the device as already parked, second confirms the hold.
    controller.apply_night_min_soc_idle_control(states, [0], min_output_limit=30)
    controller.apply_night_min_soc_idle_control(states, [0], min_output_limit=30)

    holds = _events(caplog, "night_min_soc_idle_hold_skip_write")
    assert holds
    assert all(r.levelno == logging.DEBUG for r in holds)
    assert not _events(caplog, "night_min_soc_idle_hold_skip_write", logging.INFO)
    assert not _events(caplog, "night_min_soc_idle_park_write")
