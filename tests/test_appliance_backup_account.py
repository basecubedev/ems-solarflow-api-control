# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who owns the backup account, and what removal is therefore allowed to do.

The package needs a login for the confined SFTP export. Adopting an account
that was already on the host — changing its shell, expiring it, and on purge
deleting it together with everything under its home — is not something a
package may do to an operator's host. Ownership is therefore recorded when the
account is created, and every destructive step is gated on that record.
"""

import pytest

from tests.helpers.appliance_backup_account import BACKUP_USER, BackupAccountHarness

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]


@pytest.fixture
def host(tmp_path):
    return BackupAccountHarness(tmp_path)


# --- creation ---------------------------------------------------------------


def test_a_missing_account_is_created_and_marked_package_owned(host):
    result = host.run("ensure")

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.account_exists()
    record = host.record()
    assert record["account"] == BACKUP_USER
    assert record["created_by_package"] is True
    assert record["home_created_by_package"] is True
    assert record["home"] == str(host.home)


def test_ensure_is_idempotent_for_a_package_owned_account(host):
    host.run("ensure")
    first = host.record()

    result = host.run("ensure")

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.record()["created_by_package"] == first["created_by_package"]


def test_a_pre_existing_account_without_a_marker_fails_the_installation(host):
    host.add_account(shell="/bin/bash")

    result = host.run("ensure")

    assert result.returncode != 0
    assert BACKUP_USER in result.stderr
    assert not host.record(), host.record()
    assert "usermod" not in " ".join(host.calls()), host.calls()


def test_a_pre_existing_account_conflict_names_the_resolution(host):
    host.add_account(shell="/bin/bash")

    result = host.run("ensure")

    assert "conflict" in result.stderr.lower() or "already exists" in result.stderr.lower()


# --- disabling authentication ----------------------------------------------


def test_disable_moves_the_key_aside_and_expires_the_account(host):
    host.run("ensure")
    keys = host.write_key()

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not keys.exists()
    assert (host.home / ".ssh" / "authorized_keys.disabled-by-appliance").is_file()
    assert any(line.startswith("usermod") or line.startswith("chage") for line in host.calls())


def test_disable_keeps_both_files_when_a_preserved_copy_already_exists(host):
    host.run("ensure")
    host.write_key(text="ssh-ed25519 AAAA live\n")
    host.write_key(name="authorized_keys.disabled-by-appliance", text="ssh-ed25519 AAAA old\n")

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
    preserved = (host.home / ".ssh" / "authorized_keys.disabled-by-appliance").read_text(
        encoding="utf-8"
    )
    conflicts = sorted(
        item.name for item in (host.home / ".ssh").iterdir() if "conflict" in item.name
    )
    assert preserved == "ssh-ed25519 AAAA old\n"
    assert conflicts, sorted(item.name for item in (host.home / ".ssh").iterdir())
    assert not (host.home / ".ssh" / "authorized_keys").exists()


def test_disable_fails_when_the_key_file_cannot_be_removed(host):
    """A mode root can ignore proves nothing, so the move itself is faulted."""

    host.run("ensure")
    keys = host.write_key()
    host.stub_command("mv", "exit 1")

    result = host.run("disable")

    assert result.returncode != 0, result.stdout
    assert keys.is_file(), "the key file vanished although the move failed"


# --- purge ------------------------------------------------------------------


def test_purge_removes_a_package_created_account_and_its_home(host):
    host.run("ensure")
    host.write_key()

    result = host.run("purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not host.account_exists()
    assert not host.home.exists()


def test_purge_never_deletes_an_account_it_did_not_create(host):
    home = host.add_account(shell="/bin/bash")
    (home / "operator-notes.txt").write_text("keep me\n", encoding="utf-8")

    result = host.run("purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.account_exists()
    assert (home / "operator-notes.txt").read_text(encoding="utf-8") == "keep me\n"
    assert "deluser" not in " ".join(host.calls()), host.calls()


def test_purge_keeps_a_home_the_package_did_not_create(host):
    home = host.add_account()
    (home / "operator-notes.txt").write_text("keep me\n", encoding="utf-8")
    host.write_record(created_by_package=True, home_created_by_package=False)
    host.write_key()

    result = host.run("purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "operator-notes.txt").is_file()
    assert not (home / ".ssh" / "authorized_keys").exists()


def test_purge_removes_only_the_managed_key_files(host):
    home = host.add_account()
    host.write_record(created_by_package=True, home_created_by_package=False)
    host.write_key()
    host.write_key(name="authorized_keys.disabled-by-appliance")
    (home / ".ssh" / "id_operator").write_text("operator key\n", encoding="utf-8")

    host.run("purge")

    assert (home / ".ssh" / "id_operator").is_file()
    assert not (home / ".ssh" / "authorized_keys").exists()
    assert not (home / ".ssh" / "authorized_keys.disabled-by-appliance").exists()


def test_purge_reports_an_account_it_could_not_remove(host):
    host.run("ensure")
    host.environment["EMS_STUB_DELUSER_RC"] = "1"

    result = host.run("purge")

    assert result.returncode != 0
    assert BACKUP_USER in (result.stdout + result.stderr)


# --- package removal --------------------------------------------------------


def test_removal_aborts_when_authentication_cannot_be_disabled(host):
    host.run("ensure")
    host.write_key()
    host.stub_command("ems-appliance", "exit 1")
    host.stub_command("backup-account.sh", "exit 1")

    result = host.run_prerm("remove", EMS_APPLIANCE_LIBDIR=str(host.stub_dir))

    assert result.returncode != 0, result.stdout + result.stderr


def test_removal_stops_when_an_export_bind_survived_the_teardown(host):
    """The teardown named the targets; prerm is the only thing that can still
    refuse on its behalf. Removing the package over a surviving bind would
    leave read-only views of the EMS directories mounted with nothing on the
    host naming them. The authentication is revoked above that step, so this
    can only fail on the mounts.
    """

    host.run("ensure")
    host.write_key()
    host.stub_command("ems-appliance", "exit 0")
    host.stub_command(
        "setup-export-root.sh",
        'echo "ems-appliance: these export mounts could not be removed:'
        ' /srv/ems-appliance-export/data" >&2\nexit 1',
    )

    result = host.run_prerm("remove", EMS_APPLIANCE_LIBDIR=str(host.stub_dir))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "could not be removed" in result.stderr, result.stderr
    assert "authentication could not be disabled" not in result.stderr, result.stderr


def test_removal_falls_back_to_the_direct_key_removal(host):
    host.run("ensure")
    keys = host.write_key()
    host.stub_command("ems-appliance", "exit 1")

    result = host.run_prerm("remove")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not keys.exists()
    assert (host.home / ".ssh" / "authorized_keys.disabled-by-appliance").is_file()


def test_purge_reports_what_it_could_not_remove(host):
    host.run("ensure")
    host.environment["EMS_STUB_DELUSER_RC"] = "1"

    result = host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_leaves_ems_data_alone(host):
    host.run("ensure")
    config = host.install_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.json").write_text("{}\n", encoding="utf-8")

    host.run_postrm("purge", EMS_APPLIANCE_LIBDIR=str(host.package_bin))

    assert (config / "config.json").is_file()


# --- what a flashed image does to the recorded identity ----------------------


def test_a_record_written_before_the_image_was_packed_still_owns_the_account(host):
    """The field defect: no appliance flashed from an image can install a package.

    ``home_identity`` is ``stat -c '%d:%i'``. ``%d`` is the device number of the
    mount, not a property of the filesystem, and the record is written during the
    image build -- while the root filesystem is still a directory tree on the
    build host, before rpi-image-gen packs it into ext4 and reassigns inodes.
    Neither value survives, so ``package_owns_account`` is false on every flashed
    card and the postinst aborts every install, update, revert and reinstall.

    The values here are the ones a real Raspberry Pi reported: 2049 is 8*256+1,
    ``/dev/sda1`` on the machine that built the image.
    """

    host.run("ensure")
    recorded = host.record()
    assert recorded["created_by_package"] is True

    host.write_record(
        marker_nonce=recorded["home_marker_nonce"],
        write_marker=False,
        home_device="2049",
        home_inode="2375248",
    )

    result = host.run("ensure")

    assert result.returncode == 0, result.stdout + result.stderr
    healed = host.record()
    # Left exactly as the build wrote them, on purpose. Nothing compares them
    # any more, so rewriting them would add a second writer for a value that
    # decides nothing -- and the build-host numbers are worth keeping as the
    # record of where this filesystem was assembled.
    assert healed["home_device"] == "2049"
    assert healed["home_inode"] == "2375248"
    assert healed["home_marker_nonce"] == recorded["home_marker_nonce"]


# --- the exit code prerm reads as proof -------------------------------------


def test_disable_refuses_rather_than_reporting_success_for_a_foreign_account(host):
    """prerm reads a zero exit as proof that authentication was withdrawn.

    The CLI returns EXIT_ERROR for exactly this state, which is why prerm falls
    through to this script -- and this script returned 0 having deliberately
    done nothing. `apt remove` then reported nothing, removed the package, and
    left a live key on an unexpired account. Purge goes on to delete the sshd
    Match block that confines it, so what remains is an unconfined SFTP login.

    The header of prerm promises the opposite: "if neither the CLI nor the
    direct maintainer fallback can take the authentication away, the package
    stays installed".
    """

    home = host.add_account(shell="/bin/bash")
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    host.write_key()

    result = host.run("disable")

    assert result.returncode != 0, result.stdout + result.stderr
    assert BACKUP_USER in result.stdout + result.stderr


def test_disable_refuses_on_a_record_this_package_cannot_read(host):
    """A schema this build does not know is not proof of ownership either."""

    import json

    host.run("ensure")
    host.write_key()
    record = host.record()
    record["schema_version"] = 2
    host.marker.write_text(json.dumps(record), encoding="utf-8")

    result = host.run("disable")

    assert result.returncode != 0, result.stdout + result.stderr


def test_disable_still_succeeds_for_the_account_the_package_created(host):
    host.run("ensure")
    host.write_key()

    result = host.run("disable")

    assert result.returncode == 0, result.stdout + result.stderr
