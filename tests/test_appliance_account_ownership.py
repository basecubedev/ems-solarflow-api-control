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
import os
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

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]


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
    marker = backup_ownership.read_declaration(host.home_marker())

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


# --- key material behind a symbolic link -----------------------------------

ROOT_KEY = "ssh-ed25519 AAAAroot root@host\n"
OTHER_KEY = "ssh-ed25519 AAAAother other@host\n"
DISABLED_NAME = "authorized_keys.disabled-by-appliance"


def foreign_key_directory(host):
    """A directory outside the backup home, say root's own ``.ssh``.

    Its ``authorized_keys`` carries a key whose hash the package recorded, so a
    withdrawal that followed a link into it would delete that file; the key
    beside it is nobody's the package knows, so it would be quarantined.
    """

    foreign = host.root / "root" / ".ssh"
    foreign.mkdir(parents=True)
    (foreign / "authorized_keys").write_text(ROOT_KEY, encoding="utf-8")
    (foreign / DISABLED_NAME).write_text(OTHER_KEY, encoding="utf-8")
    host.register_managed_key("AAAAroot")
    return foreign


def foreign_contents(foreign):
    return {item.name: item.read_text(encoding="utf-8") for item in foreign.iterdir()}


def symlinked_key_directory(host):
    """The package's own account and home, with ``.ssh`` linked elsewhere."""

    import shutil

    host.run("ensure")
    foreign = foreign_key_directory(host)
    ssh_dir = host.home / ".ssh"
    if ssh_dir.is_dir() and not ssh_dir.is_symlink():
        shutil.rmtree(ssh_dir)
    ssh_dir.symlink_to(foreign, target_is_directory=True)
    return ssh_dir, foreign


PURGES = {
    "helper": lambda host: host.run("purge"),
    "postrm": lambda host: host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin)),
}

OPERATOR_KEY = "ssh-ed25519 AAAAoperator operator@laptop\n"


def drop_real_ssh(host):
    import shutil

    ssh_dir = host.home / ".ssh"
    if ssh_dir.is_dir() and not ssh_dir.is_symlink():
        shutil.rmtree(ssh_dir)
    return ssh_dir


def in_home_store(host, name=".ssh-store"):
    store = host.home / name
    store.mkdir()
    (store / "authorized_keys").write_text(OPERATOR_KEY, encoding="utf-8")
    return store


def key_file_link(host, target):
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.unlink(missing_ok=True)
    os.symlink(target, keys)
    return keys


def plant_link(host, plant):
    """Every kind of link at ``.ssh`` or a key file purge might meet.

    Returns the link and the places outside the home whose state it must keep.
    """

    host.run("ensure")
    foreign = foreign_key_directory(host)
    outside = [host.root / "root"]
    if plant == "outside":
        link = drop_real_ssh(host)
        os.symlink(str(foreign), link)
    elif plant == "outside-key-file":
        link = key_file_link(host, str(foreign / DISABLED_NAME))
    elif plant == "preserved-key-file":
        link = host.home / ".ssh" / DISABLED_NAME
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(foreign / DISABLED_NAME), link)
    elif plant == "in-home-relative":
        in_home_store(host)
        link = drop_real_ssh(host)
        os.symlink(".ssh-store", link)
    elif plant == "in-home-absolute":
        store = in_home_store(host)
        link = drop_real_ssh(host)
        os.symlink(str(store), link)
    elif plant == "the-home-itself":
        (host.home / "authorized_keys").write_text(OPERATOR_KEY, encoding="utf-8")
        link = drop_real_ssh(host)
        os.symlink(".", link)
    elif plant == "in-home-key-file":
        (host.home / "kept-keys").write_text(OPERATOR_KEY, encoding="utf-8")
        link = key_file_link(host, "../kept-keys")
    elif plant == "dot-dot":
        (host.home / "a").mkdir()
        link = drop_real_ssh(host)
        os.symlink(os.path.join("a", os.path.relpath(foreign, host.home / "a")), link)
    elif plant == "outside-hop-into-the-home":
        store = in_home_store(host, "store")
        hop = host.root / "alice" / "hop"
        hop.parent.mkdir()
        os.symlink(str(store), hop)
        outside.append(hop.parent)
        link = drop_real_ssh(host)
        os.symlink(str(hop), link)
    elif plant == "trailing-line-break":
        os.symlink(str(foreign), host.home / "keys\n")
        link = drop_real_ssh(host)
        os.symlink("keys\n", link)
    elif plant == "home-and-a-line-break":
        sibling = host.home.with_name(host.home.name + "\n")
        sibling.mkdir()
        (sibling / "authorized_keys").write_text(OPERATOR_KEY, encoding="utf-8")
        outside.append(sibling)
        link = drop_real_ssh(host)
        os.symlink(str(host.home) + "\n/", link)
    elif plant == "numbered-conflict-key-file":
        link = host.home / ".ssh" / (DISABLED_NAME + ".conflict.2")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(foreign / DISABLED_NAME), link)
    elif plant == "loop":
        link = drop_real_ssh(host)
        os.symlink(".ssh", link)
    elif plant == "dangling":
        link = drop_real_ssh(host)
        os.symlink(str(host.root / "nowhere"), link)
    else:
        link = host.home / ".ssh" / (DISABLED_NAME + ".conflict")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(host.root / "nowhere"), link)
    return link, outside


LINK_PLANTS = [
    "outside",
    "outside-key-file",
    "preserved-key-file",
    "in-home-relative",
    "in-home-absolute",
    "the-home-itself",
    "in-home-key-file",
    "dot-dot",
    "outside-hop-into-the-home",
    "trailing-line-break",
    "home-and-a-line-break",
    "numbered-conflict-key-file",
    "loop",
    "dangling",
    "dangling-conflict",
]


def tree_state(path):
    """Every entry under ``path`` as it is, links as links, never followed."""

    if not os.path.lexists(path):
        return None
    state = {}
    for directory, names, files in os.walk(path, followlinks=False):
        for name in names + files:
            entry = os.path.join(directory, name)
            relative = os.path.relpath(entry, path)
            if os.path.islink(entry):
                state[relative] = ("link", os.readlink(entry))
            elif os.path.isfile(entry):
                with open(entry, "rb") as handle:
                    state[relative] = ("file", handle.read())
            else:
                state[relative] = ("directory",)
    return state


def lock_calls(host):
    return [line for line in host.calls() if line.startswith("usermod --expiredate 1 --lock")]


@pytest.mark.parametrize("purge", sorted(PURGES))
@pytest.mark.parametrize("plant", LINK_PLANTS)
def test_purge_follows_no_link_and_leaves_the_home_for_a_later_purge(host, purge, plant):
    """A link at ``.ssh`` or a key file, of any kind, stops the purge for the
    account: nothing behind it is read, moved or removed, the home stays as it
    is, with the account locked and expired and its record kept, and the home
    and the link are named for the operator to resolve."""

    link, outside = plant_link(host, plant)
    home_before = tree_state(host.home)
    outside_before = [tree_state(path) for path in outside]
    managed_before = host.managed_keys.read_text(encoding="utf-8")
    shown = os.readlink(link).replace("\n", "?")

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert tree_state(host.home) == home_before, output
    assert [tree_state(path) for path in outside] == outside_before, output
    assert quarantined(host) == [], quarantined(host)
    assert host.account_exists(), output
    assert lock_calls(host), host.calls()
    assert host.marker.is_file()
    assert host.managed_keys.read_text(encoding="utf-8") == managed_before
    assert f"{link} is a symbolic link to {shown}; nothing behind it was read" in output, output
    assert f"{host.home} was left in place" in output, output
    assert f"{BACKUP_USER} was locked and expired" in output, output
    assert "Remove every link named above, reinstall the package, then purge it again" in output
    if purge == "helper":
        assert result.returncode != 0, output
    else:
        assert result.returncode == 0, output
        unfinished = [
            line for line in result.stderr.splitlines() if "purge did not complete for:" in line
        ]
        assert unfinished, result.stderr
        assert f"the-home-directory-{host.home}" in unfinished[0].split(), unfinished


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_a_kept_home_does_not_claim_a_lock_that_failed(host, purge):
    plant_link(host, "outside")
    host.environment["EMS_STUB_USERMOD_RC"] = "6"

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert "locked and expired" not in output, output
    assert f"{BACKUP_USER} could not be locked or expired" in output, output
    assert host.account_exists()
    if purge == "helper":
        assert result.returncode != 0, output
    else:
        unfinished = [
            line for line in result.stderr.splitlines() if "purge did not complete for:" in line
        ]
        assert unfinished, result.stderr
        assert f"the-{BACKUP_USER}-account" in unfinished[0].split(), unfinished
        assert f"the-home-directory-{host.home}" in unfinished[0].split(), unfinished


def unfinished_items(result):
    lines = [line for line in result.stderr.splitlines() if "purge did not complete for:" in line]
    assert lines, result.stderr
    return lines[0].split()


PACKAGE_KEY = "ssh-ed25519 AAAApackagekey backup@appliance\n"


def without_the_packages_keys(host, key_file):
    """What an operator does with ``the keys <list> lists by hash``."""

    import hashlib

    listed = set(host.managed_keys.read_text(encoding="utf-8").split())
    kept = [
        line
        for line in key_file.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(line.split()) < 2
        or hashlib.sha256(line.split()[1].encode("utf-8")).hexdigest() not in listed
    ]
    key_file.write_text("".join(kept), encoding="utf-8")


def created_home_message(host):
    return (
        f"{host.home} was left in place because a key path in it is a symbolic link and "
        f"{BACKUP_USER} no longer exists. Remove every link named above, then move aside from "
        f"{host.home} "
        f"what you want to keep, then remove {host.home}, {host.marker} and "
        f"{host.managed_keys} by hand"
    )


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_a_kept_home_whose_account_is_gone_names_the_way_out_that_works(host, purge):
    """Without the account a reinstall refuses the home it finds, so the message
    says what to keep and what to remove by hand. Followed, the operator's key
    survives, the package's goes with the home, and a fresh installation then
    attributes nothing it did not write: a key the old list knew is quarantined
    at that installation's purge, not deleted as its own."""

    import shutil

    link, _outside = plant_link(host, "preserved-key-file")
    host.write_key(text=PACKAGE_KEY)
    operator = host.write_key(name=DISABLED_NAME + ".conflict.3", text=OPERATOR_KEY, managed=False)
    host.register_managed_key("AAAAenrolledbefore")
    host.remove_account()

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert "locked and expired" not in output, output
    assert "reinstall the package" not in output, output
    assert created_home_message(host) in output, output
    assert not lock_calls(host), host.calls()
    assert host.home.is_dir()
    if purge == "postrm":
        assert f"the-home-directory-{host.home}" in unfinished_items(result)
    else:
        assert result.returncode != 0, output

    link.unlink()
    kept = host.root / "kept-by-the-operator"
    shutil.move(str(operator), kept)
    shutil.rmtree(host.home)
    host.marker.unlink()
    host.managed_keys.unlink()
    assert kept.read_text(encoding="utf-8") == OPERATOR_KEY

    reinstall = host.run("ensure")
    assert reinstall.returncode == 0, reinstall.stdout + reinstall.stderr
    host.write_key(text="ssh-ed25519 AAAAenrolledbefore operator@laptop\n", managed=False)
    final = PURGES[purge](host)

    assert not host.home.exists(), final.stdout + final.stderr
    assert not host.account_exists(), final.stdout + final.stderr
    assert quarantined(host), "a key the old list knew was deleted, not quarantined"


def adopted_home_with_linked_keys(host):
    """A home that was there, empty, before the package; ``.ssh`` now links to
    the operator's own key directory inside it."""

    host.home.mkdir(parents=True, exist_ok=True)
    host.home.chmod(0o755)
    installed = host.run("ensure")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert host.record()["home_created_by_package"] is False
    store = host.home / "operator-keys"
    store.mkdir()
    (store / "authorized_keys").write_text(OPERATOR_KEY, encoding="utf-8")
    link = drop_real_ssh(host)
    os.symlink("operator-keys", link)
    return link, store


def adopted_home_message(host):
    return (
        f"{host.home} was left in place because a key path in it is a symbolic link and "
        f"{BACKUP_USER} no longer exists; this package did not create {host.home}, so it "
        f"stays yours. Remove every link named above, then take the keys {host.managed_keys} "
        f"lists by hash "
        f"out of {host.home}/.ssh, then remove {host.marker} and {host.managed_keys} by hand"
    )


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_an_adopted_home_whose_account_is_gone_stays_the_operators(host, purge):
    """A home the package did not create is never removed, so the way out names
    the link, the package's own keys, the ownership record and the list."""

    link, store = adopted_home_with_linked_keys(host)
    host.remove_account()

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert f"remove {host.home}," not in output, output
    assert adopted_home_message(host) in output, output
    assert (store / "authorized_keys").read_text(encoding="utf-8") == OPERATOR_KEY
    if purge == "postrm":
        items = unfinished_items(result)
        assert f"the-ownership-record-{host.marker}" in items, items
        assert f"the-home-directory-{host.home}" not in items, items
    else:
        assert result.returncode != 0, output


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_an_adopted_homes_way_out_removes_the_packages_keys_and_keeps_the_operators(host, purge):
    """Followed literally: the package's key leaves the operator's home before the
    list that identifies it is deleted, and the operator's key behind the link stays."""

    host.home.mkdir(parents=True, exist_ok=True)
    host.home.chmod(0o755)
    assert host.run("ensure").returncode == 0
    assert host.record()["home_created_by_package"] is False
    keys = host.write_key(text=PACKAGE_KEY)
    with keys.open("a", encoding="utf-8") as handle:
        handle.write("ssh-ed25519 AAAAmine operator@laptop\n")
    elsewhere = host.root / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "k").write_text(OPERATOR_KEY, encoding="utf-8")
    link = host.home / ".ssh" / DISABLED_NAME
    os.symlink(str(elsewhere / "k"), link)
    host.remove_account()

    result = PURGES[purge](host)

    assert adopted_home_message(host) in result.stdout + result.stderr
    link.unlink()
    without_the_packages_keys(host, keys)
    host.marker.unlink()
    host.managed_keys.unlink()

    assert "AAAApackagekey" not in keys.read_text(encoding="utf-8")
    assert "AAAAmine" in keys.read_text(encoding="utf-8")
    assert (elsewhere / "k").read_text(encoding="utf-8") == OPERATOR_KEY
    assert host.home.is_dir()


@pytest.mark.parametrize("purge", sorted(PURGES))
@pytest.mark.parametrize("home", ["created", "adopted"])
@pytest.mark.parametrize(
    "plant", ["outside", "dot-dot", "trailing-line-break", "home-and-a-line-break"]
)
def test_following_a_gone_account_way_out_literally_leaves_roots_keys_alone(
    host, purge, home, plant
):
    """``.ssh`` reaches root's own ``.ssh``; the account was removed by hand. Every
    step the message names is taken literally, in its order, and root's keys
    and anything else outside the home stay as they were: the link goes first,
    so no later step acts through it."""

    import shutil

    if home == "adopted":
        host.home.mkdir(parents=True, exist_ok=True)
        host.home.chmod(0o755)
    link, outside = plant_link(host, plant)
    outside_before = [tree_state(path) for path in outside]
    host.remove_account()

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    if home == "created":
        assert created_home_message(host) in output, output
        link.unlink()
        kept = host.root / "kept-by-the-operator"
        kept.mkdir()
        for name in os.listdir(host.home):
            shutil.move(str(host.home / name), str(kept / name))
        shutil.rmtree(host.home)
    else:
        assert adopted_home_message(host) in output, output
        link.unlink()
        ssh_dir = host.home / ".ssh"
        if ssh_dir.is_dir() and not ssh_dir.is_symlink():
            for key_file in ssh_dir.iterdir():
                without_the_packages_keys(host, key_file)
    host.marker.unlink()
    host.managed_keys.unlink()

    assert [tree_state(path) for path in outside] == outside_before


def named_links(output):
    return [
        line.split("ems-appliance: ", 1)[-1].split(" is a symbolic link to ", 1)[0]
        for line in output.splitlines()
        if " is a symbolic link to " in line
    ]


@pytest.mark.parametrize("purge", sorted(PURGES))
@pytest.mark.parametrize("home", ["created", "adopted"])
def test_two_links_left_by_two_disables_are_both_named_and_both_removed_first(
    host, purge, home
):
    """``authorized_keys`` linked to root's own key file; the package's disable
    moves the link to the preserved name, and after it is linked again, to a
    conflict name. Purge names both links, and following the message removes
    every one of them before any step acts inside the home, so root's keys stay."""

    import shutil

    if home == "adopted":
        host.home.mkdir(parents=True, exist_ok=True)
        host.home.chmod(0o755)
    assert host.run("ensure").returncode == 0
    foreign = foreign_key_directory(host)
    ssh_dir = host.home / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    for _ in range(2):
        keys = ssh_dir / "authorized_keys"
        keys.unlink(missing_ok=True)
        os.symlink(str(foreign / "authorized_keys"), keys)
        disabled = host.run("disable")
        assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    links = sorted(str(item) for item in ssh_dir.iterdir() if item.is_symlink())
    assert links == [
        str(ssh_dir / DISABLED_NAME),
        str(ssh_dir / (DISABLED_NAME + ".conflict")),
    ], links
    roots_before = tree_state(foreign)
    host.remove_account()

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert sorted(named_links(output)) == links, output
    if home == "created":
        assert created_home_message(host) in output, output
    else:
        assert adopted_home_message(host) in output, output
    for link in named_links(output):
        os.unlink(link)
    if home == "created":
        kept = host.root / "kept-by-the-operator"
        kept.mkdir()
        for name in os.listdir(host.home):
            shutil.move(str(host.home / name), str(kept / name))
        shutil.rmtree(host.home)
    else:
        for key_file in ssh_dir.iterdir():
            without_the_packages_keys(host, key_file)
    host.marker.unlink()
    host.managed_keys.unlink()

    assert tree_state(foreign) == roots_before


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_a_record_that_does_not_say_the_package_created_the_home_reads_as_adopted(host, purge):
    """Only an explicit ``home_created_by_package: true`` lets the way out name the
    home for removal; a record without the field is treated as an adopted home."""

    import json

    plant_link(host, "outside")
    record = json.loads(host.marker.read_text(encoding="utf-8"))
    record.pop("home_created_by_package")
    host.marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    host.remove_account()

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert adopted_home_message(host) in output, output
    assert f"remove {host.home}," not in output, output


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_an_adopted_home_keeps_the_operators_keys_through_the_reinstall_way_out(host, purge):
    link, store = adopted_home_with_linked_keys(host)

    PURGES[purge](host)
    link.unlink()
    reinstall = host.run("ensure")
    assert reinstall.returncode == 0, reinstall.stdout + reinstall.stderr
    final = PURGES[purge](host)

    output = final.stdout + final.stderr
    assert not host.account_exists(), output
    assert (store / "authorized_keys").read_text(encoding="utf-8") == OPERATOR_KEY
    assert "was left in place because" not in output, output


@pytest.mark.parametrize("purge", sorted(PURGES))
def test_purge_without_a_link_still_removes_the_account_and_its_home(host, purge):
    host.run("ensure")
    host.write_key()
    host.write_key(name=DISABLED_NAME, text=OPERATOR_KEY, managed=False)

    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert not host.home.exists(), output
    assert not host.account_exists(), output
    assert not host.marker.exists(), output
    assert [
        (host.quarantine_dir / name).read_text(encoding="utf-8") for name in quarantined(host)
    ] == [OPERATOR_KEY]
    assert "was left in place" not in output, output


@pytest.mark.parametrize("purge", sorted(PURGES))
@pytest.mark.parametrize("plant", ["outside", "in-home-relative", "outside-key-file"])
def test_a_purge_after_the_link_is_removed_and_a_reinstall_completes(host, purge, plant):
    """The way out the message names: remove the link, reinstall, purge again.
    The managed-key list survived the first purge, so the package's own key is
    deleted then, not quarantined as somebody else's."""

    link, _outside = plant_link(host, plant)
    ssh_dir = host.home / ".ssh"
    if ssh_dir.is_dir() and not ssh_dir.is_symlink():
        host.write_key(name=DISABLED_NAME, text="ssh-ed25519 AAAApackage package@appliance\n")
    PURGES[purge](host)
    assert host.home.is_dir()

    link.unlink()
    reinstall = host.run("ensure")
    assert reinstall.returncode == 0, reinstall.stdout + reinstall.stderr
    result = PURGES[purge](host)

    output = result.stdout + result.stderr
    assert not host.home.exists(), output
    assert not host.account_exists(), output
    assert not host.marker.exists(), output
    assert not host.managed_keys.exists(), output
    assert quarantined(host) == [], quarantined(host)
    assert "was left in place" not in output, output


@pytest.mark.parametrize("dangling", [False, True], ids=["link-to-a-file", "dangling-link"])
@pytest.mark.parametrize("expires", [True, False], ids=["expired", "not-expired"])
def test_disable_moves_a_key_file_link_aside_as_the_link_it_is(host, dangling, expires):
    """sshd reads authorized_keys through a link; the fallback moves the link, as
    ``backup-access disable`` does, and leaves nothing where sshd reads keys."""

    host.run("ensure")
    target = host.root / "root" / ".ssh" / "authorized_keys"
    target.parent.mkdir(parents=True)
    if not dangling:
        target.write_text(ROOT_KEY, encoding="utf-8")
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.unlink(missing_ok=True)
    keys.symlink_to(target)
    if not expires:
        host.environment["EMS_STUB_USERMOD_RC"] = "1"
        host.environment["EMS_STUB_CHAGE_RC"] = "1"

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not os.path.lexists(keys), sorted(item.name for item in keys.parent.iterdir())
    preserved = keys.with_name(DISABLED_NAME)
    assert preserved.is_symlink() and os.readlink(preserved) == str(target)
    if not dangling:
        assert target.read_text(encoding="utf-8") == ROOT_KEY


def test_disable_fails_while_something_it_cannot_move_stays_where_sshd_reads_keys(host):
    host.run("ensure")
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.unlink(missing_ok=True)
    keys.mkdir()

    result = host.run("disable")

    assert result.returncode != 0, result.stdout + result.stderr
    assert str(keys) in result.stderr, result.stderr


def test_disable_fails_when_a_key_file_link_is_still_there_after_the_move(host):
    """A move that reports success is checked: a dangling link left behind fails the run."""

    host.run("ensure")
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.unlink(missing_ok=True)
    keys.symlink_to(host.root / "nowhere")
    host.stub_command("mv", "exit 0")

    result = host.run("disable")

    assert result.returncode != 0, result.stdout + result.stderr
    assert keys.is_symlink()


def test_the_removal_fallback_says_on_stderr_that_the_account_was_not_expired(host):
    """prerm keeps the fallback's stdout quiet but passes its stderr through, so
    ``apt remove`` tells the operator the account could not be expired."""

    host.run("ensure")
    keys = host.write_key()
    host.stub_command("ems-appliance", "exit 1")
    host.environment["EMS_STUB_USERMOD_RC"] = "1"
    host.environment["EMS_STUB_CHAGE_RC"] = "1"

    result = host.run_prerm("remove")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not os.path.lexists(keys)
    assert "could not be expired" in result.stderr, result.stdout + result.stderr
    assert "could not be expired" not in result.stdout, result.stdout


def test_disable_treats_a_dangling_link_at_the_preserved_name_as_taken(host):
    """As ``backup-access disable`` does: an entry there, a link included, is a conflict."""

    host.run("ensure")
    keys = host.write_key()
    preserved = keys.with_name(DISABLED_NAME)
    preserved.symlink_to(host.root / "nowhere")

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
    assert preserved.is_symlink()
    assert keys.with_name(DISABLED_NAME + ".conflict").is_file()
    assert not os.path.lexists(keys)


def test_disable_treats_a_dangling_link_at_a_conflict_name_as_taken(host):
    host.run("ensure")
    keys = host.write_key(text="ssh-ed25519 AAAA live\n")
    host.write_key(name=DISABLED_NAME, text="ssh-ed25519 AAAA old\n")
    conflict = keys.with_name(DISABLED_NAME + ".conflict")
    conflict.symlink_to(host.root / "nowhere")

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
    assert conflict.is_symlink()
    assert keys.with_name(DISABLED_NAME + ".conflict.2").read_text(encoding="utf-8") == (
        "ssh-ed25519 AAAA live\n"
    )


def test_disable_moves_nothing_through_a_symlinked_key_directory_and_expires_the_account(host):
    ssh_dir, foreign = symlinked_key_directory(host)
    before = foreign_contents(foreign)

    result = host.run("disable")

    assert foreign_contents(foreign) == before
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(
        line.startswith(("usermod", "chage")) for line in host.calls()
    ), host.calls()
    assert f"{ssh_dir} is a symbolic link" in result.stderr, result.stderr


def test_disable_fails_closed_when_a_symlinked_key_directory_account_cannot_expire(host):
    _ssh_dir, foreign = symlinked_key_directory(host)
    before = foreign_contents(foreign)
    host.environment["EMS_STUB_USERMOD_RC"] = "1"
    host.environment["EMS_STUB_CHAGE_RC"] = "1"

    result = host.run("disable")

    assert result.returncode != 0, result.stdout + result.stderr
    assert foreign_contents(foreign) == before


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
    """The marker refuses it, and it is the only half that could.

    A filesystem hands a replacement directory the inode the original just
    released, so the recorded pair was never able to tell the two apart. It is
    no longer compared at all -- it cannot survive an image build -- and this
    case is exactly why that costs nothing: what refuses a replaced home is the
    absent marker, as it always really was.
    """

    replace_home(owned, present_recorded_identity=False)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.MARKER_MISSING, verdict


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


def test_python_ownership_refuses_a_home_replaced_by_a_symlink(owned, tmp_path):
    """The type check has to be its own gate, not a passenger.

    ``os.lstat`` + ``S_ISDIR`` used to ride inside ``home_identity``, and the
    device/inode comparison was its only caller -- so it was the comparison that
    happened to refuse a symbolic link. Dropping the comparison without lifting
    the predicate out let a home redirected into an attacker-chosen directory
    verify as package-owned, and ``backup_confinement`` moves and re-arms
    ``authorized_keys`` through that verdict.
    """

    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    marker = owned.home / backup_ownership.HOME_MARKER_NAME
    (elsewhere / backup_ownership.HOME_MARKER_NAME).write_bytes(marker.read_bytes())

    import shutil

    shutil.rmtree(owned.home)
    owned.home.symlink_to(elsewhere)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=owned.entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict


def test_python_ownership_refuses_a_home_replaced_by_a_file(owned):
    """Same gate, the other shape it has to refuse."""

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


def test_the_key_withdrawal_is_the_same_text_in_both_maintainer_scripts():
    """Purge withdraws key material twice over, and both refuse a link the same way."""

    account = shared_block(ACCOUNT_SCRIPT, "package key withdrawal")
    postrm = shared_block(POSTRM_SCRIPT, "package key withdrawal")

    assert account == postrm, "the two key withdrawals have drifted apart"
    assert "withdraw_managed_keys" in account
    assert "home_left_in_place" in account
    assert '[ -L "$entry" ]' in account


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


def test_python_ownership_refuses_a_home_reached_through_a_redirected_ancestor(owned, tmp_path):
    """One ownership question, two destructive gates, two answers.

    `backup-account.sh` walks the whole parent chain in `home_is_recorded`,
    with a comment saying the device/inode pair had been quietly carrying that
    check. `verify_home` dropped the same comparison and did not pick the walk
    up: `home_is_real_directory` lstats the last component and nothing else. So
    on a host where `/var/lib` is a symlink to another disk -- an ordinary
    administrator's move -- `ems-appliance backup-access disable` moves and
    re-arms `authorized_keys` through the redirected path while
    `backup-account.sh disable` declines to touch it, and security-model.md
    promises one rule for both.
    """

    # A home one level down, so there is an ancestor to redirect: var/ becomes a
    # symlink to a directory on "another disk", exactly as relocating /var/lib
    # by hand produces. The home itself is untouched by that move.
    real = tmp_path / "otherdisk" / "var"
    real.mkdir(parents=True)
    home = tmp_path / "var" / "ems-backup"
    home.mkdir(parents=True)
    bind_home(owned.state, home)
    entry = SimpleNamespace(pw_uid=1500, pw_gid=1500, pw_dir=str(home))
    assert backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=entry)["owned"]

    (home).rename(real / "ems-backup")
    (tmp_path / "var").rmdir()
    (tmp_path / "var").symlink_to(real)

    verdict = backup_ownership.verify_ownership(owned.paths, BACKUP_USER, entry=entry)

    assert verdict["owned"] is False, verdict
    assert verdict["reason"] == backup_ownership.HOME_MISMATCH, verdict
