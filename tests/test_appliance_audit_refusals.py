# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refusals that read no password are written once per source and window.

A sign-in or password check refused as busy or rate-limited costs its source
nothing, so a client on the LAN can repeat it as fast as it connects. Written
one line each, some ten thousand of them rotated the audit trail's one 4 MB
generation out. The first such refusal from a source is still written as it
comes, the repeats become one count when the window ends, and every event that
read a password, or succeeded, is written each time.
"""

import pytest

from appliance.audit import AuditLog, RefusalAggregator

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

WINDOW = 300


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def build(tmp_path, **kwargs):
    log = AuditLog(tmp_path / "audit.log")
    clock = Clock()
    return log, clock, RefusalAggregator(log, window_seconds=WINDOW, time_fn=clock, **kwargs)


def refuse(aggregator, source="192.168.1.50", *, event="login.failure",
           result="denied", reason="rate_limited"):
    return aggregator.record(
        event, user="appliance-admin", source_ip=source, result=result, reason=reason
    )


def written(log):
    return log.tail(limit=100000)


def test_repeated_refusals_from_one_source_are_written_once_in_a_window(tmp_path):
    log, _, aggregator = build(tmp_path)

    for _ in range(10000):
        refuse(aggregator)

    entries = written(log)
    assert len(entries) == 1, len(entries)
    assert entries[0]["source_ip"] == "192.168.1.50"
    assert entries[0]["target"] == "rate_limited"
    assert "detail" not in entries[0]


def test_the_repeats_are_written_as_one_count_once_the_window_has_ended(tmp_path):
    log, clock, aggregator = build(tmp_path)
    for _ in range(50):
        refuse(aggregator)

    clock.now += WINDOW
    refuse(aggregator)

    first, count, reopened = written(log)
    assert count["action"] == "login.failure"
    assert count["result"] == "denied"
    assert count["target"] == "rate_limited"
    assert count["source_ip"] == "192.168.1.50"
    assert count["detail"] == {"repeated": 49, "since": first["timestamp"]}
    assert "detail" not in reopened


def test_an_ended_window_is_written_by_the_next_event_of_any_source(tmp_path):
    log, clock, aggregator = build(tmp_path)
    for _ in range(3):
        refuse(aggregator)

    clock.now += WINDOW
    aggregator.record(
        "login.success", user="appliance-admin", source_ip="192.168.1.9",
        result="success", reason="",
    )

    actions = [(entry["action"], entry.get("detail", {}).get("repeated")) for entry in written(log)]
    assert actions == [("login.failure", None), ("login.failure", 2), ("login.success", None)]


def test_busy_refusals_are_counted_the_same_way(tmp_path):
    log, _, aggregator = build(tmp_path)

    for event in ("login.failure", "password.change", "password.confirm"):
        for _ in range(20):
            refuse(aggregator, event=event, result="failure", reason="busy")

    assert [entry["action"] for entry in written(log)] == [
        "login.failure", "password.change", "password.confirm",
    ]


@pytest.mark.parametrize(
    "event,result,reason",
    [
        ("login.failure", "failure", "invalid_password"),
        ("login.success", "success", ""),
        ("password.change", "success", "password_changed"),
        ("password.confirm", "failure", "password_changed"),
        ("logout", "success", "session_ended"),
    ],
)
def test_an_event_that_read_a_password_or_succeeded_is_written_every_time(
    tmp_path, event, result, reason
):
    log, _, aggregator = build(tmp_path)

    for _ in range(5):
        refuse(aggregator, event=event, result=result, reason=reason)

    assert len(written(log)) == 5


def test_every_source_has_its_first_refusal_written(tmp_path):
    log, _, aggregator = build(tmp_path)

    for source in ("192.168.1.50", "192.168.1.51", "192.168.1.52"):
        for _ in range(10):
            refuse(aggregator, source)

    assert [entry["source_ip"] for entry in written(log)] == [
        "192.168.1.50", "192.168.1.51", "192.168.1.52",
    ]


def test_addresses_an_ipv6_host_picks_inside_its_64_are_one_source(tmp_path):
    """An IPv6 host chooses its own addresses inside its /64; counted per address,
    every refusal would be a first one."""

    log, clock, aggregator = build(tmp_path)
    for host in range(1, 200):
        refuse(aggregator, f"2001:db8:1:2::{host:x}")
    refuse(aggregator, "2001:db8:1:3::1")

    first, other = written(log)
    assert first["source_ip"] == "2001:db8:1:2::1"
    assert other["source_ip"] == "2001:db8:1:3::1"

    clock.now += WINDOW
    refuse(aggregator, "2001:db8:1:2::1")
    count = written(log)[2]
    assert count["source_ip"] == "2001:db8:1:2::/64"
    assert count["detail"]["repeated"] == 198


def test_the_open_windows_are_bounded_and_an_evicted_count_is_written(tmp_path):
    log, clock, aggregator = build(tmp_path, max_entries=2)

    for source in ("10.0.0.1", "10.0.0.2"):
        for _ in range(3):
            refuse(aggregator, source)
        clock.now += 1
    refuse(aggregator, "10.0.0.3")

    summary = [(entry["source_ip"], entry.get("detail", {}).get("repeated")) for entry in written(log)]
    assert summary == [
        ("10.0.0.1", None), ("10.0.0.2", None), ("10.0.0.1", 2), ("10.0.0.3", None),
    ]
    for host in range(4, 300):
        refuse(aggregator, f"10.0.1.{host % 250}")
    assert len(aggregator) <= 2
