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

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

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

    commands = " ".join(item["command"] for item in status["examples"])
    assert "rsync -a ems-backup@" in commands
    assert "scp -r ems-backup@" in commands


def test_backup_access_never_shows_a_private_key(tmp_path):
    services = appliance(tmp_path)
    payload = str(services.backup.status())
    assert "PRIVATE KEY" not in payload
    assert "id_ed25519" not in payload
