# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup confinement over a socket-activated sshd, on a real systemd.

``tests/test_appliance_ssh_socket_activation.py`` states the cases against a
model of sshd; here sshd does it. ``ssh.socket`` holds port 22 and
``ssh.service`` runs only once systemd started it for the socket, so a SIGHUP
reload kills the daemon and ``/run/sshd`` is gone whenever it is stopped. A Pi
3B+ met both on 2026-11-01, and the next Manager install failed, as did its
revert.

Marked ``docker`` because a privileged, systemd-capable container is required.
"""

import pytest

from tests.helpers.appliance_systemd import (
    BACKUP_KEY,
    BACKUP_USER,
    SystemdContainer,
    SystemdUnavailable,
    build_package,
    docker_available,
    repack_with_version,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.backup_restore,
    pytest.mark.skipif(not docker_available(), reason="a Docker daemon is required"),
    pytest.mark.appliance,
]

EXPORT_UNIT = "ems-appliance-export.service"


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
        container.seed_ems_installation()
        container.install_package(package)
        try:
            container.enable_sshd()
        except SystemdUnavailable as exc:
            pytest.skip(str(exc))
        container.setup_export_root()
        switched = container.shell(
            "systemctl disable --now ssh.service && systemctl enable --now ssh.socket 2>&1"
        )
        assert switched.returncode == 0, switched.stdout
        yield container
    finally:
        container.stop()


def activate(host):
    return host.shell("/usr/bin/ems-appliance backup-access activate 2>&1", timeout=300)


def open_session(host):
    """Hold a backup-account connection open; ``-f`` returns once it is authenticated."""

    opened = host.shell(
        "ssh -f -N -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-i {BACKUP_KEY} {BACKUP_USER}@127.0.0.1 2>&1"
    )
    assert opened.returncode == 0, opened.stdout
    for _ in range(20):
        sessions = host.shell(f"pgrep -f '^sshd-session: {BACKUP_USER}'").stdout.split()
        if sessions:
            return sessions
        host.run(["sleep", "1"], timeout=30)
    raise AssertionError("the connection has no session process on the server")


def test_activation_restarts_a_daemon_the_socket_started_and_keeps_its_sessions(host):
    started = host.shell("systemctl start ssh.service 2>&1")
    assert started.returncode == 0, started.stdout + host.journal("ssh.service")
    assert host.unit_active("ssh.socket") == "active"
    first = activate(host)
    assert "reason: sshd_reload_failed" not in first.stdout, first.stdout
    sessions = open_session(host)
    try:
        result = activate(host)

        assert "reason: sshd_reload_failed" not in result.stdout, result.stdout
        assert host.unit_active("ssh.service") == "active", host.journal("ssh.service")
        assert host.unit_active("ssh.socket") == "active"
        for pid in sessions:
            assert host.shell(f"kill -0 {pid}").returncode == 0, f"session {pid} did not survive"
        assert host.sftp(["ls /"]).returncode == 0, "no new login after the restart"
    finally:
        host.shell(f"pkill -f 'ssh -f -N .*{BACKUP_USER}@127.0.0.1'")


def test_activation_checks_sshd_while_no_connection_is_open(host):
    host.shell("systemctl stop ssh.service")
    assert host.shell("test -e /run/sshd").returncode != 0, (
        "ssh.service no longer removes /run/sshd when it stops; this case proves nothing"
    )

    result = activate(host)

    assert "reason: sshd_config_invalid" not in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout


def test_a_failure_the_export_unit_was_left_with_does_not_fail_an_upgrade(
    host, package, tmp_path_factory
):
    """The board's case: sshd could not check its configuration during the install.

    Without host keys ``sshd -t`` fails, so the host configuration does not
    start the export unit itself, and only the postinst can clear the failure
    an earlier run left.
    """

    dropin = f"/run/systemd/system/{EXPORT_UNIT}.d"
    host.shell(
        f"mkdir -p {dropin} && printf '[Service]\\nExecStartPost=/bin/false\\n' > {dropin}/fail.conf"
        f" && systemctl daemon-reload; systemctl restart {EXPORT_UNIT};"
        f" rm -rf {dropin}; systemctl daemon-reload"
    )
    assert host.unit_active(EXPORT_UNIT) == "failed", host.journal(EXPORT_UNIT)
    host.shell("systemctl stop ssh.service; mkdir -p /root/host-keys && mv /etc/ssh/ssh_host_* /root/host-keys/")
    try:
        assert host.shell("sshd -t").returncode != 0, "sshd still validates; this case proves nothing"
        upgraded = repack_with_version(package, "0.1.2", tmp_path_factory.mktemp("upgrade"))
        result = host.install_package(upgraded, expect_success=False)

        assert result.returncode == 0, result.stdout
        assert host.shell(
            "dpkg-query -W -f='${db:Status-Status} ${Version}' ems-appliance-manager"
        ).stdout == "installed 0.1.2"
    finally:
        host.shell("mv /root/host-keys/* /etc/ssh/ && rmdir /root/host-keys")
