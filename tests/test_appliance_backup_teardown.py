# SPDX-License-Identifier: AGPL-3.0-or-later
"""What happens to the backup account when the package goes away.

The confinement is not a property of the account — it is a property of the
packaged sshd drop-in plus the read-only bind mounts. Remove the package and
both disappear, so an account and a key that survive would, after the next sshd
restart, hold an *unconfined* SFTP session over the whole host filesystem.

Removal must therefore take the authentication with it, and purge must take the
login authority, the key material and the feature's ACL grants with it — while
EMS configuration, data and backups stay untouched in every case.

Marked ``docker``: only a booted guest with a real sshd can answer this.
"""

import pytest

from tests.helpers.appliance_systemd import (
    BACKUP_USER,
    EXPORT_NAMES,
    EXPORT_ROOT,
    INSTALL_ROOT,
    SystemdContainer,
    SystemdUnavailable,
    build_package,
    docker_available,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.backup_restore,
    pytest.mark.skipif(not docker_available(), reason="a Docker daemon is required"),
]

CONFIG_MARKER = "teardown-config-marker"
BACKUP_HOME = "/var/lib/ems-backup"


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return build_package(tmp_path_factory.mktemp("appliance-deb"))


def prepared_host(package):
    container = SystemdContainer()
    container.start()
    container.seed_ems_installation(marker=CONFIG_MARKER)
    container.install_package(package)
    container.enable_sshd()
    container.setup_export_root()
    return container


@pytest.fixture(scope="module")
def removed(package):
    try:
        container = prepared_host(package)
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        assert "config" in container.sftp(["ls /"]).stdout, "the export was not usable to begin with"
        container.remove_package()
        container.reload_sshd()
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def purged(package):
    try:
        container = prepared_host(package)
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.purge_package()
        container.reload_sshd()
        yield container
    finally:
        container.stop()


def ems_data_intact(host):
    return (
        CONFIG_MARKER in host.read_file(f"{INSTALL_ROOT}/config/config.json")
        and "backup-bytes" in host.read_file(f"{INSTALL_ROOT}/backups/backup-1.tar")
        and "runtime" in host.read_file(f"{INSTALL_ROOT}/data/runtime-state.json")
    )


# --- removal ----------------------------------------------------------------


def test_removal_unmounts_every_export_bind(removed):
    for name in EXPORT_NAMES:
        state = removed.shell(f"mountpoint -q {EXPORT_ROOT}/{name}")
        assert state.returncode != 0, f"{name} is still mounted after removal"


def test_removal_denies_the_preserved_key_an_sftp_session(removed):
    result = removed.sftp(["ls /"])

    assert result.returncode != 0, result.stdout
    assert "config" not in result.stdout, result.stdout


def test_removal_denies_an_unconfined_view_of_the_host(removed):
    result = removed.sftp(["get /etc/passwd /tmp/leaked-passwd"])

    assert result.returncode != 0, result.stdout
    assert not removed.exists("/tmp/leaked-passwd")


def test_removal_keeps_ems_configuration_data_and_backups(removed):
    assert ems_data_intact(removed)


def test_a_reinstall_restores_the_confinement_and_the_preserved_key(removed, package):
    removed.install_package(package)
    removed.setup_export_root()
    removed.reload_sshd()

    listing = removed.sftp(["ls /"])

    assert listing.returncode == 0, listing.stdout
    for name in EXPORT_NAMES:
        assert name in listing.stdout, listing.stdout
    assert removed.sftp(["get /etc/passwd /tmp/leaked-2"]).returncode != 0


# --- purge ------------------------------------------------------------------


def test_purge_removes_the_package_created_login_authority(purged):
    assert not purged.account_exists(BACKUP_USER)


def test_purge_removes_the_backup_home_and_its_authorized_keys(purged):
    assert not purged.exists(f"{BACKUP_HOME}/.ssh/authorized_keys")
    assert not purged.exists(BACKUP_HOME)


def test_purge_removes_the_feature_specific_acl_entries(purged):
    assert purged.acl_entries(INSTALL_ROOT, user=BACKUP_USER) == []
    for name in EXPORT_NAMES:
        assert purged.acl_entries(f"{INSTALL_ROOT}/{name}", user=BACKUP_USER) == [], name


def test_purge_leaves_unrelated_acl_entries_alone(package):
    """Only the entries this feature granted are withdrawn."""

    try:
        container = prepared_host(package)
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        outsider = container.add_unrelated_user()
        container.shell(f"setfacl -m u:{outsider}:rx {INSTALL_ROOT}/config", check=True)

        container.purge_package()

        assert container.acl_entries(f"{INSTALL_ROOT}/config", user=outsider) != []
        assert container.acl_entries(f"{INSTALL_ROOT}/config", user=BACKUP_USER) == []
    finally:
        container.stop()


def test_purge_removes_the_export_root_once_the_mounts_are_gone(purged):
    assert not purged.exists(EXPORT_ROOT)


def test_an_sshd_reload_after_purge_grants_the_old_key_nothing(purged):
    result = purged.sftp(["ls /"])

    assert result.returncode != 0, result.stdout
    assert not purged.exists("/tmp/leaked-passwd")


def test_purge_keeps_ems_configuration_data_and_backups(purged):
    assert ems_data_intact(purged)


# --- removal fails closed ---------------------------------------------------


def test_removal_stops_when_authentication_cannot_be_revoked(package):
    """A key without the chroot that confines it must block the removal."""

    try:
        container = prepared_host(package)
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.shell(
            "mv /usr/bin/ems-appliance /usr/bin/ems-appliance.real && "
            "printf '#!/bin/sh\\nexit 1\\n' > /usr/bin/ems-appliance && "
            "chmod 0755 /usr/bin/ems-appliance && "
            "mv /usr/lib/ems-appliance-manager/backup-account.sh "
            "/usr/lib/ems-appliance-manager/backup-account.real && "
            "printf '#!/bin/sh\\nexit 1\\n' > /usr/lib/ems-appliance-manager/backup-account.sh && "
            "chmod 0755 /usr/lib/ems-appliance-manager/backup-account.sh",
            timeout=120,
        )
        result = container.shell(
            "DEBIAN_FRONTEND=noninteractive dpkg --remove ems-appliance-manager 2>&1", timeout=600
        )

        assert result.returncode != 0, result.stdout
        assert container.exists(f"{BACKUP_HOME}/.ssh/authorized_keys"), result.stdout
    finally:
        container.shell(
            "mv -f /usr/bin/ems-appliance.real /usr/bin/ems-appliance 2>/dev/null; "
            "mv -f /usr/lib/ems-appliance-manager/backup-account.real "
            "/usr/lib/ems-appliance-manager/backup-account.sh 2>/dev/null; true"
        )
        container.stop()


def test_purge_keeps_a_replacement_home_that_inherited_the_recorded_inode(package):
    """The exact state a filesystem handing a released inode back produces."""

    record = "/var/lib/ems-appliance-manager/agent/package-state/backup-account.json"
    try:
        container = prepared_host(package)
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.shell(
            f"mv {BACKUP_HOME} {BACKUP_HOME}-moved && mkdir -p {BACKUP_HOME}/.ssh && "
            f"echo 'ssh-ed25519 AAAAoperator operator@laptop' > {BACKUP_HOME}/.ssh/authorized_keys"
            " && python3 - <<'PY'\n"
            "import json, os\n"
            f"path = '{record}'\n"
            "data = json.load(open(path))\n"
            f"entry = os.stat('{BACKUP_HOME}')\n"
            "data['home_device'] = str(entry.st_dev)\n"
            "data['home_inode'] = str(entry.st_ino)\n"
            "json.dump(data, open(path, 'w'))\n"
            "PY",
            timeout=180,
        )

        result = container.purge_package()

        assert container.shell(f"test -d {BACKUP_HOME}").returncode == 0, result.stdout
        assert (
            container.read_file(f"{BACKUP_HOME}/.ssh/authorized_keys").strip()
            == "ssh-ed25519 AAAAoperator operator@laptop"
        )
        assert container.shell("getent passwd ems-backup").returncode == 0, result.stdout
        assert "purge did not complete" in (result.stdout + result.stderr), result.stdout
    finally:
        container.stop()


def test_purge_keeps_an_account_the_package_did_not_create(package):
    """Ownership is recorded; without it, purge withdraws nothing but its own."""

    try:
        container = prepared_host(package)
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.shell(
            "rm -f /var/lib/ems-appliance-manager/agent/package-state/backup-account.json && "
            f"echo operator-file > {BACKUP_HOME}/keep-me.txt",
            timeout=120,
        )
        container.purge_package()

        assert container.account_exists(BACKUP_USER), "an unowned account must survive purge"
        assert container.shell(f"cat {BACKUP_HOME}/keep-me.txt").stdout.strip() == "operator-file"
    finally:
        container.stop()


def test_purge_removes_the_generated_ssh_policy(purged):
    assert not purged.exists("/etc/ssh/sshd_config.d/ems-appliance-backup.conf")
    assert not purged.exists("/etc/ems-appliance-manager/host-paths.env")
    assert not purged.exists("/etc/systemd/system/ems-appliance-export.path.d")
