# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a failed command's own output is worth to the operator.

An appliance has no shell. When a host tool refuses, the text it wrote is the
only account of why, and every layer between it and the browser is a place it
can be dropped. A first Admin install failed on this appliance with nothing but
``compose_up_failed``, because the result carrying docker's answer was thrown
away one line after it was received.
"""

import errno
import os
import select
import time
import subprocess
from pathlib import Path

import pytest

from appliance.admin_lifecycle import command_failure_detail
from appliance import commands
from appliance.commands import CommandResult, CommandRunner

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


class _Killed:
    """``subprocess.run`` that times out after the tool already said something."""

    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, kwargs.get("timeout", 1), output=self.stdout, stderr=self.stderr
        )


def runner(tmp_path):
    executable = tmp_path / "docker"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return CommandRunner(executables={"docker": [str(executable)]})


def test_a_killed_command_keeps_what_it_had_already_written(tmp_path, monkeypatch):
    """A pull killed at the timeout has usually already named its own problem."""

    monkeypatch.setattr(
        subprocess, "run", _Killed(stdout="pulling layer 3\n", stderr="no space left on device\n")
    )

    result = runner(tmp_path).run("docker", ["pull", "example"], timeout=1)

    assert result.timed_out is True
    assert result.ok is False
    assert "pulling layer 3" in result.stdout
    assert "no space left on device" in result.stderr
    assert "timed out" in result.stderr


def test_a_killed_command_that_said_nothing_still_says_it_timed_out(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _Killed())

    result = runner(tmp_path).run("docker", ["pull", "example"], timeout=1)

    assert result.timed_out is True
    assert result.stderr.strip() == "timed out"


def test_the_reported_detail_is_the_tools_last_meaningful_line():
    result = CommandResult(
        "docker",
        ("compose", "up"),
        1,
        "",
        'no configuration file provided: not found\n\n',
    )

    assert command_failure_detail(result) == "no configuration file provided: not found"


def test_a_secret_in_command_output_is_redacted_before_it_is_reported():
    result = CommandResult("docker", ("compose", "up"), 1, "", "env MQTT_PASSWORD=hunter2 rejected")

    assert "hunter2" not in command_failure_detail(result)


def test_a_command_that_said_nothing_is_reported_as_saying_nothing():
    assert command_failure_detail(CommandResult("docker", ("compose", "up"), 1, "", "")) == (
        "no output"
    )


# --- a tool an operator waits on ---------------------------------------------


def sleeper(tmp_path, *, child=False, detached=False):
    """A tool that writes its pid (and its own child's) and then waits.

    A detached child holds none of the tool's pipes, so only stopping the
    whole group ends it; reading the pipes to their end proves nothing there.
    """

    body = ['echo "$$" > "$3"']
    if child:
        body += ["sleep 30 >/dev/null 2>&1 &" if detached else "sleep 30 &", 'echo "$!" > "$3.child"']
    body += ['echo started', 'exec sleep "$2"']
    executable = tmp_path / "docker"
    executable.write_text("#!/bin/sh\n" + "\n".join(body) + "\n", encoding="utf-8")
    executable.chmod(0o755)
    return CommandRunner(executables={"docker": [str(executable)]})


def written(*paths):
    """True once every pid file holds a pid: a stop before that proves nothing."""

    return all(path.exists() and path.read_text().strip() for path in paths)


def ended(pid_file):
    """Whether the sleep named in ``pid_file`` ends; its exit is awaited, not polled.

    A pid the kernel has already handed to another process or thread names
    something else, and the sleep is gone.
    """

    pid = int(pid_file.read_text().strip())
    try:
        handle = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise
        return True
    try:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return True
        if b"sleep" not in command:
            return True
        return bool(select.select([handle], [], [], 5)[0])
    finally:
        os.close(handle)


def test_a_watched_tool_that_finishes_gives_its_ordinary_result(tmp_path):
    result = sleeper(tmp_path).run_watched(
        "docker", ["pull", "0", str(tmp_path / "pid")], timeout=30, interval=5,
        keep_going=lambda elapsed: True,
    )

    assert result.ok
    assert result.stdout == "started\n"


def test_a_watched_tool_told_to_stop_is_gone_when_the_call_returns(tmp_path):
    """The field fault was a pull that went on after it was cancelled."""

    asked = []
    started = time.monotonic()

    result = sleeper(tmp_path).run_watched(
        "docker",
        ["pull", "30", str(tmp_path / "pid")],
        timeout=60,
        interval=0.05,
        keep_going=lambda elapsed: asked.append(elapsed) or not written(tmp_path / "pid"),
    )

    assert result.stopped and not result.ok
    assert asked and asked[0] >= 0.05
    assert time.monotonic() - started < commands._DRAIN_SECONDS
    assert ended(tmp_path / "pid")


def test_a_watched_tool_past_its_deadline_is_gone_when_the_call_returns(tmp_path):
    started = time.monotonic()
    result = sleeper(tmp_path).run_watched(
        "docker", ["pull", "30", str(tmp_path / "pid")], timeout=1.0, interval=0.05,
        keep_going=lambda elapsed: True,
    )

    assert result.timed_out and not result.ok
    assert result.stderr.endswith("timed out")
    assert time.monotonic() - started < commands._DRAIN_SECONDS
    assert ended(tmp_path / "pid")


def test_stopping_a_watched_tool_stops_what_it_started(tmp_path):
    result = sleeper(tmp_path, child=True).run_watched(
        "docker", ["pull", "30", str(tmp_path / "pid")], timeout=60, interval=0.05,
        keep_going=lambda elapsed: not written(tmp_path / "pid", tmp_path / "pid.child"),
    )

    assert result.stopped
    assert ended(tmp_path / "pid")
    assert ended(tmp_path / "pid.child")


def test_a_watched_tool_does_not_outlive_a_callback_that_raises(tmp_path):
    """Writing the progress note can fail on a full or failing card."""

    def failing(elapsed):
        if not written(tmp_path / "pid"):
            return True
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        sleeper(tmp_path).run_watched(
            "docker", ["pull", "30", str(tmp_path / "pid")], timeout=60, interval=0.05,
            keep_going=failing,
        )

    assert ended(tmp_path / "pid")


def test_stopping_a_watched_tool_stops_a_child_that_holds_none_of_its_pipes(tmp_path):
    started = time.monotonic()
    result = sleeper(tmp_path, child=True, detached=True).run_watched(
        "docker", ["pull", "30", str(tmp_path / "pid")], timeout=60, interval=0.05,
        keep_going=lambda elapsed: not written(tmp_path / "pid", tmp_path / "pid.child"),
    )

    assert result.stopped
    assert time.monotonic() - started < commands._DRAIN_SECONDS
    assert ended(tmp_path / "pid.child")


def test_a_child_that_left_the_group_does_not_hold_the_call(tmp_path, monkeypatch):
    """A process in a session of its own survives the group kill and holds the
    pipes; the call closes them instead of waiting for it."""

    monkeypatch.setattr(commands, "_DRAIN_SECONDS", 0.2)
    executable = tmp_path / "docker"
    executable.write_text(
        '#!/bin/sh\necho "$$" > "$3"\nsetsid sleep 30 &\nexec sleep 30\n', encoding="utf-8"
    )
    executable.chmod(0o755)
    started = time.monotonic()

    result = CommandRunner(executables={"docker": [str(executable)]}).run_watched(
        "docker", ["pull", "30", str(tmp_path / "pid")], timeout=60, interval=0.05,
        keep_going=lambda elapsed: not written(tmp_path / "pid"),
    )

    assert result.stopped
    assert time.monotonic() - started < 5
