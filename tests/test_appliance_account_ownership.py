# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact ownership of the backup account, its home, its keys and its ACLs.

A name is not an identity. Everything this package removes has to be provably
the thing this package created, or the operator's host loses data a package was
never allowed to touch: an account someone else made, a home that was already
there, key material nobody attributed, an ACL entry that predates the
installation.

The purge paths run twice here — through the account helper and through the
maintainer script dpkg actually invokes — because they are two implementations
of the same rule and only one of them used to be tested.
"""

import json
from types import SimpleNamespace

import pytest

from appliance import backup_ownership
from tests.helpers.appliance_backup_account import (
    ACCOUNT_SCRIPT,
    BACKUP_USER,
    POSTRM_SCRIPT,
    BackupAccountHarness,
)
from tests.helpers.appliance_export_script import EXPORT_SCRIPT

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore]


@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


def bind_home(state, home, *, nonce=None, marker=True, device=None, inode=None, **fields):
    """Write the record ``ensure`` writes, and the marker it leaves in the home."""

    nonce = nonce or backup_ownership.new_marker_nonce()
    try:
        entry = home.stat()
        device = str(entry.st_dev) if device is None else str(device)
        inode = str(entry.st_ino) if inode is None else str(inode)
    except OSError:
        device, inode = device or "", inode or ""
    payload = {
        "schema_version": backup_ownership.RECORD_SCHEMA_VERSION,
        "account": BACKUP_USER,
        "created_by_package": True,
        "uid": 1500,
        "primary_gid": 1500,
        "home": str(home),
        "home_device": device,
        "home_inode": inode,
        "home_marker": str(home / backup_ownership.HOME_MARKER_NAME),
        "home_marker_nonce": nonce,
        "home_created_by_package": True,
        "installation_id": "test-installation",
    }
    payload.update(fields)
    if marker and home.is_dir():
        write_marker(home, nonce)
    (state / backup_ownership.RECORD_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def write_marker(home, nonce, *, account=BACKUP_USER, uid=1500, gid=1500, home_path=None):
    path = home / backup_ownership.HOME_MARKER_NAME
    path.write_text(
        backup_ownership.render_home_marker(
            account=account,
            uid=uid,
            primary_gid=gid,
            home=str(home_path if home_path is not None else home),
            installation_id="test-installation",
            nonce=nonce,
        ),
        encoding="utf-8",
    )
    path.chmod(0o400)
    return path


@pytest.fixture
def owned(tmp_path):
    """A package-state directory whose record binds a real home directory."""

    state = tmp_path / "package-state"
    state.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    record = bind_home(state, home)
    paths = SimpleNamespace(package_state_dir=state)
    passwd = SimpleNamespace(pw_uid=1500, pw_gid=1500, pw_dir=str(home))
    return SimpleNamespace(
        paths=paths, home=home, entry=passwd, state=state, nonce=record["home_marker_nonce"]
    )


def operator_account(host):
    """An ``ems-backup`` login the operator made, with their own key file."""

    home = host.add_account(shell="/bin/sh", uid=4242)
    keys = home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("ssh-ed25519 AAAAoperator operator@laptop\n", encoding="utf-8")
    return home, keys


# --- a key file that is not this package's to remove ------------------------


def test_helper_purge_keeps_the_keys_of_an_account_it_does_not_own(host):
    home, keys = operator_account(host)

    result = host.run("purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.account_exists()
    assert keys.is_file(), sorted(item.name for item in (home / ".ssh").iterdir())
    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAAoperator operator@laptop\n"


def test_postrm_purge_keeps_the_keys_of_an_account_it_does_not_own(host):
    home, keys = operator_account(host)

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.account_exists()
    assert keys.is_file(), sorted(item.name for item in (home / ".ssh").iterdir())


def test_a_purge_that_touches_nothing_names_the_ownership_conflict(host):
    operator_account(host)

    result = host.run("purge")

    combined = (result.stdout + result.stderr).lower()
    assert "not created by this package" in combined or "ownership" in combined, combined
    assert BACKUP_USER in result.stdout + result.stderr


def test_purge_leaves_shell_group_and_expiry_of_a_foreign_account_alone(host):
    operator_account(host)

    host.run("purge")

    calls = " ".join(host.calls())
    assert "usermod" not in calls, host.calls()
    assert "chage" not in calls, host.calls()
    assert "deluser" not in calls, host.calls()
    assert "delgroup" not in calls, host.calls()
    assert host.account_field(6) == "/bin/sh"


# --- a home directory that was already there --------------------------------


def test_a_pre_existing_home_with_unknown_keys_is_never_adopted(host):
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("ssh-ed25519 AAAAunknown someone@elsewhere\n", encoding="utf-8")

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.account_exists()
    assert not host.record(), host.record()
    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAAunknown someone@elsewhere\n"
    assert "adduser" not in " ".join(host.calls()), host.calls()


def test_a_pre_existing_home_conflict_is_named(host):
    (host.home / "operator-data").mkdir(parents=True, exist_ok=True)

    result = host.run("ensure")

    assert result.returncode != 0
    assert str(host.home) in result.stderr
    assert (host.home / "operator-data").is_dir()


def test_an_empty_pre_existing_home_is_adopted_but_not_owned(host):
    host.home.mkdir(parents=True, exist_ok=True)
    host.home.chmod(0o755)

    result = host.run("ensure")

    assert result.returncode == 0, result.stdout + result.stderr
    record = host.record()
    assert record["home_created_by_package"] is False, record
    assert record["created_by_package"] is True, record


def test_a_pre_existing_home_that_is_a_symlink_is_refused(host):
    elsewhere = host.root / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    host.home.parent.mkdir(parents=True, exist_ok=True)
    host.home.symlink_to(elsewhere)

    result = host.run("ensure")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not host.account_exists()
    assert host.home.is_symlink()


# --- a replacement account that only shares the name ------------------------


def test_a_same_name_replacement_account_is_not_package_owned(host):
    host.run("ensure")
    host.remove_account()
    replacement = host.root / "home" / "backup-operator"
    host.add_account(home=replacement, shell="/bin/bash", uid=9001)
    (replacement / "operator-notes.txt").write_text("keep me\n", encoding="utf-8")

    result = host.run("purge")

    assert result.returncode != 0, result.stdout + result.stderr
    assert host.account_exists()
    assert host.account_field(2) == "9001"
    assert (replacement / "operator-notes.txt").read_text(encoding="utf-8") == "keep me\n"
    assert "deluser" not in " ".join(host.calls()), host.calls()


def test_postrm_purge_refuses_a_same_name_replacement_account(host):
    host.run("ensure")
    host.remove_account()
    replacement = host.root / "home" / "backup-operator"
    host.add_account(home=replacement, shell="/bin/bash", uid=9001)

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.account_exists()
    assert host.account_field(2) == "9001"
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_the_ownership_record_binds_the_account_identity(host):
    host.run("ensure")

    record = host.record()

    assert record["schema_version"] >= 2, record
    assert record["uid"] == 1500, record
    assert record["primary_gid"] == 1500, record
    assert record["home"] == str(host.home), record
    assert record["home_device"], record
    assert record["home_inode"], record
    assert record["installation_id"], record


def test_the_record_and_the_marker_name_one_installation(host):
    """Two halves of one ownership proof, so two identifiers prove nothing."""

    host.run("ensure")

    record = host.record()
    marker = backup_ownership.read_home_marker(host.home_marker())

    assert marker["installation_id"] == record["installation_id"], (marker, record)


def test_a_marker_from_another_installation_is_not_ownership(host):
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
            installation_id="another-installation",
            nonce=record["home_marker_nonce"],
        ),
        encoding="utf-8",
    )
    marker.chmod(0o400)

    verdict = backup_ownership.verify_ownership(
        SimpleNamespace(package_state_dir=host.package_state),
        BACKUP_USER,
        entry=SimpleNamespace(pw_uid=1500, pw_gid=1500, pw_dir=str(host.home)),
    )

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.MARKER_MISMATCH, verdict


# --- a home directory that was replaced under the same path -----------------


def replaced_home(host):
    """Same account, same uid, same home *path* — a different directory.

    This is the case a username check cannot see: the home the package recorded
    was moved away and something the operator owns now sits at that path.
    """

    host.run("ensure")
    moved_aside = host.root / "var" / "lib" / "moved-aside"
    host.home.rename(moved_aside)
    host.home.mkdir(parents=True)
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("ssh-ed25519 AAAAoperator operator@laptop\n", encoding="utf-8")
    return keys, moved_aside


def quarantined(host):
    if not host.quarantine_dir.is_dir():
        return []
    return sorted(item.name for item in host.quarantine_dir.iterdir())


def test_helper_purge_keeps_a_replacement_home_and_its_key(host):
    keys, _ = replaced_home(host)

    result = host.run("purge")

    assert host.account_exists(), "the account behind a replaced home was deleted"
    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAAoperator operator@laptop\n"
    assert quarantined(host) == [], quarantined(host)
    assert "deluser" not in " ".join(host.calls()), host.calls()
    assert "setfacl" not in " ".join(host.calls()), host.calls()
    assert result.returncode != 0, result.stdout + result.stderr


def test_postrm_purge_keeps_a_replacement_home_and_its_key(host):
    keys, _ = replaced_home(host)

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.account_exists(), "the account behind a replaced home was deleted"
    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAAoperator operator@laptop\n"
    assert quarantined(host) == [], quarantined(host)
    assert "deluser" not in " ".join(host.calls()), host.calls()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_a_replacement_home_purge_names_the_ownership_mismatch(host):
    replaced_home(host)

    result = host.run("purge")

    combined = (result.stdout + result.stderr).lower()
    assert "home" in combined, combined
    assert str(host.home) in result.stdout + result.stderr


def test_disable_leaves_a_replacement_home_key_in_place(host):
    """Authentication still goes away — through the account, not the key file."""

    keys, _ = replaced_home(host)

    result = host.run("disable")

    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAAoperator operator@laptop\n"
    assert not (keys.parent / "authorized_keys.disabled-by-appliance").exists()
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(
        line.startswith(("usermod", "chage")) for line in host.calls()
    ), host.calls()
    assert str(host.home) in result.stderr, result.stderr


def test_disable_fails_closed_when_a_replacement_home_account_cannot_expire(host):
    replaced_home(host)
    host.environment["EMS_STUB_USERMOD_RC"] = "1"
    host.environment["EMS_STUB_CHAGE_RC"] = "1"

    result = host.run("disable")

    assert result.returncode != 0, result.stdout + result.stderr


def test_describe_does_not_call_a_replacement_home_package_owned(host):
    replaced_home(host)

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_a_home_that_no_longer_exists_is_not_package_owned(host):
    host.run("ensure")
    import shutil

    shutil.rmtree(host.home)

    result = host.run("describe")

    assert '"package_owned":false' in result.stdout.replace(" ", ""), result.stdout


def test_purge_refuses_when_the_recorded_home_is_gone(host):
    host.run("ensure")
    import shutil

    shutil.rmtree(host.home)

    result = host.run("purge")

    assert host.account_exists(), "the account was deleted although its home was unstatable"
    assert result.returncode != 0, result.stdout + result.stderr


def test_a_home_replaced_by_a_symlink_is_not_package_owned(host):
    host.run("ensure")
    elsewhere = host.root / "var" / "lib" / "operator-target"
    elsewhere.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.rmtree(host.home)
    host.home.symlink_to(elsewhere)

    result = host.run("purge")

    assert host.account_exists()
    assert host.home.is_symlink(), "the symlink the operator put there was replaced"
    assert elsewhere.is_dir()
    assert result.returncode != 0, result.stdout + result.stderr


# --- the Python side of the same predicate ----------------------------------


def test_python_ownership_accepts_the_recorded_home(owned):
    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is True, verdict


def test_python_ownership_refuses_a_home_that_cannot_be_stated(owned):
    """An unstatable home proves nothing, so it can never be package-owned."""

    import shutil

    shutil.rmtree(owned.home)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict


def replace_home(owned, *, present_recorded_identity):
    """Move the home aside and put a directory somebody else made in its place.

    Whether the replacement inherits the recorded inode is the allocator's
    decision, not this test's, so the record is rewritten to say what it would
    have said in either case. Nothing else about the replacement changes.
    """

    import shutil

    shutil.rmtree(owned.home)
    owned.home.mkdir()
    (owned.home / "operator-data").write_text("somebody else's\n", encoding="utf-8")
    record = json.loads((owned.state / backup_ownership.RECORD_NAME).read_text(encoding="utf-8"))
    if present_recorded_identity:
        status = owned.home.stat()
        record["home_device"], record["home_inode"] = str(status.st_dev), str(status.st_ino)
    else:
        record["home_inode"] = str(int(record["home_inode"] or 0) + 1)
    (owned.state / backup_ownership.RECORD_NAME).write_text(
        json.dumps(record), encoding="utf-8"
    )
    return owned.home


def test_python_ownership_refuses_a_replaced_home_whose_inode_was_reused(owned):
    """The recorded device and inode, on a directory this package never created."""

    replace_home(owned, present_recorded_identity=True)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.MARKER_MISSING, verdict


def test_python_ownership_refuses_a_replaced_home_with_a_fresh_inode(owned):
    replace_home(owned, present_recorded_identity=False)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict


def test_python_ownership_refuses_a_replaced_home_on_any_filesystem(owned):
    """Whatever the allocator does with the freed inode, this is not ownership."""

    import shutil

    shutil.rmtree(owned.home)
    owned.home.mkdir()

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] in backup_ownership.MISMATCH_REASONS, verdict


def test_python_ownership_refuses_a_home_that_is_a_symlink(owned):
    import shutil

    target = owned.home.with_name("operator-target")
    target.mkdir()
    shutil.rmtree(owned.home)
    owned.home.symlink_to(target)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict


def test_python_ownership_refuses_a_home_that_is_not_a_directory(owned):
    import shutil

    shutil.rmtree(owned.home)
    owned.home.write_text("not a directory\n", encoding="utf-8")

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict


def test_python_ownership_refuses_a_record_without_a_bound_home(owned):
    record = backup_ownership.record_file(owned.paths)
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["home_device"] = ""
    payload["home_inode"] = ""
    record.write_text(json.dumps(payload), encoding="utf-8")

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict


# --- ACL entries that are not this package's to withdraw --------------------


def test_purge_keeps_an_operator_acl_entry_for_the_same_user(host):
    config = host.install_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.json").write_text("{}\n", encoding="utf-8")
    host.set_acl(config, BACKUP_USER, "rwx")
    host.set_acl(config / "config.json", BACKUP_USER, "rw-")
    operator_account(host)

    host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(config, BACKUP_USER) == "rwx", host.acl_state()
    assert host.acl_entry(config / "config.json", BACKUP_USER) == "rw-", host.acl_state()


def exported_tree(host):
    """An install root the package granted a recursive read ACL on."""

    config = host.install_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    granted = config / "config.json"
    granted.write_text("{}\n", encoding="utf-8")
    host.set_acl(config, BACKUP_USER, "r-x")
    host.set_acl(config, BACKUP_USER, "r-x", kind="default")
    host.set_acl(granted, BACKUP_USER, "r--")
    host.run("ensure")
    return config, granted


def test_purge_removes_only_the_acl_entries_the_manifest_records(host):
    config, granted = exported_tree(host)
    operator = config / "operator.json"
    operator.write_text("{}\n", encoding="utf-8")
    host.set_acl(operator, BACKUP_USER, "rwx")
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[
            (config, "access", None, "r-x"),
            (config, "default", None, "r-x"),
            (granted, "access", None, "r--"),
        ],
    )

    host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(operator, BACKUP_USER) == "rwx", host.acl_state()
    assert host.acl_entry(granted, BACKUP_USER) is None, host.acl_state()
    assert host.acl_entry(config, BACKUP_USER) is None, host.acl_state()
    assert host.acl_entry(config, BACKUP_USER, kind="default") is None, host.acl_state()


def test_purge_restores_the_exact_permissions_that_predate_the_package(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", "rwx", "r-x"), (granted, "access", None, "r--")],
    )

    host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(config, BACKUP_USER) == "rwx", host.acl_state()
    assert host.acl_entry(granted, BACKUP_USER) is None, host.acl_state()


def test_purge_keeps_an_acl_the_operator_added_after_the_installation(host):
    """A recursive grant is not licence to strip the whole subtree."""

    config, granted = exported_tree(host)
    later = config / "operator-added.json"
    later.write_text("{}\n", encoding="utf-8")
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", None, "r-x"), (granted, "access", None, "r--")],
    )
    host.set_acl(later, BACKUP_USER, "rw-")

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(later, BACKUP_USER) == "rw-", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr
    assert str(later) in result.stderr, result.stderr


def test_purge_keeps_a_package_entry_the_operator_changed(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", None, "r-x"), (granted, "access", None, "r--")],
    )
    host.set_acl(granted, BACKUP_USER, "rwx")

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "rwx", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_keeps_an_acl_on_an_object_that_was_replaced(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", None, "r-x"), (granted, "access", None, "r--")],
    )
    granted.unlink()
    granted.write_text("{}\n", encoding="utf-8")
    host.set_acl(granted, BACKUP_USER, "r--")

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_a_failing_setfacl_is_reported_and_the_manifest_is_kept(host):
    config, granted = exported_tree(host)
    manifest = host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", None, "r-x"), (granted, "access", None, "r--")],
    )
    host.environment["EMS_STUB_SETFACL_RC"] = "1"

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr
    assert manifest.is_file(), "the manifest was deleted although cleanup was incomplete"


def test_purge_keeps_acl_entries_for_unrelated_users(host):
    config, granted = exported_tree(host)
    host.set_acl(config, "operator", "rwx")
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(config, "access", None, "r-x"), (granted, "access", None, "r--")],
    )

    host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(config, "operator") == "rwx", host.acl_state()


def test_purge_without_a_manifest_preserves_acls_and_warns(host):
    config = host.install_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    host.set_acl(config, BACKUP_USER, "r-x")
    host.run("ensure")

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(config, BACKUP_USER) == "r-x", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_refuses_a_manifest_whose_transaction_never_committed(host):
    """Presence is not a commit; a manifest left by a failed one is not acted on."""

    config, granted = exported_tree(host)
    host.write_acl_manifest(
        entries=[(granted, "access", None, "r--")], roots=[(config, "recursive")]
    )
    (host.package_state / "acl-transaction.state").write_text(
        "rollback_required\n", encoding="utf-8"
    )

    result = host.run_postrm("purge")

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "rollback_required" in result.stderr, result.stderr


def test_purge_acts_on_a_manifest_whose_transaction_committed(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        entries=[(granted, "access", None, "r--")], roots=[(config, "recursive")]
    )
    (host.package_state / "acl-transaction.state").write_text("committed\n", encoding="utf-8")

    host.run_postrm("purge")

    assert host.acl_entry(granted, BACKUP_USER) is None, host.acl_state()


def test_purge_reports_an_uncommitted_manifest_left_beside_the_authoritative_one(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        entries=[(granted, "access", None, "r--")], roots=[(config, "recursive")]
    )
    host.acl_manifest.with_name(host.acl_manifest.name + ".uncommitted").write_text(
        "schema=3\n", encoding="utf-8"
    )

    result = host.run_postrm("purge")

    assert "uncommitted-acl-manifest" in result.stderr, result.stderr


def test_purge_refuses_an_acl_manifest_of_an_unknown_schema(host):
    config, granted = exported_tree(host)
    host.write_acl_manifest(
        roots=[(config, "recursive")],
        entries=[(granted, "access", None, "r--")],
        schema=99,
    )

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


# --- the two shell implementations of one rule ------------------------------


def shared_block(path, title):
    """The delimited region two maintainer scripts have to agree on, verbatim."""

    text = path.read_text(encoding="utf-8")
    begin = f"# --- BEGIN {title} "
    end = f"# --- END {title}"
    assert begin in text and end in text, f"{path} carries no {title} block"
    body = text.split(begin, 1)[1].split("\n", 1)[1]
    return body.split(end, 1)[0]


def test_the_home_authority_is_the_same_text_in_both_maintainer_scripts():
    """dpkg has removed the helper by the time purge runs, so it is written twice."""

    account = shared_block(ACCOUNT_SCRIPT, "package-home authority")
    postrm = shared_block(POSTRM_SCRIPT, "package-home authority")

    assert account == postrm, "the two package-home authorities have drifted apart"
    assert "home_marker_is_recorded" in account
    assert "home_is_recorded" in account


def test_the_object_identity_is_the_same_text_in_the_export_and_purge_scripts():
    export = shared_block(EXPORT_SCRIPT, "package-object identity")
    postrm = shared_block(POSTRM_SCRIPT, "package-object identity")

    assert export == postrm, "the two ACL object identities have drifted apart"
    assert "object_identity" in export


def test_both_maintainer_scripts_require_the_same_record_schema():
    for path in (ACCOUNT_SCRIPT, POSTRM_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert f"RECORD_SCHEMA={backup_ownership.RECORD_SCHEMA_VERSION}" in text, path
        assert f"HOME_MARKER_SCHEMA={backup_ownership.HOME_MARKER_SCHEMA_VERSION}" in text, path
        assert f"HOME_MARKER_NAME={backup_ownership.HOME_MARKER_NAME}" in text, path


def test_both_acl_readers_require_the_same_manifest_schema():
    for path in (EXPORT_SCRIPT, POSTRM_SCRIPT):
        text = path.read_text(encoding="utf-8")
        assert f"ACL_SCHEMA={backup_ownership.ACL_MANIFEST_SCHEMA_VERSION}" in text, path


def test_purge_refuses_a_corrupt_acl_manifest(host):
    config, granted = exported_tree(host)
    host.acl_manifest.parent.mkdir(parents=True, exist_ok=True)
    host.acl_manifest.write_text("not a manifest\n\x00\n", encoding="utf-8")

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert host.acl_entry(granted, BACKUP_USER) == "r--", host.acl_state()
    assert host.acl_entry(config, BACKUP_USER) == "r-x", host.acl_state()
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr
