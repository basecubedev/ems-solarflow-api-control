# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Manager's sessions end when they say they do, whatever the wall clock does.

No test pinned the idle limit, the absolute cap under constant use, or what a
limit of 0 meant, and the store ran on the wall clock of a board without a
real-time clock: NTP stepping it after boot stretched or ended every session.
"""

import pytest

from appliance.auth import SessionStore

pytestmark = [pytest.mark.unit, pytest.mark.appliance]


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def store(clock, wall=None, *, idle=1800, absolute=43200):
    return SessionStore(idle_timeout=idle, absolute_max=absolute, time_fn=clock, wall_fn=wall or clock)


def test_an_idle_session_ends_after_the_idle_limit():
    clock = Clock()
    sessions = store(clock)
    session = sessions.create("g1")

    clock.now += 1799
    assert sessions.get(session.session_id, "g1") is not None
    clock.now += 2
    assert sessions.get(session.session_id, "g1") is None


def test_constant_use_does_not_outlive_the_absolute_cap():
    clock = Clock()
    sessions = store(clock, idle=1800, absolute=7200)
    session = sessions.create("g1")

    for _ in range(8):
        clock.now += 1000
        sessions.touch(session.session_id, "g1")

    assert sessions.get(session.session_id, "g1") is None


@pytest.mark.parametrize("value", [0, -1])
def test_a_limit_of_zero_or_less_is_refused_not_unlimited(value):
    with pytest.raises(ValueError):
        SessionStore(idle_timeout=value)
    with pytest.raises(ValueError):
        SessionStore(absolute_max=value)


def test_a_wall_clock_step_neither_ends_nor_stretches_a_session():
    """The board has no RTC; NTP steps the wall clock after boot."""

    monotonic, wall = Clock(500.0), Clock(1_600_000_000.0)
    sessions = store(monotonic, wall, idle=1800)
    session = sessions.create("g1")

    wall.now += 86_400 * 365
    assert sessions.get(session.session_id, "g1") is not None
    wall.now -= 86_400 * 730
    monotonic.now += 1801
    assert sessions.get(session.session_id, "g1") is None


def test_the_displayed_expiry_is_on_the_wall_clock():
    monotonic, wall = Clock(500.0), Clock(1_600_000_000.0)
    sessions = store(monotonic, wall, idle=1800, absolute=43200)
    session = sessions.create("g1")

    assert sessions.expires_at_wall(session) == pytest.approx(1_600_000_000.0 + 1800)


class CountingLock:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "call",
    [
        lambda s, sid: s.create("g1"),
        lambda s, sid: s.get(sid, "g1"),
        lambda s, sid: s.touch(sid, "g1"),
        lambda s, sid: s.destroy(sid),
        lambda s, sid: s.destroy_all(),
        lambda s, sid: s.cleanup(),
    ],
)
def test_every_change_to_the_session_table_holds_its_lock(call):
    """cleanup() iterated the table while another request thread popped from
    it: "dictionary changed size during iteration" dropped a sign-in."""

    sessions = store(Clock())
    sid = sessions.create("g1").session_id
    sessions._lock = CountingLock()

    call(sessions, sid)

    assert sessions._lock.entered >= 1
