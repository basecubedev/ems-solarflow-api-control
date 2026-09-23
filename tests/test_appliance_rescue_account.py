# SPDX-License-Identifier: AGPL-3.0-or-later
"""The account that gets an operator back in when nothing else does.

Under A/B a failed update rebooted into the other slot. A single-slot appliance
has no such move, and until now it had no login either: no human account, root
locked, ``sulogin`` at rescue.target. An appliance whose automatic revert fails
was a re-flash.

So a rescue account ships with a password that is written down. That is a
deliberate trade and it is stated once rather than argued: the credentials are
public knowledge, so anything that later exposes the appliance beyond a private
home network is a login for whoever finds it. Changing the password is offered
and never demanded -- the console reports which state it is in, and does not
insist.

What is defended here is that the offer stays honest: the appliance can tell
the difference between the shipped password and a changed one, an install never
resets a password an operator chose, and nothing claims a state it cannot read.
"""

import stat
from pathlib import Path

import pytest

from appliance import rescue_account

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.fixture(autouse=True)
def packaged_hash(monkeypatch):
    monkeypatch.setenv("EMS_APPLIANCE_DATADIR", str(PACKAGING / "config"))
    rescue_account.default_hash.cache_clear()
    yield
    rescue_account.default_hash.cache_clear()


def host(tmp_path, *, passwd=None, shadow=None):
    etc = tmp_path / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    if passwd is not None:
        (etc / "passwd").write_text(passwd, encoding="utf-8")
    if shadow is not None:
        (etc / "shadow").write_text(shadow, encoding="utf-8")
    return tmp_path


def passwd_line(shell="/bin/bash", uid=1001):
    return f"{rescue_account.ACCOUNT}:x:{uid}:{uid}::/home/{rescue_account.ACCOUNT}:{shell}\n"


def shadow_line(field):
    return f"{rescue_account.ACCOUNT}:{field}:20000:0:99999:7:::\n"


# --- what the appliance can say about the account ----------------------------


def test_a_freshly_flashed_appliance_reports_the_shipped_password(tmp_path):
    root = host(tmp_path, passwd=passwd_line(), shadow=shadow_line(rescue_account.default_hash()))

    state = rescue_account.state(root)

    assert state.present
    assert state.password_is_default is True
    assert not state.locked
    assert state.unreadable == ""


def test_an_operator_who_changed_it_is_reported_as_having_changed_it(tmp_path):
    root = host(tmp_path, passwd=passwd_line(), shadow=shadow_line("$6$other$hash"))

    state = rescue_account.state(root)

    assert state.present
    assert state.password_is_default is False


def test_a_locked_account_is_named_as_locked_rather_than_as_changed(tmp_path):
    """`!` in front of a hash is a different fact from a different hash."""

    root = host(
        tmp_path,
        passwd=passwd_line(),
        shadow=shadow_line("!" + rescue_account.default_hash()),
    )

    state = rescue_account.state(root)

    assert state.locked
    assert state.password_is_default is True


def test_an_account_that_is_not_there_is_not_reported_as_secure(tmp_path):
    root = host(tmp_path, passwd="root:x:0:0::/root:/bin/sh\n", shadow="root:!:20000:0:99999:7:::\n")

    state = rescue_account.state(root)

    assert not state.present
    assert state.password_is_default is None


def test_a_shadow_file_this_process_cannot_read_says_so(tmp_path):
    """Unreadable is not "changed": the console must not imply an answer."""

    root = host(tmp_path, passwd=passwd_line())

    state = rescue_account.state(root)

    assert state.present
    assert state.password_is_default is None
    assert state.unreadable


def test_a_login_shell_is_what_makes_it_a_rescue_account(tmp_path):
    root = host(
        tmp_path,
        passwd=passwd_line(shell="/usr/sbin/nologin"),
        shadow=shadow_line(rescue_account.default_hash()),
    )

    state = rescue_account.state(root)

    assert state.can_log_in is False


def test_the_report_is_json_serialisable_and_carries_no_hash(tmp_path):
    """The console is told which state it is in, never the material."""

    root = host(tmp_path, passwd=passwd_line(), shadow=shadow_line(rescue_account.default_hash()))

    payload = rescue_account.state(root).to_dict()

    assert set(payload) == {
        "account",
        "present",
        "password_is_default",
        "password_set",
        "locked",
        "can_log_in",
        "shell",
        "uid",
        "unreadable",
    }
    assert rescue_account.default_hash() not in str(payload)


# --- the shipped default -----------------------------------------------------


def test_the_default_password_hashes_to_what_the_package_ships():
    """One owner for the constant: the file the postinst reads."""

    import subprocess

    shipped = rescue_account.default_hash()
    salt = shipped.split("$")[2]
    recomputed = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, rescue_account.DEFAULT_PASSWORD],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert recomputed == shipped


def test_the_hash_is_a_modern_one():
    assert rescue_account.default_hash().startswith("$6$")


def test_a_missing_hash_file_is_a_refusal_rather_than_an_empty_password(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_APPLIANCE_DATADIR", str(tmp_path))
    rescue_account.default_hash.cache_clear()

    with pytest.raises(rescue_account.RescueAccountError):
        rescue_account.default_hash()


# --- what the package does ---------------------------------------------------


def test_the_package_ships_the_hash_and_the_helper():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")

    assert "rescue-password.hash" in build
    assert "rescue-account.sh" in build
    assert (PACKAGING / "config" / "rescue-password.hash").is_file()
    helper = PACKAGING / "bin" / "rescue-account.sh"
    assert helper.is_file()
    assert stat.S_IMODE(helper.stat().st_mode) & stat.S_IXUSR


def test_the_postinst_creates_the_account_and_never_resets_it():
    """An upgrade must not undo a password an operator chose."""

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    helper = (PACKAGING / "bin" / "rescue-account.sh").read_text(encoding="utf-8")

    assert "rescue-account.sh" in postinst
    assert "getent passwd" in helper
    assert "already exists" in helper


def test_the_helper_needs_no_python():
    """It runs from a postinst that is replacing appliance/*.py."""

    commands = "\n".join(
        line
        for line in (PACKAGING / "bin" / "rescue-account.sh").read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )

    assert "python" not in commands.lower()


def test_the_image_carries_what_a_console_login_needs():
    """A rescue account that cannot become root is not a rescue account."""

    for layer in ("ems-appliance.yaml",):
        text = (PACKAGING / "image" / "layer" / layer).read_text(encoding="utf-8")
        assert "\n    - sudo\n" in text, layer


def test_the_password_is_documented_where_an_operator_will_look():
    recovery = (ROOT / "docs" / "appliance" / "console-recovery.md").read_text(encoding="utf-8")

    assert rescue_account.ACCOUNT in recovery
    assert rescue_account.DEFAULT_PASSWORD in recovery
    # The trade is stated, not buried.
    assert "public knowledge" in recovery
    assert "optional" in recovery.lower()
    # The refusal is a promise the package writes; whether the daemon keeps it
    # is reported, and the document says so rather than asserting it.
    assert "reports whether the running daemon" in recovery


# --- what the console is allowed to say --------------------------------------


def test_the_state_reaches_the_console_through_the_status_payload():
    """One reader, one payload: the browser never inspects an account itself."""

    status = (ROOT / "appliance" / "status.py").read_text(encoding="utf-8")

    assert "rescue_account.state(" in status
    assert '"rescue"' in status


def test_the_console_distinguishes_every_state_the_backend_can_prove():
    """Including the one that is not an answer.

    Rendering "could not read" as "changed" would tell an owner their appliance
    is safer than this code can see.
    """

    app = (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")
    section = app.split("function rescueState(rescue) {", 1)[1].split("\n  }", 1)[0]

    assert "not present" in section
    assert "unknown" in section
    assert "shipped password" in section
    assert "changed" in section
    assert "password_is_default === null" in section, "unreadable is its own state"


def test_the_console_offers_the_change_and_does_not_demand_it():
    app = (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")
    section = app.split("function rescueState(rescue) {", 1)[1].split("\n  }", 1)[0]

    assert "sudo passwd " in section
    assert "public knowledge" in section
    # No button, no plan, no operation: the appliance never changes it for you.
    assert "planOperation" not in section


def test_the_password_is_not_the_account_name():
    """Equal would put the password into every payload that names the account.

    A status field, a log line and a support archive all carry ``ems-rescue``
    legitimately. If that string were also the password, "this archive carries
    no password" would be a property nobody could test — and the console's own
    status payload would be leaking one every time it rendered.
    """

    assert rescue_account.DEFAULT_PASSWORD != rescue_account.ACCOUNT


# --- an account that exists is not the same as an account that works ---------


def test_an_account_with_no_password_at_all_is_not_reported_as_changed(tmp_path):
    """`*` is what adduser --disabled-password writes, not a password.

    `stored = field.lstrip("!*")` is empty for it, so `password_is_default` was
    False and the console showed a green "changed / The password is no longer
    the shipped one" over an account that has no password and cannot log in
    anywhere. The one thing an operator would check before relying on it says
    the opposite of the truth.
    """

    root = host(tmp_path, passwd=passwd_line(), shadow=shadow_line("*"))

    state = rescue_account.state(root)

    assert state.present
    assert state.password_set is False
    assert state.password_is_default is not False, "no password is not a changed password"
    assert not state.can_log_in


def test_a_locked_account_with_a_real_hash_still_has_a_password(tmp_path):
    root = host(
        tmp_path, passwd=passwd_line(), shadow=shadow_line("!" + rescue_account.default_hash())
    )

    state = rescue_account.state(root)

    assert state.password_set is True


def test_the_console_names_an_account_with_no_password(tmp_path):
    source = (
        Path(__file__).resolve().parents[1] / "appliance" / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "password_set" in source, "the console cannot distinguish a state it never reads"


def rescue_helper_run(tmp_path, *, shadow_field, chpasswd_exit=0):
    """The shipped helper, against a fake getent/adduser/chpasswd/usermod."""

    import os
    import subprocess

    tools = tmp_path / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    shadow = tmp_path / "shadow"
    shadow.write_text(f"{rescue_account.ACCOUNT}:{shadow_field}:20000:0:99999:7:::\n", "utf-8")
    log = tools / "calls.log"

    (tools / "getent").write_text(
        "#!/bin/sh\n"
        f'echo "getent $*" >> "{log}"\n'
        'case "$1" in\n'
        f'  passwd) [ "$2" = "{rescue_account.ACCOUNT}" ] && exit 0; exit 2 ;;\n'
        "  shadow)\n"
        f'    line=$(grep "^$2:" "{shadow}" 2>/dev/null) || exit 2\n'
        '    printf "%s\\n" "$line"; exit 0 ;;\n'
        "  group) exit 2 ;;\n"
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    (tools / "adduser").write_text(f'#!/bin/sh\necho "adduser $*" >> "{log}"\n', encoding="utf-8")
    (tools / "usermod").write_text(f'#!/bin/sh\necho "usermod $*" >> "{log}"\n', encoding="utf-8")
    (tools / "chpasswd").write_text(
        f'#!/bin/sh\ncat >> "{log}.stdin"\necho "chpasswd $*" >> "{log}"\nexit {chpasswd_exit}\n',
        encoding="utf-8",
    )
    for name in ("getent", "adduser", "usermod", "chpasswd"):
        (tools / name).chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = f"{tools}:{environment['PATH']}"
    environment["EMS_APPLIANCE_DATADIR"] = str(PACKAGING / "config")
    environment["EMS_APPLIANCE_RESCUE_HASH_FILE"] = str(
        PACKAGING / "config" / "rescue-password.hash"
    )
    result = subprocess.run(
        ["sh", str(PACKAGING / "bin" / "rescue-account.sh")],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    calls = log.read_text(encoding="utf-8") if log.is_file() else ""
    return result, calls


def test_an_account_left_without_a_password_gets_one_on_the_next_run(tmp_path):
    """"Exists" is not "finished", and the rescue is what pays for the mistake.

    The helper creates the account with --disabled-password and sets the
    documented hash in a second step. Anything between the two -- a locked
    /etc/shadow, a full filesystem, an interrupted install -- leaves an account
    with no password. `dpkg --configure -a` then runs the helper again, it finds
    the account, reports "already exists; leaving it untouched" and exits 0. The
    install completes, and the account console-recovery.md calls "the account
    you log in with at a keyboard and monitor" can never log in. Reinstalling
    does not help: postrm never deletes it.
    """

    result, calls = rescue_helper_run(tmp_path, shadow_field="*")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "chpasswd" in calls, result.stdout + result.stderr


def test_a_password_an_operator_chose_is_never_reset(tmp_path):
    result, calls = rescue_helper_run(tmp_path, shadow_field="$6$operator$chose$this")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "chpasswd" not in calls


def test_a_deliberately_locked_account_is_not_re_enabled(tmp_path):
    """`passwd -l` leaves `!` in front of a hash; that is a decision, not a gap."""

    result, calls = rescue_helper_run(tmp_path, shadow_field="!$6$operator$chose$this")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "chpasswd" not in calls
# --- what the running daemon says --------------------------------------------


def appliance_paths():
    from appliance.paths import AppliancePaths

    return AppliancePaths(
        install_root=Path("/opt/ems-solarflow"),
        config_dir=Path("/etc/ems-appliance-manager"),
        state_dir=Path("/var/lib/ems-appliance-manager"),
        log_dir=Path("/var/log/ems-appliance-manager"),
        runtime_dir=Path("/run/ems-appliance-manager"),
        export_root=Path("/srv/ems-appliance-export"),
    )


def test_the_running_daemon_is_asked_whether_it_refuses_the_rescue_password(tmp_path):
    """A drop-in on disk is a promise; ``sshd -T -C user=ems-rescue`` is the fact.

    The backup account's confinement is read back from the daemon before the
    console calls it confined. The refusal of the rescue password -- printed in
    this repository, on an account that reaches root -- was documented and
    never asked for.
    """

    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    payload = services.status.system()["rescue"]

    assert payload["ssh"]["state"] == "refused"
    assert payload["ssh"]["user"] == rescue_account.ACCOUNT
    assert payload["ssh"]["violations"] == []


def test_a_dropin_the_daemon_never_read_is_a_network_login(tmp_path):
    """The concrete case.

    An ``/etc/ssh/sshd_config`` carried over from an older install has no
    ``Include /etc/ssh/sshd_config.d/*.conf`` line, and dpkg never rewrites a
    modified conffile. The Match block the package wrote is on disk; the
    daemon applies the global policy, which on a Raspberry Pi somebody already
    administers over a password says yes.
    """

    from tests.helpers.appliance import SSHD_CONFIG, build_test_services

    services = build_test_services(tmp_path)
    services.host.sshd_rescue_match = SSHD_CONFIG.replace(
        "passwordauthentication no", "passwordauthentication yes"
    )
    payload = services.status.system()["rescue"]["ssh"]

    assert payload["state"] == "accepted"
    assert payload["restrictions"]["passwordauthentication"]["value"] == "yes"
    # Not written at all, so sshd's default applies -- and that default asks
    # for the same password through PAM.
    assert "kbdinteractiveauthentication" in payload["violations"]


def test_a_policy_that_could_not_be_read_is_never_a_refusal(tmp_path):
    """Not knowing is its own answer, and it is not the reassuring one."""

    from tests.helpers.appliance import build_test_services

    failing = build_test_services(tmp_path / "failing")
    failing.host.fail_command("sshd")
    assert failing.status.system()["rescue"]["ssh"]["state"] == "unknown"

    without = build_test_services(tmp_path / "without")
    without.host.tools.discard("sshd")
    assert without.status.system()["rescue"]["ssh"]["state"] == "absent"


def test_the_check_asks_for_exactly_what_the_policy_writes():
    """Generator and check name the same directives, or one of them drifts.

    The rescue block leaves ``PubkeyAuthentication`` to the global setting on
    purpose -- console-recovery.md offers a key login on that account -- so the
    check must not demand it either.
    """

    from appliance.config import ApplianceConfig
    from appliance.host_config import render_sshd_policy
    from appliance.ssh_policy import RESCUE_REFUSED_METHODS, parse_sshd_config

    policy = render_sshd_policy(appliance_paths(), ApplianceConfig(), shell_access_enabled=False)
    block = policy.split(f"Match User {rescue_account.ACCOUNT}\n", 1)[1].split("Match ", 1)[0]
    written = parse_sshd_config(block)

    assert dict(RESCUE_REFUSED_METHODS) == {
        option: value for option, value in written.items() if option != "permitrootlogin"
    }
    assert "pubkeyauthentication" not in dict(RESCUE_REFUSED_METHODS)


def test_the_console_names_the_state_the_daemon_reported():
    """Four answers, and the alarming one comes before the password verdict."""

    app = (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")
    section = app.split("function rescueState(rescue) {", 1)[1].split("\n  }", 1)[0]
    card = app.split("function rescueCard() {", 1)[1].split("\n  }", 1)[0]

    assert 'state === "accepted"' in section
    assert section.index('"accepted"') < section.index("password_is_default"), (
        "a password sshd accepts from the network is reported before whether it is the shipped one"
    )
    assert "SSH password" in card
    for state in ("refused", "accepted", "unknown", "absent"):
        assert f'"{state}"' in app.split("function rescueSshLabel(", 1)[1].split("\n  }", 1)[0]
