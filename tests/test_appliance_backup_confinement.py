# SPDX-License-Identifier: AGPL-3.0-or-later
"""Activating the backup account's confinement, and reporting it honestly.

The packaged sshd drop-in is a file on disk. What protects the host is the
policy the *running* daemon applies, so a configuration that was written but
never validated, never reloaded or silently overridden must never be reported
as an active read-only confinement — and the account must not stay usable while
the UI merely says "degraded".

Two things are separated here: what the appliance observes (the effective
policy for the backup user), and what it does when the observation is not the
promised one (disable the account's authentication until it is).
"""

import pytest

from appliance.backup_confinement import (
    STATE_ACTIVE,
    STATE_DEGRADED,
    STATE_UNAVAILABLE,
    BackupAccessActivation,
    evaluate_policy,
)
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.backup_restore]

PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH1cQ0kFvL5gLIQ0Q0mV3P6pC5J2Xw5RIu5Hn3fJ0hVb backup\n"
)

COMPLIANT = """permitrootlogin no
passwordauthentication no
kbdinteractiveauthentication no
pubkeyauthentication yes
permittty no
allowtcpforwarding no
allowagentforwarding no
x11forwarding no
permittunnel no
gatewayports no
chrootdirectory {export_root}
forcecommand internal-sftp -P symlink,hardlink,rename
"""


def appliance(tmp_path, *, match=None):
    services = build_test_services(tmp_path)
    host = services.host
    home = tmp_path / "var" / "lib" / "ems-backup"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "authorized_keys").write_text(PUBLIC_KEY, encoding="utf-8")
    host.add_account("ems-backup", home)
    host.sshd_backup_match = (
        COMPLIANT.format(export_root=services.paths.export_root) if match is None else match
    )
    services.home = home
    return services


def activation(services):
    return BackupAccessActivation(
        runner=services.runner,
        config=services.config,
        paths=services.paths,
        systemd=services.systemd,
        probe=services.probe,
    )


def keys(services):
    return services.home / ".ssh" / "authorized_keys"


def disabled_keys(services):
    return services.home / ".ssh" / "authorized_keys.disabled-by-appliance"


# --- effective policy -------------------------------------------------------


def test_a_compliant_match_block_confirms_every_promised_restriction(tmp_path):
    services = appliance(tmp_path)

    policy = evaluate_policy(
        services.ssh.effective_config(user="ems-backup"),
        export_root=str(services.paths.export_root),
    )

    assert policy["confirmed"] is True, policy
    assert policy["violations"] == []
    for option in (
        "chrootdirectory",
        "forcecommand",
        "passwordauthentication",
        "kbdinteractiveauthentication",
        "pubkeyauthentication",
        "permittty",
        "allowtcpforwarding",
        "allowagentforwarding",
        "x11forwarding",
        "permittunnel",
        "gatewayports",
    ):
        assert policy["restrictions"][option]["confirmed"] is True, option


@pytest.mark.parametrize(
    "option,value",
    [
        ("allowtcpforwarding", "yes"),
        ("allowagentforwarding", "yes"),
        ("permittty", "yes"),
        ("passwordauthentication", "yes"),
        ("kbdinteractiveauthentication", "yes"),
        ("x11forwarding", "yes"),
        ("permittunnel", "yes"),
        ("gatewayports", "yes"),
    ],
)
def test_one_relaxed_restriction_breaks_the_confinement(tmp_path, option, value):
    services = appliance(tmp_path)
    relaxed = "\n".join(
        f"{option} {value}" if line.startswith(f"{option} ") else line
        for line in services.host.sshd_backup_match.splitlines()
    )
    services.host.sshd_backup_match = relaxed + "\n"

    policy = evaluate_policy(
        services.ssh.effective_config(user="ems-backup"),
        export_root=str(services.paths.export_root),
    )

    assert policy["confirmed"] is False
    assert option in policy["violations"], policy


def test_an_unverified_restriction_is_not_reported_as_enforced(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_backup_match = "\n".join(
        line
        for line in services.host.sshd_backup_match.splitlines()
        if not line.startswith("permittty")
    ) + "\n"

    policy = evaluate_policy(
        services.ssh.effective_config(user="ems-backup"),
        export_root=str(services.paths.export_root),
    )

    assert policy["restrictions"]["permittty"]["confirmed"] is False
    assert policy["confirmed"] is False


def test_a_chroot_pointing_somewhere_else_breaks_the_confinement(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_backup_match = services.host.sshd_backup_match.replace(
        f"chrootdirectory {services.paths.export_root}", "chrootdirectory /"
    )

    policy = evaluate_policy(
        services.ssh.effective_config(user="ems-backup"),
        export_root=str(services.paths.export_root),
    )

    assert policy["confirmed"] is False
    assert "chrootdirectory" in policy["violations"]


# --- fail-closed activation -------------------------------------------------


def test_a_verified_confinement_activates_backup_access(tmp_path):
    services = appliance(tmp_path)

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert report["authentication_disabled"] is False
    assert keys(services).exists()


def test_an_invalid_sshd_configuration_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_config_valid = False

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert report["reason"] == "sshd_config_invalid"
    assert report["authentication_disabled"] is True
    assert not keys(services).exists()
    assert disabled_keys(services).read_text(encoding="utf-8") == PUBLIC_KEY


def test_a_failed_reload_of_a_running_daemon_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    services.host.units["ssh.service"] = {"active": "active", "enabled": "enabled"}
    services.host.reload_failures.add("ssh.service")

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert report["reason"] == "sshd_reload_failed"
    assert report["authentication_disabled"] is True
    assert not keys(services).exists()


def test_a_stopped_daemon_needs_no_reload_to_activate(tmp_path):
    """A daemon that is not running cannot be applying an older policy."""

    services = appliance(tmp_path)
    services.host.units["ssh.service"] = {"active": "inactive", "enabled": "enabled"}
    services.host.reload_failures.add("ssh.service")

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert keys(services).exists()


def test_an_unconfirmed_match_policy_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_backup_match = services.host.sshd_backup_match.replace(
        "allowtcpforwarding no", "allowtcpforwarding yes"
    )

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert report["reason"] == "confinement_not_confirmed"
    assert report["authentication_disabled"] is True
    assert "allowtcpforwarding" in report["policy"]["violations"]
    assert not keys(services).exists()


def test_a_confirmed_confinement_restores_previously_disabled_keys(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_config_valid = False
    activation(services).activate()
    assert not keys(services).exists()

    services.host.sshd_config_valid = True
    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert keys(services).read_text(encoding="utf-8") == PUBLIC_KEY
    assert not disabled_keys(services).exists()


def test_disabling_is_idempotent_and_never_loses_the_keys(tmp_path):
    service = activation(appliance(tmp_path))

    first = service.disable(reason="package_removed")
    second = service.disable(reason="package_removed")

    assert first["authentication_disabled"] is True
    assert second["authentication_disabled"] is True
    assert service.disabled_keys_path().read_text(encoding="utf-8") == PUBLIC_KEY


def test_a_missing_openssh_reports_unavailable_without_touching_the_keys(tmp_path):
    services = appliance(tmp_path)
    services.host.tools.discard("sshd")

    report = activation(services).activate()

    assert report["state"] == STATE_UNAVAILABLE, report
    assert report["authentication_disabled"] is False
    assert keys(services).exists()


# --- what the UI is told ----------------------------------------------------


def test_the_reported_status_derives_its_note_from_the_observed_policy(tmp_path):
    services = appliance(tmp_path)
    services.host.sshd_backup_match = services.host.sshd_backup_match.replace(
        "allowtcpforwarding no", "allowtcpforwarding yes"
    )

    status = services.backup.status()

    assert status["confined"] is False
    assert status["confinement"]["confirmed"] is False
    assert "allowtcpforwarding" in status["confinement"]["violations"]
    assert "no forwarding" not in status["note"].lower()


def test_a_fully_verified_status_may_promise_the_restrictions(tmp_path):
    services = appliance(tmp_path)
    for source in services.paths.export_paths().values():
        source.mkdir(parents=True, exist_ok=True)
    services.host.write_export_mounts()

    status = services.backup.status()

    assert status["confinement"]["confirmed"] is True, status["confinement"]
    assert status["confined"] is True
    assert "forwarding" in status["note"].lower()


# --- authentication follows the complete confinement ------------------------


def seed_exports(services, *, names=("config", "backups", "data"), read_only=True):
    import shutil

    for name, source in services.paths.export_paths().items():
        if name in names:
            source.mkdir(parents=True, exist_ok=True)
        elif source.is_dir():
            shutil.rmtree(source)
    services.host.write_export_mounts(names=names, read_only=read_only)
    return services


def test_activation_needs_the_export_mounts_not_only_the_ssh_policy(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services)

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert report["exports"]["exact"] is True, report["exports"]


def test_an_unmounted_export_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    for name in ("config", "backups", "data"):
        (services.paths.install_root / name).mkdir(parents=True, exist_ok=True)
    services.host.write_export_mounts(names=("config", "backups"))

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert report["reason"] == "exports_not_confined", report
    assert not keys(services).exists()


def test_a_read_write_export_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services, read_only=False)

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert not keys(services).exists()


def test_a_foreign_source_at_an_export_target_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services)
    services.host.write_export_mounts(
        source_for=lambda name: "/etc" if name == "config" else services.paths.install_root / name
    )

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert not keys(services).exists()


def test_an_unmanaged_entry_in_the_export_root_disables_backup_authentication(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services)
    (services.paths.export_root / "host-note.txt").write_text("note\n", encoding="utf-8")

    report = activation(services).activate()

    assert report["state"] == STATE_DEGRADED, report
    assert report["reason"] in ("export_root_not_exclusive", "exports_not_confined"), report
    assert not keys(services).exists()


def test_a_missing_ems_directory_stays_pending_and_still_activates(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services, names=("config", "backups"))

    report = activation(services).activate()

    assert report["state"] == STATE_ACTIVE, report
    assert "data" in report["exports"]["pending"], report["exports"]


# --- key-file conflicts are never resolved by discarding a key --------------


def test_a_key_conflict_keeps_both_files_and_stays_disabled(tmp_path):
    services = appliance(tmp_path)
    disabled_keys(services).write_text("ssh-ed25519 AAAA older\n", encoding="utf-8")
    service = activation(services)

    report = service.disable(reason="package_removed")

    assert report["authentication_disabled"] is True
    assert not keys(services).exists()
    assert disabled_keys(services).read_text(encoding="utf-8") == "ssh-ed25519 AAAA older\n"
    conflicts = sorted(
        item.name for item in disabled_keys(services).parent.iterdir() if "conflict" in item.name
    )
    assert conflicts, sorted(item.name for item in disabled_keys(services).parent.iterdir())


def test_a_key_conflict_blocks_activation_until_the_operator_resolves_it(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services)
    disabled_keys(services).write_text("ssh-ed25519 AAAA older\n", encoding="utf-8")
    service = activation(services)
    service.disable(reason="package_removed")

    report = service.activate()

    assert report["state"] == STATE_DEGRADED, report
    assert report["reason"] == "key_conflict", report
    assert not keys(services).exists()


# --- what the runtime status proves -----------------------------------------


def test_the_status_reports_the_mounted_source_identity(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services)

    status = services.backup.status()

    config = next(item for item in status["paths"] if item["name"] == "config")
    assert config["mounted_source"] == str(services.paths.install_root / "config"), config
    assert config["source_verified"] is True, config


def test_a_foreign_read_only_mount_is_not_reported_as_confined(tmp_path):
    services = appliance(tmp_path)
    seed_exports(services)
    services.host.write_export_mounts(
        source_for=lambda name: "/etc" if name == "config" else services.paths.install_root / name
    )

    status = services.backup.status()

    assert status["confined"] is False, status["export_access"]
    config = next(item for item in status["paths"] if item["name"] == "config")
    assert config["source_verified"] is False, config
