# SPDX-License-Identifier: AGPL-3.0-or-later
"""Status collection, health normalisation, log bounding and the support archive.

Status collection is read-only and fault-isolated: one probe that fails must
degrade its own section, never the whole overview.
"""

import json
import tarfile

import pytest

from appliance.agent import AgentHandlers
from appliance.hostprobe import HostProbe
from appliance.status import (
    HEALTH_ATTENTION,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    SECTION_OK,
    SECTION_UNAVAILABLE,
    section,
)
from appliance.support_archive import EXCLUDED_BY_DEFAULT
from tests.helpers.appliance import ADMIN_CONTAINER, ADMIN_REPOSITORY, build_test_services

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

OS_RELEASE = """PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Raspberry Pi OS"
VERSION_ID="12"
VERSION="12 (bookworm)"
VERSION_CODENAME=bookworm
ID=debian
"""


def host_files(tmp_path):
    (tmp_path / "etc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc" / "os-release").write_text(OS_RELEASE, encoding="utf-8")
    (tmp_path / "proc" / "device-tree").mkdir(parents=True, exist_ok=True)
    (tmp_path / "proc" / "device-tree" / "model").write_text("Raspberry Pi 5 Model B Rev 1.0\x00")
    (tmp_path / "proc" / "uptime").write_text("1036800.00 900000.00\n", encoding="utf-8")
    (tmp_path / "proc" / "meminfo").write_text(
        "MemTotal:        8054304 kB\nMemAvailable:    6000000 kB\n", encoding="utf-8"
    )
    (tmp_path / "sys" / "class" / "thermal" / "thermal_zone0").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sys" / "class" / "thermal" / "thermal_zone0" / "temp").write_text("51234\n")
    return tmp_path


def appliance(tmp_path, *, healthy=True):
    host_files(tmp_path)
    services = build_test_services(tmp_path)
    services.host.write_deployment(tag="v1.0.0")
    services.host.publish_image("v1.0.0")
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    if healthy:
        services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    return services


# --- host probes -----------------------------------------------------------


def test_hardware_and_os_are_read_from_the_host(tmp_path):
    probe = HostProbe(root=str(host_files(tmp_path)))
    assert probe.hardware()["model"] == "Raspberry Pi 5 Model B Rev 1.0"
    operating_system = probe.operating_system()
    assert operating_system["name"] == "Raspberry Pi OS"
    assert operating_system["codename"] == "bookworm"
    assert operating_system["kernel"]


def test_uptime_temperature_and_memory_are_normalised(tmp_path):
    probe = HostProbe(root=str(host_files(tmp_path)))
    assert probe.uptime()["days"] == 12
    assert probe.temperature() == {"celsius": 51.2, "available": True}
    memory = probe.memory()
    assert memory["total_mb"] == 7865
    assert 0 < memory["used_percent"] < 100


def test_a_missing_probe_file_degrades_that_probe_only(tmp_path):
    probe = HostProbe(root=str(tmp_path))
    assert probe.hardware()["model"] == "unknown"
    assert probe.temperature() == {"celsius": None, "available": False}
    assert probe.uptime()["seconds"] == 0
    assert probe.operating_system()["name"] == "unknown"


def test_reboot_requirement_is_read_from_the_marker(tmp_path):
    (tmp_path / "var" / "run").mkdir(parents=True)
    (tmp_path / "var" / "run" / "reboot-required").write_text("", encoding="utf-8")
    probe = HostProbe(root=str(tmp_path))
    assert probe.reboot_required()["required"] is True


# --- fault isolation -------------------------------------------------------


def test_a_failing_section_does_not_take_down_the_overview():
    def explode():
        raise RuntimeError("probe failed")

    result = section("updates", explode)
    assert result["status"] == SECTION_UNAVAILABLE
    assert result["error"] == "RuntimeError"


def test_overview_collects_every_section(tmp_path):
    services = appliance(tmp_path)
    overview = services.status.overview()
    for name in ("system", "docker", "admin", "updates", "network", "ssh", "operations"):
        assert overview[name]["status"] == SECTION_OK, name
    assert overview["appliance_version"]
    assert overview["health"]["level"] in (HEALTH_HEALTHY, HEALTH_ATTENTION, HEALTH_DEGRADED)


def test_overview_survives_a_broken_probe(tmp_path):
    services = appliance(tmp_path)

    def explode():
        raise OSError("no nmcli")

    services.status.network_state = explode
    overview = services.status.overview()
    assert overview["network"]["status"] == SECTION_UNAVAILABLE
    assert overview["system"]["status"] == SECTION_OK
    assert "network_unavailable" in [item["code"] for item in overview["health"]["warnings"]]


# --- health normalisation --------------------------------------------------


def test_a_stopped_docker_daemon_is_degraded(tmp_path):
    services = appliance(tmp_path)
    services.host.docker_running = False
    health = services.status.overview()["health"]
    assert health["level"] == HEALTH_DEGRADED
    assert "docker_not_running" in [item["code"] for item in health["warnings"]]


def test_a_missing_admin_container_is_degraded(tmp_path):
    services = appliance(tmp_path, healthy=False)
    health = services.status.overview()["health"]
    assert health["level"] == HEALTH_DEGRADED
    assert "admin_not_installed" in [item["code"] for item in health["warnings"]]


def test_pending_security_updates_ask_for_attention(tmp_path):
    services = appliance(tmp_path)
    codes = [item["code"] for item in services.status.overview()["health"]["warnings"]]
    assert "security_updates_pending" in codes


def test_the_last_successful_operation_is_reported(tmp_path):
    services = appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "restart"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    last = services.status.overview()["health"]["last_successful_operation"]
    assert last["type"] == "admin.lifecycle"


# --- logs ------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "appliance_web",
        "appliance_agent",
        "operations",
        "audit",
        "admin_container",
        "ems_container",
        "docker_daemon",
        "boot",
        "packages",
    ],
)
def test_every_declared_log_source_answers(tmp_path, source):
    services = appliance(tmp_path)
    services.host.run_container("ems-solarflow", f"{ADMIN_REPOSITORY}:v1.0.0")
    log = services.status.read_log(source, 20)
    assert log["source"] == source
    assert log["lines"] <= 20
    assert isinstance(log["text"], str)


def test_log_output_is_redacted(tmp_path):
    services = appliance(tmp_path)
    log = services.status.read_log("admin_container", 50)
    assert "supersecret" not in log["text"]


def test_log_output_is_bounded_by_the_requested_line_count(tmp_path):
    services = appliance(tmp_path)
    services.paths.operations_log.parent.mkdir(parents=True, exist_ok=True)
    services.paths.operations_log.write_text(
        "\n".join(f"line {index}" for index in range(2000)), encoding="utf-8"
    )
    log = services.status.read_log("operations", 25)
    assert log["lines"] <= 25


# --- support archive -------------------------------------------------------


def test_support_archive_lists_its_members_before_it_is_created(tmp_path):
    services = appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    plan = handlers.dispatch({"operation": "support.plan_archive"})["plan"]
    assert "status.json" in plan["members"]
    assert set(plan["excluded"]) == set(EXCLUDED_BY_DEFAULT)


def test_support_archive_contains_a_manifest_and_no_secrets(tmp_path):
    services = appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "support.plan_archive"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])
    archive_path = operation.result["path"]

    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
        manifest = json.loads(archive.extractfile("manifest.json").read().decode("utf-8"))
        contents = b"".join(
            archive.extractfile(name).read() for name in names if name != "manifest.json"
        ).decode("utf-8", errors="replace")

    assert "manifest.json" in names
    assert "status.json" in names
    assert manifest["files"]
    assert "supersecret" not in contents
    assert "PRIVATE KEY" not in contents


def test_a_full_persistent_partition_is_a_warning(tmp_path):
    """On an A/B appliance / is a read-only slot partition whose usage cannot
    move; everything that grows is on the persistent one, which was measured and
    then never evaluated."""

    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    sections = {
        "system": {
            "status": "ok",
            "storage": {
                "root": {"available": True, "used_percent": 12},
                "ems_data": {"available": True, "used_percent": 97},
            },
        }
    }

    health = services.status._health(sections)
    codes = {warning["code"] for warning in health["warnings"]}

    assert "persistent_storage_low" in codes
    assert "storage_low" not in codes


def test_a_full_slot_root_is_still_a_warning(tmp_path):
    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    sections = {
        "system": {
            "status": "ok",
            "storage": {
                "root": {"available": True, "used_percent": 95},
                "ems_data": {"available": True, "used_percent": 10},
            },
        }
    }

    codes = {w["code"] for w in services.status._health(sections)["warnings"]}

    assert "storage_low" in codes


def test_an_under_voltage_board_is_reported(tmp_path):
    """A Pi that browns out under load corrupts a slot write and fails in ways
    that look like anything but a power supply."""

    from appliance.hostprobe import HostProbe

    alarm = tmp_path / "sys/class/hwmon/hwmon0"
    alarm.mkdir(parents=True)
    (alarm / "in0_lcrit_alarm").write_text("1\n")

    assert HostProbe(root=tmp_path).power() == {"available": True, "under_voltage": True}


def test_a_healthy_supply_is_reported_as_such(tmp_path):
    from appliance.hostprobe import HostProbe

    alarm = tmp_path / "sys/class/hwmon/hwmon0"
    alarm.mkdir(parents=True)
    (alarm / "in0_lcrit_alarm").write_text("0\n")

    assert HostProbe(root=tmp_path).power()["under_voltage"] is False


def test_a_board_that_publishes_no_alarm_is_unknown_not_healthy(tmp_path):
    from appliance.hostprobe import HostProbe

    assert HostProbe(root=tmp_path).power() == {"available": False, "under_voltage": None}
