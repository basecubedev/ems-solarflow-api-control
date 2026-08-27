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
