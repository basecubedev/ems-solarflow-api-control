# SPDX-License-Identifier: AGPL-3.0-or-later
"""The packaged systemd and tmpfiles configuration is the deployed reality.

Everything the appliance claims about privilege separation is only true if the
shipped unit files and tmpfiles rules say so. These checks read the real
packaged files: a sandbox directive that blocks a required operation, or two
files that declare different owners for the same directory, must fail here.
"""

import configparser
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"
AGENT_UNIT = PACKAGING / "systemd" / "ems-appliance-agent.service"
WEB_UNIT = PACKAGING / "systemd" / "ems-appliance-web.service"
TMPFILES = PACKAGING / "tmpfiles" / "ems-appliance-manager.conf"
POSTINST = PACKAGING / "debian" / "postinst"
CONTROL = PACKAGING / "debian" / "control"

RUNTIME_DIR = "/run/ems-appliance-manager"
STATE_DIR = "/var/lib/ems-appliance-manager"
LOG_DIR = "/var/log/ems-appliance-manager"

WEB_USER = "ems-appliance-web"
APPLIANCE_GROUP = "ems-appliance"


def unit(path):
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def service(path):
    return unit(path)["Service"]


def tmpfiles_rules():
    """Parse the shipped tmpfiles rules into ``path -> (mode, owner, group)``."""

    rules = {}
    for line in TMPFILES.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        fields = entry.split()
        if len(fields) < 5 or fields[0] not in ("d", "D", "z", "Z"):
            continue
        rules[fields[1]] = {"type": fields[0], "mode": fields[2], "owner": fields[3], "group": fields[4]}
    return rules


# --- agent network sandbox -------------------------------------------------


def test_agent_sandbox_allows_the_address_families_its_operations_need():
    # The agent performs loopback health checks, APT metadata refreshes and
    # release-index retrieval. AF_UNIX alone makes every one of them fail.
    families = service(AGENT_UNIT)["RestrictAddressFamilies"].split()
    assert "AF_UNIX" in families
    assert "AF_INET" in families, families
    assert "AF_INET6" in families, families


def test_agent_sandbox_still_blocks_unnecessary_address_families():
    # AF_NETLINK is permitted: ss(8) cannot inspect listening ports without it,
    # and a port check that cannot run must not be reported as a result.
    families = set(service(AGENT_UNIT)["RestrictAddressFamilies"].split())
    for blocked in ("AF_PACKET", "AF_BLUETOOTH", "AF_XDP", "AF_VSOCK"):
        assert blocked not in families


def test_agent_sandbox_documents_why_a_hardening_directive_was_relaxed():
    text = AGENT_UNIT.read_text(encoding="utf-8")
    families_line = [line for line in text.splitlines() if line.startswith("RestrictAddressFamilies")]
    assert families_line, "the unit must state its address-family policy explicitly"
    assert "health" in text.lower() or "apt" in text.lower(), (
        "a relaxed sandbox directive must name the operation that requires it"
    )


def test_agent_keeps_the_hardening_that_does_not_block_its_operations():
    values = service(AGENT_UNIT)
    assert values["ProtectHome"] == "yes"
    assert values["RestrictNamespaces"] == "yes"
    assert values["LockPersonality"] == "yes"
    assert values["RestrictRealtime"] == "yes"


def test_agent_may_install_packages_that_ship_setuid_binaries():
    # dpkg restores setuid bits while unpacking; RestrictSUIDSGID=yes makes the
    # chmod fail and the package installation abort.
    values = service(AGENT_UNIT)
    assert values.get("RestrictSUIDSGID", "no") != "yes", (
        "RestrictSUIDSGID=yes blocks APT package installation"
    )


def test_web_service_is_never_granted_agent_privileges():
    values = service(WEB_UNIT)
    assert values["User"] == WEB_USER
    assert values["NoNewPrivileges"] == "yes"
    assert values["CapabilityBoundingSet"] == ""
    assert "AF_UNIX" in values["RestrictAddressFamilies"]


# --- runtime directory and socket ownership --------------------------------


def test_runtime_directory_ownership_is_declared_once_and_consistently():
    values = service(AGENT_UNIT)
    rules = tmpfiles_rules()
    assert RUNTIME_DIR in rules, "the runtime directory must be declared"

    # systemd creates RuntimeDirectory as the unit's User:Group. When the unit
    # runs as root:root the web account can never traverse a 0750 directory,
    # whatever the tmpfiles rule claims.
    declared_group = rules[RUNTIME_DIR]["group"]
    assert values.get("RuntimeDirectoryMode") == rules[RUNTIME_DIR]["mode"]
    assert values.get("Group") == declared_group, (
        f"the agent runs as group {values.get('Group')!r} but tmpfiles declares "
        f"{declared_group!r} for {RUNTIME_DIR}"
    )


def test_web_service_joins_the_group_that_owns_the_socket():
    values = service(WEB_UNIT)
    rules = tmpfiles_rules()
    group = rules[RUNTIME_DIR]["group"]
    joined = {values.get("Group", ""), *values.get("SupplementaryGroups", "").split()}
    assert group in joined


def test_the_socket_directory_is_not_world_traversable():
    rules = tmpfiles_rules()
    mode = rules[RUNTIME_DIR]["mode"]
    assert mode == "0750", mode
    assert not mode.endswith(("1", "5", "7"))


def test_the_postinst_creates_the_group_that_guards_the_socket():
    postinst = POSTINST.read_text(encoding="utf-8")
    assert APPLIANCE_GROUP in postinst
    assert f"--ingroup {APPLIANCE_GROUP}" in postinst or "usermod" in postinst


# --- state ownership -------------------------------------------------------


def test_agent_owned_state_is_not_writable_by_the_web_account():
    rules = tmpfiles_rules()
    agent_owned = [path for path in rules if "/agent" in path or path.endswith("/audit")]
    assert agent_owned, "agent-owned state directories must be declared separately"
    for path in agent_owned:
        assert rules[path]["owner"] == "root", (path, rules[path])
        assert rules[path]["mode"] in ("0750", "0700"), (path, rules[path])


def test_web_owned_state_is_a_separate_subtree():
    rules = tmpfiles_rules()
    web_owned = [path for path in rules if "/web" in path]
    assert web_owned, "web-owned state must live in its own subtree"
    for path in web_owned:
        assert rules[path]["owner"] == WEB_USER, (path, rules[path])


def test_the_shared_state_root_is_not_writable_by_the_web_account():
    rules = tmpfiles_rules()
    assert rules[STATE_DIR]["owner"] == "root", rules[STATE_DIR]
    assert rules[LOG_DIR]["owner"] == "root", rules[LOG_DIR]


def test_operation_and_known_good_state_belong_to_the_agent():
    rules = tmpfiles_rules()
    for name in ("operations", "known-good", "compose-backup"):
        matches = [path for path in rules if path.endswith(f"/{name}")]
        assert matches, f"{name} must be declared"
        for path in matches:
            assert "/agent/" in path, path
            assert rules[path]["owner"] == "root", (path, rules[path])


def test_audit_logs_are_owned_by_the_agent():
    rules = tmpfiles_rules()
    audit = [path for path in rules if path.endswith("/audit")]
    assert audit, "the audit log directory must be declared"
    for path in audit:
        assert rules[path]["owner"] == "root", (path, rules[path])


def test_state_directories_are_not_declared_by_both_units_with_different_owners():
    agent = service(AGENT_UNIT)
    web = service(WEB_UNIT)
    shared = {"StateDirectory", "LogsDirectory", "RuntimeDirectory"}
    for directive in shared:
        agent_value = agent.get(directive, "")
        web_value = web.get(directive, "")
        if agent_value and web_value:
            assert agent_value != web_value, (
                f"both units declare {directive}={agent_value!r}; systemd would chown it "
                "to whichever service started last"
            )


# --- backup account --------------------------------------------------------


def test_the_backup_account_has_no_interactive_shell():
    script = (PACKAGING / "bin" / "backup-account.sh").read_text(encoding="utf-8")
    creation = re.search(r"adduser[^\n]*\n(?:[^\n]*\n){0,2}", script)
    assert creation, "the backup account must be created by the account helper"
    assert "/bin/sh" not in creation.group(0), (
        "the backup account is documented as backup-only and must not get a shell"
    )
    assert "nologin" in creation.group(0)


def test_the_generated_sshd_policy_carries_every_restriction():
    """The policy is generated from the configured export root, not shipped."""

    from appliance.config import ApplianceConfig
    from appliance.host_config import render_sshd_policy
    from appliance.paths import AppliancePaths

    paths = AppliancePaths(
        install_root=Path("/opt/ems-solarflow"),
        config_dir=Path("/etc/ems-appliance-manager"),
        state_dir=Path("/var/lib/ems-appliance-manager"),
        log_dir=Path("/var/log/ems-appliance-manager"),
        runtime_dir=Path("/run/ems-appliance-manager"),
        export_root=Path("/srv/ems-appliance-export"),
    )
    text = render_sshd_policy(paths, ApplianceConfig())
    assert "ChrootDirectory /srv/ems-appliance-export" in text
    assert "Match User ems-backup" in text
    for directive in (
        "PasswordAuthentication no",
        "PubkeyAuthentication yes",
        "PermitTTY no",
        "AllowTcpForwarding no",
        "X11Forwarding no",
        "PermitTunnel no",
        "GatewayPorts no",
        "ForceCommand internal-sftp",
    ):
        assert directive in text, directive


def test_the_static_sshd_drop_in_is_no_longer_shipped():
    """A conffile could disagree with the root-owned generated policy."""

    assert not (PACKAGING / "sshd").exists()
    conffiles = (PACKAGING / "debian" / "conffiles").read_text(encoding="utf-8")
    assert "sshd_config.d" not in conffiles, conffiles


def test_access_configuration_failures_are_not_swallowed():
    postinst = POSTINST.read_text(encoding="utf-8")
    for line in postinst.splitlines():
        if "setfacl" in line:
            assert "|| true" not in line, (
                "a failed export ACL must be visible, not swallowed"
            )


# --- dependencies ----------------------------------------------------------


def control_field(control, name):
    """One deb822 field, with its continuation lines folded in.

    dpkg joins a field with the lines that follow it starting with whitespace,
    so a parser that reads only the first line silently loses half of a folded
    Depends and calls the package under-declared.
    """

    lines = control.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{name}:"):
            continue
        value = [line.partition(":")[2]]
        for continuation in lines[index + 1 :]:
            if not continuation[:1].isspace():
                break
            value.append(continuation)
        return " ".join(part.strip() for part in value)
    return ""


def test_required_host_tools_are_declared_as_dependencies():
    declared = control_field(CONTROL.read_text(encoding="utf-8"), "Depends")
    assert declared
    for package in ("python3", "systemd", "adduser", "acl", "iproute2", "procps", "ca-certificates"):
        assert package in declared, f"{package} must be a declared dependency"


def test_optional_host_tools_stay_recommendations():
    recommends = control_field(CONTROL.read_text(encoding="utf-8"), "Recommends")
    assert recommends
    for package in ("network-manager", "openssh-server"):
        assert package in recommends, package


