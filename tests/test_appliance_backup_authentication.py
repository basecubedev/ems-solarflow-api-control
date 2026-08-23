# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the backup account can actually authenticate.

Every configuration tier so far asked sshd what it *would* do: `sshd -T -C
user=ems-backup` reports the chroot, the forced `internal-sftp` and the refused
forwardings, and all of it is correct. None of it is a login. In a real guest
the login fails, and it fails for a reason no configuration check can see.

OpenSSH applies two independent rules to `authorized_keys`:

* it opens the file with the *account's* credentials — `user_key_allowed2()`
  calls `temporarily_use_uid(pw)` before `fopen()`, so the account needs search
  permission on every directory down to the file and read permission on it;
* `auth_secure_path()` separately requires that the file and each directory
  above it up to the home are owned by root or by the account, and are not
  writable by group or other.

A key directory that is root-owned and `0700` satisfies the second rule and
breaks the first: the account has no search permission, `fopen()` returns
EACCES, sshd finds no key and closes the connection `[preauth]`. That is the
observed failure, and it is invisible to `sshd -T`.

So the two rules are asserted here together, against both writers of that
directory: the Python key store the Appliance Manager writes through, and the
`harden_home` step of the packaged account script.
"""

import os
import stat

import pytest

from appliance.sshkeys import AUTHORIZED_KEYS_MODE, SSH_DIR_MODE, AuthorizedKeysStore
from tests.helpers.appliance_backup_account import PACKAGE_BIN, BackupAccountHarness

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

ED25519 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf "
    "appliance-test@example.invalid"
)

ACCOUNT_UID = 1500
ACCOUNT_GID = 1500

@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


def store_for(tmp_path, recorded):
    """The key store as the appliance builds it, with `os.chown` recorded.

    A developer host is not root, so the real `chown` would fail and be
    swallowed. What the store *asks* for is the thing under test, so it is
    captured instead of attempted.
    """

    home = tmp_path / "home" / "ems-backup"
    home.mkdir(parents=True, exist_ok=True)
    store = AuthorizedKeysStore(home, owner_uid=ACCOUNT_UID, owner_gid=ACCOUNT_GID)
    real = os.chown

    def record(path, uid, gid, *args, **kwargs):
        recorded.append((str(path), uid, gid))
        try:
            real(path, -1, -1, *args, **kwargs)
        except OSError:
            pass

    return store, record


# --- the rule sshd applies when it opens the file ---------------------------


def test_the_account_can_reach_the_key_file_the_appliance_wrote(tmp_path, monkeypatch):
    """sshd opens the file as the account, so the account must get there."""

    recorded = []
    store, record = store_for(tmp_path, recorded)
    monkeypatch.setattr(os, "chown", record)
    store.add(ED25519)

    directory = stat.S_IMODE(os.stat(store.ssh_dir).st_mode)
    keys = stat.S_IMODE(os.stat(store.path).st_mode)

    assert directory & stat.S_IXGRP, (
        f".ssh is {directory:04o}; the account cannot search it, so sshd's open "
        "of authorized_keys fails with EACCES"
    )
    assert directory & stat.S_IRGRP, f".ssh is {directory:04o}; the account cannot list it"
    assert keys & stat.S_IRGRP, f"authorized_keys is {keys:04o}; the account cannot read it"


def test_the_key_material_is_group_owned_by_the_account(tmp_path, monkeypatch):
    """Group permission is only reachable through the account's own group."""

    recorded = []
    store, record = store_for(tmp_path, recorded)
    monkeypatch.setattr(os, "chown", record)
    store.add(ED25519)

    targets = {path: (uid, gid) for path, uid, gid in recorded}
    assert str(store.ssh_dir) in targets, f"the key directory is never owned: {recorded}"
    assert str(store.path) in targets, f"the key file is never owned: {recorded}"
    for path, (uid, gid) in targets.items():
        assert gid == ACCOUNT_GID, f"{path} is group-owned by {gid}, not by the account"
        assert uid == 0, f"{path} is owned by {uid}; the account must not own its own authority"


# --- the rule sshd applies before it trusts the file ------------------------


def test_the_key_material_satisfies_openssh_strict_modes(tmp_path, monkeypatch):
    """Nothing on the path may be writable by group or other."""

    recorded = []
    store, record = store_for(tmp_path, recorded)
    monkeypatch.setattr(os, "chown", record)
    store.add(ED25519)

    for target in (store.ssh_dir, store.path):
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert not mode & stat.S_IWGRP, f"{target} is {mode:04o}: group-writable"
        assert not mode & stat.S_IWOTH, f"{target} is {mode:04o}: other-writable"


def test_the_account_cannot_rewrite_its_own_authorisation(tmp_path, monkeypatch):
    """Read is what authentication needs; write is what would let it self-authorise."""

    recorded = []
    store, record = store_for(tmp_path, recorded)
    monkeypatch.setattr(os, "chown", record)
    store.add(ED25519)

    assert not stat.S_IMODE(os.stat(store.path).st_mode) & stat.S_IWGRP
    assert not stat.S_IMODE(os.stat(store.ssh_dir).st_mode) & stat.S_IWGRP
    assert AUTHORIZED_KEYS_MODE & stat.S_IRGRP
    assert SSH_DIR_MODE & (stat.S_IRGRP | stat.S_IXGRP)


def test_the_key_file_is_never_world_readable(tmp_path, monkeypatch):
    recorded = []
    store, record = store_for(tmp_path, recorded)
    monkeypatch.setattr(os, "chown", record)
    store.add(ED25519)

    assert not stat.S_IMODE(os.stat(store.path).st_mode) & stat.S_IROTH
    assert not stat.S_IMODE(os.stat(store.ssh_dir).st_mode) & stat.S_IROTH


# --- the same directory, as the package leaves it ---------------------------


def provisioned(host):
    """An installed account with a key, as the appliance leaves it.

    The first `ensure` is what creates the account and binds the ownership
    marker; a key written before it would look like an operator's home the
    package must refuse to adopt.
    """

    first = host.run("ensure")
    assert first.returncode == 0, first.stdout + first.stderr
    host.write_key()
    return host.run("ensure")


def test_the_packaged_hardening_leaves_the_account_able_to_authenticate(host):
    result = provisioned(host)

    assert result.returncode == 0, result.stdout + result.stderr
    mode = stat.S_IMODE(os.stat(host.home / ".ssh").st_mode)
    assert mode & stat.S_IXGRP, (
        f"the packaged hardening leaves .ssh {mode:04o}; the account cannot search it"
    )
    assert mode & stat.S_IRGRP, f"the packaged hardening leaves .ssh {mode:04o}"
    assert not mode & (stat.S_IWGRP | stat.S_IWOTH), f".ssh is {mode:04o}"


def test_the_packaged_hardening_group_owns_the_key_directory_by_the_account():
    """Ownership only happens as root, which this harness is not.

    The chown is therefore read out of the script rather than observed, and the
    real combination is proven in the guest tier, where sshd is the one asking.
    """

    text = (PACKAGE_BIN / "backup-account.sh").read_text(encoding="utf-8")
    hardening = text.partition("harden_home()")[2].partition("\n}")[0]

    assert 'chown "root:$(account_group)" "$directory/.ssh"' in hardening, hardening
    assert "0750" in hardening, hardening
    assert "0700" not in hardening, hardening


def test_the_packaged_hardening_keeps_the_home_out_of_the_accounts_reach(host):
    provisioned(host)

    mode = stat.S_IMODE(os.stat(host.home).st_mode)
    assert not mode & (stat.S_IWGRP | stat.S_IWOTH), f"the home is {mode:04o}"
    assert mode & stat.S_IXOTH, f"the home is {mode:04o}; sshd cannot walk down to .ssh"
