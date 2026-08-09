# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packaging and deployment contract.

The privilege separation only exists if the shipped systemd units and the
package layout actually implement it, so the unit files, the tmpfiles rules and
the host configuration are checked here rather than described in prose only.
"""

import configparser
import os
import re
import stat
from pathlib import Path

import pytest

from appliance.config import load_allowed_images, load_config
from appliance.paths import AppliancePaths
from appliance.version import APPLIANCE_VERSION, SUPPORTED_ARCHITECTURES, SUPPORTED_PI_MODELS

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"
AGENT_UNIT = PACKAGING / "systemd" / "ems-appliance-agent.service"
WEB_UNIT = PACKAGING / "systemd" / "ems-appliance-web.service"


def unit(path):
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


# --- package metadata ------------------------------------------------------


def test_control_declares_an_arm64_package_for_raspberry_pi_os():
    control = (PACKAGING / "debian" / "control").read_text(encoding="utf-8")
    assert "Package: ems-appliance-manager" in control
    assert "Architecture: arm64" in control
    assert "Depends: python3" in control
    assert "systemd" in control


def test_package_version_matches_the_python_package_version():
    control = (PACKAGING / "debian" / "control").read_text(encoding="utf-8")
    assert f"Version: {APPLIANCE_VERSION}" in control


def test_supported_platforms_are_declared_in_one_place():
    assert SUPPORTED_ARCHITECTURES == ("arm64",)
    assert SUPPORTED_PI_MODELS == ("Raspberry Pi 4", "Raspberry Pi 5")


def test_maintainer_scripts_are_executable_shell():
    for name in ("postinst", "prerm", "postrm"):
        script = PACKAGING / "debian" / name
        assert script.is_file(), name
        assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR, name
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh"), name


def test_configuration_files_are_marked_as_conffiles():
    conffiles = (PACKAGING / "debian" / "conffiles").read_text(encoding="utf-8").split()
    assert "/etc/ems-appliance-manager/appliance.conf" in conffiles
    assert "/etc/ems-appliance-manager/allowed-images.conf" in conffiles


def test_the_build_script_produces_a_checksum_and_asks_for_a_signature():
    script = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    assert "sha256sum" in script
    assert "detach-sign" in script
    assert "ems-appliance-manager_${VERSION}_${ARCH}" in script


# --- privilege separation --------------------------------------------------


def test_the_agent_runs_as_root_and_owns_the_socket_group():
    service = unit(AGENT_UNIT)["Service"]
    assert service["User"] == "root"
    assert service["Group"] == "ems-appliance"
    assert service["ExecStart"] == "/usr/bin/ems-appliance agent"
    assert service["RuntimeDirectory"] == "ems-appliance-manager"
    assert service["RuntimeDirectoryMode"] == "0750"
    # Loopback health checks, apt and the release index need IP sockets, and
    # ss(8) needs netlink; every other address family stays blocked.
    families = set(service["RestrictAddressFamilies"].split())
    assert families == {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"}


def test_the_web_process_does_not_run_as_root():
    service = unit(WEB_UNIT)["Service"]
    assert service["User"] == "ems-appliance-web"
    assert service["User"] != "root"
    assert service["Group"] == "ems-appliance"
    assert service["NoNewPrivileges"] == "yes"
    assert service["CapabilityBoundingSet"] == ""


def test_the_web_process_is_confined_to_the_appliance_state():
    service = unit(WEB_UNIT)["Service"]
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "yes"
    writable = service["ReadWritePaths"].split()
    assert writable == [
        "/var/lib/ems-appliance-manager/web",
        "/var/log/ems-appliance-manager/web",
    ]


def test_the_web_process_never_receives_the_docker_socket():
    text = WEB_UNIT.read_text(encoding="utf-8")
    assert "docker.sock" not in text
    assert "SupplementaryGroups=docker" not in text
    assert "docker" not in unit(WEB_UNIT)["Service"].get("SupplementaryGroups", "")


def test_the_web_unit_waits_for_the_agent():
    section = unit(WEB_UNIT)["Unit"]
    assert "ems-appliance-agent.service" in section["After"]
    assert "ems-appliance-agent.service" in section["Wants"]


def test_the_appliance_does_not_run_inside_the_ems_compose_stack():
    compose_files = [ROOT / "docker-compose.yml", ROOT / "docker-compose.example.yml"]
    for path in compose_files:
        if path.is_file():
            assert "ems-appliance" not in path.read_text(encoding="utf-8"), path


def test_the_socket_directory_is_root_owned_and_group_restricted():
    rules = (PACKAGING / "tmpfiles" / "ems-appliance-manager.conf").read_text(encoding="utf-8")
    assert re.search(r"^d /run/ems-appliance-manager 0750 root ems-appliance", rules, re.M)
    assert re.search(r"^d /var/lib/ems-appliance-manager 0750 root ems-appliance", rules, re.M)
    assert re.search(r"^d /var/log/ems-appliance-manager 0750 root ems-appliance", rules, re.M)
    assert re.search(
        r"^d /var/lib/ems-appliance-manager/web 0750 ems-appliance-web ems-appliance", rules, re.M
    )


AGENT_PRIVATE_DIRECTORIES = (
    "/var/lib/ems-appliance-manager/agent",
    "/var/lib/ems-appliance-manager/agent/operations",
    "/var/lib/ems-appliance-manager/agent/known-good",
    "/var/lib/ems-appliance-manager/agent/compose-backup",
    "/var/lib/ems-appliance-manager/agent/package-state",
    "/var/lib/ems-appliance-manager/agent/recovery",
    "/var/lib/ems-appliance-manager/agent/ssh-keys",
    "/var/lib/ems-appliance-manager/agent/support",
    "/var/lib/ems-appliance-manager/agent/packages",
    "/var/log/ems-appliance-manager/agent",
    "/var/log/ems-appliance-manager/audit",
)


@pytest.mark.parametrize("directory", AGENT_PRIVATE_DIRECTORIES)
def test_agent_state_is_declared_root_only(directory):
    """The shared group buys access to the socket, never to agent state."""

    rules = (PACKAGING / "tmpfiles" / "ems-appliance-manager.conf").read_text(encoding="utf-8")
    assert re.search(rf"^d {re.escape(directory)} 0700 root root", rules, re.M), directory
    assert not re.search(rf"^d {re.escape(directory)} \S+ \S+ ems-appliance", rules, re.M)


def test_the_web_unit_cannot_even_see_agent_state():
    unit = (PACKAGING / "systemd" / "ems-appliance-web.service").read_text(encoding="utf-8")
    inaccessible = [line for line in unit.splitlines() if line.startswith("InaccessiblePaths=")]
    assert inaccessible, unit
    for path in (
        "/var/lib/ems-appliance-manager/agent",
        "/var/log/ems-appliance-manager/agent",
        "/var/log/ems-appliance-manager/audit",
    ):
        assert path in inaccessible[0], path


def test_the_agent_creates_root_only_files_by_default():
    unit = (PACKAGING / "systemd" / "ems-appliance-agent.service").read_text(encoding="utf-8")
    assert "UMask=0077" in unit


def test_the_postinst_tightens_a_previously_group_readable_agent_tree():
    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    assert "chown -R root:root" in postinst
    assert "chmod 0700" in postinst


# --- smoke-test drivers ----------------------------------------------------

SCRIPTS = ROOT / "scripts"
SMOKE_SCRIPTS = (
    "appliance-guest-smoke.sh",
    "appliance-smoke-amd64.sh",
    "appliance-smoke-arm64.sh",
)


@pytest.mark.parametrize("name", SMOKE_SCRIPTS)
def test_the_smoke_drivers_are_executable(name):
    script = SCRIPTS / name
    assert script.is_file(), name
    assert script.stat().st_mode & stat.S_IXUSR, name


@pytest.mark.parametrize("name", ["appliance-smoke-amd64.sh", "appliance-smoke-arm64.sh"])
def test_a_run_that_could_not_happen_is_never_reported_as_a_pass(name):
    """Exit 3 and "NOT RUN" keep a missing QEMU from looking like a green run."""

    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "RESULT: NOT RUN" in text, name
    assert "exit 3" in text, name
    assert "fail_environment" in text, name


@pytest.mark.parametrize("name", ["appliance-smoke-amd64.sh", "appliance-smoke-arm64.sh"])
def test_the_smoke_drivers_clean_up_after_themselves(name):
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in text, name
    assert "mktemp -d" in text, name


def test_both_architectures_run_the_same_guest_check():
    for name in ("appliance-smoke-amd64.sh", "appliance-smoke-arm64.sh"):
        assert "appliance-guest-smoke.sh" in (SCRIPTS / name).read_text(encoding="utf-8"), name


def test_the_guest_check_covers_the_security_boundaries():
    guest = (SCRIPTS / "appliance-guest-smoke.sh").read_text(encoding="utf-8")
    for expected in (
        "verify-install",
        "cannot list",
        "cannot read the audit log",
        "no password reached the audit log",
        "chroot-safe",
        "660 root ems-appliance",
        "700 root root",
        "/api/session/setup",
        "/api/session/logout",
    ):
        assert expected in guest, expected


def test_the_arm64_driver_boots_a_real_aarch64_guest():
    arm64 = (SCRIPTS / "appliance-smoke-arm64.sh").read_text(encoding="utf-8")
    assert "qemu-system-aarch64" in arm64
    assert "-machine virt" in arm64
    # An emulated container would not be a booted system; say so explicitly.
    assert "not a booted system" in arm64


ACCOUNT_SCRIPT = PACKAGING / "bin" / "backup-account.sh"


def test_the_postinst_creates_unprivileged_service_accounts():
    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    assert "adduser --system" in postinst
    assert "ems-appliance-web" in postinst
    assert "nologin" in postinst
    assert "backup-account.sh ensure" in postinst


def test_the_backup_account_records_that_the_package_created_it():
    """Purge may only delete an account this package is known to have created."""

    script = ACCOUNT_SCRIPT.read_text(encoding="utf-8")
    assert "created_by_package" in script
    assert "home_created_by_package" in script
    assert "package_owns_account" in script


def test_the_backup_account_script_is_shipped_and_executable():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    assert "backup-account.sh" in build
    assert os.access(ACCOUNT_SCRIPT, os.X_OK)


def test_removal_never_deletes_ems_data():
    """Purge withdraws ACL grants inside the EMS installation; it removes nothing there."""

    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    assert "/var/lib/ems-appliance-manager" in postrm
    destructive = ("rm -rf", "rm -f", "rmdir", "mv ", "truncate", "> ")
    for line in postrm.splitlines():
        statement = line.split("#", 1)[0]
        if "INSTALL_ROOT" not in statement:
            continue
        for command in destructive:
            assert command not in statement, statement


def test_installation_never_restructures_an_existing_ems_install():
    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    for destructive in ("rm -rf /opt/ems-solarflow", "mv /opt/ems-solarflow", "> /opt/ems-solarflow"):
        assert destructive not in postinst


# --- shipped configuration -------------------------------------------------


def test_the_shipped_configuration_loads(tmp_path):
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    for name in ("appliance.conf", "allowed-images.conf"):
        (config_dir / name).write_text(
            (PACKAGING / "config" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    paths = AppliancePaths(
        install_root=tmp_path / "opt",
        config_dir=config_dir,
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "log",
        runtime_dir=tmp_path / "run",
    )
    config = load_config(paths)

    assert config.web_port == 8080
    assert config.admin_port == 8090
    assert config.web_user == "ems-appliance-web"
    assert config.socket_group == "ems-appliance"
    assert config.supported_architectures == ("arm64",)
    assert config.ssh_key_accounts == ("ems-backup",)
    assert config.automatic_security_updates is False


def test_the_shipped_image_allowlist_is_the_project_repository():
    images = load_allowed_images(PACKAGING / "config" / "allowed-images.conf")
    assert images.repositories == ("ghcr.io/basecubedev/ems-solarflow-admin",)
    assert images.expected_source.startswith("https://github.com/basecubedev/")
    assert images.allow_prerelease is False
    assert images.legacy_exempt_tags == ()


def test_the_release_channel_has_no_mutable_fallback_configured():
    text = (PACKAGING / "config" / "appliance.conf").read_text(encoding="utf-8")
    assert re.search(r"^release_index_url\s*=\s*$", text, re.M)
    assert ":latest" not in text


def test_logrotate_bounds_the_appliance_logs():
    rules = (PACKAGING / "logrotate" / "ems-appliance-manager").read_text(encoding="utf-8")
    assert "/var/log/ems-appliance-manager/*.log" in rules
    assert "rotate" in rules
    assert "create 0640 ems-appliance-web ems-appliance" in rules


def test_the_cli_wrapper_runs_the_packaged_python_module():
    wrapper = (PACKAGING / "bin" / "ems-appliance").read_text(encoding="utf-8")
    assert "PYTHONPATH=/usr/lib/ems-appliance-manager" in wrapper
    assert "python3 -m appliance" in wrapper


# --- runtime dependencies ---------------------------------------------------

# Every executable a shipped maintainer script or unit invokes, and the Debian
# package that provides it. "essential" marks packages dpkg guarantees.
REQUIRED_EXECUTABLES = {
    "setfacl": "acl",
    "mountpoint": "util-linux",
    "findmnt": "util-linux",
    "mount": "mount",
    "umount": "mount",
    "ss": "iproute2",
    "systemctl": "systemd",
    "systemd-tmpfiles": "systemd",
    "adduser": "adduser",
    "deluser": "adduser",
    "usermod": "passwd",
    "chage": "passwd",
    "python3": "python3",
    "getent": "essential",
    "readlink": "essential",
    "stat": "essential",
    "chown": "essential",
    "chmod": "essential",
    "mkdir": "essential",
    "rm": "essential",
}


def declared_dependencies():
    control = (PACKAGING / "debian" / "control").read_text(encoding="utf-8")
    depends = ""
    for block in control.split("\n"):
        if block.startswith("Depends:"):
            depends = block.partition(":")[2]
    return {
        entry.split("(")[0].strip()
        for alternative in depends.split(",")
        for entry in alternative.split("|")
        if entry.strip()
    }


@pytest.mark.parametrize("tool,package", sorted(REQUIRED_EXECUTABLES.items()))
def test_every_required_executable_comes_from_a_declared_dependency(tool, package):
    if package == "essential":
        return
    assert package in declared_dependencies(), f"{tool} needs {package} in Depends"


def test_the_mount_helpers_are_an_explicit_dependency():
    """util-linux does not ship mount(8)/umount(8) on Debian; the mount package does."""

    assert "mount" in declared_dependencies()


def test_the_export_script_only_calls_tools_the_package_depends_on():
    script = (PACKAGING / "bin" / "setup-export-root.sh").read_text(encoding="utf-8")
    for tool in ("setfacl", "mountpoint", "findmnt", "mount", "umount"):
        if tool in script:
            package = REQUIRED_EXECUTABLES[tool]
            assert package in declared_dependencies(), f"{tool} -> {package}"


# --- the agent sandbox ------------------------------------------------------


def test_the_agent_may_use_netlink_and_no_unrelated_address_family():
    """``ss`` needs AF_NETLINK; without it the Repair port check cannot run."""

    families = unit(AGENT_UNIT)["Service"]["RestrictAddressFamilies"].split()

    assert set(families) == {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"}, families


# --- host path configuration ------------------------------------------------

EXPORT_PATH_UNIT = PACKAGING / "systemd" / "ems-appliance-export.path"
EXPORT_SERVICE_UNIT = PACKAGING / "systemd" / "ems-appliance-export.service"


def test_the_shipped_path_unit_watches_the_default_install_root():
    watched = unit(EXPORT_PATH_UNIT)["Path"]["PathChanged"]
    assert watched == "/opt/ems-solarflow"


def test_a_custom_install_root_is_applied_to_the_path_unit_by_the_package():
    """A ``.path`` unit cannot expand variables, so the package generates a drop-in."""

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    assert "host-config" in postinst, postinst
    assert "ems-appliance-export.path.d" in (
        postinst + (PACKAGING / "bin" / "setup-export-root.sh").read_text(encoding="utf-8")
    ) or "host-config --apply" in postinst


def test_the_shell_and_python_read_the_same_host_path_variables():
    script = (PACKAGING / "bin" / "setup-export-root.sh").read_text(encoding="utf-8")
    from appliance.paths import ENV_EXPORT_ROOT, ENV_INSTALL_ROOT

    assert ENV_INSTALL_ROOT in script, ENV_INSTALL_ROOT
    assert ENV_EXPORT_ROOT in script, ENV_EXPORT_ROOT
    # The old, script-only names must not survive as a second contract.
    assert "EMS_INSTALL_ROOT" not in script.replace(ENV_INSTALL_ROOT, "")
    assert "EMS_EXPORT_ROOT" not in script.replace(ENV_EXPORT_ROOT, "")


def test_the_units_read_the_generated_host_path_environment():
    for path in (AGENT_UNIT, WEB_UNIT, EXPORT_SERVICE_UNIT):
        text = path.read_text(encoding="utf-8")
        assert "EnvironmentFile=-/etc/ems-appliance-manager/host-paths.env" in text, path.name


# --- backup access teardown -------------------------------------------------


def test_removal_disables_backup_authentication():
    prerm = (PACKAGING / "debian" / "prerm").read_text(encoding="utf-8")
    assert "backup-access" in prerm, prerm


def test_purge_removes_the_backup_account_its_keys_and_the_feature_acls():
    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    script = ACCOUNT_SCRIPT.read_text(encoding="utf-8")
    assert "backup-account.sh" in postrm, postrm
    assert "setfacl" in postrm and "-x" in postrm, postrm
    assert "deluser" in script, script
    assert "authorized_keys" in script, script


def test_purge_reports_what_it_could_not_withdraw():
    """A stale mount or a surviving account must not read as a clean purge."""

    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    assert "incomplete" in postrm
    assert "purge did not complete" in postrm


def test_removal_fails_closed_when_authentication_cannot_be_revoked():
    prerm = (PACKAGING / "debian" / "prerm").read_text(encoding="utf-8")
    assert "exit 1" in prerm, prerm
    assert "backup-account.sh" in prerm, prerm


def test_purge_only_removes_acl_entries_of_the_backup_account():
    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    # -b/--remove-all would drop unrelated ACLs an operator set themselves.
    assert "setfacl -b" not in postrm
    assert "--remove-all" not in postrm


# --- authentication follows the export state --------------------------------

DISABLE_UNIT = PACKAGING / "systemd" / "ems-appliance-backup-access-disable.service"


def test_a_failed_export_run_disables_backup_authentication():
    """Removing the confinement must remove the account's usable access."""

    export = EXPORT_SERVICE_UNIT.read_text(encoding="utf-8")
    assert "OnFailure=ems-appliance-backup-access-disable.service" in export, export
    assert DISABLE_UNIT.is_file()
    assert "backup-access disable" in DISABLE_UNIT.read_text(encoding="utf-8")


def test_a_successful_export_run_revalidates_backup_access():
    export = EXPORT_SERVICE_UNIT.read_text(encoding="utf-8")
    assert "ExecStartPost=/usr/bin/ems-appliance backup-access activate" in export, export


def test_the_disable_unit_is_shipped_by_the_package():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    assert "ems-appliance-backup-access-disable.service" in build
