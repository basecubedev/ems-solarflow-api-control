# SPDX-License-Identifier: AGPL-3.0-or-later
"""The names the appliance shares with the deployment it hosts.

The appliance runs on the host, outside every container, so it cannot import
the project's own modules at runtime: it has to carry its own copy of the
identities it looks the deployment up by. A copy is a projection, not a second
authority, so it is pinned here against the module that owns it. Without this,
the appliance resolves a container name nothing on the host ever creates, finds
no EMS service, and an A/B update can commit a slot on which the power
controller never came back.
"""

import re
from pathlib import Path

import pytest

from admin.container_names import DEFAULT_EMS_CONTAINER
from appliance import config as appliance_config

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = ROOT / "packaging" / "appliance" / "config" / "appliance.conf"


def shipped_value(key):
    match = re.search(rf"^{key}\s*=\s*(\S+)\s*$", SHIPPED_CONFIG.read_text(encoding="utf-8"), re.M)
    assert match, f"{key} is not set in the shipped appliance.conf"
    return match.group(1)


def test_the_appliance_looks_for_the_ems_container_this_project_creates():
    assert appliance_config.DEFAULT_EMS_CONTAINER == DEFAULT_EMS_CONTAINER


def test_the_shipped_configuration_names_the_same_ems_container():
    assert shipped_value("ems_container") == DEFAULT_EMS_CONTAINER


def test_the_compose_file_this_project_ships_creates_that_container():
    """The authority's default is only useful while the compose file agrees."""

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert f"container_name: {DEFAULT_EMS_CONTAINER}" in compose


def published_host_ports(compose_text):
    return {
        int(match.group(1))
        for match in re.finditer(r'^\s*-\s*"(\d+):\d+"\s*$', compose_text, re.M)
    }


def test_the_manager_ui_does_not_claim_a_port_the_hosted_deployment_publishes():
    """The appliance exists to host this deployment, so it must not displace it.

    Both bind the host directly: the manager runs outside Docker on
    ``web_address``, and the compose file publishes the EMS dashboard. Whichever
    starts second fails, and on a fresh appliance that is the dashboard.
    """

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert appliance_config.DEFAULT_WEB_PORT not in published_host_ports(compose)
    assert int(shipped_value("web_port")) not in published_host_ports(compose)


def installer_value(key):
    match = re.search(
        rf'^{key}="([^"]*)"', (ROOT / "deploy" / "admin" / "install-admin-console.sh").read_text(
            encoding="utf-8"
        ), re.M
    )
    assert match, f"{key} is not set in install-admin-console.sh"
    return match.group(1)


def test_the_installer_the_appliance_runs_deploys_the_repository_it_validates():
    """A first installation is written by the shipped script, pinned by the
    appliance. Two different repositories would mean the operator confirmed one
    image and the compose file names another until the digest pin corrects it.
    """

    assert installer_value("IMAGE") == appliance_config.DEFAULT_ADMIN_REPOSITORY


def test_the_installer_defines_the_admin_service_the_appliance_looks_for():
    """The appliance edits the service by name; a script that writes a different
    one leaves a deployment nothing can find."""

    assert installer_value("COMPOSE_SERVICE") == appliance_config.DEFAULT_ADMIN_SERVICE
    assert installer_value("CONTAINER_NAME") == appliance_config.DEFAULT_ADMIN_CONTAINER


def test_the_installer_writes_the_files_the_appliance_resolves():
    from appliance.admin_deployment import COMPOSE_CANDIDATES, ENV_CANDIDATES

    assert installer_value("COMPOSE_FILE") in COMPOSE_CANDIDATES
    assert installer_value("ENV_FILE") in ENV_CANDIDATES


def test_the_package_ships_the_installer_the_appliance_calls():
    from appliance.admin_bootstrap import INSTALLER_NAME
    from appliance.paths import DEFAULT_PACKAGE_LIBDIR

    build = (ROOT / "packaging" / "appliance" / "build-deb.sh").read_text(encoding="utf-8")

    assert f"{DEFAULT_PACKAGE_LIBDIR}/{INSTALLER_NAME}" in build.replace("$STAGE", "")


def test_the_shipped_configuration_names_the_deployment_account_the_package_creates():
    postinst = (ROOT / "packaging" / "appliance" / "debian" / "postinst").read_text(
        encoding="utf-8"
    )

    assert shipped_value("deployment_user") == appliance_config.DEFAULT_DEPLOYMENT_USER
    assert f"adduser --system --ingroup {appliance_config.DEFAULT_DEPLOYMENT_USER}" in postinst


# Everything that drives the packaged web service over HTTP. None of them may
# carry its own idea of the port: they run against a real installation, where
# the answer to a stale port is "connection refused" and not "the port moved".
WEB_PROBES = (
    "scripts/appliance-guest-smoke.sh",
    "tests/helpers/appliance_systemd.py",
    "tests/helpers/appliance_http_client.py",
)


@pytest.mark.parametrize("relative", WEB_PROBES)
def test_no_probe_of_the_packaged_web_service_carries_its_own_port(relative):
    """A gate that cannot run is a gate that goes stale unnoticed.

    The ARM64 gate needs qemu, so it sat at NOT RUN while the manager UI moved
    to another port; three probes kept aiming at the old one and only said so
    on the first real boot. Each now takes the port from whatever owns it —
    the installed configuration in the guest, the config module on the host,
    the caller's argument in the copied-in client.
    """

    text = (ROOT / relative).read_text(encoding="utf-8")
    hardcoded = re.findall(r"127\.0\.0\.1:(\d+)", text)
    stale = [port for port in hardcoded if port != "{port}"]

    assert not stale, f"{relative} hardcodes port(s) {stale}"


def test_the_guest_smoke_test_reads_the_port_from_the_installed_configuration():
    smoke = (ROOT / "scripts" / "appliance-guest-smoke.sh").read_text(encoding="utf-8")

    assert "web_port" in smoke, "the guest smoke test does not read web_port from the config"


def test_the_container_driver_takes_the_port_from_the_module_that_owns_it():
    driver = (ROOT / "tests" / "helpers" / "appliance_systemd.py").read_text(encoding="utf-8")

    assert "DEFAULT_WEB_PORT" in driver


def test_the_shipped_configuration_is_the_port_authority_the_manager_uses():
    assert int(shipped_value("web_port")) == appliance_config.DEFAULT_WEB_PORT


# --- the OS update transport (DOC-005) ---------------------------------------


def test_the_shipped_configuration_names_every_os_release_key_the_code_reads():
    """A key the code defaults but the file never mentions is a key nobody sets.

    The release directory and the keyring decide where an OS update comes from
    and what is allowed to sign it. Both were resolved from a Python default
    and appeared in no shipped file, so an operator had nothing to point at.
    """

    for key, default in (
        ("os_release_dir", appliance_config.DEFAULT_OS_RELEASE_DIR),
        ("os_release_keyring", appliance_config.DEFAULT_OS_RELEASE_KEYRING),
    ):
        assert shipped_value(key) == default


def test_the_shipped_configuration_ships_no_default_release_source():
    """An appliance with no configured index must say so, not guess a host."""

    conf = SHIPPED_CONFIG.read_text(encoding="utf-8")

    assert re.search(r"^os_release_index_url\s*=\s*$", conf, re.M), (
        "os_release_index_url must be present and empty in the shipped configuration"
    )
    assert appliance_config.DEFAULT_OS_RELEASE_INDEX_URL == ""


# --- Admin's transition record, which the appliance reads but never owns ------


def test_the_appliance_looks_for_the_transition_file_admin_writes():
    """The appliance yields to Admin's replacement, so it has to find the record.

    It runs outside every container and cannot import the module that owns
    these names, so it carries a copy. A copy that drifts means the appliance
    silently stops yielding and both layers write the deployment at once.
    """

    from admin.admin_update import PENDING_TRANSITION_FILE
    from appliance import admin_transition

    assert admin_transition.TRANSITION_FILE == PENDING_TRANSITION_FILE


def test_the_appliance_looks_in_the_state_directory_admin_uses():
    from appliance import admin_transition

    source = (ROOT / "admin" / "admin_update.py").read_text(encoding="utf-8")

    assert f'/ "{admin_transition.STATE_SUBDIR}"' in source, (
        "admin_update.py no longer resolves its state directory the way the appliance expects"
    )


def test_the_appliance_reads_the_data_dir_variable_admin_reads():
    from appliance import admin_transition

    source = (ROOT / "admin" / "releases.py").read_text(encoding="utf-8")

    assert f'"{admin_transition.ENV_ADMIN_DATA_DIR}"' in source


def test_the_installer_writes_that_variable_into_the_deployment():
    from appliance import admin_transition

    installer = (ROOT / "deploy" / "admin" / "install-admin-console.sh").read_text(
        encoding="utf-8"
    )

    assert admin_transition.ENV_ADMIN_DATA_DIR in installer
