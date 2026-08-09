# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packaging and deployment contract.

The privilege separation only exists if the shipped systemd units and the
package layout actually implement it, so the unit files, the tmpfiles rules and
the host configuration are checked here rather than described in prose only.
"""

import configparser
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


def test_the_agent_runs_as_root_and_only_on_a_unix_socket():
    service = unit(AGENT_UNIT)["Service"]
    assert service["User"] == "root"
    assert service["ExecStart"] == "/usr/bin/ems-appliance agent"
    assert service["RestrictAddressFamilies"] == "AF_UNIX"
    assert service["RuntimeDirectory"] == "ems-appliance-manager"
    assert service["RuntimeDirectoryMode"] == "0750"


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
    assert writable == ["/var/lib/ems-appliance-manager", "/var/log/ems-appliance-manager"]


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
    assert re.search(r"^d /var/lib/ems-appliance-manager 0750 ems-appliance-web", rules, re.M)
    assert re.search(r"^d /var/log/ems-appliance-manager 0750 ems-appliance-web", rules, re.M)


def test_the_postinst_creates_unprivileged_service_accounts():
    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    assert "adduser --system" in postinst
    assert "ems-appliance-web" in postinst
    assert "ems-backup" in postinst
    assert "nologin" in postinst


def test_removal_never_deletes_ems_data():
    postrm = (PACKAGING / "debian" / "postrm").read_text(encoding="utf-8")
    assert "/var/lib/ems-appliance-manager" in postrm
    assert "/opt/ems-solarflow" not in postrm.replace(
        "# EMS data under /opt/ems-solarflow", ""
    ).replace("EMS backups under /opt/ems-solarflow are never touched.", "")


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
