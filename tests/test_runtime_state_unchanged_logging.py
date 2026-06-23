# SPDX-License-Identifier: AGPL-3.0-or-later
import logging

from ems.controller import (
    RUNTIME_STATE_UNCHANGED_INFO_INTERVAL_S,
    EMSController,
)


def make_controller():
    return EMSController(devices=[], shelly=None, sleep_enabled=False)


def _unchanged_records(caplog):
    return [
        record
        for record in caplog.records
        if "event=runtime_device_state_unchanged" in record.getMessage()
    ]


def test_unchanged_state_logs_info_once_then_debug(caplog):
    controller = make_controller()
    fields = {"device": "WR1", "field": "gridOffMode"}

    caplog.set_level(logging.DEBUG)
    for _ in range(4):
        controller.log_runtime_state_unchanged(fields)

    records = _unchanged_records(caplog)
    info = [r for r in records if r.levelno == logging.INFO]
    debug = [r for r in records if r.levelno == logging.DEBUG]

    assert len(info) == 1
    assert len(debug) == 3


def test_unchanged_state_emits_info_again_after_throttle_window(caplog):
    controller = make_controller()
    fields = {"device": "WR1", "field": "gridOffMode"}

    caplog.set_level(logging.DEBUG)
    controller.log_runtime_state_unchanged(fields)
    controller._unchanged_log_times[("WR1", "gridOffMode")] -= (
        RUNTIME_STATE_UNCHANGED_INFO_INTERVAL_S + 1
    )
    controller.log_runtime_state_unchanged(fields)

    info = [r for r in _unchanged_records(caplog) if r.levelno == logging.INFO]
    assert len(info) == 2
