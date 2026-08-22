# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ownership that survives a filesystem handing a released inode straight back.

Device and inode are not an identity. When a directory or a file is unlinked,
the allocator is free to give the very next creation the inode it just freed —
ext4 does it routinely, and whether it happens at all is a property of the
filesystem, not of the code under test. A package that decides "this is the home
I created" or "this is the ACL entry I granted" from ``device:inode`` alone will
therefore, on some hosts and not on others, delete an operator's directory or
withdraw an operator's ACL.

So none of these tests wait for the allocator to cooperate. Each one presents
the exact state inode reuse produces — the recorded device and inode, on an
object this package never created — and requires the refusal, on every
filesystem, every time.
"""

import json

import pytest

from appliance import backup_ownership
from appliance.backup_confinement import STATE_ACTIVE, BackupAccessActivation
from tests.helpers.appliance import build_test_services, seed_backup_account
from tests.helpers.appliance_backup_account import BACKUP_USER, BackupAccountHarness
from tests.helpers.appliance_object_identity import (
    identity_with_reused_inode,
    object_identity,
)

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

OPERATOR_KEY = "ssh-ed25519 AAAAoperator operator@laptop\n"


@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


# --- the home directory -----------------------------------------------------


def replaced_home(host, *, reuse_inode):
    """The recorded home was moved aside and a directory the operator owns is there.

    With ``reuse_inode`` the record is rewritten to the replacement's device and
    inode, which is what the package would have read had the filesystem handed
    the freed inode straight back. Nothing else about the replacement changes.
    """

    host.run("ensure")
    moved_aside = host.root / "var" / "lib" / "moved-aside"
    host.home.rename(moved_aside)
    host.home.mkdir(parents=True)
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(OPERATOR_KEY, encoding="utf-8")
    if reuse_inode:
        record = host.record()
        entry = host.home.stat()
        record["home_device"] = str(entry.st_dev)
        record["home_inode"] = str(entry.st_ino)
        host.marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return keys, moved_aside


def quarantined(host):
    if not host.quarantine_dir.is_dir():
        return []
    return sorted(item.name for item in host.quarantine_dir.iterdir())


@pytest.mark.parametrize("reuse_inode", [False, True], ids=["fresh_inode", "reused_inode"])
def test_the_helper_never_purges_a_replacement_home(host, reuse_inode):
    keys, _ = replaced_home(host, reuse_inode=reuse_inode)

    result = host.run("purge")

    assert host.account_exists(), "the account behind a replaced home was deleted"
    assert keys.read_bytes() == OPERATOR_KEY.encode("utf-8")
    assert quarantined(host) == [], quarantined(host)
    assert "deluser" not in " ".join(host.calls()), host.calls()
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.parametrize("reuse_inode", [False, True], ids=["fresh_inode", "reused_inode"])
def test_postrm_purge_never_removes_a_replacement_home(host, reuse_inode):
    keys, moved_aside = replaced_home(host, reuse_inode=reuse_inode)

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.account_exists()
    assert host.home.is_dir(), "the replacement home was removed"
    assert keys.read_bytes() == OPERATOR_KEY.encode("utf-8")
    assert moved_aside.is_dir()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


@pytest.mark.parametrize("reuse_inode", [False, True], ids=["fresh_inode", "reused_inode"])
def test_disable_withdraws_access_through_the_account_not_the_key_file(host, reuse_inode):
    keys, _ = replaced_home(host, reuse_inode=reuse_inode)

    result = host.run("disable")

    assert keys.read_bytes() == OPERATOR_KEY.encode("utf-8")
    assert not (keys.parent / "authorized_keys.disabled-by-appliance").exists()
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(line.startswith(("usermod", "chage")) for line in host.calls()), host.calls()


@pytest.mark.parametrize("reuse_inode", [False, True], ids=["fresh_inode", "reused_inode"])
def test_describe_never_calls_a_replacement_home_package_owned(host, reuse_inode):
    replaced_home(host, reuse_inode=reuse_inode)

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_replacement_home_that_copied_the_metadata_is_still_refused(host):
    """Mode, owner and timestamps are copyable; the marker's secret is not."""

    host.run("ensure")
    original = host.home.stat()
    moved_aside = host.root / "var" / "lib" / "moved-aside"
    host.home.rename(moved_aside)
    host.home.mkdir(parents=True)
    host.home.chmod(original.st_mode & 0o7777)
    record = host.record()
    entry = host.home.stat()
    record["home_device"] = str(entry.st_dev)
    record["home_inode"] = str(entry.st_ino)
    host.marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_marker_written_for_another_home_does_not_bind_this_one(host):
    """The marker names the home it belongs to, so a copied one is not evidence."""

    host.run("ensure")
    record = host.record()
    elsewhere = host.root / "var" / "lib" / "elsewhere"
    elsewhere.mkdir(parents=True)
    marker = host.home_marker()
    marker.chmod(0o600)
    marker.write_text(
        backup_ownership.render_home_marker(
            account=BACKUP_USER,
            uid=record["uid"],
            primary_gid=record["primary_gid"],
            home=str(elsewhere),
            installation_id="test-installation",
            nonce=record["home_marker_nonce"],
        ),
        encoding="utf-8",
    )

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_marker_carrying_another_secret_does_not_bind_the_home(host):
    host.run("ensure")
    record = host.record()
    marker = host.home_marker()
    marker.chmod(0o600)
    marker.write_text(
        backup_ownership.render_home_marker(
            account=BACKUP_USER,
            uid=record["uid"],
            primary_gid=record["primary_gid"],
            home=str(host.home),
            installation_id="test-installation",
            nonce=backup_ownership.new_marker_nonce(),
        ),
        encoding="utf-8",
    )

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_world_writable_marker_is_not_evidence(host):
    host.run("ensure")
    host.home_marker().chmod(0o666)

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_missing_marker_is_not_silently_recreated_by_purge(host):
    host.run("ensure")
    host.home_marker().unlink()

    result = host.run("purge")

    assert host.account_exists(), "the account was deleted although its home was unproven"
    assert host.home.is_dir()
    assert result.returncode != 0, result.stdout + result.stderr


def test_an_ensure_run_records_the_marker_it_wrote(host):
    host.run("ensure")

    record = host.record()

    assert record["schema_version"] == backup_ownership.RECORD_SCHEMA_VERSION, record
    assert record["home_marker"] == str(host.home_marker()), record
    assert len(record["home_marker_nonce"]) >= 32, record
    values = backup_ownership.read_declaration(host.home_marker())
    assert values["nonce"] == record["home_marker_nonce"], values
    assert values["home"] == str(host.home), values
    assert values["account"] == BACKUP_USER, values


def test_a_second_ensure_keeps_the_marker_it_already_bound(host):
    host.run("ensure")
    first = host.record()

    result = host.run("ensure")

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.record()["home_marker_nonce"] == first["home_marker_nonce"], host.record()


# --- the legacy record an upgrade finds -------------------------------------


def make_legacy_record(host):
    """A schema-2 record: the account is this package's, the home has no marker."""

    host.run("ensure")
    record = host.record()
    host.home_marker().unlink()
    for field in ("home_marker", "home_marker_nonce"):
        record.pop(field, None)
    record["schema_version"] = backup_ownership.LEGACY_RECORD_SCHEMA_VERSION
    host.marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def test_a_legacy_record_is_never_migrated_by_an_installation(host):
    """An install adopts nothing; it says what a person has to decide."""

    make_legacy_record(host)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "an install adopted a home it cannot prove"
    assert host.record()["schema_version"] == backup_ownership.LEGACY_RECORD_SCHEMA_VERSION
    assert "migrate-ownership" in result.stdout + result.stderr


def test_a_legacy_record_is_migrated_when_it_is_adopted_explicitly(host):
    legacy = make_legacy_record(host)

    result = host.run("migrate-ownership")

    assert result.returncode == 0, result.stdout + result.stderr
    record = host.record()
    assert record["schema_version"] == backup_ownership.RECORD_SCHEMA_VERSION, record
    assert record["home_marker_nonce"], record
    assert record["home_created_by_package"] == legacy["home_created_by_package"], record
    assert host.home_marker().is_file()


@pytest.mark.parametrize("command", ("ensure", "migrate-ownership"))
def test_a_legacy_record_whose_home_moved_is_not_adopted(host, command):
    make_legacy_record(host)
    host.home.rename(host.root / "var" / "lib" / "moved-aside")
    host.home.mkdir(parents=True)

    result = host.run(command)

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "an unproven home was adopted"
    assert host.record()["schema_version"] == backup_ownership.LEGACY_RECORD_SCHEMA_VERSION


@pytest.mark.parametrize("command", ("ensure", "migrate-ownership"))
def test_a_legacy_record_with_an_unattributable_key_is_not_adopted(host, command):
    make_legacy_record(host)
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("ssh-ed25519 AAAAsomeone-else nobody@elsewhere\n", encoding="utf-8")

    result = host.run(command)

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.home_marker().exists(), "a home holding an unattributed key was adopted"
    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAAsomeone-else nobody@elsewhere\n"


def test_a_legacy_record_is_never_treated_as_owned_before_it_migrates(host):
    make_legacy_record(host)

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_purge_before_migration_leaves_the_account_and_home_alone(host):
    make_legacy_record(host)

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.account_exists(), "a legacy record was treated as proof of ownership"
    assert host.home.is_dir()
    assert "not created by this package" in result.stdout + result.stderr


# --- the Python side --------------------------------------------------------


def python_state(tmp_path, *, reuse_inode):
    state = tmp_path / "package-state"
    state.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    nonce = backup_ownership.new_marker_nonce()
    original = home.stat()
    replacement_device, replacement_inode = str(original.st_dev), str(original.st_ino)
    if not reuse_inode:
        replacement_device, replacement_inode = "4242", "4242"
    (state / backup_ownership.RECORD_NAME).write_text(
        json.dumps(
            {
                "schema_version": backup_ownership.RECORD_SCHEMA_VERSION,
                "account": BACKUP_USER,
                "created_by_package": True,
                "uid": 1500,
                "primary_gid": 1500,
                "home": str(home),
                "home_device": replacement_device,
                "home_inode": replacement_inode,
                "home_marker": str(home / backup_ownership.HOME_MARKER_NAME),
                "home_marker_nonce": nonce,
                "home_created_by_package": True,
                "installation_id": "test-installation",
            }
        ),
        encoding="utf-8",
    )
    return state, home


class Paths:
    def __init__(self, state):
        self.package_state_dir = state


ENTRY = type("Entry", (), {"pw_uid": 1500, "pw_gid": 1500})


def passwd_entry(home):
    entry = ENTRY()
    entry.pw_dir = str(home)
    return entry


@pytest.mark.parametrize("reuse_inode", [False, True], ids=["fresh_inode", "reused_inode"])
def test_python_ownership_refuses_a_home_without_the_marker(tmp_path, reuse_inode):
    state, home = python_state(tmp_path, reuse_inode=reuse_inode)

    verdict = backup_ownership.verify_ownership(
        Paths(state), BACKUP_USER, entry=passwd_entry(home)
    )

    assert verdict["owned"] is False, verdict
    expected = (
        backup_ownership.MARKER_MISSING if reuse_inode else backup_ownership.HOME_MISMATCH
    )
    assert verdict["reason"] == expected, verdict


def bind_marker(state, home):
    record = json.loads((state / backup_ownership.RECORD_NAME).read_text(encoding="utf-8"))
    marker = home / backup_ownership.HOME_MARKER_NAME
    marker.write_text(
        backup_ownership.render_home_marker(
            account=BACKUP_USER,
            uid=1500,
            primary_gid=1500,
            home=str(home),
            installation_id="test-installation",
            nonce=record["home_marker_nonce"],
        ),
        encoding="utf-8",
    )
    marker.chmod(0o400)
    return marker


def break_nothing(marker):
    return backup_ownership.OWNED


def break_by_removing(marker):
    marker.unlink()
    return backup_ownership.MARKER_MISSING


def break_by_rewriting(marker):
    marker.chmod(0o600)
    marker.write_text("schema_version=1\naccount=ems-backup\nnonce=guessed\n", encoding="utf-8")
    marker.chmod(0o400)
    return backup_ownership.MARKER_MISMATCH


def break_by_opening_it_up(marker):
    marker.chmod(0o666)
    return backup_ownership.MARKER_MISMATCH


def break_by_replacing_with_a_symlink(marker):
    elsewhere = marker.with_name("marker-elsewhere")
    elsewhere.write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
    marker.unlink()
    marker.symlink_to(elsewhere)
    return backup_ownership.MARKER_MISMATCH


def break_by_hard_linking_it(marker):
    marker.with_name("marker-second-name").hardlink_to(marker)
    return backup_ownership.MARKER_MISMATCH


# Each case controls exactly the input it names, so the reason is the assertion
# and not a guess about what the environment did.
#
# "Unreadable" is deliberately absent: chmod 000 is a refusal for an ordinary
# user and no obstacle at all for root, so what such a case would assert is the
# privilege of whoever ran the suite. A marker root can still read, that is
# root-owned, singly linked and carries the recorded secret, *is* this package's
# marker — the cases below are the ones that mean the same thing either way.
@pytest.mark.parametrize(
    "damage",
    [break_nothing, break_by_removing, break_by_rewriting, break_by_opening_it_up,
     break_by_replacing_with_a_symlink, break_by_hard_linking_it],
    ids=["intact", "removed", "rewritten", "world_writable", "symlink", "hard_linked"],
)
def test_the_marker_state_decides_the_reason(tmp_path, damage):
    state, home = python_state(tmp_path, reuse_inode=True)
    expected = damage(bind_marker(state, home))

    verdict = backup_ownership.verify_ownership(
        Paths(state), BACKUP_USER, entry=passwd_entry(home)
    )

    assert verdict["owned"] is (expected == backup_ownership.OWNED), verdict
    assert verdict["reason"] == expected, verdict
    assert verdict["reason"] in backup_ownership.MISMATCH_REASONS + (backup_ownership.OWNED,)


def test_a_changed_home_generation_alone_does_not_grant_ownership(tmp_path):
    """Ownership needs the marker; no timestamp on the directory substitutes."""

    state, home = python_state(tmp_path, reuse_inode=True)

    verdict = backup_ownership.verify_ownership(
        Paths(state), BACKUP_USER, entry=passwd_entry(home)
    )

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.MARKER_MISSING, verdict


def test_python_ownership_refuses_a_legacy_record_until_it_is_migrated(tmp_path):
    state, home = python_state(tmp_path, reuse_inode=True)
    record = json.loads((state / backup_ownership.RECORD_NAME).read_text(encoding="utf-8"))
    record["schema_version"] = backup_ownership.LEGACY_RECORD_SCHEMA_VERSION
    (state / backup_ownership.RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")

    verdict = backup_ownership.verify_ownership(
        Paths(state), BACKUP_USER, entry=passwd_entry(home)
    )

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.MIGRATION_REQUIRED, verdict


def test_the_account_half_of_the_identity_still_holds_without_the_home(tmp_path):
    """A fail-closed step needs the account even when the home cannot be proven."""

    state, home = python_state(tmp_path, reuse_inode=True)

    verdict = backup_ownership.verify_account(
        Paths(state), BACKUP_USER, entry=passwd_entry(home)
    )

    assert verdict["owned"] is True, verdict


# --- activation must not mutate a home it cannot prove ----------------------


def replacement_home_services(tmp_path):
    services = build_test_services(tmp_path)
    home = tmp_path / "var" / "lib" / "ems-backup"
    seed_backup_account(services, home=home, key="", marker=False)
    keys = home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(OPERATOR_KEY, encoding="utf-8")
    return services, keys


def activation(services):
    return BackupAccessActivation(
        runner=services.runner,
        config=services.config,
        paths=services.paths,
        systemd=services.systemd,
        probe=services.probe,
    )


def test_activation_leaves_a_replacement_home_key_byte_identical(tmp_path):
    services, keys = replacement_home_services(tmp_path)

    report = activation(services).activate()

    assert report["state"] != STATE_ACTIVE, report
    assert keys.read_bytes() == OPERATOR_KEY.encode("utf-8")
    assert not (keys.parent / "authorized_keys.disabled-by-appliance").exists()
    assert sorted(item.name for item in keys.parent.iterdir()) == ["authorized_keys"]


def test_activation_names_the_home_it_could_not_prove(tmp_path):
    """The replacement carries no marker, and the report says exactly that."""

    services, _ = replacement_home_services(tmp_path)

    report = activation(services).activate()

    assert report["reason"] == "backup_account_home_marker_missing", report
    assert report["reason"].removeprefix("backup_account_") in (
        backup_ownership.MISMATCH_REASONS
    ), report
    assert report["operator_state_untouched"] is True, report
    assert report["home_owned"] is False, report


def test_disabling_a_replacement_home_expires_the_account_instead(tmp_path):
    services, keys = replacement_home_services(tmp_path)

    outcome = activation(services).disable(reason="test")

    assert outcome["changed"] is False, outcome
    assert outcome["authentication_disabled"] is True, outcome
    assert outcome["account_owned"] is True, outcome
    assert keys.read_bytes() == OPERATOR_KEY.encode("utf-8")


def test_disabling_without_the_account_changes_nothing_at_all(tmp_path):
    services = build_test_services(tmp_path)
    home = tmp_path / "var" / "lib" / "ems-backup"
    seed_backup_account(services, home=home, key="", account=False, marker=False)
    keys = home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(OPERATOR_KEY, encoding="utf-8")

    outcome = activation(services).disable(reason="test")

    assert outcome["changed"] is False, outcome
    assert outcome["account_owned"] is False, outcome
    assert keys.read_bytes() == OPERATOR_KEY.encode("utf-8")
    assert not [call for call in services.host.calls if call[0] == "chage"], services.host.calls


def test_a_package_owned_home_still_has_its_key_preserved(tmp_path):
    """The non-destructive rule is a boundary, not a refusal to act."""

    services = build_test_services(tmp_path)
    home = tmp_path / "var" / "lib" / "ems-backup"
    seed_backup_account(services, home=home)
    keys = home / ".ssh" / "authorized_keys"

    outcome = activation(services).disable(reason="test")

    assert outcome["changed"] is True, outcome
    assert outcome["home_owned"] is True, outcome
    assert not keys.exists()
    assert (keys.parent / "authorized_keys.disabled-by-appliance").is_file()


# --- ACL objects ------------------------------------------------------------


def exported_tree(host):
    config = host.install_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    granted = config / "config.json"
    granted.write_text("{}\n", encoding="utf-8")
    host.set_acl(config, BACKUP_USER, "r-x")
    host.set_acl(config, BACKUP_USER, "r-x", kind="default")
    host.set_acl(granted, BACKUP_USER, "r--")
    host.run("ensure")
    return config, granted


def test_purge_keeps_an_acl_on_a_file_that_reused_the_recorded_inode(host):
    config, granted = exported_tree(host)
    recorded = object_identity(granted)
    granted.unlink()
    granted.write_text("{}\n", encoding="utf-8")
    host.set_acl(granted, BACKUP_USER, "r--")
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", None, "r-x"), (granted, "access", None, "r--")],
        identities={granted: identity_with_reused_inode(recorded, granted)},
    )

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_keeps_an_acl_on_a_directory_that_reused_the_recorded_inode(host):
    config, granted = exported_tree(host)
    nested = config / "nested"
    nested.mkdir()
    host.set_acl(nested, BACKUP_USER, "r-x")
    recorded = object_identity(nested)
    nested.rmdir()
    nested.mkdir()
    host.set_acl(nested, BACKUP_USER, "r-x")
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(nested, "access", None, "r-x")],
        identities={nested: identity_with_reused_inode(recorded, nested)},
    )

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(nested, BACKUP_USER) == "r-x", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_keeps_an_acl_on_an_object_whose_owner_changed(host):
    """An entry means something else once the object belongs to somebody else."""

    config, granted = exported_tree(host)
    recorded = object_identity(granted).split(":")
    recorded[3] = str(int(recorded[3]) + 1)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(granted, "access", None, "r--")],
        identities={granted: ":".join(recorded)},
    )

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_keeps_an_acl_whose_recorded_identity_is_unavailable(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(granted, "access", None, "r--")],
        identities={granted: "-"},
    )

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_withdraws_the_entry_on_an_object_that_is_still_the_same_one(host):
    """The conservative rule may not turn into "never remove anything"."""

    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[
            (config, "access", None, "r-x"),
            (config, "default", None, "r-x"),
            (granted, "access", None, "r--"),
        ],
    )

    host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) is None, host.acl_state()
    assert host.acl_entry(config, BACKUP_USER) is None, host.acl_state()


def test_purge_skips_an_object_that_no_longer_exists(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[
            (config, "access", None, "r-x"),
            (config, "default", None, "r-x"),
            (granted, "access", None, "r--"),
        ],
    )
    granted.unlink()

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(config, BACKUP_USER) is None, host.acl_state()
    assert "purge did not complete" not in result.stderr, result.stdout + result.stderr


def test_a_recorded_identity_is_never_satisfied_by_a_device_and_inode_alone(host):
    """The property every test above depends on, stated once and checked."""

    config = host.install_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    granted = config / "config.json"
    granted.write_text("{}\n", encoding="utf-8")
    recorded = object_identity(granted)
    granted.unlink()
    granted.write_text("{}\n", encoding="utf-8")

    reused = identity_with_reused_inode(recorded, granted)

    assert reused.split(":")[:2] == object_identity(granted).split(":")[:2]
    assert reused != object_identity(granted)
