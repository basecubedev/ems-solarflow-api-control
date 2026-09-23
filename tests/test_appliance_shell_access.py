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

from pathlib import Path

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


def test_enabling_writes_the_flag_and_the_policy_in_one_move(tmp_path, monkeypatch):
    applied = {}

    def recording_apply(paths, config, *, activation=None):
        applied["policy"] = render_sshd_policy(paths, config)
        return {"applied": True}

    monkeypatch.setattr("appliance.host_config.apply_host_config", recording_apply)

    paths = paths_at(tmp_path)
    shell_access.apply(paths, ApplianceConfig(), value=True, activation=None)

    assert shell_access.enabled(paths) is True
    assert "    PubkeyAuthentication yes\n" in block_for(applied["policy"], shell_access.ACCOUNT), (
        "the transaction must render the policy the new flag asks for, not the old one"
    )


def test_a_rolled_back_policy_takes_the_flag_back_with_it(tmp_path, monkeypatch):
    """A flag left on after the transaction put the old files back would report
    a reachable account while sshd still refuses it -- state and daemon
    disagreeing is what that transaction exists to prevent."""

    def refusing_apply(paths, config, *, activation=None):
        raise RuntimeError("sshd refused the candidate policy")

    monkeypatch.setattr("appliance.host_config.apply_host_config", refusing_apply)

    paths = paths_at(tmp_path)
    with pytest.raises(RuntimeError):
        shell_access.apply(paths, ApplianceConfig(), value=True, activation=None)

    assert shell_access.enabled(paths) is False


def test_the_operation_is_allowlisted_and_holds_the_lock():
    """The agent executes names, not requests. An operation that is not in the
    allowlist cannot be reached at all, and one that changes a login policy
    without the lock could interleave with the host-config transaction."""

    from appliance.protocol import OPERATIONS

    spec = OPERATIONS["ssh.plan_shell_access"]

    assert spec.mutating is True
    assert spec.takes_lock is True
    assert [field.name for field in spec.fields] == ["enabled"]


def test_the_console_card_posts_only_to_routes_the_web_service_maps():
    """A button wired to a path nothing serves is a control that looks available
    and does nothing; the pair is asserted rather than assumed."""

    root = Path(__file__).resolve().parents[1]
    app_js = (root / "appliance" / "static" / "app.js").read_text(encoding="utf-8")
    web_py = (root / "appliance" / "web.py").read_text(encoding="utf-8")

    for endpoint in ("/api/ssh/shell-access/enable", "/api/ssh/shell-access/disable"):
        assert endpoint in app_js, f"{endpoint} is not reachable from the console"
        assert f'"{endpoint}"' in web_py, f"{endpoint} is not served"


def test_the_console_says_what_the_account_costs_before_it_is_enabled():
    """The operator is deciding whether the console becomes a root login. That
    belongs on the card, not only in a document they may never open."""

    root = Path(__file__).resolve().parents[1]
    app_js = (root / "appliance" / "static" / "app.js").read_text(encoding="utf-8")
    card = app_js.split("function renderShellAccessCard(", 1)[1].split("\n  }\n", 1)[0]

    assert "reaches root" in card
    assert "never accepts a password" in card


def write_conf(paths, text):
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.appliance_conf.write_text("[appliance]\n" + text, encoding="utf-8")


def test_an_appliance_upgraded_from_before_this_account_may_still_be_keyed(tmp_path):
    """appliance.conf is a dpkg conffile, so an upgrade never rewrites it. Every
    box installed before this account existed carries a list naming only the
    backup account -- written when there was nothing else to name. Reading that
    as a refusal left the console half of the feature inert on exactly the
    appliances it was built for, which is how this was found: enabled, and no
    way to give it a key."""

    paths = paths_at(tmp_path)
    write_conf(paths, "ssh_key_accounts = ems-backup\n")

    assert shell_access.ACCOUNT in load_config(paths).ssh_key_accounts


def test_a_refusal_that_was_actually_made_is_honoured(tmp_path):
    paths = paths_at(tmp_path)
    write_conf(paths, "ssh_key_accounts = ems-backup\nshell_key_deployment = no\n")
    config = load_config(paths)

    assert shell_access.ACCOUNT not in config.ssh_key_accounts
    assert config.backup_user in config.ssh_key_accounts


def test_refusing_deployment_leaves_the_backup_account_alone(tmp_path):
    paths = paths_at(tmp_path)
    write_conf(paths, "shell_key_deployment = no\n")

    assert load_config(paths).ssh_key_accounts == ("ems-backup",)


def test_the_console_offers_whichever_accounts_the_configuration_allows():
    """The key form's account list is read from the backend payload. A form that
    named accounts itself would have hidden this defect instead of showing it."""

    root = Path(__file__).resolve().parents[1]
    app_js = (root / "appliance" / "static" / "app.js").read_text(encoding="utf-8")
    form = app_js.split("function renderKeyForm(", 1)[1].split("\n  }\n", 1)[0]

    assert "ssh.accounts" in form
    assert "ems-backup" not in form, "the account list must come from the backend"


def test_a_home_under_slash_home_is_reported_as_unusable():
    """The agent writes authorized_keys under ProtectHome=yes, which gives it an
    empty read-only tmpfs where /home would be. 0.3.3 and 0.3.4 put the account
    there, and a live appliance failed the key deployment with EROFS instead of
    refusing at any gate. The answer is reported next to the gates now."""

    assert shell_access.home_is_writable("/var/lib/ems-shell") is True
    assert shell_access.home_is_writable("/home/ems-shell") is False
    assert shell_access.home_is_writable("") is False
    assert shell_access.home_is_writable(None) is False


def test_the_packaged_account_script_asks_for_a_writable_home():
    """Asserted against the packaged script because that is what dpkg runs, and
    a Python constant agreeing with itself would prove nothing."""

    root = Path(__file__).resolve().parents[1]
    script = (root / "packaging" / "appliance" / "bin" / "shell-account.sh").read_text(
        encoding="utf-8"
    )

    assert "HOME_DIR=${EMS_APPLIANCE_SHELL_HOME:-/var/lib/$ACCOUNT}" in script
    assert shell_access.home_is_writable("/var/lib/ems-shell")


class _Runner:
    def __init__(self, passwd):
        self._passwd = passwd

    def available(self, tool):
        return True

    def run(self, tool, args=(), **kwargs):
        from appliance.commands import CommandResult

        return CommandResult(tool=tool, args=tuple(args), returncode=0,
                             stdout=self._passwd, stderr="")


def service_for(home):
    from appliance.ssh_service import SshService

    return SshService(
        runner=_Runner(f"ems-shell:x:998:998::{home}:/bin/bash\n"),
        systemd=None, config=ApplianceConfig(), operations=None, paths=None,
    )


def test_a_key_is_refused_before_the_operation_runs_not_after(tmp_path):
    """The refusal used to arrive as a bare OSError at the end of an apply: the
    plan said yes because list() answers an absent file with [], and only the
    write discovered the read-only tmpfs. CLAUDE.md's rule for the manager
    update is the same one -- every refusal happens before it runs."""

    from appliance.ssh_service import SshServiceError

    with pytest.raises(SshServiceError) as refused:
        service_for("/home/ems-shell").keystore(shell_access.ACCOUNT)

    assert refused.value.code == "home_unwritable"
    assert "cannot be written" in refused.value.message


def test_a_writable_home_is_not_refused(tmp_path):
    store = service_for("/var/lib/ems-shell").keystore(shell_access.ACCOUNT)

    assert store is not None
