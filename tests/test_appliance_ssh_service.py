# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSH service control, key deployment and the dedicated backup account.

Enabling SSH must never enable password authentication, and only host accounts
the appliance configuration lists may be touched.
"""

import pytest

from appliance.agent import AgentHandlers
from appliance.operations import STATE_SUCCEEDED
from appliance.ssh_service import parse_passwd_entry, parse_sshd_config
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ED25519 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf "
    "appliance-test@example.invalid"
)
ED25519_FINGERPRINT = "SHA256:49CipW8FlH8lOK6o3jsEAdmPpX8qEhdzW2S/R0YYQaM"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"


def appliance(tmp_path):
    services = build_test_services(tmp_path)
    home = tmp_path / "home" / "ems-backup"
    home.mkdir(parents=True, exist_ok=True)
    services.host.add_account("ems-backup", home)
    return services


def handlers_for(services):
    return AgentHandlers(services, executor=lambda target: target())


def plan_and_execute(services, operation, **fields):
    handlers = handlers_for(services)
    planned = handlers.dispatch({"operation": operation, **fields})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


# --- parsing ---------------------------------------------------------------


def test_passwd_entry_is_parsed():
    account = parse_passwd_entry(
        "ems-backup", "root:x:0:0::/root:/bin/bash\nems-backup:x:1500:1500::/srv/backup:/bin/false\n"
    )
    assert account.exists is True
    assert account.home == "/srv/backup"
    assert account.uid == 1500


def test_missing_account_is_reported_as_missing():
    assert parse_passwd_entry("nobody-here", "").exists is False


def test_effective_sshd_config_is_parsed():
    values = parse_sshd_config("port 22\nPasswordAuthentication no\n")
    assert values["passwordauthentication"] == "no"


# --- status ----------------------------------------------------------------


def test_status_reports_service_accounts_and_hardening(tmp_path):
    services = appliance(tmp_path)
    status = services.ssh.status()

    assert status["service"]["unit"] == "ssh.service"
    assert status["enabled"] is False
    assert status["password_authentication"] == "no"
    assert status["hardening"]["passwordauthentication"]["compliant"] is True
    assert status["hardening"]["permitrootlogin"]["recommended"] == "no"
    names = {account["name"] for account in status["accounts"]}
    assert names == {"ems-backup", "pi"}


def test_status_reports_a_missing_host_account(tmp_path):
    services = appliance(tmp_path)
    accounts = {item["name"]: item for item in services.ssh.status()["accounts"]}
    assert accounts["pi"]["exists"] is False
    assert accounts["pi"]["key_count"] == 0


# --- service ---------------------------------------------------------------


def test_enabling_ssh_enables_the_unit_without_password_logins(tmp_path):
    services = appliance(tmp_path)
    operation, plan = plan_and_execute(services, "ssh.plan_service", enabled=True)

    assert operation.state == STATE_SUCCEEDED
    assert operation.result["enabled"] is True
    assert services.host.units["ssh.service"]["active"] == "active"
    assert "Password authentication stays disabled" in plan["note"]
    assert services.ssh.status()["password_authentication"] == "no"


def test_disabling_ssh_stops_and_disables_the_unit(tmp_path):
    services = appliance(tmp_path)
    plan_and_execute(services, "ssh.plan_service", enabled=True)
    services.operations.acknowledge(services.operations.list()[0].operation_id)
    plan_and_execute(services, "ssh.plan_service", enabled=False)
    assert services.host.units["ssh.service"]["enabled"] == "disabled"


def test_no_operation_can_turn_on_password_authentication(tmp_path):
    services = appliance(tmp_path)
    sshd_calls = [
        args
        for tool, args, _ in services.host.calls
        if tool == "sshd" and any("password" in str(item).lower() for item in args)
    ]
    assert sshd_calls == []
    from appliance.protocol import OPERATIONS

    assert not [name for name in OPERATIONS if "password" in name]


# --- keys ------------------------------------------------------------------


def test_adding_a_public_key_writes_authorized_keys(tmp_path):
    services = appliance(tmp_path)
    operation, plan = plan_and_execute(
        services, "ssh.plan_key_add", account="ems-backup", public_key=ED25519
    )

    assert operation.state == STATE_SUCCEEDED
    assert plan["key"]["fingerprint"] == ED25519_FINGERPRINT
    store = services.ssh.keystore("ems-backup")
    assert [key.fingerprint for key in store.list()] == [ED25519_FINGERPRINT]


def test_a_private_key_is_refused_before_any_operation_is_created(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {"operation": "ssh.plan_key_add", "account": "ems-backup", "public_key": PRIVATE_KEY}
        )
    assert getattr(excinfo.value, "code", "") == "private_key_rejected"
    assert services.operations.list() == []


def test_a_duplicate_key_is_refused(tmp_path):
    services = appliance(tmp_path)
    plan_and_execute(services, "ssh.plan_key_add", account="ems-backup", public_key=ED25519)
    services.operations.acknowledge(services.operations.list()[0].operation_id)

    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {"operation": "ssh.plan_key_add", "account": "ems-backup", "public_key": ED25519}
        )
    assert getattr(excinfo.value, "code", "") == "duplicate_public_key"


def test_a_key_can_be_removed_by_fingerprint(tmp_path):
    services = appliance(tmp_path)
    plan_and_execute(services, "ssh.plan_key_add", account="ems-backup", public_key=ED25519)
    services.operations.acknowledge(services.operations.list()[0].operation_id)

    operation, _ = plan_and_execute(
        services, "ssh.plan_key_remove", account="ems-backup", fingerprint=ED25519_FINGERPRINT
    )
    assert operation.state == STATE_SUCCEEDED
    assert services.ssh.keystore("ems-backup").list() == []


def test_removing_an_unknown_fingerprint_is_refused(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {
                "operation": "ssh.plan_key_remove",
                "account": "ems-backup",
                "fingerprint": "SHA256:" + "A" * 43,
            }
        )
    assert getattr(excinfo.value, "code", "") == "unknown_public_key"


def test_revoke_all_requires_a_confirmation_and_reports_the_count(tmp_path):
    services = appliance(tmp_path)
    plan_and_execute(services, "ssh.plan_key_add", account="ems-backup", public_key=ED25519)
    services.operations.acknowledge(services.operations.list()[0].operation_id)

    handlers = handlers_for(services)
    planned = handlers.dispatch({"operation": "ssh.plan_revoke_all", "account": "ems-backup"})
    assert planned["plan"]["key_count"] == 1
    assert "stops immediately" in planned["plan"]["warning"]
    assert planned["operation"]["state"] == "awaiting_confirmation"
    assert services.ssh.keystore("ems-backup").list()

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    assert services.ssh.keystore("ems-backup").list() == []


def test_keys_for_an_account_the_host_does_not_have_are_refused(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {"operation": "ssh.plan_key_add", "account": "pi", "public_key": ED25519}
        )
    assert getattr(excinfo.value, "code", "") == "account_missing"


def test_an_account_outside_the_configuration_is_refused(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "ssh.plan_key_add", "account": "root", "public_key": ED25519})
    assert getattr(excinfo.value, "code", "") == "account_not_allowed"


# --- backup access ---------------------------------------------------------


def test_backup_access_reports_paths_and_ready_to_use_commands(tmp_path):
    services = appliance(tmp_path)
    for directory in ("config", "data", "backups"):
        (services.paths.install_root / directory).mkdir(parents=True, exist_ok=True)
    (services.paths.ems_backups_dir / "backup.tar.gz").write_bytes(b"x" * 2048)

    status = services.backup.status()

    assert status["account"]["name"] == "ems-backup"
    assert status["account"]["exists"] is True
    assert status["write_access"] is False
    names = {item["name"]: item for item in status["paths"]}
    assert set(names) == {"config", "data", "backups"}
    assert all(item["access"] == "read-only" for item in status["paths"])
    assert names["backups"]["size_bytes"] == 2048

    # The packaged sshd drop-in forces internal-sftp, so the shown commands must
    # be SFTP; rsync and scp would need a remote shell this account does not get.
    assert status["protocol"] == "sftp"
    assert status["shell_access"] is False
    commands = " ".join(item["command"] for item in status["examples"])
    assert "sftp -r ems-backup@" in commands
    assert "rsync" not in commands
    assert "scp " not in commands


def test_backup_access_never_shows_a_private_key(tmp_path):
    services = appliance(tmp_path)
    payload = str(services.backup.status())
    assert "PRIVATE KEY" not in payload
    assert "id_ed25519" not in payload


# --- export confinement ----------------------------------------------------


def seeded_exports(tmp_path):
    services = appliance(tmp_path)
    for directory in ("config", "data", "backups"):
        (services.paths.install_root / directory).mkdir(parents=True, exist_ok=True)
    return services


def test_a_confined_export_root_is_reported_as_configured(tmp_path):
    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(read_only=True)

    status = services.backup.status()

    assert status["confined"] is True
    assert status["chroot"]["enforced"] is True
    assert status["chroot"]["configured"] == str(services.paths.export_root)
    assert status["export_access"]["status"] == "configured"
    for entry in status["paths"]:
        assert entry["state"] == "mounted", entry
        assert entry["read_only"] is True, entry
        assert entry["export_target"].endswith(entry["name"])


def test_a_writable_export_is_never_called_read_only(tmp_path):
    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(read_only=False)

    status = services.backup.status()

    assert status["confined"] is False
    assert status["export_access"]["status"] == "degraded"
    assert "read-write" in status["export_access"]["detail"]


def test_an_unpublished_export_is_reported_as_degraded(tmp_path):
    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(names=("config", "backups"))

    status = services.backup.status()

    assert status["confined"] is False
    assert status["export_access"]["status"] == "degraded"
    assert "data" in status["export_access"]["detail"]


def test_a_missing_chroot_is_reported_even_when_the_mounts_are_right(tmp_path):
    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(read_only=True)
    services.host.sshd_backup_match = "forcecommand internal-sftp\npermittty no\n"

    status = services.backup.status()

    assert status["chroot"]["enforced"] is False
    assert status["confined"] is False
    assert status["export_access"]["status"] == "degraded"
    assert "chroot" in status["export_access"]["detail"]


def test_a_host_without_an_ems_installation_is_pending_not_broken(tmp_path):
    import shutil

    services = appliance(tmp_path)
    for source in services.paths.export_paths().values():
        shutil.rmtree(source, ignore_errors=True)
    services.host.write_export_mounts(names=())

    status = services.backup.status()

    assert status["export_access"]["status"] == "pending"
    assert status["export_access"]["missing"] == ["config", "backups", "data"]


# --- what each word in the status means -------------------------------------
#
# Five words are reported and none of them is a synonym for another:
#
#   state=mounted   the kernel has a mount at that target — nothing more
#   read_only       that mount carries "ro"
#   source_verified that mount really publishes the configured EMS directory
#   confined        all three of the above, per entry; policy as well, overall
#   configured      the operator-facing verdict, which needs every one of them
#
# Collapsing any two of them is how a writable or redirected export starts
# being reported as a confined one, so each is pinned separately here.


def test_a_mounted_export_is_not_called_read_only_by_being_mounted(tmp_path):
    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(read_only=False)

    entries = {item["name"]: item for item in services.backup.status()["paths"]}

    for entry in entries.values():
        assert entry["state"] == "writable", entry
        assert entry["read_only"] is False, entry
        assert entry["confined"] is False, entry


def test_a_mount_that_publishes_something_else_is_never_verified(tmp_path):
    services = seeded_exports(tmp_path)
    stranger = services.paths.install_root.parent / "somewhere-else"
    stranger.mkdir(parents=True, exist_ok=True)
    services.host.write_export_mounts(read_only=True, source_for=lambda name: stranger)

    status = services.backup.status()
    entries = {item["name"]: item for item in status["paths"]}

    for entry in entries.values():
        # Read-only and mounted, and still not the configured directory.
        assert entry["read_only"] is True, entry
        assert entry["source_verified"] is False, entry
        assert entry["confined"] is False, entry
    assert status["confined"] is False
    assert status["export_access"]["status"] == "degraded"


def test_confined_is_the_conjunction_and_not_any_one_of_its_parts(tmp_path):
    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(read_only=True)

    status = services.backup.status()

    for entry in status["paths"]:
        assert entry["confined"] == (
            entry["state"] == "mounted" and entry["read_only"] and entry["source_verified"]
        ), entry
    assert status["confined"] is True
    assert status["export_access"]["status"] == "configured"


def test_the_export_verdict_still_needs_sshd_to_agree(tmp_path):
    """Every mount right, and the account not chrooted: not configured."""

    services = seeded_exports(tmp_path)
    services.host.write_export_mounts(read_only=True)
    services.host.sshd_backup_match = "forcecommand internal-sftp\npermittty no\n"

    status = services.backup.status()

    assert all(entry["confined"] for entry in status["paths"])
    assert status["confined"] is False
    assert status["export_access"]["status"] == "degraded"


def test_the_shown_commands_use_paths_inside_the_chroot(tmp_path):
    services = seeded_exports(tmp_path)
    commands = " ".join(item["command"] for item in services.backup.status()["examples"])

    # Inside the chroot the export root is "/", so a host path would not resolve.
    assert ":/backups" in commands
    assert ":/config" in commands
    assert str(services.paths.install_root) not in commands
