# SPDX-License-Identifier: AGPL-3.0-or-later
"""Switching the EMS off ends the full-charge assist's charge like its others.

The assist starts its charge through the state reconciler, so no transport has
it on record, and since off is off (OFF-1) the reconciler writes nothing while
control is off: the charge ran on unwatched to a full battery and left the
device in AC input (review 3, round 11). The owner's answer of 2026-10-10:
ended like any charge the EMS started -- one exit, then nothing -- no assist
starts while control is off, and one that is still due goes on once it is back.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.charge_record import (
    CHARGE_EXIT_FINAL,
    EXIT_RESEND_SECONDS,
    FOUND_EXIT_ATTEMPTS,
)
from ems.mqtt_control.dispatch import failed, withheld
from ems.controller import EMSController
from ems.state_store import BatteryFullChargeStateStore
from tests.test_battery_full_charge_assist import (
    ShellyStub,
    configure_assist,
    device,
    event_types,
    posted_properties,
    state,
)
from tests.test_off_means_off import Runtime

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

DUE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
POWER = 200


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def assist_due(tmp_path):
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(True, DUE)
    store.update_device_state(
        "WR1", DUE, has_battery=True, next_due_at=DUE.isoformat()
    )
    return store


def controller_for(store, runtime):
    dev = device(max_soc=90)
    dev.session.post.return_value = SimpleNamespace(status_code=200)
    controller = EMSController(
        devices=[dev],
        shelly=ShellyStub(),
        sleep_enabled=False,
        runtime_state=runtime,
        battery_full_charge_store=store,
    )
    controller.run_startup_ac_mode_reconcile_once = Mock()
    controller.set_output_limit = Mock(return_value=True)
    return controller, dev


def run(
    controller,
    telemetry,
    clock,
    assist_enabled=True,
    state_gate=True,
    ac_mode=True,
):
    with (
        patch("ems.controller.fetch_all_devices", return_value=[telemetry]),
        patch("ems.controller.cfg.SYSTEM_ENABLED", True),
        patch("ems.controller.cfg.MAX_TOTAL_POWER", 800),
        patch("ems.controller.cfg.MAX_DEVICE_POWER", 800),
        patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 0),
        patch("ems.controller.cfg.DEADBAND", 10),
        patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 10),
        patch("ems.controller.cfg.SIMULATION_MODE", False),
        patch("ems.controller.cfg.ARGS", SimpleNamespace(replay=None)),
        patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=state_gate,
        ),
        patch(
            "ems.controller.cfg.WINTER_CONFIG",
            {**cfg.WINTER_DEFAULTS, "enabled": False},
        ),
        patch("time.monotonic", clock),
        configure_assist(
            enabled=assist_enabled,
            assist_start_soc=80,
            enable_ac_charge_mode=ac_mode,
            ac_charge_power=POWER,
        ),
    ):
        controller.run_once()


def charging(limit=POWER):
    return state(
        soc=88, max_soc=100, ac_mode=1, ac_status=2, input_limit_w=limit
    )


def left():
    return state(soc=88, max_soc=100, ac_mode=2, input_limit_w=0)


def exits(controller):
    return [
        call
        for call in controller.set_output_limit.call_args_list
        if call.kwargs.get("charge_exit") == CHARGE_EXIT_FINAL
    ]


def switch_off(runtime, switch):
    if switch == "system":
        runtime.system["enabled"] = False
    else:
        runtime.devices["WR1"] = {"enabled": False}


def started_assist(tmp_path, seen=POWER):
    """The assist starts; the device then sits in its charge at ``seen`` W."""

    store = assist_due(tmp_path)
    runtime = Runtime()
    controller, dev = controller_for(store, runtime)
    clock = Clock()
    run(
        controller, state(soc=85, max_soc=90, ac_mode=2, input_limit_w=0), clock
    )
    assert {"acMode": 1} in posted_properties(dev), "the assist never started"
    assert {"inputLimit": POWER} in posted_properties(dev)
    if seen is not None:
        clock.now += 5
        run(controller, charging(seen), clock)
    dev.session.post.reset_mock()
    return store, runtime, controller, dev, clock


@pytest.mark.parametrize("switch", ["system", "device"])
def test_switching_off_ends_the_assist_charge_with_one_exit(tmp_path, switch):
    store, runtime, controller, dev, clock = started_assist(tmp_path)
    switch_off(runtime, switch)

    for _ in range(3):
        clock.now += 5
        run(controller, charging(), clock)

    assert len(exits(controller)) == 1
    assert exits(controller)[0].args[1] == 0
    assert posted_properties(dev) == []
    assert store.get_device_state("WR1")["full_charge_assist_active"] is True


def test_a_device_that_left_the_charge_is_sent_nothing_more(tmp_path):
    _, runtime, controller, dev, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(
            controller,
            state(soc=88, max_soc=100, ac_mode=2, input_limit_w=0),
            clock,
        )

    assert len(exits(controller)) == 1
    assert posted_properties(dev) == []


def test_a_device_that_charges_on_is_sent_the_exit_again_after_the_window(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)

    clock.now += EXIT_RESEND_SECONDS - 1
    run(controller, charging(), clock)
    assert len(exits(controller)) == 1

    clock.now += 1
    run(controller, charging(), clock)
    assert len(exits(controller)) == 2


def test_the_assist_goes_on_once_control_is_back(tmp_path):
    store, runtime, controller, dev, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)
    run(
        controller,
        state(soc=88, max_soc=100, ac_mode=2, input_limit_w=0),
        clock,
    )

    runtime.system["enabled"] = True
    clock.now += 5
    run(
        controller,
        state(soc=88, max_soc=100, ac_mode=2, input_limit_w=0),
        clock,
    )

    assert {"acMode": 1} in posted_properties(dev)
    assert {"inputLimit": POWER} in posted_properties(dev)
    assert store.get_device_state("WR1")["full_charge_assist_active"] is True


@pytest.mark.parametrize("switch", ["system", "device"])
def test_no_assist_starts_while_control_is_off(tmp_path, switch):
    store = assist_due(tmp_path)
    runtime = Runtime()
    switch_off(runtime, switch)
    controller, dev = controller_for(store, runtime)

    run(
        controller,
        state(soc=85, max_soc=90, ac_mode=2, input_limit_w=0),
        Clock(),
    )

    assert posted_properties(dev) == []
    assert (
        store.get_device_state("WR1")["full_charge_assist_active"] is not True
    )
    assert "full_charge_assist_started" not in event_types(store)


def test_no_exit_while_control_is_on(tmp_path):
    _, _, controller, _, clock = started_assist(tmp_path)

    for _ in range(4):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert exits(controller) == []


def test_a_device_put_back_after_it_left_is_left_alone(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)
    clock.now += 5
    run(controller, left(), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_a_failed_read_changes_nothing(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)
    clock.now += 5
    run(controller, None, clock)

    clock.now += EXIT_RESEND_SECONDS
    run(controller, charging(), clock)

    assert len(exits(controller)) == 2


def test_a_device_without_state_reconciliation_gets_no_assist_exit(tmp_path):
    _, runtime, controller, dev, clock = started_assist(tmp_path)
    dev.supports_state_reconciliation = False
    switch_off(runtime, "system")

    run(controller, charging(), clock)

    assert exits(controller) == []


def test_a_device_that_clamps_the_setpoint_gets_the_exit_again_at_its_clamp(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=150)
    switch_off(runtime, "system")
    run(controller, charging(150), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(150), clock)

    assert len(exits(controller)) == 2


def test_a_device_that_does_not_report_its_input_limit_gets_the_exit_again(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=0)
    switch_off(runtime, "system")
    run(controller, charging(0), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(0), clock)

    assert len(exits(controller)) == 2


def test_an_exit_the_transport_could_not_deliver_goes_out_again_at_once(
    tmp_path, caplog
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    controller.set_output_limit.return_value = False
    with caplog.at_level(logging.WARNING):
        run(controller, charging(), clock)
    assert "event=ac_charge_end_failed" in caplog.text
    assert "delivered=False" in caplog.text

    controller.set_output_limit.return_value = True
    clock.now += 5
    run(controller, charging(), clock)
    clock.now += 5
    run(controller, charging(), clock)

    assert len(exits(controller)) == 2


def test_an_assist_aborted_while_off_keeps_the_exit_for_its_charge(tmp_path):
    store, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)

    for _ in range(2):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock, assist_enabled=False)

    assert store.get_device_state("WR1")["full_charge_assist_active"] is False
    assert len(exits(controller)) == 3


def test_an_unreachable_device_is_sent_nothing_on_its_last_report(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, None, clock)

    assert len(exits(controller)) == 1


@pytest.mark.parametrize("limit", [POWER, 450, 0])
def test_a_charge_the_ems_could_not_hold_gets_no_exit(tmp_path, limit):
    store = assist_due(tmp_path)
    runtime = Runtime()
    controller, _ = controller_for(store, runtime)
    clock = Clock()
    run(
        controller,
        state(soc=85, max_soc=90, ac_mode=2, input_limit_w=0),
        clock,
        state_gate=False,
    )
    clock.now += 5
    run(controller, charging(limit), clock, state_gate=False)
    assert store.get_device_state("WR1")["full_charge_assist_active"] is True

    switch_off(runtime, "system")
    run(controller, charging(limit), clock, state_gate=False)

    assert exits(controller) == []


def test_an_assist_without_ac_charging_has_no_charge_to_end(tmp_path):
    store = assist_due(tmp_path)
    runtime = Runtime()
    controller, _ = controller_for(store, runtime)
    clock = Clock()
    run(
        controller,
        state(soc=85, max_soc=90, ac_mode=2, input_limit_w=0),
        clock,
        ac_mode=False,
    )
    clock.now += 5
    run(controller, charging(), clock, ac_mode=False)

    switch_off(runtime, "system")
    run(controller, charging(), clock, ac_mode=False)

    assert exits(controller) == []


def completed(tmp_path, seen=POWER):
    store, runtime, controller, dev, clock = started_assist(tmp_path, seen=seen)
    clock.now += 5
    full = state(
        soc=100,
        max_soc=100,
        soc_limit=1,
        ac_mode=1,
        ac_status=2,
        input_limit_w=seen,
    )
    run(controller, full, clock)
    assert store.get_device_state("WR1")["full_charge_assist_active"] is False
    assert {"acMode": 2} in posted_properties(dev), "the restore never went out"
    return runtime, controller, clock


def test_an_assist_whose_restore_was_taken_leaves_no_charge_to_end(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    clock.now += 5
    run(
        controller,
        state(soc=100, max_soc=100, soc_limit=1, ac_mode=2, input_limit_w=0),
        clock,
    )

    switch_off(runtime, "system")
    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert exits(controller) == []


def test_an_assist_whose_restore_was_not_taken_is_ended_when_control_goes_off(
    tmp_path,
):
    runtime, controller, clock = completed(tmp_path)
    switch_off(runtime, "system")
    clock.now += 5
    run(controller, charging(), clock)
    assert exits(controller) == [], (
        "the restore was the exit; its window is still open"
    )
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_a_restore_the_device_took_needs_no_exit_after_it(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    switch_off(runtime, "system")
    clock.now += 5

    for _ in range(3):
        run(controller, left(), clock)
        clock.now += EXIT_RESEND_SECONDS

    assert exits(controller) == []


def test_a_second_switch_off_after_the_assist_went_on_ends_it_again(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)
    clock.now += 5
    run(controller, left(), clock)

    runtime.system["enabled"] = True
    clock.now += 5
    run(controller, charging(), clock)
    switch_off(runtime, "system")
    clock.now += 5
    run(controller, charging(), clock)

    assert len(exits(controller)) == 2


def test_a_device_unreachable_while_control_was_on_still_gets_the_exit(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    clock.now += 5
    run(controller, None, clock)
    switch_off(runtime, "system")
    clock.now += 5

    run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_an_exit_the_write_gate_held_back_waits_its_window(tmp_path, caplog):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    controller.set_output_limit.return_value = withheld(0)

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            run(controller, charging(), clock)
            clock.now += 5

    assert len(exits(controller)) == 1
    assert "event=ac_charge_end_withheld" in caplog.text
    assert "event=ac_charge_ended_on_disable" not in caplog.text


def test_an_exit_the_device_refused_waits_its_window(tmp_path, caplog):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    controller.set_output_limit.return_value = failed(
        0, reason="http_write_failed"
    )

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            run(controller, charging(), clock)
            clock.now += 5

    assert len(exits(controller)) == 1
    assert "event=ac_charge_end_failed" in caplog.text
    assert "delivered=True" in caplog.text
    assert "event=ac_charge_ended_on_disable" not in caplog.text


@pytest.mark.parametrize(
    "seen", [None, 0], ids=["never seen in AC input", "seen at zero"]
)
def test_a_clamp_shown_before_the_exit_is_the_assist_s(tmp_path, seen):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=seen)
    switch_off(runtime, "system")
    run(controller, charging(150), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(150), clock)

    assert len(exits(controller)) == 2


def test_the_written_setpoint_counts_beside_a_clamp(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=150)
    switch_off(runtime, "system")
    run(controller, charging(150), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(), clock)

    assert len(exits(controller)) == 2


def test_a_device_without_an_input_limit_that_left_is_not_fought_for(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=None)
    switch_off(runtime, "system")
    run(controller, charging(0), clock)
    clock.now += 5
    run(controller, left(), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(0), clock)

    assert len(exits(controller)) == 1


def restarted(controller, runtime):
    """A new EMS process over the same store and the same device."""

    fresh = EMSController(
        devices=controller.devices,
        shelly=ShellyStub(),
        sleep_enabled=False,
        runtime_state=runtime,
        battery_full_charge_store=controller.battery_full_charge_store,
    )
    fresh.run_startup_ac_mode_reconcile_once = Mock()
    fresh.set_output_limit = Mock(return_value=True)
    return fresh


def test_a_state_gate_shut_while_on_releases_the_charge(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    clock.now += 5
    run(controller, charging(), clock, state_gate=False)
    switch_off(runtime, "system")
    clock.now += 5

    run(controller, charging(), clock, state_gate=False)

    assert exits(controller) == []


def test_a_restore_stuck_after_the_device_left_ac_input_keeps_no_charge(
    tmp_path,
):
    runtime, controller, clock = completed(tmp_path)
    store = controller.battery_full_charge_store
    controller.devices[0].session.post.return_value = SimpleNamespace(
        status_code=500
    )
    store.update_device_state(
        "WR1", datetime.now(timezone.utc), restore_pending=True
    )
    clock.now += 5
    run(
        controller,
        state(soc=100, max_soc=100, soc_limit=1, ac_mode=2, input_limit_w=0),
        clock,
    )
    switch_off(runtime, "system")

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert exits(controller) == []


def test_a_device_unreachable_during_the_restore_still_gets_the_exit(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    clock.now += 5
    run(controller, None, clock)
    switch_off(runtime, "system")
    clock.now += 5
    run(controller, charging(), clock)
    assert exits(controller) == [], "the restore's window is still open"
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_a_restart_during_the_restore_still_ends_the_charge(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    fresh = restarted(controller, runtime)
    clock.now += 5
    run(fresh, charging(), clock)
    switch_off(runtime, "system")
    clock.now += EXIT_RESEND_SECONDS

    run(fresh, charging(), clock)

    assert len(exits(fresh)) == 1


def test_a_device_at_zero_that_charges_on_is_sent_the_exit_again(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=0)
    switch_off(runtime, "system")
    run(controller, charging(0), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(0), clock)

    assert len(exits(controller)) == 2


def test_a_restore_of_the_soc_window_alone_keeps_no_charge(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    store = controller.battery_full_charge_store
    controller.devices[0].session.post.return_value = SimpleNamespace(
        status_code=500
    )
    store.update_device_state(
        "WR1", datetime.now(timezone.utc), restore_pending=True
    )
    clock.now += 5
    run(
        controller,
        state(soc=100, max_soc=100, soc_limit=1, ac_mode=2, input_limit_w=0),
        clock,
    )
    clock.now += 5
    run(controller, charging(), clock)
    switch_off(runtime, "system")
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(), clock)

    assert exits(controller) == []


def test_an_unreachable_spell_across_the_switch_off_does_not_forfeit_the_exit(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=0)
    switch_off(runtime, "system")
    for _ in range(12):
        run(controller, None, clock)
        clock.now += 300

    run(controller, charging(0), clock)

    assert len(exits(controller)) == 1


def test_a_charge_at_another_setpoint_after_the_final_exit_is_left_alone(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=None)
    switch_off(runtime, "system")
    run(controller, left(), clock)
    clock.now += 5
    run(controller, charging(100), clock)
    clock.now += 5
    run(controller, left(), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(100), clock)

    assert len(exits(controller)) == 1


def test_a_resumed_assist_on_a_clamping_device_is_ended_again(tmp_path):
    _, runtime, controller, dev, clock = started_assist(tmp_path, seen=150)
    clock.now += 90
    run(controller, charging(150), clock)
    switch_off(runtime, "system")
    clock.now += 5
    run(controller, charging(150), clock)
    clock.now += 5
    run(controller, left(), clock)

    runtime.system["enabled"] = True
    clock.now += 5
    run(controller, left(), clock)
    assert {"acMode": 1} in posted_properties(dev)
    switch_off(runtime, "system")
    clock.now += 5
    run(controller, charging(150), clock)

    assert len(exits(controller)) == 2


def out_of_charge_during_restore(kind):
    if kind == "no acMode":
        return state(
            soc=100,
            max_soc=100,
            soc_limit=1,
            ac_mode=0,
            ac_status=0,
            input_limit_w=0,
        )
    return state(
        soc=100,
        max_soc=100,
        soc_limit=1,
        ac_mode=1,
        ac_status=1,
        input_limit_w=0,
    )


@pytest.mark.parametrize("kind", ["no acMode", "idle in AC input"])
@pytest.mark.parametrize("extra", [0, 1, 2, 3])
def test_a_device_out_of_the_charge_during_the_restore_keeps_no_charge(
    tmp_path, kind, extra
):
    runtime, controller, clock = completed(tmp_path)
    for _ in range(1 + extra):
        clock.now += 5
        run(controller, out_of_charge_during_restore(kind), clock)
    switch_off(runtime, "system")

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert exits(controller) == []


def empty_in_ac_input():
    return state(soc=88, max_soc=100, ac_mode=1, ac_status=1, input_limit_w=0)


@pytest.mark.parametrize("limit", [POWER, 450, 800])
def test_a_charge_the_device_shows_at_the_switch_off_is_ended_at_most_three_times(
    tmp_path, limit
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")

    for _ in range(FOUND_EXIT_ATTEMPTS + 2):
        run(controller, charging(limit), clock)
        clock.now += EXIT_RESEND_SECONDS

    assert len(exits(controller)) == FOUND_EXIT_ATTEMPTS


def test_a_device_out_of_the_charge_at_the_switch_off_gets_the_one_final_exit(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")

    for _ in range(3):
        run(controller, left(), clock)
        clock.now += EXIT_RESEND_SECONDS

    assert len(exits(controller)) == 1


def test_a_device_left_empty_in_ac_input_by_the_exit_is_sent_nothing_more(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, empty_in_ac_input(), clock)

    assert len(exits(controller)) == 1


def test_a_clamp_first_shown_after_the_exit_is_someone_else_s(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=0)
    switch_off(runtime, "system")
    run(controller, charging(0), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(150), clock)

    assert len(exits(controller)) == 1


def test_a_setpoint_the_assist_never_wrote_after_the_exit_releases_the_charge(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)
    clock.now += 5
    run(controller, charging(450), clock)

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_a_higher_charge_still_shown_from_before_the_assist_is_its_own(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=450)
    switch_off(runtime, "system")
    run(controller, charging(450), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(450), clock)

    assert len(exits(controller)) == 2


def test_an_on_period_puts_the_assist_s_charge_back_on_record(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    run(controller, charging(), clock)
    clock.now += 5
    run(controller, left(), clock)
    runtime.system["enabled"] = True
    clock.now += 5
    run(controller, None, clock)
    switch_off(runtime, "system")
    clock.now += 600

    run(controller, charging(), clock)

    assert len(exits(controller)) == 2


def test_an_unreachable_device_is_written_nothing_until_it_answers(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    for _ in range(3):
        run(controller, None, clock)
        clock.now += 5
    assert exits(controller) == []

    run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_the_exit_count_starts_again_with_each_switch_off(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    for _ in range(FOUND_EXIT_ATTEMPTS):
        run(controller, charging(), clock)
        clock.now += EXIT_RESEND_SECONDS
    runtime.system["enabled"] = True
    run(controller, charging(), clock)
    switch_off(runtime, "system")
    clock.now += 5

    run(controller, charging(), clock)

    assert len(exits(controller)) == FOUND_EXIT_ATTEMPTS + 1


def test_a_restart_during_the_restore_with_the_device_unreachable_ends_it_when_it_answers(
    tmp_path,
):
    runtime, controller, clock = completed(tmp_path)
    fresh = restarted(controller, runtime)
    clock.now += 5
    run(fresh, None, clock)
    switch_off(runtime, "system")
    clock.now += 5

    run(fresh, charging(), clock)

    assert len(exits(fresh)) == 1


def test_the_restore_keeps_the_assist_s_setpoint_on_record(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    fresh = restarted(controller, runtime)
    clock.now += 5
    run(fresh, charging(0), clock)
    switch_off(runtime, "system")
    clock.now += EXIT_RESEND_SECONDS

    run(fresh, charging(POWER), clock)

    assert len(exits(fresh)) == 1


@pytest.mark.parametrize(
    "switch,reason",
    [("system", "control_disabled"), ("device", "device_disabled")],
)
def test_the_exit_is_logged_with_the_charge_and_the_switch(
    tmp_path, caplog, switch, reason
):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, switch)

    with caplog.at_level(logging.INFO):
        run(controller, charging(), clock)

    ended = [
        r.getMessage()
        for r in caplog.records
        if "ac_charge_ended_on_disable" in r.getMessage()
    ]
    assert len(ended) == 1
    assert "charge=battery_full_charge_assist" in ended[0]
    assert f"reason={reason}" in ended[0]


def clamped_charging():
    return charging(150)


def test_a_clamping_device_that_ignored_the_restore_is_ended_at_its_clamp(
    tmp_path,
):
    runtime, controller, clock = completed(tmp_path, seen=150)
    clock.now += 5
    run(controller, clamped_charging(), clock)
    switch_off(runtime, "system")

    for _ in range(5):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, clamped_charging(), clock)

    assert len(exits(controller)) == FOUND_EXIT_ATTEMPTS


def test_a_clamping_device_whose_aborted_assist_could_not_restore_is_ended(
    tmp_path,
):
    _, runtime, controller, dev, clock = started_assist(tmp_path, seen=150)
    dev.session.post.return_value = SimpleNamespace(status_code=500)
    clock.now += 5
    run(controller, clamped_charging(), clock, assist_enabled=False)
    switch_off(runtime, "system")

    for _ in range(5):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, clamped_charging(), clock, assist_enabled=False)

    assert len(exits(controller)) == FOUND_EXIT_ATTEMPTS


def test_a_restart_during_the_restore_on_a_clamping_device_still_ends_it(
    tmp_path,
):
    runtime, controller, clock = completed(tmp_path, seen=150)
    fresh = restarted(controller, runtime)
    clock.now += 5
    run(fresh, clamped_charging(), clock)
    switch_off(runtime, "system")

    for _ in range(5):
        clock.now += EXIT_RESEND_SECONDS
        run(fresh, clamped_charging(), clock)

    assert len(exits(fresh)) == FOUND_EXIT_ATTEMPTS


def test_the_window_runs_from_the_last_restore_the_reconciler_wrote(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    clock.now += 20
    run(controller, charging(), clock)
    switch_off(runtime, "system")
    clock.now += 15
    run(controller, charging(), clock)
    assert exits(controller) == []
    clock.now += 15

    run(controller, charging(), clock)

    assert len(exits(controller)) == 1


def test_a_restart_while_only_the_soc_window_is_restored_holds_no_charge(
    tmp_path,
):
    runtime, controller, clock = completed(tmp_path)
    store = controller.battery_full_charge_store
    clock.now += 5
    run(controller, left(), clock)
    controller.devices[0].session.post.return_value = SimpleNamespace(
        status_code=500
    )
    store.update_device_state(
        "WR1", datetime.now(timezone.utc), restore_pending=True
    )
    fresh = restarted(controller, runtime)
    clock.now += 5
    run(fresh, charging(), clock)
    switch_off(runtime, "system")

    for _ in range(3):
        clock.now += EXIT_RESEND_SECONDS
        run(fresh, charging(), clock)

    assert exits(fresh) == []


def test_a_clamp_seen_while_the_assist_ran_still_counts_after_a_report_without_one(
    tmp_path,
):
    _, runtime, controller, _, clock = started_assist(tmp_path, seen=150)
    switch_off(runtime, "system")
    run(controller, charging(0), clock)
    clock.now += EXIT_RESEND_SECONDS

    run(controller, charging(150), clock)

    assert len(exits(controller)) == 2


def test_the_exit_count_starts_again_after_a_restore(tmp_path):
    _, runtime, controller, _, clock = started_assist(tmp_path)
    switch_off(runtime, "system")
    for _ in range(FOUND_EXIT_ATTEMPTS):
        run(controller, charging(), clock)
        clock.now += EXIT_RESEND_SECONDS
    runtime.system["enabled"] = True
    full = state(
        soc=100,
        max_soc=100,
        soc_limit=1,
        ac_mode=1,
        ac_status=2,
        input_limit_w=POWER,
    )
    run(controller, full, clock)
    switch_off(runtime, "system")

    for _ in range(FOUND_EXIT_ATTEMPTS + 1):
        clock.now += EXIT_RESEND_SECONDS
        run(controller, charging(), clock)

    assert len(exits(controller)) == 2 * FOUND_EXIT_ATTEMPTS


def test_a_restore_that_never_reached_the_device_still_counts_as_its_exit(tmp_path):
    runtime, controller, clock = completed(tmp_path)
    controller.devices[0].session.post.side_effect = ConnectionError("offline")
    clock.now += 20
    run(controller, charging(), clock)
    switch_off(runtime, "system")
    clock.now += 15
    run(controller, charging(), clock)
    assert exits(controller) == []
    clock.now += 15

    run(controller, charging(), clock)

    assert len(exits(controller)) == 1
