# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shell account an operator can reach over SSH, and what gates it.

``ems-backup`` is an SFTP account with no shell, a chroot and a forced command,
and ``ems-rescue`` is a console account whose published password sshd refuses.
Neither gets an operator a root shell over the network, which is what debugging
a box that will not finish an update actually needs.

This account does. That is a real escalation and it is written down rather than
implied: the console can deploy a key onto a sudo-capable shell account, so
whoever reaches the console reaches root on the appliance. The compensating
control is not secrecy, it is that two independent things must both be true
before a login exists -- an enable flag in root-owned agent state, and a key --
and that each of them is false by default and false again whenever it cannot be
read.

Passwords never enter it. ``PasswordAuthentication no`` alone leaves PAM's
keyboard-interactive path, which asks for the same password, so both are denied
in the same block -- the mistake ``ems-rescue`` was already fixed for.
"""

import json

import pytest

from appliance import shell_access
from appliance.config import ApplianceConfig, ConfigError, load_config
from appliance.host_config import render_sshd_policy
from appliance.paths import AppliancePaths

pytestmark = [pytest.mark.unit, pytest.mark.appliance]


def paths_at(root):
    return AppliancePaths(
        install_root=root / "opt" / "ems-solarflow",
        config_dir=root / "etc" / "ems-appliance-manager",
        state_dir=root / "var" / "lib" / "ems-appliance-manager",
        log_dir=root / "var" / "log" / "ems-appliance-manager",
        runtime_dir=root / "run" / "ems-appliance-manager",
        export_root=root / "srv" / "ems-appliance-export",
    )


def block_for(text, account):
    """One account's directives, and nothing from the block after it."""

    assert f"Match User {account}\n" in text, f"no Match block for {account}"
    rest = text.split(f"Match User {account}\n", 1)[1]
    return rest.split("Match User", 1)[0]


def directives(block):
    return {
        line.strip().split(None, 1)[0].lower(): line.strip().split(None, 1)[1].strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_the_account_cannot_log_in_before_anyone_enables_it(tmp_path):
    """The shipped state is no login at all, not a login waiting for a key.

    A key alone must not be a login: the console can deploy one, and an
    appliance whose only gate is "has somebody put a key here" has one gate.
    """

    policy = render_sshd_policy(paths_at(tmp_path), ApplianceConfig())
    applied = directives(block_for(policy, shell_access.ACCOUNT))

    assert applied["pubkeyauthentication"] == "no"
    assert applied["passwordauthentication"] == "no"
    assert applied["kbdinteractiveauthentication"] == "no"


def test_enabling_admits_a_key_and_still_never_a_password(tmp_path):
    policy = render_sshd_policy(
        paths_at(tmp_path), ApplianceConfig(), shell_access_enabled=True
    )
    applied = directives(block_for(policy, shell_access.ACCOUNT))

    assert applied["pubkeyauthentication"] == "yes"
    assert applied["passwordauthentication"] == "no"
    assert applied["kbdinteractiveauthentication"] == "no"


def test_an_enabled_shell_account_is_still_not_a_tunnel(tmp_path):
    """It is a shell, so it keeps a TTY and no chroot -- but an account that can
    forward ports turns one key into a route into the LAN behind the box."""

    policy = render_sshd_policy(
        paths_at(tmp_path), ApplianceConfig(), shell_access_enabled=True
    )
    block = block_for(policy, shell_access.ACCOUNT)
    applied = directives(block)

    assert "chrootdirectory" not in applied, "a chroot would defeat the purpose"
    assert "forcecommand" not in applied, "a forced command is not a shell"
    assert applied["permittty"] == "yes"
    for closed in ("allowtcpforwarding", "allowagentforwarding", "x11forwarding", "permittunnel"):
        assert applied[closed] == "no", f"{closed} must be closed on a shell account"
    assert applied["permitopen"] == "none"


def test_the_policy_stays_scoped_to_its_own_accounts(tmp_path):
    """Unchanged invariant, restated with the new block present: this package
    installs on a Pi somebody already administers, and a global directive here
    would rewrite their own sshd policy."""

    policy = render_sshd_policy(
        paths_at(tmp_path), ApplianceConfig(), shell_access_enabled=True
    )
    global_directives = [
        line
        for line in policy.splitlines()
        if line and not line.startswith(("#", " ", "\t", "Match "))
    ]

    assert global_directives == []


def test_state_that_cannot_be_read_is_a_disabled_account(tmp_path):
    """Fail closed. A truncated write, a half-restored backup or a state file
    from a future schema must not read as permission."""

    paths = paths_at(tmp_path)
    paths.agent_state_dir.mkdir(parents=True)

    for unreadable in ("", "{", "null", "[]", json.dumps({"enabled": "yes"})):
        shell_access.state_path(paths).write_text(unreadable, encoding="utf-8")
        assert shell_access.enabled(paths) is False, f"{unreadable!r} read as enabled"


def test_a_missing_state_file_is_a_disabled_account(tmp_path):
    assert shell_access.enabled(paths_at(tmp_path)) is False


def test_the_enable_flag_round_trips_and_stays_root_private(tmp_path):
    paths = paths_at(tmp_path)

    shell_access.set_enabled(paths, True)
    assert shell_access.enabled(paths) is True
    assert shell_access.state_path(paths).stat().st_mode & 0o077 == 0, (
        "agent state is root-private; a group- or world-readable flag is a leak"
    )

    shell_access.set_enabled(paths, False)
    assert shell_access.enabled(paths) is False


def test_the_console_may_deploy_a_key_to_this_account(tmp_path):
    """The operator chose the console path deliberately. What it must not do is
    become a way to name any account on the host."""

    config = ApplianceConfig()

    assert shell_access.ACCOUNT in config.ssh_key_accounts
    assert config.backup_user in config.ssh_key_accounts


def test_no_other_account_can_be_named_for_key_deployment(tmp_path):
    """Two accounts are package-owned and judged; a third name is a request to
    put a key somewhere nothing here reasons about."""

    paths = paths_at(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.appliance_conf.write_text(
        "[appliance]\nssh_key_accounts = root\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError) as refused:
        load_config(paths)

    assert refused.value.code == "ssh_key_accounts_unsupported"
