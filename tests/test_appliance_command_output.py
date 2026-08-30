# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a failed command's own output is worth to the operator.

An appliance has no shell. When a host tool refuses, the text it wrote is the
only account of why, and every layer between it and the browser is a place it
can be dropped. A first Admin install failed on this appliance with nothing but
``compose_up_failed``, because the result carrying docker's answer was thrown
away one line after it was received.
"""

import subprocess

import pytest

from appliance.admin_lifecycle import command_failure_detail
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
