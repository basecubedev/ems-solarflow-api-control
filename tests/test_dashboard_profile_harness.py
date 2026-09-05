# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract for the one piece of the dashboard profiler that decides something.

Most of `scripts/dashboard_profile/` is instrument: it records numbers a person
then reads, and a test of it would be a mock of a mock. `looks_occluded()` is
different. It is the gate that decides whether a case counts, and it exists
because a whole after-fix matrix once came back at 1.0 fps -- a window that had
opened behind another application, throttled by the browser to one animation
frame per second -- and was very nearly reported as a regression.

Its exception matters as much as its rule: two matrices put the dashboard behind
another page on purpose, and there the 1 fps *is* the measurement. A gate that
could not tell those apart would retry them three times and then discard the
result it was asked for.

See reports/dashboard-perf/final-dashboard-performance-audit.md, section 22.
"""

import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
]

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "scripts", ROOT / "scripts" / "dashboard_profile"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

profile_bench = pytest.importorskip(
    "profile_bench", reason="the dashboard profiler needs the scripts/ helpers"
)


def case(**over):
    base = {"foreground": "dashboard", "neighbour": False}
    base.update(over)
    return base


def result(fps, frame_p95):
    return {"dashboard": {"fps": fps, "frameP95Ms": frame_p95}, "neighbour": None}


def test_a_window_throttled_to_one_frame_a_second_is_not_a_measurement():
    assert profile_bench.looks_occluded(case(), result(1.0, 1000.0)) is True


def test_a_slow_page_is_still_a_measurement():
    """The gate must not swallow a genuine collapse. The authenticated control
    view really did draw at 35.6 fps with a 34.8 ms frame time, and that was the
    largest finding in the audit."""

    assert profile_bench.looks_occluded(case(), result(35.6, 34.8)) is False
    assert profile_bench.looks_occluded(case(), result(11.5, 62.6)) is False


def test_a_background_the_scenario_asked_for_is_left_alone():
    """`hiddentab` and `unfocused` put the dashboard behind another page on
    purpose. Retrying those would discard the result that was asked for."""

    behind = {"dashboard": {"fps": 1.0, "frameP95Ms": 1000.0},
              "neighbour": {"fps": 144.0, "frameP95Ms": 7.0}}
    assert profile_bench.looks_occluded(
        case(foreground="neighbour", neighbour=True), behind) is False


def test_the_dashboard_is_still_judged_when_a_neighbour_is_merely_open():
    """A neighbour being open is not the same as the dashboard being behind it.
    With the dashboard in front, an unasked-for throttle still counts."""

    assert profile_bench.looks_occluded(
        case(neighbour=True), result(1.0, 1000.0)) is True


def test_a_case_that_produced_nothing_is_not_called_occluded():
    assert profile_bench.looks_occluded(case(), None) is False
    assert profile_bench.looks_occluded(case(), {"dashboard": None,
                                                 "neighbour": None}) is False


def test_a_page_with_no_frame_rate_at_all_is_not_called_occluded():
    """`fps` is None when too few frames were sampled to divide by. That is a
    short window, not a throttled one."""

    assert profile_bench.looks_occluded(
        case(), {"dashboard": {"fps": None, "frameP95Ms": None},
                 "neighbour": None}) is False


def test_the_neighbour_is_judged_when_the_neighbour_is_the_page_in_front():
    """`matrix_neighbour` reads the neighbour's own responsiveness while the
    dashboard sits behind it. Then the neighbour is what has to be trusted."""

    occluded = {"dashboard": {"fps": 1.0, "frameP95Ms": 1000.0},
                "neighbour": {"fps": 1.0, "frameP95Ms": 1000.0}}
    assert profile_bench.looks_occluded(
        case(foreground="neighbour", neighbour=True), occluded) is True

    fine = {"dashboard": {"fps": 1.0, "frameP95Ms": 1000.0},
            "neighbour": {"fps": 143.9, "frameP95Ms": 7.0}}
    assert profile_bench.looks_occluded(
        case(foreground="neighbour", neighbour=True), fine) is False
