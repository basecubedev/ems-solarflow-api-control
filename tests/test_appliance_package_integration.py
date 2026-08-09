# SPDX-License-Identifier: AGPL-3.0-or-later
"""The packaged appliance on a real systemd host.

These tests install the real ``.deb`` into a booted Debian container and let
systemd start the shipped units. They are the only place where the sandbox, the
runtime-directory ownership and the web/agent state boundary are observed as
they actually behave — the FakeHost suite cannot see any of that.

Marked ``docker`` because a Docker daemon and a privileged, systemd-capable
container are required.
"""

import json
import shlex

import pytest

from tests.helpers.appliance_systemd import (
    AGENT_UNIT,
    APPLIANCE_GROUP,
    BACKUP_USER,
    LOG_DIR,
    RUNTIME_DIR,
    SOCKET_PATH,
    STATE_DIR,
    WEB_UNIT,
    WEB_USER,
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
]

pytestmark.append(
    pytest.mark.skipif(not docker_available(), reason="a Docker daemon is required")
)


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
        container.install_package(package)
        container.add_unrelated_user()
        yield container
    finally:
        container.stop()


# --- installation ----------------------------------------------------------


def test_the_package_installs_with_its_dependencies(host):
    status = host.shell("dpkg-query -W -f='${Status}' ems-appliance-manager").stdout
    assert status == "install ok installed", status
    for tool in ("/usr/bin/ems-appliance", "/usr/lib/systemd/system/ems-appliance-agent.service"):
        assert host.exists(tool), tool


def test_service_accounts_and_the_socket_group_exist(host):
    assert host.shell(f"getent passwd {WEB_USER}").returncode == 0
    assert host.shell(f"getent passwd {BACKUP_USER}").returncode == 0
    assert host.shell(f"getent group {APPLIANCE_GROUP}").returncode == 0
    groups = host.shell(f"id -nG {WEB_USER}").stdout.split()
    assert APPLIANCE_GROUP in groups, groups


def test_declared_dependencies_are_present(host):
    for command in ("setfacl", "ss", "ps"):
        assert host.shell(f"command -v {command}").returncode == 0, command


# Every executable a shipped maintainer script, unit or the export setup runs.
# mount(8) and umount(8) come from the mount package, which util-linux does not
# pull in on a minimal Debian.
MANDATORY_EXECUTABLES = (
    "setfacl",
    "mountpoint",
    "findmnt",
    "mount",
    "umount",
    "ss",
    "chage",
    "usermod",
    "deluser",
    "readlink",
    "stat",
    "getent",
    "python3",
)


def test_every_mandatory_executable_exists_without_recommended_packages(package):
    """Install the package with its Depends only: the tools must still be there."""

    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        result = container.shell(
            "DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq openssh-server "
            "openssh-sftp-server avahi-daemon avahi-utils network-manager 2>&1; true",
            timeout=600,
        )
        container.copy_in(package, "/root/appliance.deb")
        result = container.shell(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
            "/root/appliance.deb 2>&1",
            timeout=900,
        )
        assert result.returncode == 0, result.stdout

        missing = [
            command
            for command in MANDATORY_EXECUTABLES
            if container.shell(f"command -v {command} >/dev/null").returncode != 0
        ]
        assert missing == [], missing
        # The export setup must still run to completion on this minimal guest.
        container.seed_ems_installation()
        setup = container.setup_export_root()
        assert setup.returncode == 0, setup.stdout
    finally:
        container.stop()


# --- systemd startup -------------------------------------------------------


def test_both_services_start_under_systemd(host):
    assert host.wait_for_unit(AGENT_UNIT), host.journal(AGENT_UNIT)
    assert host.wait_for_unit(WEB_UNIT), host.journal(WEB_UNIT)


def test_the_effective_agent_sandbox_allows_the_required_address_families(host):
    families = host.unit_property(AGENT_UNIT, "RestrictAddressFamilies")
    assert "AF_UNIX" in families, families
    assert "AF_INET" in families, families
    assert "AF_INET6" in families, families


def test_the_effective_web_service_is_unprivileged(host):
    values = host.unit_properties(
        WEB_UNIT, ["User", "Group", "SupplementaryGroups", "NoNewPrivileges", "CapabilityBoundingSet"]
    )
    assert values["User"] == WEB_USER
    assert values["NoNewPrivileges"] in ("yes", "true")
    joined = f"{values['Group']} {values['SupplementaryGroups']}"
    assert APPLIANCE_GROUP in joined, values


def test_the_agent_reaches_a_loopback_http_endpoint_under_its_sandbox(host):
    """The health check the Admin verification depends on must work."""

    host.shell(
        "nohup python3 -m http.server 18099 --bind 127.0.0.1 >/tmp/probe.log 2>&1 & sleep 2",
        timeout=120,
    )
    script = (
        "import urllib.request; "
        "print(urllib.request.urlopen('http://127.0.0.1:18099/', timeout=10).status)"
    )
    families = host.unit_property(AGENT_UNIT, "RestrictAddressFamilies")
    result = host.shell(
        f'systemd-run --pipe --wait --property="RestrictAddressFamilies={families}" '
        f"/usr/bin/python3 -c {shlex.quote(script)} 2>&1",
        timeout=180,
    )
    assert "Address family not supported" not in result.stdout, result.stdout
    assert "200" in result.stdout, result.stdout


def test_apt_metadata_refresh_works_under_the_agent_sandbox(host):
    """apt-get update reports fetch failures as warnings, so check the fetch."""

    families = host.unit_property(AGENT_UNIT, "RestrictAddressFamilies")
    result = host.shell(
        "rm -rf /var/lib/apt/lists/* && systemd-run --pipe --wait "
        f'--property="RestrictAddressFamilies={families}" '
        "/usr/bin/apt-get update 2>&1",
        timeout=900,
    )
    assert "Temporary failure resolving" not in result.stdout, result.stdout[-800:]
    assert "Err:" not in result.stdout, result.stdout[-800:]
    assert "Get:" in result.stdout or "Hit:" in result.stdout, result.stdout[-800:]


def test_package_installation_works_under_the_agent_sandbox(host):
    """dpkg restores setuid bits while unpacking; the sandbox must allow it."""

    properties = " ".join(
        f'--property="{name}={host.unit_property(AGENT_UNIT, name)}"'
        for name in ("RestrictAddressFamilies", "RestrictSUIDSGID")
        if host.unit_property(AGENT_UNIT, name)
    )
    result = host.shell(
        f"systemd-run --pipe --wait {properties} "
        "/usr/bin/apt-get install -y --reinstall passwd 2>&1",
        timeout=900,
    )
    assert "Operation not permitted" not in result.stdout, result.stdout[-800:]
    assert "returned an error code" not in result.stdout, result.stdout[-800:]


# --- socket ownership ------------------------------------------------------


def test_the_runtime_directory_is_group_owned_by_the_appliance_group(host):
    entry = host.stat(RUNTIME_DIR)
    assert entry is not None, "the runtime directory must exist while the agent runs"
    assert entry["owner"] == "root", entry
    assert entry["group"] == APPLIANCE_GROUP, entry
    assert entry["mode"] == "750", entry


def test_the_socket_is_group_readable_and_not_world_accessible(host):
    entry = host.stat(SOCKET_PATH)
    assert entry is not None, host.journal(AGENT_UNIT)
    assert entry["owner"] == "root", entry
    assert entry["group"] == APPLIANCE_GROUP, entry
    assert entry["mode"] == "660", entry


def test_the_web_account_can_traverse_the_runtime_directory(host):
    assert host.can_traverse(RUNTIME_DIR, user=WEB_USER)


def test_the_web_account_can_use_the_agent_socket(host):
    result = host.agent_socket_reachable(user=WEB_USER)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout.strip())
    assert payload["ok"] is True, payload


def test_an_unrelated_local_user_cannot_reach_the_agent_socket(host):
    result = host.agent_socket_reachable(user="ems-outsider")
    assert result.returncode != 0 or '"ok": true' not in result.stdout.lower(), result.stdout


def test_socket_permissions_survive_an_agent_restart(host):
    host.run(["systemctl", "restart", AGENT_UNIT], timeout=120)
    assert host.wait_for_unit(AGENT_UNIT)
    assert host.wait_for_path(SOCKET_PATH), host.journal(AGENT_UNIT)
    assert host.stat(RUNTIME_DIR)["group"] == APPLIANCE_GROUP
    assert host.stat(SOCKET_PATH)["mode"] == "660"
    assert host.agent_socket_reachable(user=WEB_USER).returncode == 0


def test_socket_permissions_survive_a_web_restart(host):
    host.run(["systemctl", "restart", WEB_UNIT], timeout=120)
    assert host.wait_for_unit(WEB_UNIT)
    assert host.wait_for_path(SOCKET_PATH)
    assert host.stat(RUNTIME_DIR)["group"] == APPLIANCE_GROUP
    assert host.agent_socket_reachable(user=WEB_USER).returncode == 0


# --- state ownership -------------------------------------------------------


def test_the_web_account_owns_only_its_own_state(host):
    web_state = host.stat(f"{STATE_DIR}/web")
    assert web_state["owner"] == WEB_USER, web_state
    assert host.can_write(f"{STATE_DIR}/web", user=WEB_USER)


@pytest.mark.parametrize(
    "path",
    [
        f"{STATE_DIR}/agent",
        f"{STATE_DIR}/agent/operations",
        f"{STATE_DIR}/agent/known-good",
        f"{STATE_DIR}/agent/compose-backup",
        f"{STATE_DIR}/agent/ssh-keys",
        f"{STATE_DIR}/agent/recovery",
        f"{STATE_DIR}/agent/support",
        f"{LOG_DIR}/agent",
        f"{LOG_DIR}/audit",
    ],
)
def test_agent_state_is_owned_by_root_alone(host, path):
    """The shared group is for the socket; it is not a read grant on state."""

    entry = host.stat(path)
    assert entry is not None, path
    assert entry["owner"] == "root", entry
    assert entry["group"] == "root", entry
    assert entry["mode"] == "700", entry


@pytest.mark.parametrize(
    "path",
    [
        f"{STATE_DIR}/agent",
        f"{STATE_DIR}/agent/operations",
        f"{STATE_DIR}/agent/known-good",
        f"{LOG_DIR}/agent",
        f"{LOG_DIR}/audit",
    ],
)
def test_the_web_account_can_neither_traverse_nor_write_agent_state(host, path):
    assert not host.can_write(path, user=WEB_USER), path
    assert not host.can_traverse(path, user=WEB_USER), path
    assert host.shell(f"ls {path}", user=WEB_USER).returncode != 0, path


def test_the_web_account_cannot_read_a_persisted_confirmation_token(host):
    """A readable operation record would hand the web process a live token."""

    planned = host.agent_call({"operation": "admin.plan_repair"})
    payload = json.loads(planned.stdout.strip())
    assert payload["ok"] is True, payload
    operation_id = payload["result"]["operation"]["operation_id"]
    token = payload["result"]["confirmation_token"]
    record = f"{STATE_DIR}/agent/operations/{operation_id}.json"
    try:
        assert token in host.shell(f"cat {record}").stdout, "the token must be persisted"

        as_web = host.shell(f"cat {record}", user=WEB_USER)
        assert as_web.returncode != 0, as_web.stdout
        assert token not in as_web.stdout

        found = host.shell(
            f"grep -r . {STATE_DIR}/agent {LOG_DIR}/agent {LOG_DIR}/audit 2>/dev/null",
            user=WEB_USER,
        )
        assert token not in found.stdout, "agent state must not be readable at all"
    finally:
        host.agent_call({"operation": "operations.cancel", "operation_id": operation_id})


def test_the_web_account_cannot_read_the_known_good_record(host):
    marker = f"{STATE_DIR}/agent/known-good/history.json"
    host.shell(f'printf \'[{{"admin_digest": "sha256:secret-rollback-identity"}}]\' > {marker} '
               f"&& chown root:root {marker} && chmod 0600 {marker}")
    result = host.shell(f"cat {marker}", user=WEB_USER)
    assert result.returncode != 0
    assert "secret-rollback-identity" not in result.stdout


def test_the_audit_log_can_be_neither_read_nor_rewritten_by_the_web_account(host):
    audit = f"{LOG_DIR}/audit/audit.log"
    host.shell(f"touch {audit} && chown root:root {audit} && chmod 0600 {audit} "
               f'&& printf \'{{"action":"login.success"}}\\n\' > {audit}')
    assert not host.can_append(audit, user=WEB_USER)
    assert host.shell(f"rm -f {audit}", user=WEB_USER).returncode != 0
    assert host.shell(f"cat {audit}", user=WEB_USER).returncode != 0


def test_redacted_agent_state_is_still_reachable_through_the_typed_api(host):
    """Losing direct reads must not lose the operator's view of that state."""

    for operation, key in (
        ("operations.list", "recent"),
        ("admin.get", "known_good"),
        ("backup.get", "export_access"),
    ):
        result = host.agent_call({"operation": operation}, user=WEB_USER)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        assert payload["ok"] is True, payload
        assert key in payload["result"], payload["result"].keys()

    logs = host.agent_call(
        {"operation": "logs.read", "source": "audit", "lines": 20}, user=WEB_USER
    )
    assert logs.returncode == 0, logs.stderr
    assert json.loads(logs.stdout.strip())["ok"] is True


def test_an_operation_record_carries_no_token_through_the_api(host):
    planned = host.agent_call({"operation": "admin.plan_repair"})
    payload = json.loads(planned.stdout.strip())["result"]
    operation_id = payload["operation"]["operation_id"]
    try:
        assert "confirmation_token" not in payload["operation"]
        fetched = host.agent_call(
            {"operation": "operations.get", "operation_id": operation_id}, user=WEB_USER
        )
        record = json.loads(fetched.stdout.strip())["result"]["operation"]
        assert not record.get("confirmation_token")
    finally:
        host.agent_call({"operation": "operations.cancel", "operation_id": operation_id})


def test_the_state_root_is_not_group_writable_as_a_shortcut(host):
    entry = host.stat(STATE_DIR)
    assert entry["owner"] == "root", entry
    assert entry["mode"] in ("750", "755"), entry


# --- backup account --------------------------------------------------------


def test_the_backup_account_has_no_interactive_shell(host):
    shell = host.shell(f"getent passwd {BACKUP_USER} | cut -d: -f7").stdout.strip()
    assert shell in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false"), shell


def test_the_backup_ssh_restrictions_are_installed(host):
    config = "/etc/ssh/sshd_config.d/ems-appliance-backup.conf"
    assert host.exists(config)
    text = host.shell(f"cat {config}").stdout
    assert "ForceCommand internal-sftp" in text
    assert "PermitTTY no" in text
    assert "AllowTcpForwarding no" in text


def test_backup_exports_are_readable_but_not_writable(host):
    host.shell(
        "mkdir -p /opt/ems-solarflow/config /opt/ems-solarflow/backups /opt/ems-solarflow/data "
        "&& echo seed > /opt/ems-solarflow/config/config.json "
        "&& dpkg-reconfigure -f noninteractive ems-appliance-manager >/dev/null 2>&1 || true",
        timeout=300,
    )
    host.setup_export_root()
    assert host.shell("cat /opt/ems-solarflow/config/config.json", user=BACKUP_USER).returncode == 0
    assert not host.can_write("/opt/ems-solarflow/config", user=BACKUP_USER)
    assert not host.can_write("/opt/ems-solarflow/backups", user=BACKUP_USER)


def test_unrelated_host_paths_are_not_exported_to_the_backup_account(host):
    assert host.shell("cat /etc/shadow", user=BACKUP_USER).returncode != 0
    assert not host.can_write("/etc", user=BACKUP_USER)


# --- lifecycle -------------------------------------------------------------


def test_reinstall_is_idempotent_and_preserves_state(host, package):
    marker = f"{STATE_DIR}/agent/known-good/history.json"
    host.shell(f"printf '[]' > {marker} && chown root:{APPLIANCE_GROUP} {marker}")
    host.install_package(package)
    assert host.wait_for_path(SOCKET_PATH), host.journal(AGENT_UNIT)
    assert host.exists(marker)
    assert host.stat(f"{STATE_DIR}/agent")["owner"] == "root"
    assert host.wait_for_unit(AGENT_UNIT)
    assert host.agent_socket_reachable(user=WEB_USER).returncode == 0


def test_the_status_api_answers_through_the_socket(host):
    result = host.agent_call({"operation": "status.get"})
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["ok"] is True
    assert payload["result"]["system"]["status"] == "ok"


# --- effective systemd configuration ---------------------------------------


def test_systemd_analyze_verify_accepts_the_installed_units(host):
    result = host.shell(
        "systemd-analyze verify /usr/lib/systemd/system/ems-appliance-agent.service "
        "/usr/lib/systemd/system/ems-appliance-web.service "
        "/usr/lib/systemd/system/ems-appliance-export.service "
        "/usr/lib/systemd/system/ems-appliance-export.path 2>&1",
        timeout=180,
    )
    assert "not executable" not in result.stdout, result.stdout
    assert "Failed" not in result.stdout, result.stdout


def test_the_export_unit_keeps_its_mounts_visible_to_sshd(host):
    """Any sandboxing directive would give the unit a private mount namespace."""

    unit = host.shell("systemctl cat ems-appliance-export.service").stdout
    for isolating in ("PrivateMounts", "ProtectSystem", "PrivateTmp", "ProtectHome"):
        assert isolating not in unit, f"{isolating} would hide the export binds from sshd"
    assert host.unit_property("ems-appliance-export.path", "Unit") == "ems-appliance-export.service"


def test_the_effective_configuration_matches_the_shipped_units(host):
    shipped = host.shell(f"systemctl cat {AGENT_UNIT}").stdout
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in shipped
    directives = [line.split("=", 1)[0] for line in shipped.splitlines() if "=" in line]
    assert "RestrictSUIDSGID" not in directives

    effective = host.unit_properties(
        AGENT_UNIT,
        [
            "User",
            "Group",
            "RuntimeDirectoryMode",
            "ProtectHome",
            "RestrictAddressFamilies",
            "Restart",
        ],
    )
    assert effective["User"] == "root"
    assert effective["Group"] == APPLIANCE_GROUP
    assert effective["RuntimeDirectoryMode"] == "0750"
    assert effective["ProtectHome"] == "yes"
    assert effective["Restart"] == "on-failure"


def test_the_web_service_startup_order_is_effective(host):
    after = host.unit_property(WEB_UNIT, "After")
    assert AGENT_UNIT in after, after


# --- upgrade ---------------------------------------------------------------


def test_a_package_upgrade_preserves_state_and_permissions(host, package, tmp_path_factory):
    marker = f"{STATE_DIR}/agent/known-good/history.json"
    host.shell(f"printf '[{{\"admin_version\": \"v9.9.9\"}}]' > {marker}")
    auth = f"{STATE_DIR}/web/auth/auth.json"
    host.shell(f"printf '{{\"generation\": \"keep-me\"}}' > {auth} && chown {WEB_USER} {auth}")

    upgraded = repack_with_version(package, "0.1.1", tmp_path_factory.mktemp("upgrade"))
    host.install_package(upgraded)

    assert "v9.9.9" in host.shell(f"cat {marker}").stdout
    assert "keep-me" in host.shell(f"cat {auth}").stdout
    assert host.stat(f"{STATE_DIR}/agent")["owner"] == "root"
    assert host.stat(f"{STATE_DIR}/web")["owner"] == WEB_USER
    assert host.wait_for_unit(AGENT_UNIT)
    assert host.wait_for_path(SOCKET_PATH)
    assert host.agent_socket_reachable(user=WEB_USER).returncode == 0
    assert host.shell("dpkg-query -W -f='${Version}' ems-appliance-manager").stdout == "0.1.1"


# --- migration -------------------------------------------------------------


def test_the_package_migrates_a_legacy_shared_layout(host, package):
    host.shell(
        f"systemctl stop {WEB_UNIT} {AGENT_UNIT}; "
        f"rm -rf {STATE_DIR}/web {STATE_DIR}/agent; "
        f"mkdir -p {STATE_DIR}/known-good {STATE_DIR}/operations; "
        f"printf '[{{\"admin_version\": \"v0.9.9\"}}]' > {STATE_DIR}/known-good/history.json; "
        f"printf '{{\"generation\": \"legacy\"}}' > {STATE_DIR}/auth.json",
        timeout=120,
    )

    result = host.shell("/usr/bin/ems-appliance migrate-state 2>&1", timeout=300)

    assert result.returncode == 0, result.stdout
    assert "v0.9.9" in host.shell(f"cat {STATE_DIR}/agent/known-good/history.json").stdout
    assert "legacy" in host.shell(f"cat {STATE_DIR}/web/auth/auth.json").stdout
    assert host.stat(f"{STATE_DIR}/agent/known-good")["owner"] == "root"
    assert host.stat(f"{STATE_DIR}/web/auth")["owner"] == WEB_USER
    assert not host.exists(f"{STATE_DIR}/auth.json")

    host.shell("/usr/bin/ems-appliance migrate-state", timeout=300)
    assert "v0.9.9" in host.shell(f"cat {STATE_DIR}/agent/known-good/history.json").stdout

    host.shell(f"systemctl start {AGENT_UNIT} {WEB_UNIT}", timeout=120)
    assert host.wait_for_path(SOCKET_PATH)
