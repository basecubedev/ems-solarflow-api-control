# SPDX-License-Identifier: AGPL-3.0-or-later
"""A failing control cycle no longer ends the EMS process.

An exception in ``run_once`` used to end the process, and the shutdown returned
a charging inverter on the way out. As the owner decided (K8 in
docs/developer/review-coverage.md), the cycle now stops where it raised: what
it wrote before that point stands, nothing after it is written, the loop waits
its interval and tries again, and the log says so without flooding.
"""

import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.controller import CYCLE_FAILURE_WARN_EVERY, EMSController
from test_write_gates import RuntimeStateStub, ShellyStub, device, state

pytestmark = [pytest.mark.unit, pytest.mark.power_control]


class _Cycles:
    """``run_once`` that raises for the first ``failures`` calls, then succeeds."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError(f"broken cycle {self.calls}")


def _controller(monkeypatch, failures, *, sleep_enabled=True):
    controller = EMSController(
        [], SimpleNamespace(get_power=lambda: 0), sleep_enabled=sleep_enabled
    )
    cycles = _Cycles(failures)
    monkeypatch.setattr(controller, "run_once", cycles)
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return controller, cycles, slept


def test_a_failing_cycle_is_survived_and_waits_its_interval(monkeypatch):
    controller, _, slept = _controller(monkeypatch, failures=1)

    assert controller.run_guarded() is False

    assert controller.cycle_failures == 1
    assert slept == [controller._loop_interval_after_failure()]


def test_the_cycle_after_a_failure_runs_and_is_reported_as_recovered(monkeypatch, caplog):
    controller, cycles, _ = _controller(monkeypatch, failures=2)

    with caplog.at_level(logging.INFO):
        results = [controller.run_guarded() for _ in range(3)]

    assert results == [False, False, True]
    assert cycles.calls == 3
    assert controller.cycle_failures == 0
    assert "event=control_cycle_recovered failed_cycles=2" in caplog.text


def test_a_lasting_failure_logs_its_traceback_once_and_then_a_warning_a_minute(
    monkeypatch, caplog
):
    controller, _, _ = _controller(monkeypatch, failures=CYCLE_FAILURE_WARN_EVERY * 2)

    with caplog.at_level(logging.DEBUG):
        for _ in range(CYCLE_FAILURE_WARN_EVERY * 2):
            controller.run_guarded()

    failed = [record for record in caplog.records if "control_cycle_failed" in record.getMessage()]
    assert [record.levelno for record in failed] == [logging.ERROR, logging.WARNING, logging.WARNING]
    assert failed[0].exc_info is not None
    assert f"consecutive={CYCLE_FAILURE_WARN_EVERY * 2}" in failed[-1].getMessage()


def test_an_interrupt_still_ends_the_loop(monkeypatch):
    controller, _, _ = _controller(monkeypatch, failures=0)

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(controller, "run_once", interrupted)

    with pytest.raises(KeyboardInterrupt):
        controller.run_guarded()


def _run_a_cycle_that_raises_on_wr2(devices):
    controller = EMSController(
        devices=devices,
        shelly=ShellyStub(1800),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    controller.run_startup_ac_mode_reconcile_once = Mock()
    written = []

    def set_output_limit(dev, value, charge_exit=None):
        if dev.name == "WR2":
            raise RuntimeError("transport bug on WR2")
        written.append(dev.name)
        return True

    controller.set_output_limit = set_output_limit
    with patch(
        "ems.controller.fetch_all_devices", return_value=[state(), state(), state()]
    ), patch.multiple(
        "ems.controller.cfg",
        SYSTEM_ENABLED=True,
        MAX_TOTAL_POWER=2400,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=0,
        DEADBAND=10,
        SOC_RECONCILE_INTERVAL=0,
    ):
        result = controller.run_guarded()
    return controller, result, written


def test_a_failed_cycle_stops_where_it_raised():
    """What the cycle wrote before the exception stands; nothing after it is written."""

    controller, result, written = _run_a_cycle_that_raises_on_wr2(
        [device("WR1"), device("WR2"), device("WR3")]
    )

    assert result is False
    assert controller.cycle_failures == 1
    assert written == ["WR1"]


def test_a_failed_cycle_leaves_a_charge_it_did_not_reach_running(caplog):
    """Unlike the crash it replaces, a failed cycle returns no charge."""

    charging = device("WR3")
    charging.charge_commanded = True

    with caplog.at_level(logging.INFO):
        _, result, written = _run_a_cycle_that_raises_on_wr2(
            [device("WR1"), device("WR2"), charging]
        )

    assert result is False
    assert written == ["WR1"]
    assert "ac_charge_release" not in caplog.text
