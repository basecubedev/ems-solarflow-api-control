# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup account, over a real SSH connection.

``ForceCommand internal-sftp`` removes the shell but not the filesystem: an
account with only that restriction can still read ``/etc/passwd``, walk
``/usr`` and list every world-readable path on the host. Proving that
``/etc/shadow`` is unreadable proves nothing about any of that.

These tests install the real package into a booted Debian container, start a
real sshd, authorise a real key and then drive real ``sftp`` and ``ssh``
sessions. Nothing here is simulated, so "export-only" either holds or it does
not.

Marked ``docker`` because a privileged, systemd-capable container is required —
the export root is built from read-only bind mounts.
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

CONFIG_MARKER = "exported-config-marker"


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return build_package(tmp_path_factory.mktemp("appliance-deb"))


@pytest.fixture(scope="module")
def host(package):
    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.seed_ems_installation(marker=CONFIG_MARKER)
        container.install_package(package)
        try:
            container.enable_sshd()
        except SystemdUnavailable as exc:
            pytest.skip(str(exc))
        container.setup_export_root()
        yield container
    finally:
        container.stop()


def sftp_output(host, *commands):
    return host.sftp(list(commands)).stdout


# --- the export root itself -------------------------------------------------


def test_the_export_root_is_a_root_owned_chroot(host):
    """sshd refuses a chroot that anyone but root can write."""

    entry = host.stat(EXPORT_ROOT)
    assert entry is not None, host.setup_export_root().stdout
    assert entry["owner"] == "root", entry
    assert entry["group"] == "root", entry
    assert entry["mode"] == "755", entry
    assert not host.can_write(EXPORT_ROOT, user=BACKUP_USER)


@pytest.mark.parametrize("name", EXPORT_NAMES)
def test_each_export_is_a_read_only_bind_mount(host, name):
    target = f"{EXPORT_ROOT}/{name}"
    options = host.shell(f"findmnt -no OPTIONS --mountpoint {target}").stdout.strip()
    assert options, f"{target} is not a mount point: {host.setup_export_root().stdout}"
    assert "ro" in options.split(","), options


def test_the_exported_data_is_the_live_ems_data(host):
    """A shadow copy would go stale; the export must be the real directory."""

    host.shell(f"rm -f /tmp/live-check.json; printf 'fresh-value\\n' > "
               f"{INSTALL_ROOT}/config/live-check.json")
    assert "fresh-value" in host.read_file(f"{EXPORT_ROOT}/config/live-check.json")

    result = host.sftp(["get /config/live-check.json /tmp/live-check.json"])
    assert result.returncode == 0, result.stdout
    assert "fresh-value" in host.read_file("/tmp/live-check.json")


def test_the_sshd_configuration_chroots_the_backup_account(host):
    effective = host.shell(
        f"sshd -T -C user={BACKUP_USER},host=localhost,addr=127.0.0.1"
    ).stdout.lower()
    assert f"chrootdirectory {EXPORT_ROOT}" in effective, effective
    assert "forcecommand internal-sftp" in effective, effective
    assert "permittty no" in effective, effective
    assert "allowtcpforwarding no" in effective, effective
    assert "passwordauthentication no" in effective, effective


# --- what the account can do ------------------------------------------------


def test_the_account_can_list_every_export(host):
    listing = sftp_output(host, "ls /")
    for name in EXPORT_NAMES:
        assert name in listing, listing
    result = host.sftp(["ls /config", "ls /backups", "ls /data"])
    assert result.returncode == 0, result.stdout


def test_the_account_can_read_an_exported_file(host):
    host.shell("rm -f /tmp/fetched.json")
    result = host.sftp(["get /config/config.json /tmp/fetched.json"])
    assert result.returncode == 0, result.stdout
    assert CONFIG_MARKER in host.read_file("/tmp/fetched.json")


# --- what the account cannot do ---------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "put /etc/hostname /config/injected.json",
        "rm /config/config.json",
        "rename /config/config.json /config/moved.json",
        "mkdir /config/new-directory",
        "rmdir /config",
        "chmod 777 /config/config.json",
    ],
)
def test_every_write_is_refused(host, command):
    host.shell("printf 'x\\n' > /tmp/upload-probe")
    result = host.sftp([command.replace("/etc/hostname", "/tmp/upload-probe")])
    assert result.returncode != 0, result.stdout
    assert CONFIG_MARKER in host.read_file(f"{INSTALL_ROOT}/config/config.json")
    assert not host.exists(f"{INSTALL_ROOT}/config/injected.json")
    assert not host.exists(f"{INSTALL_ROOT}/config/moved.json")
    assert not host.exists(f"{INSTALL_ROOT}/config/new-directory")


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/etc/shadow", "/usr/bin/env", "/root/.ssh", "/var/lib"]
)
def test_the_host_filesystem_is_not_reachable(host, path):
    result = host.sftp([f"get {path} /tmp/escaped"])
    assert result.returncode != 0, result.stdout
    assert "root:x:0:0" not in result.stdout, result.stdout
    assert not host.exists("/tmp/escaped")


@pytest.mark.parametrize(
    "target",
    ["/../etc/passwd", "/config/../../etc/passwd", "../../../../etc/passwd", "/config/../../.."],
)
def test_path_traversal_out_of_the_export_root_is_refused(host, target):
    result = host.sftp([f"get {target} /tmp/traversed"])
    assert "root:x:0:0" not in result.stdout, result.stdout
    assert not host.exists("/tmp/traversed")


def test_the_secrets_sibling_of_the_exports_is_not_visible(host):
    """Only the three exported directories are published, not their parent."""

    listing = sftp_output(host, "ls /")
    assert "secrets" not in listing, listing
    result = host.sftp(["get /secrets/mqtt.pass /tmp/secret"])
    assert result.returncode != 0, result.stdout
    assert not host.exists("/tmp/secret")


@pytest.mark.parametrize(
    "command",
    [
        "symlink /etc/passwd /config/escape-link",
        "symlink / /data/root-link",
        "ln /etc/passwd /config/escape-hardlink",
    ],
)
def test_symlink_and_hardlink_escapes_are_refused(host, command):
    result = host.sftp([command])
    assert result.returncode != 0, result.stdout
    assert not host.exists(f"{INSTALL_ROOT}/config/escape-link")
    assert not host.exists(f"{INSTALL_ROOT}/config/escape-hardlink")
    assert not host.exists(f"{INSTALL_ROOT}/data/root-link")


# --- what the SSH session cannot do -----------------------------------------


def test_an_interactive_command_is_refused(host):
    result = host.ssh("'cat /etc/passwd'")
    assert "root:x:0:0" not in result.stdout, result.stdout


def test_a_shell_is_refused(host):
    result = host.ssh("'id; uname -a'")
    assert "uid=" not in result.stdout, result.stdout


def test_a_tty_is_refused(host):
    result = host.ssh("-tt 'echo tty-granted'")
    assert "tty-granted" not in result.stdout, result.stdout
    assert "PTY allocation request failed" in result.stdout or result.returncode != 0, result.stdout


def test_port_forwarding_is_refused(host):
    result = host.ssh("-o ExitOnForwardFailure=yes -N -R 19099:127.0.0.1:22", timeout=90)
    assert result.returncode != 0, result.stdout
    assert "19099" not in host.shell("ss -ltn").stdout


def test_password_authentication_is_refused(host):
    result = host.shell(
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive "
        f"-o ConnectTimeout=15 {BACKUP_USER}@127.0.0.1 'id' 2>&1 </dev/null",
        timeout=120,
    )
    assert result.returncode != 0, result.stdout
    assert "Permission denied" in result.stdout, result.stdout


# --- durability -------------------------------------------------------------


def test_confinement_survives_a_package_reinstall(host, package):
    host.install_package(package)
    options = host.shell(f"findmnt -no OPTIONS --mountpoint {EXPORT_ROOT}/config").stdout
    assert "ro" in options.split(","), options
    assert host.sftp(["ls /config"]).returncode == 0
    assert host.sftp(["put /tmp/upload-probe /config/after-reinstall"]).returncode != 0


def test_confinement_is_reestablished_after_the_mounts_are_lost(host):
    """A reboot starts from an empty /srv; the unit must rebuild the export."""

    for name in EXPORT_NAMES:
        host.shell(f"umount {EXPORT_ROOT}/{name} 2>/dev/null || true")
    assert not host.shell(f"findmnt -no OPTIONS --mountpoint {EXPORT_ROOT}/config").stdout.strip()

    host.shell("systemctl start ems-appliance-export.service", timeout=300)
    options = host.shell(f"findmnt -no OPTIONS --mountpoint {EXPORT_ROOT}/config").stdout
    assert "ro" in options.split(","), host.journal("ems-appliance-export.service")
    assert host.sftp(["ls /config"]).returncode == 0


def test_an_export_path_created_later_is_published(host):
    """The unit is the trigger: it publishes the path and re-validates access."""

    host.shell(f"umount {EXPORT_ROOT}/data 2>/dev/null || true; rm -rf {INSTALL_ROOT}/data")
    host.publish_exports()
    assert not host.shell(f"findmnt -no OPTIONS --mountpoint {EXPORT_ROOT}/data").stdout.strip()

    host.shell(f"mkdir -p {INSTALL_ROOT}/data && printf 'late\\n' > {INSTALL_ROOT}/data/late.json")
    host.publish_exports()

    options = host.shell(f"findmnt -no OPTIONS --mountpoint {EXPORT_ROOT}/data").stdout
    assert "ro" in options.split(","), options
    assert "late" in sftp_output(host, "get /data/late.json /tmp/late.json")


def test_a_raw_setup_run_does_not_re_enable_access_on_its_own(host):
    """Authentication follows a verified boundary, not a script's exit code."""

    host.shell("/usr/bin/ems-appliance backup-access disable >/dev/null 2>&1", timeout=180)
    host.setup_export_root()

    assert "Permission denied" in sftp_output(host, "ls /config")

    host.publish_exports()
    assert host.sftp(["ls /config"]).returncode == 0


def test_the_agent_reads_the_host_mount_table_not_its_own_namespace(host):
    """A sandboxed unit gets a snapshot namespace; the report must not be stale."""

    import json

    host.shell(f"umount {EXPORT_ROOT}/data 2>/dev/null || true", timeout=120)
    try:
        payload = json.loads(host.agent_call({"operation": "backup.get"}).stdout.strip())["result"]
        data = [item for item in payload["paths"] if item["name"] == "data"][0]
        assert data["state"] != "mounted", (
            "the agent reported a mount that no longer exists on the host"
        )
        assert payload["export_access"]["status"] == "degraded", payload["export_access"]
    finally:
        host.setup_export_root()

    payload = json.loads(host.agent_call({"operation": "backup.get"}).stdout.strip())["result"]
    data = [item for item in payload["paths"] if item["name"] == "data"][0]
    assert data["state"] == "mounted", data


def test_the_export_state_the_agent_reports_matches_the_host(host):
    import json

    result = host.agent_call({"operation": "backup.get"})
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())["result"]

    assert payload["confined"] is True, payload["export_access"]
    assert payload["export_root"] == EXPORT_ROOT
    assert payload["chroot"]["enforced"] is True, payload["chroot"]
    assert payload["export_access"]["status"] == "configured", payload["export_access"]
    for entry in payload["paths"]:
        if entry["exists"]:
            assert entry["state"] == "mounted", entry
            assert entry["read_only"] is True, entry


def test_a_read_write_export_is_reported_as_degraded_not_configured(host):
    """The appliance must never call a writable export 'read-only'."""

    import json

    host.shell(
        f"umount {EXPORT_ROOT}/backups 2>/dev/null || true; "
        f"mount --bind {INSTALL_ROOT}/backups {EXPORT_ROOT}/backups",
        timeout=120,
    )
    try:
        payload = json.loads(host.agent_call({"operation": "backup.get"}).stdout.strip())["result"]
        assert payload["confined"] is False, payload["export_access"]
        assert payload["export_access"]["status"] == "degraded", payload["export_access"]
        assert "backups" in payload["export_access"]["detail"], payload["export_access"]
    finally:
        host.setup_export_root()


# --- path boundaries, against a real kernel ---------------------------------


def export_status(host):
    import json

    raw = host.read_file("/var/lib/ems-appliance-manager/agent/export-access.json")
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def test_a_source_symlinked_to_etc_is_never_mounted(host):
    """The attack the export root exists to prevent, on a real host."""

    host.shell(f"mv {INSTALL_ROOT}/config {INSTALL_ROOT}/config.real && ln -s /etc {INSTALL_ROOT}/config")
    try:
        result = host.setup_export_root()

        assert result.returncode != 0, result.stdout
        assert export_status(host)["status"] == "failed", export_status(host)
        source = host.shell(f"findmnt -no FSROOT --mountpoint {EXPORT_ROOT}/config").stdout.strip()
        assert source != "/etc", source
        assert not host.exists(f"{EXPORT_ROOT}/config/passwd")
        assert host.acl_entries("/etc", user=BACKUP_USER) == []
    finally:
        host.shell(f"rm -f {INSTALL_ROOT}/config && mv {INSTALL_ROOT}/config.real {INSTALL_ROOT}/config")
        host.setup_export_root()


def test_a_symlinked_export_target_never_receives_root_operations(host):
    outside = "/srv/decoy-target"
    host.shell(
        f"mkdir -p {outside} && chmod 0700 {outside} && "
        f"umount {EXPORT_ROOT}/data 2>/dev/null; rmdir {EXPORT_ROOT}/data 2>/dev/null; "
        f"ln -s {outside} {EXPORT_ROOT}/data"
    )
    try:
        result = host.setup_export_root()

        assert result.returncode != 0, result.stdout
        entry = host.stat(outside)
        assert entry["mode"] == "700", entry
        assert host.shell(f"mountpoint -q {outside}").returncode != 0
    finally:
        host.shell(f"rm -f {EXPORT_ROOT}/data && mkdir -p {EXPORT_ROOT}/data")
        host.setup_export_root()


def test_a_target_mounted_from_the_wrong_source_is_replaced(host):
    host.shell(
        f"mkdir -p /srv/foreign && printf 'foreign\\n' > /srv/foreign/marker && "
        f"umount {EXPORT_ROOT}/data 2>/dev/null; mount --bind /srv/foreign {EXPORT_ROOT}/data"
    )
    try:
        host.setup_export_root()

        assert not host.exists(f"{EXPORT_ROOT}/data/marker")
        fsroot = host.shell(f"findmnt -no FSROOT --mountpoint {EXPORT_ROOT}/data").stdout.strip()
        assert fsroot.endswith("/ems-solarflow/data"), fsroot
    finally:
        host.setup_export_root()


def test_an_export_source_on_another_filesystem_is_still_published(host):
    """A data partition is a mount, not a redirection."""

    host.shell(f"umount {EXPORT_ROOT}/data 2>/dev/null; mount -t tmpfs tmpfs {INSTALL_ROOT}/data")
    host.shell(f"printf 'partition\\n' > {INSTALL_ROOT}/data/runtime-state.json")
    try:
        result = host.setup_export_root()

        assert result.returncode == 0, result.stdout
        assert "partition" in host.read_file(f"{EXPORT_ROOT}/data/runtime-state.json")
    finally:
        host.shell(
            f"umount {EXPORT_ROOT}/data 2>/dev/null; umount {INSTALL_ROOT}/data 2>/dev/null; true"
        )
        host.setup_export_root()


def test_the_kernel_mount_state_proves_source_and_read_only_target(host):
    for name in EXPORT_NAMES:
        fsroot = host.shell(f"findmnt -no FSROOT --mountpoint {EXPORT_ROOT}/{name}").stdout.strip()
        options = host.shell(f"findmnt -no OPTIONS --mountpoint {EXPORT_ROOT}/{name}").stdout.strip()
        assert fsroot.endswith(f"/ems-solarflow/{name}"), (name, fsroot)
        assert "ro" in options.split(","), (name, options)
