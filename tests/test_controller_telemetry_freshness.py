# SPDX-License-Identifier: AGPL-3.0-or-later
"""When a device reading still counts as a reading.

``telemetry_max_age_seconds`` is the window the control loop already calculates
inside: while a cached state is younger than it, the loop keeps using it. The
energy statistics ask the same question with the same constant, so a single
failed read is not a hole in the statistics either -- only telemetry that has
aged out of the window is. Writing to a silent device stops sooner, after one
failed read, and that is deliberately a different question.
"""
from unittest.mock import patch

import pytest

from ems.controller import EMSController

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]


def _controller(last_seen, *, online=None):
    # Without a project directory the constructor builds no full-charge store,
    # so the test never opens the installation's state database.
    with patch("ems.config.BASE_DIR", ""):
        controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    assert controller.battery_full_charge_store is None
    controller.last_seen = dict(last_seen)
    controller.device_online = dict(online or {})
    return controller


def _window(seconds):
    return patch.dict(
        "ems.config.OUTPUT_CONTROL_CONFIG",
        {"telemetry_max_age_seconds": seconds},
    )


def test_a_single_failed_read_is_still_fresh():
    """One missed cycle leaves the cached state a loop interval old.

    That state is what the control loop regulates on, so the statistics may
    integrate it too. Anything else would punch a hole into every enclosing
    period for one network hiccup.
    """

    controller = _controller({"WR1": 1_000.0}, online={"WR1": False})

    with _window(10):
        assert controller.telemetry_fresh("WR1", now=1_005.0) is True


def test_telemetry_past_the_window_is_not_fresh():
    controller = _controller({"WR1": 1_000.0}, online={"WR1": False})

    with _window(10):
        assert controller.telemetry_fresh("WR1", now=1_011.0) is False


def test_the_window_edge_still_counts():
    controller = _controller({"WR1": 1_000.0})

    with _window(10):
        assert controller.telemetry_fresh("WR1", now=1_010.0) is True


def test_a_device_that_never_answered_is_never_fresh():
    """There is no reading to age. The hole is the whole period, and real."""

    controller = _controller({}, online={"WR1": False})

    with _window(10):
        assert controller.telemetry_fresh("WR1", now=1_000.0) is False


def test_a_window_of_zero_makes_nothing_fresh():
    """The control loop reads a window of zero as 'always stale'; so do we.

    One constant, one meaning. ``diagnose`` warns about this setting rather
    than a second rule papering over it here.
    """

    controller = _controller({"WR1": 1_000.0})

    with _window(0):
        assert controller.telemetry_fresh("WR1", now=1_000.0) is False
        assert controller.telemetry_stale() is True


def test_freshness_and_the_control_loop_read_one_constant():
    """The predicate and the loop's own staleness check may never disagree."""

    controller = _controller({"WR1": 1_000.0})
    controller.devices = [type("Dev", (), {"name": "WR1"})()]

    with _window(10), patch("ems.controller.time.time", return_value=1_005.0):
        assert controller.telemetry_stale() is False
        assert controller.telemetry_fresh("WR1") is True

    with _window(10), patch("ems.controller.time.time", return_value=1_011.0):
        assert controller.telemetry_stale() is True
        assert controller.telemetry_fresh("WR1") is False


def test_the_control_loop_still_ignores_a_device_that_never_answered():
    """A regression guard on the shared age helper.

    ``telemetry_stale`` skips a device it has never seen, because it is about
    how fast to ramp and there is nothing to ramp from. The statistics ask a
    different question about the same device and answer it differently. The two
    share the arithmetic, never the verdict.
    """

    controller = _controller({})
    controller.devices = [type("Dev", (), {"name": "WR1"})()]

    with _window(10):
        assert controller.telemetry_stale() is False
        assert controller.telemetry_fresh("WR1", now=1_000.0) is False


def test_telemetry_age_is_unknown_for_a_device_never_read():
    controller = _controller({"WR1": 1_000.0})

    assert controller.telemetry_age_seconds("WR1", now=1_004.0) == 4.0
    assert controller.telemetry_age_seconds("WR2", now=1_004.0) is None


def test_freshness_is_measured_from_the_fetch_not_from_the_publish():
    """A slow cycle may not turn a successful read into a hole.

    The snapshot is built at the end of the cycle, after the device writes. On a
    slow network those writes can take longer than the staleness window, so
    judging the age at publish time would drop a sample every device answered
    -- and a consistently slow installation would record almost no energy at
    all. The reference is when the fetch returned.
    """

    controller = _controller({"WR1": 1_000.0})
    controller.last_fetch_at = 1_000.0

    with _window(10), patch("ems.controller.time.time", return_value=1_020.0):
        assert controller.telemetry_fresh("WR1") is True


def test_without_a_fetch_cycle_the_clock_is_the_reference():
    controller = _controller({"WR1": 1_000.0})

    with _window(10), patch("ems.controller.time.time", return_value=1_020.0):
        assert controller.telemetry_fresh("WR1") is False


def test_a_fetch_cycle_that_answered_nothing_still_ages_the_readings():
    """The reference advances every cycle, answered or not.

    Otherwise a device that stopped answering would stay forever as fresh as
    the moment it stopped.
    """

    controller = _controller({"WR1": 1_000.0})
    controller.last_fetch_at = 1_030.0

    with _window(10):
        assert controller.telemetry_fresh("WR1") is False


def test_the_statistics_and_the_control_loop_date_a_reading_differently():
    """Both read one window, from two reference points, on purpose.

    The control loop asks how old a value is *now*, as it decides a ramp. The
    statistics ask whether the value was current *when it was read*, because the
    snapshot is written at the end of the cycle. On a slow cycle the loop may
    call a reading stale while the statistics still integrate it -- that is the
    correct answer to each question, not a drift between them.
    """

    controller = _controller({"WR1": 1_000.0})
    controller.devices = [type("Dev", (), {"name": "WR1"})()]
    controller.last_fetch_at = 1_001.0

    with _window(10), patch("ems.controller.time.time", return_value=1_020.0):
        assert controller.telemetry_stale() is True
        assert controller.telemetry_fresh("WR1") is True


def test_the_snapshot_reaches_the_controllers_own_answer():
    """The snapshot looks the predicate up by name, so the name is pinned here.

    ``dashboard/telemetry.py`` asks ``getattr(controller, "telemetry_fresh")``
    and treats a controller that cannot answer as measuring. A rename on this
    side would therefore degrade the gate to "always measured" -- the behaviour
    this change replaced -- without a single test failing. This one walks the
    real lookup against a real controller.
    """

    from dashboard.telemetry import _device_telemetry_is_measured

    never_answered = _controller({}, online={"WR1": False})
    answered = _controller({"WR1": 1_000.0})
    answered.last_fetch_at = 1_002.0

    with _window(10):
        assert _device_telemetry_is_measured(never_answered, "WR1") is False
        assert _device_telemetry_is_measured(answered, "WR1") is True
