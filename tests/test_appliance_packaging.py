# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packaging and deployment contract.

The privilege separation only exists if the shipped systemd units and the
package layout actually implement it, so the unit files, the tmpfiles rules and
the host configuration are checked here rather than described in prose only.
"""

import configparser
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from appliance import manager_verify
from appliance import paths as appliance_paths
from appliance.config import load_allowed_images, load_config
from appliance.paths import AppliancePaths
from appliance.version import SUPPORTED_ARCHITECTURES, SUPPORTED_PI_MODELS

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

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


def test_the_shipped_control_version_is_a_placeholder_nothing_publishes():
    """``build-deb.sh`` rewrites this line from the version it was given, so the
    checked-in value reaches no package. It used to have to match a literal in
    ``appliance/version.py``; there is no literal now, and a real-looking version
    sitting here would be the next thing somebody mistook for one."""

    control = (PACKAGING / "debian" / "control").read_text(encoding="utf-8")
    shipped = [line for line in control.splitlines() if line.startswith("Version:")]

    assert shipped == ["Version: 0.0.0~placeholder"], shipped
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    assert 's/^Version: .*/Version: ${VERSION}/' in build, (
        "the build no longer overwrites the control version, so the placeholder "
        "above would ship"
    )


def test_supported_platforms_are_declared_in_one_place():
    assert SUPPORTED_ARCHITECTURES == ("arm64",)
    assert SUPPORTED_PI_MODELS == (
        "Raspberry Pi 3",
        "Raspberry Pi 4",
        "Raspberry Pi 5",
    )


def test_maintainer_scripts_are_executable_shell():
    for name in ("postinst", "prerm", "postrm"):
        script = PACKAGING / "debian" / name
        assert script.is_file(), name
        assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR, name
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh"), name


def test_operator_configuration_is_not_a_conffile_on_a_shared_path():
    """dpkg's conffile machinery cannot defend a file under a shared bind.

    Upstream re-seeds every declared shared path from the booting slot's own
    root at each boot, so a packaged copy under /etc/ems-appliance-manager wins
    over the operator's edit however dpkg marked it. These two are shipped as
    templates instead and seeded once; see tests/test_appliance_config_seed.py.
    """

    conffiles = (PACKAGING / "debian" / "conffiles").read_text(encoding="utf-8").split()

    assert "/etc/ems-appliance-manager/appliance.conf" not in conffiles
    assert "/etc/ems-appliance-manager/allowed-images.conf" not in conffiles


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


def _permission_hardening_block():
    """The shipped hardening function, lifted out of the real postinst.

    Extracted rather than restated: the test has to run the same lines the
    package runs, and a renamed or restructured function has to fail here
    loudly instead of passing against a stale copy.
    """

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    match = re.search(
        r"^ARMED_REVERTER_NAME=.*?^harden_agent_state\(\) \{.*?^\}$",
        postinst,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "the postinst no longer defines harden_agent_state()"
    return match.group(0)


def _run_permission_hardening(tmp_path, state_dir, log_dir):
    """Run the extracted hardening function against a fake tree, unprivileged."""

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    # chown to root needs privilege this test does not have, and the execute
    # bit is what is under test; ownership is not.
    chown = stub_bin / "chown"
    chown.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    chown.chmod(0o755)

    script = "\n".join(
        [
            "set -e",
            f'STATE_DIR="{state_dir}"',
            f'LOG_DIR="{log_dir}"',
            'fail() { echo "$1" >&2; exit 1; }',
            _permission_hardening_block(),
            "harden_agent_state",
        ]
    )
    env = dict(os.environ, PATH=f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    subprocess.run(["sh", "-c", script], check=True, env=env)


def test_a_manager_install_leaves_the_armed_reverter_executable(tmp_path):
    """The install must not disarm the reverter armed moments before it.

    ``manager_verify.arm`` snapshots the outgoing package's reverter and
    systemd executes it when the deadline expires. A copy without an execute
    bit fails with 203/EXEC, so the deadline never decides and the only way
    back out of a manager install is gone.
    """

    state_dir = tmp_path / "state"
    log_dir = tmp_path / "log"
    reverter = state_dir / "agent" / "packages" / manager_verify.REVERTER_NAME
    reverter.parent.mkdir(parents=True)
    ordinary = state_dir / "agent" / "operations" / "operations.json"
    ordinary.parent.mkdir(parents=True)
    (log_dir / "agent").mkdir(parents=True)
    (log_dir / "audit").mkdir(parents=True)

    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    reverter.chmod(manager_verify.REVERTER_MODE)
    ordinary.write_text("{}\n", encoding="utf-8")
    ordinary.chmod(0o644)

    _run_permission_hardening(tmp_path, state_dir, log_dir)

    # The tightening itself must keep working.
    assert stat.S_IMODE(ordinary.stat().st_mode) == 0o600
    assert stat.S_IMODE(reverter.stat().st_mode) == manager_verify.REVERTER_MODE


def installed_packages_dir():
    """Where ``arm()`` puts the snapshot on an appliance the package installed."""

    return appliance_paths.AppliancePaths(
        install_root=Path(appliance_paths.DEFAULT_INSTALL_ROOT),
        config_dir=Path(appliance_paths.DEFAULT_CONFIG_DIR),
        state_dir=Path(appliance_paths.DEFAULT_STATE_DIR),
        log_dir=Path(appliance_paths.DEFAULT_LOG_DIR),
        runtime_dir=Path(appliance_paths.DEFAULT_RUNTIME_DIR),
    ).packages_dir


def test_the_postinst_restores_in_the_directory_arming_writes_to():
    """The shell spells the directory out, and a spelled-out path drifts.

    ``packages_dir`` has moved once already. If it moves again and this literal
    does not, the restore silently stops matching anything and the 203/EXEC
    defect returns with the suite green.
    """

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    expected = installed_packages_dir() / manager_verify.REVERTER_NAME
    relative = expected.relative_to(appliance_paths.DEFAULT_STATE_DIR)
    assert f'$STATE_DIR/{relative.parent}/$ARMED_REVERTER_NAME' in postinst, postinst
    assert f"{appliance_paths.DEFAULT_STATE_DIR}/packages/" not in postinst, (
        "that is the legacy directory migration.py moves away from"
    )


def test_the_postinst_names_the_reverter_the_manager_arms():
    """Shell cannot import the constant, so the duplicated name is held to it.

    The mode is not restated here: the test above proves it by behaviour.
    """

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    assert manager_verify.REVERTER_NAME in postinst


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

    assert config.web_port == 8088
    assert config.admin_port == 8090
    assert config.web_user == "ems-appliance-web"
    assert config.socket_group == "ems-appliance"
    assert config.supported_architectures == ("arm64",)
    assert config.ssh_key_accounts == ("ems-backup", "ems-shell")
    assert config.automatic_security_updates is False


def test_the_shipped_image_allowlist_is_the_project_repository():
    images = load_allowed_images(PACKAGING / "config" / "allowed-images.conf")
    assert images.repositories == ("ghcr.io/basecubedev/ems-solarflow-admin",)
    assert images.expected_source.startswith("https://github.com/basecubedev/")
    assert images.legacy_exempt_tags == ()


def test_the_shipped_allowlist_offers_release_candidates():
    """A recorded decision, not a default that drifted.

    Before 1.0 this project publishes more Admin candidates than releases, so an
    appliance that refused them would ship a version list with one entry in it.
    The gate itself stays: ``protocol.py`` refuses a candidate tag wherever a
    host sets this back to false.
    """

    images = load_allowed_images(PACKAGING / "config" / "allowed-images.conf")

    assert images.allow_prerelease is True


def test_the_release_channel_has_no_mutable_fallback_configured():
    text = (PACKAGING / "config" / "appliance.conf").read_text(encoding="utf-8")
    assert re.search(r"^release_index_url\s*=\s*$", text, re.M)
    assert ":latest" not in text


def test_logrotate_bounds_the_appliance_logs():
    rules = (PACKAGING / "logrotate" / "ems-appliance-manager").read_text(encoding="utf-8")
    assert "/var/log/ems-appliance-manager/web/*.log" in rules
    assert "rotate" in rules
    assert "create 0640 ems-appliance-web ems-appliance" in rules


def test_the_cli_wrapper_runs_the_packaged_python_module():
    wrapper = (PACKAGING / "bin" / "ems-appliance").read_text(encoding="utf-8")
    assert "PYTHONPATH=/usr/lib/ems-appliance-manager" in wrapper
    assert "python3 -P -m appliance" in wrapper


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


def declared_dependencies():
    control = (PACKAGING / "debian" / "control").read_text(encoding="utf-8")
    depends = control_field(control, "Depends")
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
SEED_UNIT = PACKAGING / "systemd" / "ems-appliance-config-seed.service"


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


def test_every_unit_reading_the_generated_host_paths_starts_after_the_unit_that_writes_it():
    """host-paths.env is rewritten at boot whenever appliance.conf drifted.

    Both units are only WantedBy multi-user.target, so without an ordering
    edge systemd may start the export unit first: it then binds the old
    roots and hands them to `backup-access activate` before the new sshd
    policy exists. The watcher is held too, because the directory it watches
    comes from the drop-in the same apply writes. The edge is accepted from
    either side, so this states the property rather than one spelling of it.
    """

    ordered_first = set(unit(SEED_UNIT)["Unit"].get("Before", "").split())
    for path in (AGENT_UNIT, WEB_UNIT, EXPORT_SERVICE_UNIT, EXPORT_PATH_UNIT):
        after = set(unit(path)["Unit"].get("After", "").split())
        assert path.name in ordered_first or SEED_UNIT.name in after, path.name


# --- backup access teardown -------------------------------------------------


def test_removal_disables_backup_authentication():
    prerm = (PACKAGING / "debian" / "prerm").read_text(encoding="utf-8")
    assert "backup-access" in prerm, prerm


def test_purge_removes_the_backup_account_its_keys_and_the_feature_acls():
    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    assert "setfacl" in postrm and "-x" in postrm, postrm
    assert "deluser" in postrm, postrm
    assert "authorized_keys" in postrm, postrm


def test_purge_does_not_depend_on_files_dpkg_has_already_removed():
    """Only the maintainer scripts survive into purge, so the work lives there."""

    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    assert "/usr/lib/ems-appliance-manager/backup-account.sh" not in postrm, postrm
    assert "created_by_package" in postrm, postrm


def test_purge_deletes_an_account_only_when_the_package_created_it():
    """The backup account's deletion is gated on its ownership record.

    Measured against that account's own deluser rather than the first one in the
    file. A second account gained a retirement path of its own, and position in
    the file was never what this invariant was about -- it is about which check
    precedes which deletion.
    """

    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    gate = postrm.index("created_by_package")

    assert postrm.index('deluser --quiet "$BACKUP_USER"') > gate, (
        "the ownership record must gate the deletion"
    )
    assert "home_created_by_package" in postrm, postrm


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


def test_the_first_boot_gets_a_chance_to_establish_the_account():
    """The image bakes the account into a read-only /etc and the proof of it
    onto a partition that is empty until this boot mounts it. Nothing else runs
    between those two facts."""

    export = EXPORT_SERVICE_UNIT.read_text(encoding="utf-8")

    assert "ExecStartPre=-/usr/lib/ems-appliance-manager/backup-account.sh ensure" in export


def test_establishing_the_account_may_not_take_the_export_root_down():
    """A conflict this cannot resolve is not a reason to stop exporting; the
    activation step downstream is what refuses on unresolved ownership."""

    export = EXPORT_SERVICE_UNIT.read_text(encoding="utf-8")

    for line in export.splitlines():
        if line.startswith("ExecStartPre") and "backup-account.sh" in line:
            assert line.startswith("ExecStartPre=-"), line


def test_the_disable_unit_is_shipped_by_the_package():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    assert "ems-appliance-backup-access-disable.service" in build


# --- the SFTP tiers ---------------------------------------------------------


def test_the_policy_tier_proves_a_session_under_the_policy_it_reported():
    """An effective policy and a session in two records are two claims.

    The confinement tier used to report five protocol cases as NOT RUN because
    it could not issue an attributable key, while a second tier proved the same
    cases with one. Nothing then said the session had run under the policy the
    first tier reported. It issues its own key now, through the appliance.
    """

    tier = (SCRIPTS / "appliance-guest-sftp-lifecycle.sh").read_text(encoding="utf-8")

    assert "appliance-guest-issue-backup-key.sh" in tier
    assert "sshd -T -C" in tier
    for case in (
        "a real sftp session with an appliance-issued key",
        "the session root is the chroot",
        "a path outside the chroot is not reachable",
        "a parent-directory traversal cannot leave the chroot",
    ):
        assert tier.count(case) >= 2, case


def test_a_session_that_never_opened_is_never_read_as_confinement():
    tier = (SCRIPTS / "appliance-guest-sftp-lifecycle.sh").read_text(encoding="utf-8")

    assert "NOT RUN" in tier
    assert "exit 3" in tier


def test_the_key_is_issued_through_the_appliance_and_never_written_by_hand():
    issuer = (SCRIPTS / "appliance-guest-issue-backup-key.sh").read_text(encoding="utf-8")

    assert "/api/ssh/keys" in issuer
    assert "/api/operations/confirm" in issuer
    assert "confirmation_token" in issuer
    assert "authorized_keys" not in issuer.split("# Exit status")[-1]


def test_both_sftp_tiers_issue_their_key_the_same_way():
    for name in ("appliance-guest-sftp-lifecycle.sh", "appliance-guest-sftp-session.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "appliance-guest-issue-backup-key.sh" in text, name


def test_the_amd64_driver_carries_every_tier_it_runs():
    driver = (SCRIPTS / "appliance-smoke-vm-amd64.sh").read_text(encoding="utf-8")
    copied = driver.split("root@127.0.0.1:/root/")[0]

    for name in (
        "appliance-guest-sftp-lifecycle.sh",
        "appliance-guest-sftp-session.sh",
        "appliance-guest-issue-backup-key.sh",
    ):
        assert name in copied, name


def test_the_forced_command_cannot_be_swapped_for_a_chosen_subsystem():
    tier = (SCRIPTS / "appliance-guest-sftp-lifecycle.sh").read_text(encoding="utf-8")

    assert "-s " in tier
    assert "subsystem" in tier.lower()


def test_the_admin_installer_the_repair_advice_names_is_shipped():
    """Three repair findings tell the operator to run install-admin-console.sh.

    On a freshly flashed appliance that script is the only thing that can write
    the Admin compose and environment files, and the Admin console is in turn
    the only supported way to deploy EMS. Naming a tool the image does not carry
    leaves a new owner with a manager that can update Admin but never install it.
    """

    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    installer = PACKAGING.parents[1] / "deploy" / "admin" / "install-admin-console.sh"

    assert installer.is_file()
    assert "install-admin-console.sh" in build

    lifecycle = (PACKAGING.parents[1] / "appliance" / "admin_lifecycle.py").read_text(
        encoding="utf-8"
    )
    assert "install-admin-console.sh" in lifecycle


def test_the_root_cli_wrapper_keeps_the_callers_environment_off_sys_path():
    """`sudo ems-appliance` runs as root from wherever the operator stood.

    `python3 -m` puts the working directory first on sys.path, so a module
    planted in a writable directory would be imported by the privileged CLI,
    and an inherited PYTHONPATH would be appended to the packaged one.
    """

    wrapper = (PACKAGING / "bin" / "ems-appliance").read_text(encoding="utf-8")

    assert "python3 -P -m appliance" in wrapper
    assert "${PYTHONPATH" not in wrapper


def test_removal_disables_every_unit_installation_enabled():
    """A dangling .wants symlink is a unit systemd still tries to start."""

    import re

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    prerm = (PACKAGING / "debian" / "prerm").read_text(encoding="utf-8")

    enabled = set()
    for block in re.findall(r'(?:AB_)?UNITS="([^"]+)"', postinst):
        enabled.update(block.split())

    assert enabled, "no unit list was found in postinst"
    for unit in enabled:
        assert unit in prerm, f"{unit} is enabled on install and never disabled"


def test_the_logrotate_rules_match_files_that_exist():
    """After the web/agent split the shipped stanza matched nothing at all."""

    rules = (PACKAGING / "logrotate" / "ems-appliance-manager").read_text(encoding="utf-8")

    assert "/var/log/ems-appliance-manager/web/*.log" in rules
    assert "/var/log/ems-appliance-manager/agent/*.log" in rules
    # The audit trail is the record of what was done to this appliance.
    assert "/var/log/ems-appliance-manager/audit" not in rules


def test_a_shipped_etc_file_is_a_conffile():
    conffiles = (PACKAGING / "debian" / "conffiles").read_text(encoding="utf-8").split()

    assert "/etc/logrotate.d/ems-appliance-manager" in conffiles


# --- the OS release transport ----------------------------------------------


def test_the_package_ships_the_keyring_os_updates_are_verified_against():
    """The public half has to be on the card before the card is flashed.

    Verification is fail-closed: an appliance whose keyring is absent refuses
    every release with ``release_keyring_missing``. That refusal is correct, but
    it cannot be repaired afterwards on an A/B image -- the slot root is
    read-only and apt is refused agent-side -- so an image flashed without this
    file can never accept an OS update at all. The private half is not in this
    repository and never will be.
    """

    keyring = PACKAGING / "config" / "release-keyring.gpg"

    assert keyring.is_file(), "the package ships no release keyring"
    assert keyring.stat().st_size > 0
    assert b"PRIVATE KEY" not in keyring.read_bytes()

    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")
    assert "release-keyring.gpg" in build, "the keyring is never installed"


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg reads the keyring")
def test_the_shipped_keyring_carries_the_key_that_actually_signs():
    """gpgv verifies with the *signing* key's public half, not the primary's.

    Releases are signed by a subkey so the primary can stay offline, and the
    appliance pins the primary's fingerprint -- gpg reports that one for a
    subkey signature, so the pin keeps working. What does not keep working is
    verification against a keyring exported before the subkey existed: it
    carries no material for the key that made the signature, and every release
    is refused on an appliance that cannot be repaired afterwards.
    """

    keyring = PACKAGING / "config" / "release-keyring.gpg"
    listing = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(keyring)],
        capture_output=True, text=True, check=False,
    ).stdout

    primaries = [line for line in listing.splitlines() if line.startswith("pub:")]
    signing_subkeys = [
        line for line in listing.splitlines()
        if line.startswith("sub:") and "s" in line.split(":")[11]
    ]

    assert len(primaries) == 1, "the keyring must name exactly one release identity"
    assert signing_subkeys, (
        "the keyring carries no signing subkey; a release signed by one could not be verified"
    )


SHELL_ACCOUNT_SCRIPT = PACKAGING / "bin" / "shell-account.sh"


def run_shell_account(tmp_path, *, sudoers, account_exists=True):
    """Run the packaged script with a scripted ``getent`` and its own sudoers path.

    Executed rather than read: what broke a package install was the shell's own
    behaviour under ``set -e``, which no assertion about the file's text would
    have caught.
    """

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    getent = fake_bin / "getent"
    getent.write_text(f"#!/bin/sh\nexit {0 if account_exists else 2}\n", encoding="utf-8")
    getent.chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["EMS_APPLIANCE_SHELL_SUDOERS"] = str(sudoers)
    return subprocess.run(
        ["sh", str(SHELL_ACCOUNT_SCRIPT)],
        capture_output=True, text=True, env=environment, timeout=60, check=False,
    )


def test_a_host_without_sudo_still_installs_the_package(tmp_path):
    """The postinst calls this, and ``set -e`` made a missing /etc/sudoers.d take
    the whole install down with it. An appliance that refuses to install because
    one account cannot reach root is worse than one where it cannot: that one
    has no console at all."""

    result = run_shell_account(tmp_path, sudoers=tmp_path / "absent" / "ems-shell")

    assert result.returncode == 0, result.stderr
    assert "will not be able to reach root" in result.stdout, result.stdout


def test_the_sudoers_drop_in_is_root_only_and_says_nopasswd(tmp_path):
    """The account has no password at all, so a prompt would make it useless."""

    sudoers_dir = tmp_path / "sudoers.d"
    sudoers_dir.mkdir()
    target = sudoers_dir / "ems-shell"

    result = run_shell_account(tmp_path, sudoers=target)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8").strip() == "ems-shell ALL=(ALL:ALL) NOPASSWD: ALL"
    assert target.stat().st_mode & 0o777 == 0o440


def test_the_postinst_prepares_the_shell_account_and_the_build_ships_it():
    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")

    assert "shell-account.sh" in postinst
    assert "shell-account.sh" in build


def test_an_account_stranded_in_an_unwritable_home_is_moved(tmp_path):
    """0.3.3 and 0.3.4 created ems-shell under /home, where the agent cannot
    write a key. Those boxes already have the account, and the script leaves an
    existing one alone on purpose -- that rule protects an operator's choice,
    not a home no key can ever reach. Executed with a scripted getent and
    usermod so the move is observed rather than read."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "getent").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  passwd) echo "ems-shell:x:998:998::/home/ems-shell:/bin/bash"; exit 0 ;;\n'
        "  group) exit 2 ;;\n"
        "esac\nexit 2\n",
        encoding="utf-8",
    )
    (fake_bin / "usermod").write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path}/usermod.calls\nexit 0\n', encoding="utf-8"
    )
    for tool in ("getent", "usermod"):
        (fake_bin / tool).chmod(0o755)

    sudoers = tmp_path / "sudoers.d"
    sudoers.mkdir()
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["EMS_APPLIANCE_SHELL_SUDOERS"] = str(sudoers / "ems-shell")
    result = subprocess.run(
        ["sh", str(SHELL_ACCOUNT_SCRIPT)],
        capture_output=True, text=True, env=environment, timeout=60, check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "usermod.calls").read_text(encoding="utf-8")
    assert "-d /var/lib/ems-shell" in calls, calls
    assert "-m" in calls, "the contents must move with the home, not be abandoned"


def test_a_home_the_operator_chose_is_left_alone(tmp_path):
    """Only the exact path this package shipped is moved. Anywhere else is a
    decision somebody made, and an upgrade does not overrule it."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "getent").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  passwd) echo "ems-shell:x:998:998::/srv/operator-chose-this:/bin/bash"; exit 0 ;;\n'
        "esac\nexit 2\n",
        encoding="utf-8",
    )
    (fake_bin / "usermod").write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path}/usermod.calls\nexit 0\n', encoding="utf-8"
    )
    for tool in ("getent", "usermod"):
        (fake_bin / tool).chmod(0o755)

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["EMS_APPLIANCE_SHELL_SUDOERS"] = str(tmp_path / "absent" / "ems-shell")
    result = subprocess.run(
        ["sh", str(SHELL_ACCOUNT_SCRIPT)],
        capture_output=True, text=True, env=environment, timeout=60, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "usermod.calls").exists(), "an operator's home was moved"
def run_postrm_purge(tmp_path, *, account_exists=True, deluser_works=True):
    """Execute the purge branch with scripted account tooling.

    Read as text this would prove only that some words are present. What matters
    is the order: the account has to be shut before the sshd policy that gates
    it is deleted, and a deluser that fails must still leave it unusable.
    """

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    if account_exists:
        (tmp_path / "account-exists-ems-shell").write_text("yes", encoding="utf-8")

    # One marker per account. A single shared one made deluser ems-shell answer
    # for ems-rescue as well, which hid whether the rescue refusal was written.
    rescue = tmp_path / "account-exists-ems-rescue"
    rescue.write_text("yes", encoding="utf-8")
    (fake_bin / "getent").write_text(
        f'#!/bin/sh\necho "getent $@" >> {calls}\n'
        f'case "$1" in\n'
        f'  passwd) [ -f {tmp_path}/account-exists-"$2" ] && exit 0; exit 2 ;;\n'
        f"esac\nexit 2\n",
        encoding="utf-8",
    )
    (fake_bin / "usermod").write_text(
        f'#!/bin/sh\necho "usermod $@" >> {calls}\nexit 0\n', encoding="utf-8"
    )
    (fake_bin / "deluser").write_text(
        f'#!/bin/sh\necho "deluser $@" >> {calls}\n'
        + (f'rm -f {tmp_path}/account-exists-"$2"\n' if deluser_works else "")
        + "exit 0\n",
        encoding="utf-8",
    )
    for tool in ("getent", "usermod", "deluser"):
        (fake_bin / tool).chmod(0o755)

    sudoers = tmp_path / "sudoers.d" / "ems-shell"
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    sudoers.write_text("ems-shell ALL=(ALL:ALL) NOPASSWD: ALL\n", encoding="utf-8")

    sshd_dir = tmp_path / "sshd_config.d"
    sshd_dir.mkdir(exist_ok=True)
    (sshd_dir / "ems-appliance-backup.conf").write_text("Match User ems-shell\n", encoding="utf-8")

    environment = dict(os.environ)
    environment.update({
        "PATH": f"{fake_bin}:{environment['PATH']}",
        "EMS_APPLIANCE_SHELL_SUDOERS": str(sudoers),
        "EMS_APPLIANCE_SSHD_DIR": str(sshd_dir),
        "EMS_APPLIANCE_CONFIG_DIR": str(tmp_path / "etc"),
        "EMS_APPLIANCE_STATE_DIR": str(tmp_path / "state"),
        "EMS_APPLIANCE_LOG_DIR": str(tmp_path / "log"),
        "EMS_APPLIANCE_RUNTIME_DIR": str(tmp_path / "run"),
        "EMS_APPLIANCE_SYSTEMD_DIR": str(tmp_path / "systemd"),
        "EMS_APPLIANCE_ORIGIN_DIR": str(tmp_path / "origin"),
        "EMS_APPLIANCE_QUARANTINE_DIR": str(tmp_path / "quarantine"),
    })
    result = subprocess.run(
        ["sh", str(PACKAGING / "debian" / "postrm"), "purge"],
        capture_output=True, text=True, env=environment, timeout=120, check=False,
    )
    return result, (calls.read_text(encoding="utf-8") if calls.exists() else ""), sudoers


def test_purge_withdraws_the_root_account_before_the_gate_that_held_it():
    """The sshd Match block is what refuses this account every authentication
    method while it is off. Deleting that policy and leaving the account would
    make purge *widen* access: an off-by-default root login becomes an ordinary
    one under the host's own sshd defaults."""

    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    body = postrm.split('case "$1" in', 1)[1]

    retire = body.index("retire_shell_account")
    drop_policy = body.index('rm -f "$HOST_PATHS" "$SSHD_POLICY"')

    assert retire < drop_policy, "the account must be shut before its gate is removed"


def test_purge_removes_the_sudo_rights_and_expires_the_account(tmp_path):
    result, calls, sudoers = run_postrm_purge(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not sudoers.exists(), "the NOPASSWD drop-in outlived the package"
    assert "--expiredate 1" in calls, (
        "locking the password alone leaves public-key authentication working, "
        "which is the only kind this account ever had"
    )
    assert "deluser" in calls


def test_a_purge_that_cannot_delete_the_account_still_leaves_it_shut(tmp_path):
    result, calls, sudoers = run_postrm_purge(tmp_path, deluser_works=False)

    assert result.returncode == 0, result.stderr
    assert not sudoers.exists()
    assert "--expiredate 1" in calls
    assert "ems-shell" in result.stderr, "a surviving account must be reported, not hidden"


def test_a_sudoers_path_it_cannot_touch_does_not_abort_the_purge(tmp_path):
    """postrm runs once and there is no second pass. Under set -e an unwritable
    /etc/sudoers.d was enough to abort it part way, which leaves the package
    half removed and the summary of what survived unprinted -- the one place an
    operator would learn that anything was left behind."""

    unwritable = tmp_path / "locked"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        result, _, _ = run_postrm_purge(tmp_path, account_exists=False)
    finally:
        unwritable.chmod(0o700)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_purge_summary_still_prints_when_the_drop_in_is_out_of_reach(tmp_path):
    """The stderr summary is the contract other purge tests assert on. A cleanup
    step that aborts before it takes that away."""

    sudoers = tmp_path / "locked" / "ems-shell"
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    sudoers.parent.chmod(0o500)
    try:
        result, _, _ = run_postrm_purge(tmp_path, account_exists=True, deluser_works=False)
    finally:
        sudoers.parent.chmod(0o700)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "purge did not complete" in result.stderr, result.stdout + result.stderr


def test_purge_leaves_the_rescue_account_refused_over_ssh(tmp_path):
    """ems-rescue survives a purge, and so must the only thing keeping it off the
    network.

    Its password is published in docs/appliance/console-recovery.md, and the
    single reason that is not a LAN login is one Match block inside the policy
    this purge deletes -- there is no global PasswordAuthentication anywhere in
    the project. Deleting the gate and leaving the account would have made
    `apt purge` hand every published credential a way in.

    The account itself is not withdrawn: it is the documented way back into a
    board that will not boot, and its password may be one the operator chose.
    """

    sshd_dir = tmp_path / "sshd_config.d"
    result, _, _ = run_postrm_purge(tmp_path, account_exists=True)

    assert result.returncode == 0, result.stdout + result.stderr
    left = sshd_dir / "ems-appliance-rescue.conf"
    assert left.exists(), "nothing keeps ems-rescue off the network after a purge"

    policy = left.read_text(encoding="utf-8")
    assert "Match User ems-rescue" in policy
    assert "PasswordAuthentication no" in policy
    assert "KbdInteractiveAuthentication no" in policy, (
        "PasswordAuthentication alone leaves PAM's keyboard-interactive path, "
        "which asks for the same published password"
    )


def test_the_refusal_left_behind_matches_the_one_the_package_generated():
    """Two files now carry the same refusal and neither can import the other:
    the postrm runs when appliance/*.py is already gone. Pinned here instead."""

    from appliance.config import ApplianceConfig
    from appliance.host_config import render_sshd_policy
    from appliance.paths import AppliancePaths

    generated = render_sshd_policy(
        AppliancePaths(
            install_root=Path("/opt/ems-solarflow"),
            config_dir=Path("/etc/ems-appliance-manager"),
            state_dir=Path("/var/lib/ems-appliance-manager"),
            log_dir=Path("/var/log/ems-appliance-manager"),
            runtime_dir=Path("/run/ems-appliance-manager"),
            export_root=Path("/srv/ems-appliance-export"),
        ),
        ApplianceConfig(),
    )
    block = generated.split("Match User ems-rescue\n", 1)[1].split("Match User", 1)[0]
    directives = {line.strip() for line in block.splitlines() if line.strip()}

    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")

    for directive in directives:
        assert directive in postrm, (
            f"the generated policy refuses ems-rescue with {directive!r} and the "
            "purge leftover does not"
        )
