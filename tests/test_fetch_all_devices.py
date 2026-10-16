# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device read that raises is named, and only an error is absorbed (K-low).

``fetch_all_devices`` caught every exception a device's ``fetch`` raised with
a bare ``except: pass``: the device went offline for the cycle with no line in
the log saying which device or why, and an exit raised in a reader thread
(``SystemExit``, ``KeyboardInterrupt``) was swallowed with it. An error now
leaves that device's slot empty, as before, and is logged with the device's
name; anything that is not an ``Exception`` reaches the caller.
"""

import logging
from types import SimpleNamespace

import pytest

from ems.clients import fetch_all_devices

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]


def _device(name, fetch):
    return SimpleNamespace(name=name, fetch=fetch)


def _raise(error):
    def fetch():
        raise error

    return fetch


def test_a_failing_read_leaves_its_slot_empty_and_names_the_device(caplog):
    answered = object()

    with caplog.at_level(logging.DEBUG):
        states = fetch_all_devices([
            _device("WR1", _raise(RuntimeError("report parser broke"))),
            _device("WR2", lambda: answered),
        ])

    assert states == [None, answered]
    said = [
        record for record in caplog.records
        if "event=device_fetch_failed" in record.getMessage()
    ]
    assert len(said) == 1
    assert said[0].levelno == logging.WARNING
    assert "device=WR1" in said[0].getMessage()
    assert "report parser broke" in said[0].getMessage()


@pytest.mark.parametrize("exit_signal", [SystemExit(3), KeyboardInterrupt()])
def test_an_exit_raised_in_a_read_is_not_swallowed(exit_signal):
    with pytest.raises(type(exit_signal)):
        fetch_all_devices([
            _device("WR1", _raise(exit_signal)),
            _device("WR2", lambda: None),
        ])


def test_no_devices_read_nothing():
    assert fetch_all_devices([]) == []
