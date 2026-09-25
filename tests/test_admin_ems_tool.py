# SPDX-License-Identifier: AGPL-3.0-or-later
"""EMS tool runner tests: how a command is bounded inside the EMS container.

No Docker daemon; the subprocess runner is injected.
"""

import subprocess
from types import SimpleNamespace

import pytest

from admin import ems_tool
from admin.ems_tool import CONTAINER_EMSCTL_PATH, EmsToolRunner

pytestmark = [
    pytest.mark.admin,
    pytest.mark.unit,
]

CONTAINER = "ems-solarflow"


def _context(tmp_path):
    return SimpleNamespace(
        compose_exists=True,
        install_root=str(tmp_path),
        compose_path=str(tmp_path / "docker-compose.yml"),
    )


class _RunningDocker:
    """Docker probe that reports one running EMS container."""

    def probe(self):
        return {"state": "ready"}

    def inspect_container(self, name):
        return {"status": "running", "container_name": CONTAINER}


def _recorder(results):
    """Record every argv/kwargs pair and answer from ``results`` in order."""

    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        outcome = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run, calls


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _runner(results):
    run, calls = _recorder(results)
    return EmsToolRunner(docker=_RunningDocker(), run=run), calls


def _guard_slice(argv):
    """The argv between the container name and the interpreter."""

    return argv[argv.index(CONTAINER) + 1:argv.index("python3")]


# --- the deadline travels with the command ------------------------------


def test_a_container_command_carries_its_own_deadline_inside(tmp_path):
    runner, calls = _runner([_completed()])

    runner.run(_context(tmp_path), ["status"], timeout=90)

    argv = calls[0][0]
    assert _guard_slice(argv)[0] == ems_tool.EXEC_GUARD
    assert str(90) in _guard_slice(argv)


def test_the_inner_deadline_expires_before_the_client_stops_waiting(tmp_path):
    runner, calls = _runner([_completed()])

    runner.run(_context(tmp_path), ["status"], timeout=90)

    argv, kwargs = calls[0]
    assert 90 in [int(part) for part in _guard_slice(argv) if str(part).isdigit()]
    assert kwargs["timeout"] > 90


def test_a_command_that_ignores_the_stop_signal_is_killed_inside(tmp_path):
    runner, calls = _runner([_completed()])

    runner.run(_context(tmp_path), ["status"], timeout=90)

    guard = _guard_slice(calls[0][0])
    assert any(str(part).startswith("--kill-after=") for part in guard)


def test_the_command_still_runs_after_the_guard(tmp_path):
    runner, calls = _runner([_completed()])

    runner.run(_context(tmp_path), ["backup", "create"], timeout=30)

    argv = calls[0][0]
    assert argv[argv.index("python3") + 1] == CONTAINER_EMSCTL_PATH
    assert argv[-2:] == ["backup", "create"]


def test_stdin_stays_open_before_the_container_name(tmp_path):
    runner, calls = _runner([_completed()])

    runner.run(_context(tmp_path), ["restore"], timeout=30, input_text="secret")

    argv, kwargs = calls[0]
    assert argv[:3] == ["docker", "exec", "-i"]
    assert argv[3] == CONTAINER
    assert kwargs["input"] == "secret"
    assert "secret" not in argv


# --- what the operator is told -------------------------------------------


def test_a_command_stopped_by_its_inner_deadline_reads_as_a_timeout(tmp_path):
    runner, _calls = _runner([_completed(returncode=124)])

    result = runner.run(_context(tmp_path), ["status"], timeout=0)

    assert result.returncode is None
    assert "timed out" in result.detail.lower()


def test_a_command_killed_after_refusing_to_stop_reads_as_a_timeout(tmp_path):
    runner, _calls = _runner([_completed(returncode=137)])

    result = runner.run(_context(tmp_path), ["status"], timeout=0)

    assert result.returncode is None
    assert "timed out" in result.detail.lower()


def test_a_timeout_stopped_inside_does_not_warn_about_a_survivor(tmp_path):
    runner, _calls = _runner([_completed(returncode=124)])

    result = runner.run(_context(tmp_path), ["status"], timeout=0)

    assert "still" not in result.detail.lower()


def test_a_client_side_timeout_still_warns_the_command_may_survive(tmp_path):
    runner, _calls = _runner([subprocess.TimeoutExpired(cmd="docker", timeout=1)])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert result.returncode is None
    assert "still" in result.detail.lower()


def test_an_ordinary_exit_code_passes_through_untouched(tmp_path):
    runner, _calls = _runner([_completed(returncode=3, stdout="nope")])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert result.returncode == 3


def test_a_successful_command_reports_its_output(tmp_path):
    runner, _calls = _runner([_completed(stdout="all good")])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert result.returncode == 0
    assert "all good" in result.detail


# --- an image without the guard must still be serviceable ----------------

_GUARD_MISSING = (
    'OCI runtime exec failed: exec failed: unable to start container process: '
    'exec: "timeout": executable file not found in $PATH: unknown'
)


def test_an_image_without_the_guard_still_runs_the_command(tmp_path):
    runner, calls = _runner([
        _completed(returncode=126, stdout=_GUARD_MISSING),
        _completed(returncode=0, stdout="ran anyway"),
    ])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert result.returncode == 0
    assert len(calls) == 2
    assert ems_tool.EXEC_GUARD not in calls[1][0]


def test_the_unguarded_retry_keeps_the_deadline_the_caller_asked_for(tmp_path):
    runner, calls = _runner([
        _completed(returncode=126, stdout=_GUARD_MISSING),
        _completed(returncode=0),
    ])

    runner.run(_context(tmp_path), ["status"], timeout=30)

    assert calls[1][1]["timeout"] == 30


def test_the_unguarded_retry_still_carries_stdin(tmp_path):
    runner, calls = _runner([
        _completed(returncode=126, stdout=_GUARD_MISSING),
        _completed(returncode=0),
    ])

    runner.run(_context(tmp_path), ["restore"], timeout=30, input_text="secret")

    assert calls[1][1]["input"] == "secret"
    assert "secret" not in calls[1][0]


def test_the_command_is_never_attempted_a_third_time(tmp_path):
    runner, calls = _runner([
        _completed(returncode=126, stdout=_GUARD_MISSING),
        _completed(returncode=126, stdout=_GUARD_MISSING),
    ])

    runner.run(_context(tmp_path), ["status"], timeout=30)

    assert len(calls) == 2


def test_an_unrelated_exit_code_126_is_not_retried(tmp_path):
    runner, calls = _runner([_completed(returncode=126, stdout="permission denied")])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert len(calls) == 1
    assert result.returncode == 126


def test_a_missing_interpreter_is_reported_rather_than_retried_forever(tmp_path):
    missing_python = (
        'OCI runtime exec failed: exec failed: unable to start container '
        'process: exec: "python3": executable file not found in $PATH: unknown'
    )
    runner, calls = _runner([_completed(returncode=126, stdout=missing_python)])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert len(calls) == 1
    assert result.returncode == 126


# --- failures that were already handled stay handled ---------------------


def test_a_missing_docker_client_is_still_reported(tmp_path):
    runner, _calls = _runner([FileNotFoundError("docker")])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert result.returncode is None
    assert "docker" in result.detail.lower()


def test_a_spawn_failure_is_still_reported(tmp_path):
    runner, _calls = _runner([OSError("boom")])

    result = runner.run(_context(tmp_path), ["status"], timeout=30)

    assert result.returncode is None
    assert "boom" in result.detail


# --- a killed command is not automatically a timed-out one ---------------


def test_a_command_killed_long_before_its_deadline_is_not_a_timeout(tmp_path):
    """137 is what docker reports for any SIGKILL, the OOM killer included.

    Calling that a timeout on a budget it never reached sends the operator to
    raise a ceiling that was never the problem.
    """

    runner, _calls = _runner([_completed(returncode=137, stdout="killed")])

    result = runner.run(_context(tmp_path), ["backup", "create"], timeout=1800)

    assert result.returncode == 137
    assert "killed" in result.detail


def test_the_unguarded_retry_never_claims_the_guard_stopped_it(tmp_path):
    runner, _calls = _runner([
        _completed(returncode=126, stdout=_GUARD_MISSING),
        _completed(returncode=137),
    ])

    result = runner.run(_context(tmp_path), ["status"], timeout=0)

    assert result.returncode == 137


def test_the_client_waits_past_the_guards_own_kill(tmp_path):
    """If the client fires first the orphan is back, and every test still passes.

    These two constants are the whole fix, so their relationship is pinned here
    rather than left to whoever next tunes one of them.
    """

    assert ems_tool.EXEC_CLIENT_GRACE_SECONDS > ems_tool.EXEC_GUARD_KILL_AFTER_SECONDS


def test_the_guard_always_gets_a_kill_deadline_that_can_fire(tmp_path):
    """GNU timeout reads a zero duration as no timeout at all."""

    assert ems_tool.EXEC_GUARD_KILL_AFTER_SECONDS >= 1


def test_a_ceiling_that_rounds_to_nothing_still_arms_the_guard(tmp_path):
    runner, calls = _runner([_completed()])

    runner.run(_context(tmp_path), ["status"], timeout=0)

    assert "0" not in _guard_slice(calls[0][0])
