# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sshd started from ``ssh.socket``, the way the Appliance Manager turns SSH on.

Debian 13 ships ``ssh.socket``, and where a host has one the Manager enables SSH
through it. Two things then differ from a daemon systemd started on its own, and
a Pi 3B+ met both on 2026-11-01:

- ``systemctl reload`` sends SIGHUP; sshd re-executes itself, tries to bind
  port 22, which systemd holds for the socket, and exits with "Cannot bind any
  address". The listener has to be restarted instead: it is handed the socket
  again, and the open sessions are processes of their own.
- ``/run/sshd`` is ssh.service's runtime directory and goes away when it
  stops. A socket-activated daemon has not started before the first login
  after a boot, and on the board it had been killed by the reload above. sshd
  refuses ``-t`` and ``-T`` without the directory, so every configuration
  check read as an invalid configuration.

The export unit stayed failed, and the next Manager install failed in its
postinst, and so did the revert to the package before it.
``tests/test_appliance_ssh_socket_activation_systemd.py`` runs the same on a
real systemd.
"""

import stat
from pathlib import Path

import pytest

from appliance.backup_confinement import STATE_ACTIVE, BackupAccessActivation
from appliance.commands import RUNTIME_DIRECTORIES, CommandRunner
from appliance.config import ApplianceConfig
from appliance.host_config import apply_host_config, live_activation
from appliance.paths import AppliancePaths
from tests.helpers.appliance import SSHD_BACKUP_MATCH, build_test_services, seed_backup_account
from tests.helpers.appliance_host_runtime import FakeHost

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH1cQ0kFvL5gLIQ0Q0mV3P6pC5J2Xw5RIu5Hn3fJ0hVb backup\n"
)
ROOT = Path(__file__).resolve().parents[1]
POSTINST = ROOT / "packaging" / "appliance" / "debian" / "postinst"


def sshd_needing(directory, tmp_path):
    """An sshd that refuses to run without its privilege separation directory."""

    tool = tmp_path / "sshd"
    tool.write_text(
        "#!/bin/sh\n"
        f'[ -d "{directory}" ] || {{ echo "Missing privilege separation directory: {directory}" >&2; exit 255; }}\n',
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return tool


# --- /run/sshd ----------------------------------------------------------------


def test_the_runner_names_the_directory_ssh_service_creates():
    assert RUNTIME_DIRECTORIES == {"sshd": "/run/sshd"}


def test_sshd_finds_its_privilege_separation_directory_while_ssh_service_is_stopped(tmp_path):
    privsep = tmp_path / "run" / "sshd"
    privsep.parent.mkdir()
    runner = CommandRunner(
        executables={"sshd": (str(sshd_needing(privsep, tmp_path)),)},
        runtime_directories={"sshd": str(privsep)},
    )

    result = runner.run("sshd", ["-t"])

    assert result.ok, result.stderr
    assert stat.S_IMODE(privsep.stat().st_mode) == 0o755


def test_a_directory_that_cannot_be_made_leaves_the_verdict_to_sshd(tmp_path):
    """Fail closed: sshd still refuses, and says why, instead of the runner raising."""

    blocker = tmp_path / "run"
    blocker.write_text("not a directory\n", encoding="utf-8")
    privsep = blocker / "sshd"
    runner = CommandRunner(
        executables={"sshd": (str(sshd_needing(privsep, tmp_path)),)},
        runtime_directories={"sshd": str(privsep)},
    )

    result = runner.run("sshd", ["-t"])

    assert result.returncode == 255
    assert "Missing privilege separation directory" in result.stderr


def test_a_tool_without_a_runtime_directory_creates_nothing(tmp_path):
    tool = tmp_path / "ssh-keygen"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    privsep = tmp_path / "sshd"
    runner = CommandRunner(
        executables={"ssh-keygen": (str(tool),)},
        runtime_directories={"sshd": str(privsep)},
    )

    assert runner.run("ssh-keygen", ["-l"]).ok
    assert not privsep.exists()


# --- reloading a socket-activated daemon ---------------------------------------


def socket_activated(tmp_path):
    services = build_test_services(tmp_path)
    seed_backup_account(services, home=tmp_path / "var" / "lib" / "ems-backup", key=PUBLIC_KEY)
    services.host.sshd_backup_match = SSHD_BACKUP_MATCH.format(export_root=services.paths.export_root)
    services.host.units["ssh.socket"] = {"active": "active", "enabled": "enabled"}
    services.host.units["ssh.service"] = {"active": "active", "enabled": "disabled"}
    return services


def activation(services):
    return BackupAccessActivation(
        runner=services.runner,
        config=services.config,
        paths=services.paths,
        systemd=services.systemd,
        probe=services.probe,
    )


def systemctl_calls(services):
    return [args for tool, args, _ in services.host.calls if tool == "systemctl"]


def test_activation_restarts_a_socket_activated_daemon_instead_of_reloading_it(
    tmp_path, production_chroot_chain
):
    services = socket_activated(tmp_path)

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert ("try-restart", "ssh.service") in systemctl_calls(services)
    assert ("reload", "ssh.service") not in systemctl_calls(services)
    assert services.host.units["ssh.service"]["active"] == "active"


def test_a_daemon_systemd_started_on_its_own_is_still_reloaded(tmp_path, production_chroot_chain):
    services = socket_activated(tmp_path)
    services.host.units["ssh.socket"] = {"active": "inactive", "enabled": "disabled"}
    services.host.units["ssh.service"]["enabled"] = "enabled"

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert ("reload", "ssh.service") in systemctl_calls(services)
    assert ("try-restart", "ssh.service") not in systemctl_calls(services)


def test_the_host_configuration_restarts_a_socket_activated_daemon(tmp_path):
    paths = AppliancePaths(
        install_root=tmp_path / "opt" / "ems-solarflow",
        config_dir=tmp_path / "etc" / "ems-appliance-manager",
        state_dir=tmp_path / "var" / "lib" / "ems-appliance-manager",
        log_dir=tmp_path / "var" / "log" / "ems-appliance-manager",
        runtime_dir=tmp_path / "run" / "ems-appliance-manager",
        export_root=tmp_path / "srv" / "ems-appliance-export",
    )
    paths.config_dir.mkdir(parents=True)
    paths.install_root.mkdir(parents=True)
    systemd_dir, sshd_dir = tmp_path / "etc" / "systemd" / "system", tmp_path / "etc" / "ssh" / "sshd_config.d"
    host = FakeHost(systemd_dir=systemd_dir, sshd_dir=sshd_dir, socket_active="active")

    apply_host_config(
        paths,
        ApplianceConfig(),
        systemd_dir=str(systemd_dir),
        sshd_dir=str(sshd_dir),
        activation=live_activation(runner=host, marker=str(tmp_path)),
    )

    assert "systemctl try-restart ssh.service" in host.sequence("systemctl")
    assert "systemctl reload ssh.service" not in host.sequence("systemctl")
    assert host.ssh_active == "active"
    assert host.effective_chroot() == str(paths.export_root)


def test_no_maintainer_script_reloads_a_socket_activated_daemon():
    """prerm and postrm cannot import the Python helper, so each says it again."""

    packaging = ROOT / "packaging" / "appliance"
    scripts = sorted((packaging / "debian").iterdir()) + sorted((packaging / "bin").iterdir())
    reloads = 0
    for script in scripts:
        lines = script.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "systemctl reload ssh.service" not in line:
                continue
            reloads += 1
            branch = "\n".join(lines[max(0, index - 4) : index])
            assert "systemctl is-active --quiet ssh.socket" in branch, f"{script.name}:{index + 1}"
            assert "systemctl try-restart ssh.service" in branch, f"{script.name}:{index + 1}"
    assert reloads == 2, "prerm and postrm each reload sshd once; a new caller belongs here"


# --- the install judges its own activation -------------------------------------


def test_the_postinst_clears_a_failure_an_earlier_export_run_left_before_it_judges():
    """A failure the replaced package left behind must not fail this install.

    verify-install reads whether ems-appliance-export.service is failed, and
    nothing in the install ran that unit: the failure was an earlier run's
    verdict. Kept, it failed the install, and the revert's postinst read the
    same. The unit is not restarted instead, because a first install activates
    before sshd has host keys, and that would then fail every first install.
    """

    script = POSTINST.read_text(encoding="utf-8")
    activate = script.find("/usr/bin/ems-appliance backup-access activate")
    reset = script.find("systemctl reset-failed ems-appliance-export.service")
    verify = script.find("/usr/bin/ems-appliance verify-install")

    assert activate != -1, "the postinst no longer activates the confinement"
    assert reset != -1, "the postinst keeps a failure an earlier export run left"
    assert activate < reset < verify
    assert "systemctl restart ems-appliance-export.service" not in script
