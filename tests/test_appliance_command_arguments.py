# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the real runner accepts, and what the fake is allowed to accept.

The appliance never builds a shell string, so an argument is a single argv
member and nothing in it can start a second command. The one thing that can
is a NUL byte, which truncates the member on the way into ``execve``.

An empty argv member is ordinary. ``ssh-keygen -N '' -C ''`` is how OpenSSH is
told "no passphrase, no comment", and the appliance's first boot has no other
way to say it. Rejecting it made every host key generation on a real appliance
fail with ``host_key_generation_failed`` — and no test saw it, because the
recording runner used everywhere accepted an argv the real one refused.

So the two runners share one validator. A fake that accepts what production
refuses does not prove production works.

These are unit tests, so they inject their own executable instead of borrowing
one from the host. The runner used to resolve the tool before it looked at the
arguments, which meant a machine without ``/usr/bin/ssh-keygen`` answered
"tool_unavailable" to a NUL byte — the argument rule reported as an
installation fact. ``tests/test_appliance_command_host_tools.py`` covers the
same runner against the real binary.
"""

import pytest

from appliance.commands import (
    MAX_ARGUMENT_BYTES,
    CommandError,
    CommandRunner,
    RecordingRunner,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

RUNNERS = ("real", "recording")


@pytest.fixture
def echo_tool(tmp_path):
    tool = tmp_path / "ssh-keygen"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.fixture
def runner(echo_tool):
    def make(kind):
        if kind == "real":
            return CommandRunner(executables={"ssh-keygen": (str(echo_tool),)})
        return RecordingRunner(default="")

    return make


@pytest.mark.parametrize("kind", RUNNERS)
def test_an_empty_argument_is_a_legitimate_argv_member(kind, runner):
    """``-N ''`` is not an attack, it is how a passphrase is declined."""

    result = runner(kind).run("ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-C", ""])

    assert result.args == ("-q", "-t", "ed25519", "-N", "", "-C", "")


@pytest.mark.parametrize("kind", RUNNERS)
def test_a_nul_byte_is_refused(kind, runner):
    """It truncates the member on the way into execve, so it is never passed."""

    with pytest.raises(CommandError) as raised:
        runner(kind).run("ssh-keygen", ["-y", "-f", "/etc/passwd\x00-C"])

    assert raised.value.code == "invalid_argument"


@pytest.mark.parametrize("kind", RUNNERS)
def test_a_non_string_argument_is_refused(kind, runner):
    with pytest.raises(CommandError) as raised:
        runner(kind).run("ssh-keygen", ["-y", 7])

    assert raised.value.code == "invalid_argument"


@pytest.mark.parametrize("kind", RUNNERS)
def test_an_argument_longer_than_the_kernel_accepts_is_refused(kind, runner):
    with pytest.raises(CommandError) as raised:
        runner(kind).run("ssh-keygen", ["-C", "x" * (MAX_ARGUMENT_BYTES + 1)])

    assert raised.value.code == "invalid_argument"


@pytest.mark.parametrize("kind", RUNNERS)
def test_an_unallowlisted_tool_is_refused_by_both(kind, runner):
    with pytest.raises(CommandError) as raised:
        runner(kind).run("curl", ["https://example.invalid"])

    assert raised.value.code == "tool_not_allowed"


@pytest.mark.parametrize("bad", [["-y", 7], ["-f", "/etc/passwd\x00-C"]])
def test_an_invalid_argument_outranks_a_missing_executable(bad, tmp_path):
    """The argument rule is host independent, so it answers before resolution.

    A build host without OpenSSH installed must not report a NUL byte as
    ``tool_unavailable``: that is the installation answering a question about
    the argument.
    """

    absent = CommandRunner(executables={"ssh-keygen": (str(tmp_path / "absent"),)})

    with pytest.raises(CommandError) as raised:
        absent.run("ssh-keygen", bad)

    assert raised.value.code == "invalid_argument"


def test_a_missing_executable_is_still_reported_for_a_valid_argv(tmp_path):
    absent = CommandRunner(executables={"ssh-keygen": (str(tmp_path / "absent"),)})

    with pytest.raises(CommandError) as raised:
        absent.run("ssh-keygen", ["-t", "ed25519"])

    assert raised.value.code == "tool_unavailable"
