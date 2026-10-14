# SPDX-License-Identifier: AGPL-3.0-or-later
"""Winter mode leaves a ``minSoc`` the report does not carry unmanaged (K18).

``parse_device`` read a report without ``minSoc`` as 0 %. Right after a
restart, with no winter target remembered, winter mode adopted the configured
floor for that 0, or stepped up from it on an adjustment day, and the reconcile
wrote the result over the reserve the ramp had built. A missing ``minSoc`` is
now unknown, as a missing ``acMode`` is (K10): winter plans nothing for the
device and the reconcile writes no ``minSoc`` until the report carries one.
"""

import contextlib
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.clients import parse_device, zero_device_state
from ems.controller import EMSController
from ems.simulation import state_from_trace_device
from test_write_gates import RuntimeStateStub, ShellyStub, device

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

DARK = datetime(2026, 11, 2, 6, 0)
MORNING = datetime(2026, 11, 2, 8, 0)
LATE_MORNING = datetime(2026, 11, 2, 10, 0)
SUMMER = datetime(2026, 7, 2, 10, 0)


def _report(pv, **properties):
    return {
        "electricLevel": 60,
        "socSet": 1000,
        "solarInputPower": pv,
        "outputLimit": 0,
        "packNum": 1,
        "acMode": 2,
        "acStatus": 1,
        "smartMode": 1,
        **properties,
    }


def _unreported(pv=0):
    return parse_device({"properties": _report(pv)})


def _reported(min_soc, pv=0):
    return parse_device({"properties": _report(pv, minSoc=min_soc * 10)})


@contextlib.contextmanager
def _winter():
    with patch.object(cfg, "WINTER_CONFIG", dict(cfg.WINTER_DEFAULTS)), patch.object(
        cfg, "ZENDURE_CONFIG", [{"name": "WR1"}]
    ), patch.object(cfg, "winter_feature_enabled", lambda runtime: True), patch.object(
        cfg, "state_reconciliation_writes_allowed", lambda gate: True
    ):
        yield


def _controller():
    controlled = device("WR1")
    controlled.session.post.return_value = SimpleNamespace(status_code=200)
    controller = EMSController(
        devices=[controlled],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    return controller, controlled


def _reconcile(controller, controlled, state, now):
    with _winter():
        controller.reconcile_device_state_limits(
            controlled, state, cfg.winter_month_active(now), now
        )


def _min_soc_written(controlled):
    return [
        call.kwargs["json"]["properties"]["minSoc"]
        for call in controlled.session.post.call_args_list
        if "minSoc" in call.kwargs["json"]["properties"]
    ]


def test_a_report_without_min_soc_says_so():
    assert _reported(30).min_soc_reported is True
    assert _reported(0).min_soc_reported is True
    assert _unreported().min_soc_reported is False
    assert zero_device_state().min_soc_reported is False
    assert state_from_trace_device({"soc": 50}).min_soc_reported is False
    assert state_from_trace_device({"soc": 50, "min_soc": 15}).min_soc_reported is True
    assert state_from_trace_device({"soc": 50, "minSoc": 150}).min_soc_reported is True


def test_after_a_restart_the_floor_is_not_written_over_an_unknown_min_soc():
    """Daylight start without a remembered target: the floor was adopted for the 0."""

    controller, controlled = _controller()

    _reconcile(controller, controlled, _unreported(pv=300), LATE_MORNING)

    assert _min_soc_written(controlled) == []


def test_after_a_restart_no_step_is_taken_from_an_unknown_min_soc():
    """Dark start, then the morning: the step went up from the floor, not the reserve."""

    controller, controlled = _controller()

    _reconcile(controller, controlled, _unreported(pv=0), DARK)
    _reconcile(controller, controlled, _unreported(pv=300), MORNING)

    assert _min_soc_written(controlled) == []
    assert controller.winter.device("WR1").step_date is None


def test_summer_writes_no_reset_over_an_unknown_min_soc():
    controller, controlled = _controller()

    _reconcile(controller, controlled, _unreported(pv=300), SUMMER)

    assert _min_soc_written(controlled) == []


def test_the_max_soc_is_still_reconciled():
    controller, controlled = _controller()
    state = parse_device({"properties": _report(300, socSet=900)})

    _reconcile(controller, controlled, state, LATE_MORNING)

    written = [call.kwargs["json"]["properties"] for call in controlled.session.post.call_args_list]
    assert written == [{"socSet": 1000}]


def test_once_the_min_soc_is_known_winter_takes_the_device_up_as_first_seen():
    """Back in the report, the plan starts as for a device first seen now: in
    daylight before the step hour a step may already have been taken today, so
    none is, and the reserve the device reports is kept."""

    controller, controlled = _controller()

    _reconcile(controller, controlled, _unreported(pv=0), DARK)
    _reconcile(controller, controlled, _reported(30, pv=300), MORNING)

    assert _min_soc_written(controlled) == []
    assert controller.winter.device("WR1").target == 30


def test_an_unknown_min_soc_is_said_once_until_it_is_known_again(caplog):
    controller, controlled = _controller()

    with caplog.at_level(logging.DEBUG):
        for state, now in (
            (_unreported(pv=0), DARK),
            (_unreported(pv=0), DARK),
            (_reported(30, pv=0), DARK),
            (_unreported(pv=0), DARK),
        ):
            _reconcile(controller, controlled, state, now)

    said = [record for record in caplog.records if "event=winter_min_soc_unknown" in record.getMessage()]
    assert [record.levelno for record in said] == [logging.WARNING, logging.WARNING]
