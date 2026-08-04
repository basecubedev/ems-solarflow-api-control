# SPDX-License-Identifier: AGPL-3.0-or-later
import logging

import pytest

from ems.controller import EMSController

pytestmark = [
    pytest.mark.unit,
]


def make_controller():
    return EMSController(devices=[], shelly=None, sleep_enabled=False)


def _unchanged_records(caplog):
    return [
        record
        for record in caplog.records
        if "event=runtime_device_state_unchanged" in record.getMessage()
    ]


def test_unchanged_state_logs_debug_only(caplog):
    controller = make_controller()
    fields = {"device": "WR1", "field": "gridOffMode"}

    caplog.set_level(logging.DEBUG)
    for _ in range(4):
        controller.log_runtime_state_unchanged(fields)

    records = _unchanged_records(caplog)
    assert len(records) == 4
    assert all(record.levelno == logging.DEBUG for record in records)
    assert not [r for r in records if r.levelno == logging.INFO]
