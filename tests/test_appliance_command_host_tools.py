# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runner against the real host tool it will run on an appliance.

The unit tier injects its own executable so the argument rules answer the same
way on any machine. This tier is the other half: it uses the real
``ssh-keygen``, because "the validator is consistent" and "the appliance can
actually generate a host key" are different claims. Where the binary is
missing the case is skipped, never quietly reinterpreted as a pass.
"""

import shutil
import subprocess

import pytest

from appliance.commands import CommandError, CommandRunner

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

requires_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is required to exercise the real host tool",
)


@requires_ssh_keygen
def test_a_real_host_key_is_generated_without_a_passphrase(tmp_path):
    key = tmp_path / "ssh_host_ed25519_key"

    result = CommandRunner().run(
        "ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)]
    )

    assert result.ok, result.stderr
    assert key.exists() and key.with_suffix(".pub").exists()


@requires_ssh_keygen
def test_the_real_runner_refuses_a_nul_argument_before_it_starts_the_tool(tmp_path):
    with pytest.raises(CommandError) as raised:
        CommandRunner().run("ssh-keygen", ["-y", "-f", f"{tmp_path}/key\x00-C"])

    assert raised.value.code == "invalid_argument"


@requires_ssh_keygen
def test_a_failing_host_tool_is_reported_as_a_failure_not_as_an_exception(tmp_path):
    result = CommandRunner().run("ssh-keygen", ["-y", "-f", str(tmp_path / "absent")])

    assert not result.ok
    assert result.returncode != 0


def test_the_unit_tier_does_not_depend_on_the_host_having_ssh_keygen():
    """The reproduction: the unit module passes with the tool taken away.

    ``PATH`` is emptied and the allowlist points at a directory that has no
    binaries, which is what a minimal build container looks like.
    """

    result = subprocess.run(
        [
            "python3",
            "-c",
            "from appliance.commands import CommandError, CommandRunner\n"
            "r = CommandRunner(executables={'ssh-keygen': ('/nonexistent/ssh-keygen',)},"
            " env={'PATH': ''})\n"
            "try:\n"
            "    r.run('ssh-keygen', ['-y', 7])\n"
            "except CommandError as error:\n"
            "    print(error.code)\n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "invalid_argument"
