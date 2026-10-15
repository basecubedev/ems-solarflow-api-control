# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade's EMS health check for a running pair, run beside the read that asks.

The status read that reconciles the known-good record must not wait for the
check (several ``docker exec`` one after another): it starts one run in the
background, and a verdict counts for its pair within its window.
"""

import threading

import pytest

from admin.running_pair_health import THREAD_NAME, VERDICT_SECONDS, RunningPairHealthCheck

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.unit,
]

PAIR = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
OTHER_PAIR = ("sha256:" + "a" * 64, "sha256:" + "c" * 64)


def _diagnosis(status):
    return {"available": True, "summary": {"status": status}}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Diagnose:
    def __init__(self, *results):
        self.results = list(results)
        self.runs = 0

    def __call__(self):
        self.runs += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _inline(work):
    work()


def test_a_read_never_waits_for_the_check_and_one_check_runs_at_a_time():
    """Interleaving: read 1 starts the check; read 2 arrives while it runs.

    Neither read waits for the diagnosis, and read 2 starts no second one.
    Once the check has ended, a read answers with its verdict.
    """

    entered = threading.Event()
    release = threading.Event()
    runs = []
    threads = []

    def diagnose():
        runs.append(1)
        entered.set()
        assert release.wait(timeout=10)
        return _diagnosis("ok")

    def start(work):
        thread = threading.Thread(target=work, name=THREAD_NAME, daemon=True)
        threads.append(thread)
        thread.start()

    check = RunningPairHealthCheck(diagnose, start=start)

    assert check.verdict(PAIR) is None
    assert entered.wait(timeout=10)
    assert check.verdict(PAIR) is None
    assert check.verdict(OTHER_PAIR) is None
    release.set()
    threads[0].join(timeout=10)

    assert len(threads) == 1
    assert runs == [1]
    assert check.verdict(PAIR) is True


@pytest.mark.parametrize(
    ("result", "passed"),
    (
        (_diagnosis("ok"), True),
        (_diagnosis("warning"), True),
        (_diagnosis("failed"), False),
        (_diagnosis("unavailable"), False),
        ({"available": False, "summary": {"status": "ok"}}, False),
        ({}, False),
        (None, False),
        (RuntimeError("docker exec failed"), False),
    ),
    ids=("ok", "warning", "failed", "unavailable", "not_available", "empty", "none", "raises"),
)
def test_the_verdict_is_the_upgrade_health_gate(result, passed):
    """The same fail-closed validator the upgrade's completion applies."""

    check = RunningPairHealthCheck(_Diagnose(result), start=_inline)

    assert check.verdict(PAIR) is passed


def test_a_verdict_counts_for_its_pair_within_its_window():
    clock = _Clock()
    diagnose = _Diagnose(_diagnosis("failed"), _diagnosis("ok"), _diagnosis("ok"))
    check = RunningPairHealthCheck(diagnose, clock=clock, start=_inline)

    assert check.verdict(PAIR) is False
    clock.now += VERDICT_SECONDS - 1
    assert check.verdict(PAIR) is False
    assert diagnose.runs == 1

    assert check.verdict(OTHER_PAIR) is True
    assert diagnose.runs == 2

    clock.now += VERDICT_SECONDS
    assert check.verdict(PAIR) is True
    assert diagnose.runs == 3


def test_a_check_that_could_not_start_leaves_none_running():
    diagnose = _Diagnose(_diagnosis("ok"))

    def refuse(work):
        raise RuntimeError("can't start new thread")

    check = RunningPairHealthCheck(diagnose, start=refuse)
    assert check.verdict(PAIR) is None

    check._start = _inline
    assert check.verdict(PAIR) is True
    assert diagnose.runs == 1


def test_a_passing_check_calls_back_the_read_that_started_it_once_its_verdict_is_kept():
    """Interleaving: read 1 starts the check, read 2 arrives while it runs, the
    check passes.

    Only read 1's callback runs, in the check's thread, after the verdict is
    kept and outside the check's lock: the callback reads the verdict itself.
    """

    entered = threading.Event()
    release = threading.Event()
    called = []
    threads = []

    def diagnose():
        entered.set()
        assert release.wait(timeout=10)
        return _diagnosis("ok")

    def start(work):
        thread = threading.Thread(target=work, name=THREAD_NAME, daemon=True)
        threads.append(thread)
        thread.start()

    check = RunningPairHealthCheck(diagnose, start=start)

    assert check.verdict(PAIR, on_pass=lambda: called.append(("first", check.verdict(PAIR)))) is None
    assert entered.wait(timeout=10)
    assert check.verdict(PAIR, on_pass=lambda: called.append(("second", None))) is None
    release.set()
    threads[0].join(timeout=10)

    assert not threads[0].is_alive()
    assert called == [("first", True)]


@pytest.mark.parametrize(
    "result",
    (_diagnosis("failed"), RuntimeError("docker exec failed")),
    ids=("failed", "raises"),
)
def test_a_check_that_does_not_pass_calls_nothing_back(result):
    called = []
    check = RunningPairHealthCheck(_Diagnose(result), start=_inline)

    assert check.verdict(PAIR, on_pass=lambda: called.append(1)) is False
    assert called == []
