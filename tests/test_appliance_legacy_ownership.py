# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an ownership record from an older package may and may not authorise.

A record is a claim, not a proof. The fields an older schema carries —
``created_by_package``, an account name, a home path, a device and an inode —
are all reproducible by anything that can write into the state directory, and
device and inode are reproducible by the filesystem itself the moment an inode
is handed out again. None of them can establish that the account and the home
found now are the ones this package created.

So a record whose schema predates the root-owned home marker never upgrades
itself. It is reported, the backup access stays off, and an administrator
migrates it explicitly with a command that says what it is about to adopt.
"""

import json

import pytest

from appliance import backup_ownership
from tests.helpers.appliance_backup_account import BACKUP_USER, BackupAccountHarness

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

FOREIGN_KEY = "ssh-ed25519 AAAAforeign somebody@elsewhere\n"
FOREIGN_MARKER_TEXT = "schema_version=1\naccount=ems-backup\nnonce=guessed\n"


@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


def foreign_home(host, *, uid=4242, keys=FOREIGN_KEY):
    """An ``ems-backup`` account and home an operator made, at the configured path."""

    home = host.add_account(home=host.home, uid=uid)
    (home / "operator-data").write_text("not this package's\n", encoding="utf-8")
    key_file = home / ".ssh" / "authorized_keys"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(keys, encoding="utf-8")
    return home, key_file


def schema_less_record(host, *, home, uid=1500, gid=1500, created_by_package=True):
    """The record an installation from before the schema field left behind."""

    payload = {
        "account": BACKUP_USER,
        "created_by_package": created_by_package,
        "uid": uid,
        "primary_gid": gid,
        "home": str(home),
    }
    host.marker.parent.mkdir(parents=True, exist_ok=True)
    host.marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def schema_two_record(host, *, home, uid=1500, gid=1500, device=None, inode=None):
    """A schema-2 record: no marker, the home bound by device and inode alone."""

    if device is None or inode is None:
        status = home.stat()
        device = str(status.st_dev) if device is None else str(device)
        inode = str(status.st_ino) if inode is None else str(inode)
    payload = {
        "schema_version": backup_ownership.LEGACY_RECORD_SCHEMA_VERSION,
        "account": BACKUP_USER,
        "created_by_package": True,
        "uid": uid,
        "primary_gid": gid,
        "home": str(home),
        "home_device": device,
        "home_inode": inode,
        "home_created_by_package": True,
    }
    host.marker.parent.mkdir(parents=True, exist_ok=True)
    host.marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def managed_hashes(host):
    if not host.managed_keys.is_file():
        return []
    return [line for line in host.managed_keys.read_text(encoding="utf-8").split() if line]


# --- a record with no schema version establishes nothing ---------------------


@pytest.mark.parametrize("uid", (4242, 1500), ids=("foreign-uid", "recorded-uid"))
def test_a_schema_less_record_never_adopts_the_account_it_names(host, uid):
    """Same name, same home path, a record anyone could have written."""

    home, key_file = foreign_home(host, uid=uid)
    schema_less_record(host, home=home, uid=uid, gid=uid)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker(home).exists(), "a foreign home was marked as package-owned"
    assert managed_hashes(host) == [], "a foreign key was attributed to this package"
    assert key_file.read_text(encoding="utf-8") == FOREIGN_KEY
    assert (home / "operator-data").is_file()
    assert host.record().get("schema_version") is None, host.record()


def test_a_schema_less_record_is_refused_even_with_a_marker_like_file(host):
    """A file at the marker path is not the marker; it is somebody else's file."""

    home, _ = foreign_home(host)
    forged = host.home_marker(home)
    forged.write_text(FOREIGN_MARKER_TEXT, encoding="utf-8")
    schema_less_record(host, home=home)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert forged.read_text(encoding="utf-8") == FOREIGN_MARKER_TEXT, "the marker was rewritten"
    assert managed_hashes(host) == []


def test_a_schema_less_record_names_the_record_and_the_home_to_review(host):
    """No command can adopt this; the message says what a person has to look at."""

    foreign_home(host)
    schema_less_record(host, home=host.home)

    result = host.run("ensure")

    combined = result.stdout + result.stderr
    assert str(host.marker) in combined, combined
    assert str(host.home) in combined, combined
    assert "migrate-ownership" not in combined, combined


def test_a_schema_less_record_leaves_the_account_unchanged(host):
    home, _ = foreign_home(host)
    schema_less_record(host, home=home)

    host.run("ensure")

    assert host.account_exists()
    assert host.account_field(2) == "4242", host.account_field(2)
    assert host.account_field(5) == str(home)
    assert "adduser" not in [line.split(" ", 1)[0] for line in host.calls()], host.calls()


def test_a_schema_less_record_leaves_the_acl_state_untouched(host):
    home, _ = foreign_home(host)
    host.set_acl(home, BACKUP_USER, "rwx")
    schema_less_record(host, home=home)

    host.run("ensure")

    assert host.acl_entry(home, BACKUP_USER) == "rwx", host.acl_state()
    assert "setfacl" not in [line.split(" ", 1)[0] for line in host.calls()], host.calls()


def test_a_schema_less_record_is_never_described_as_package_owned(host):
    foreign_home(host)
    schema_less_record(host, home=host.home)

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_schema_less_record_reports_the_migration_state(host):
    foreign_home(host)
    schema_less_record(host, home=host.home)

    result = host.run("ownership-state")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "legacy_manual_migration_required", result.stdout


def test_a_schema_less_record_keeps_backup_access_disabled(host):
    home, _ = foreign_home(host)
    schema_less_record(host, home=home)
    host.run("ensure")

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / ".ssh" / "authorized_keys").read_text(encoding="utf-8") == FOREIGN_KEY


# --- schema 2 cannot be upgraded from device and inode ----------------------


def test_a_schema_two_record_is_not_migrated_automatically(host):
    host.add_account(home=host.home, uid=1500)
    schema_two_record(host, home=host.home)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "a schema-2 home was adopted without proof"
    assert host.record()["schema_version"] == backup_ownership.LEGACY_RECORD_SCHEMA_VERSION


def replacement_home_with_recorded_identity(host, *, keys=None):
    """The exact state inode reuse produces, injected rather than waited for.

    The recorded home is moved aside and a directory somebody else made takes
    its place; the record is then written with the replacement's own device and
    inode, which is what the package would have read had the filesystem handed
    the freed inode straight back.
    """

    host.add_account(home=host.home, uid=1500)
    host.home.rename(host.root / "var" / "lib" / "moved-aside")
    host.home.mkdir(parents=True)
    (host.home / "operator-data").write_text("somebody else's\n", encoding="utf-8")
    key_file = None
    if keys is not None:
        key_file = host.home / ".ssh" / "authorized_keys"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(keys, encoding="utf-8")
    replacement = host.home.stat()
    schema_two_record(
        host, home=host.home, device=replacement.st_dev, inode=replacement.st_ino
    )
    return key_file


def test_a_replacement_home_presenting_the_recorded_inode_is_not_migrated(host):
    """Nothing but device and inode agrees, and that is not an identity."""

    replacement_home_with_recorded_identity(host)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "a replacement home was adopted"
    assert host.account_exists()
    assert (host.home / "operator-data").is_file()
    assert host.record()["schema_version"] == backup_ownership.LEGACY_RECORD_SCHEMA_VERSION


def test_a_replacement_home_holding_a_key_is_not_migrated_either(host):
    key_file = replacement_home_with_recorded_identity(host, keys=FOREIGN_KEY)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "a replacement home was adopted"
    assert key_file.read_text(encoding="utf-8") == FOREIGN_KEY
    assert managed_hashes(host) == []


def test_the_explicit_migration_refuses_a_replacement_home_too(host):
    """Device and inode equality is never sufficient, not even on request."""

    replacement_home_with_recorded_identity(host)

    result = host.run("migrate-ownership")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "a replacement home was adopted on request"
    assert (host.home / "operator-data").is_file()


def test_a_schema_two_record_names_the_manual_migration(host):
    host.add_account(home=host.home, uid=1500)
    schema_two_record(host, home=host.home)

    result = host.run("ensure")

    combined = result.stdout + result.stderr
    assert "migrate-ownership" in combined, combined


def test_a_schema_two_record_reports_the_migration_state(host):
    host.add_account(home=host.home, uid=1500)
    schema_two_record(host, home=host.home)

    result = host.run("ownership-state")

    assert result.stdout.strip() == "legacy_manual_migration_required", result.stdout


# --- the explicit migration -------------------------------------------------


def package_home(host):
    """A schema-2 record over a home that really is the one this package made."""

    host.run("ensure")
    record = host.record()
    host.home_marker().unlink()
    for field in ("home_marker", "home_marker_nonce"):
        record.pop(field, None)
    record["schema_version"] = backup_ownership.LEGACY_RECORD_SCHEMA_VERSION
    host.marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def test_the_explicit_migration_binds_a_home_that_still_checks_out(host):
    legacy = package_home(host)

    result = host.run("migrate-ownership")

    assert result.returncode == 0, result.stdout + result.stderr
    record = host.record()
    assert record["schema_version"] == backup_ownership.RECORD_SCHEMA_VERSION, record
    assert record["home_marker_nonce"], record
    assert record["home_created_by_package"] == legacy["home_created_by_package"], record
    assert host.home_marker().is_file()


def test_the_explicit_migration_re_verifies_what_it_committed(host):
    package_home(host)

    host.run("migrate-ownership")

    assert host.run("describe").stdout.replace(" ", "").find('"package_owned":true') >= 0


def test_the_explicit_migration_refuses_a_schema_less_record(host):
    home, key_file = foreign_home(host)
    schema_less_record(host, home=home)

    result = host.run("migrate-ownership")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker(home).exists()
    assert key_file.read_text(encoding="utf-8") == FOREIGN_KEY
    assert managed_hashes(host) == []


def test_the_explicit_migration_refuses_a_home_holding_an_unknown_key(host):
    package_home(host)
    key_file = host.home / ".ssh" / "authorized_keys"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(FOREIGN_KEY, encoding="utf-8")

    result = host.run("migrate-ownership")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists()
    assert key_file.read_text(encoding="utf-8") == FOREIGN_KEY


def test_the_explicit_migration_refuses_a_home_that_moved(host):
    package_home(host)
    host.home.rename(host.root / "var" / "lib" / "moved-aside")
    host.home.mkdir(parents=True)

    result = host.run("migrate-ownership")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists()


def test_the_explicit_migration_refuses_a_replaced_account(host):
    package_home(host)
    host.remove_account()
    host.add_account(home=host.home, uid=4242)

    result = host.run("migrate-ownership")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists()


def test_the_explicit_migration_says_what_it_would_adopt(host):
    package_home(host)

    result = host.run("migrate-ownership")

    combined = result.stdout + result.stderr
    assert str(host.home) in combined, combined
    assert BACKUP_USER in combined, combined


def test_a_migrated_record_then_reports_the_current_state(host):
    package_home(host)
    host.run("migrate-ownership")

    result = host.run("ownership-state")

    assert result.stdout.strip() == "current", result.stdout


# --- states an operator can act on ------------------------------------------


def test_a_missing_record_reports_no_record(host):
    result = host.run("ownership-state")

    assert result.stdout.strip() == "no_ownership_record", result.stdout


def test_a_record_naming_another_account_reports_a_conflict(host):
    host.add_account(home=host.home, uid=1500)
    host.write_record()
    record = host.record()
    record["account"] = "somebody-else"
    host.marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    result = host.run("ownership-state")

    assert result.stdout.strip() == "ownership_conflict", result.stdout


def test_a_missing_marker_is_reported_as_a_missing_marker(host):
    host.run("ensure")
    host.home_marker().unlink()

    result = host.run("ownership-state")

    assert result.stdout.strip() == "marker_missing", result.stdout


def test_a_rewritten_marker_is_reported_as_a_mismatch(host):
    host.run("ensure")
    marker = host.home_marker()
    marker.chmod(0o600)
    marker.write_text(FOREIGN_MARKER_TEXT, encoding="utf-8")

    result = host.run("ownership-state")

    assert result.stdout.strip() == "marker_mismatch", result.stdout


def test_an_unreadable_record_is_reported_as_corrupt(host):
    host.marker.parent.mkdir(parents=True, exist_ok=True)
    host.marker.write_text("this is not json\n", encoding="utf-8")

    result = host.run("ownership-state")

    assert result.stdout.strip() == "record_corrupt", result.stdout


def test_an_owned_installation_is_never_reported_as_legacy(host):
    host.run("ensure")

    result = host.run("ownership-state")

    assert result.stdout.strip() == "current", result.stdout
