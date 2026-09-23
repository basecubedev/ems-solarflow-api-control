# SPDX-License-Identifier: AGPL-3.0-or-later
"""Status collection, health normalisation, log bounding and the support archive.

Status collection is read-only and fault-isolated: one probe that fails must
degrade its own section, never the whole overview.
"""

import json
import os
import tarfile

import pytest

from appliance.agent import AgentHandlers
from appliance.hostprobe import HostProbe
from appliance.status import (
    FINDING_WARNING,
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


def test_the_docker_section_names_the_container_the_ems_runs_in(tmp_path):
    """The console must not guess the EMS by its name; the backend says which."""

    from tests.helpers.appliance import appliance_config, build_test_services

    host_files(tmp_path)
    services = build_test_services(tmp_path, config=appliance_config(ems_container="ems"))

    section = services.status.overview()["docker"]

    assert section["ems_container"] == "ems"
    assert "ems" in [item["name"] for item in section["containers"]]


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


def test_a_log_the_reader_could_not_open_is_not_an_empty_log(tmp_path):
    """An empty log and a log nobody could read are two statements.

    journalctl missing raised before the process ever started, the helper
    caught everything and substituted "", and the console reported 0 lines
    and (empty) -- the same words it uses for a unit that logged nothing.
    """

    services = appliance(tmp_path)
    services.host.tools.discard("journalctl")

    log = services.status.read_log("appliance_agent", 20)

    assert log["text"] == ""
    assert log["unreadable"] == "CommandError"


def test_a_log_file_that_cannot_be_opened_is_not_an_empty_log(tmp_path):
    services = appliance(tmp_path)
    services.paths.operations_log.mkdir(parents=True, exist_ok=True)

    log = services.status.read_log("operations", 20)

    assert log["text"] == ""
    assert log["unreadable"] == "IsADirectoryError"


def test_a_log_that_is_simply_empty_still_says_empty(tmp_path):
    """The other side: the fix must not answer unreadable for every quiet log."""

    services = appliance(tmp_path)
    services.paths.operations_log.parent.mkdir(parents=True, exist_ok=True)
    services.paths.operations_log.write_text("", encoding="utf-8")

    log = services.status.read_log("operations", 20)

    assert log["text"] == ""
    assert log["unreadable"] == ""


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


def test_the_support_archive_carries_the_manager_state_it_is_asked_about(tmp_path):
    """A case about a failed manager update used to arrive without it.

    The archive carried the A/B slot state and nothing about the package this
    console runs from -- so the one question worth asking, "which package is
    kept and what did the deadline decide", was the one it could not answer.
    """

    from appliance import manager_retention, manager_verify

    services = appliance(tmp_path)
    services.paths.packages_dir.mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "seed.deb"
    staged.write_bytes(b"the package that was running")
    manager_retention.retain(
        services.paths, staged, sha256="sha256:" + "c" * 64, version="0.1.0", rotate=False
    )
    manager_verify.verdict_path(services.paths).write_text(
        json.dumps({"verdict": manager_verify.VERDICT_REVERTED, "detail": "the deadline expired"}),
        encoding="utf-8",
    )

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

    with tarfile.open(operation.result["path"], "r:gz") as archive:
        assert "manager.json" in archive.getnames()
        payload = json.loads(archive.extractfile("manager.json").read().decode("utf-8"))

    assert payload["retention"]["current"]["version"] == "0.1.0"
    assert payload["verdict"]["verdict"] == manager_verify.VERDICT_REVERTED
    assert "manager.json" in planned["plan"]["members"], "listed before it is created"


def test_the_support_archive_carries_no_password_material(tmp_path):
    """The rescue account's state is a verdict, never the hash behind it."""

    from appliance import rescue_account

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

    with tarfile.open(operation.result["path"], "r:gz") as archive:
        contents = b"".join(
            archive.extractfile(name).read() for name in archive.getnames()
        ).decode("utf-8", errors="replace")

    assert "$6$" not in contents
    assert rescue_account.DEFAULT_PASSWORD not in contents


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


def test_the_alarm_is_found_wherever_the_hwmon_device_landed(tmp_path):
    """The index the board's own sensor gets depends on driver registration.

    A live Pi 3B+ logged six kernel under-voltage events during a failing
    install while the appliance reported the supply as unknown, because that
    board publishes the alarm as hwmon1.
    """

    from appliance.hostprobe import HostProbe

    thermal = tmp_path / "sys/class/hwmon/hwmon0"
    thermal.mkdir(parents=True)
    (thermal / "name").write_text("cpu_thermal\n")

    volt = tmp_path / "sys/class/hwmon/hwmon1"
    volt.mkdir(parents=True)
    (volt / "name").write_text("rpi_volt\n")
    (volt / "in0_lcrit_alarm").write_text("1\n")

    assert HostProbe(root=tmp_path).power() == {"available": True, "under_voltage": True}


def test_another_sensors_alarm_never_answers_for_the_board(tmp_path):
    """Reporting a foreign rail's alarm as the supply would be a false alarm."""

    from appliance.hostprobe import HostProbe

    other = tmp_path / "sys/class/hwmon/hwmon0"
    other.mkdir(parents=True)
    (other / "name").write_text("some_regulator\n")
    (other / "in0_lcrit_alarm").write_text("1\n")

    volt = tmp_path / "sys/class/hwmon/hwmon1"
    volt.mkdir(parents=True)
    (volt / "name").write_text("rpi_volt\n")
    (volt / "in0_lcrit_alarm").write_text("0\n")

    assert HostProbe(root=tmp_path).power()["under_voltage"] is False


# --- the appliance's own units ---------------------------------------------


APPLIANCE_UNIT_SOURCES = {
    "manager_install": "ems-appliance-manager-install.service",
    "manager_verify": "ems-appliance-manager-verify.service",
    "export": "ems-appliance-export.service",
    "config_seed": "ems-appliance-config-seed.service",
    "grow_root": "ems-appliance-grow-root.service",
    "sshd_keys": "ems-appliance-sshd-keys.service",
    "backup_access": "ems-appliance-backup-access-disable.service",
}


@pytest.mark.parametrize("source,unit", sorted(APPLIANCE_UNIT_SOURCES.items()))
def test_each_appliance_unit_has_a_readable_journal(tmp_path, source, unit):
    """dpkg runs from ems-appliance-manager-install.service during a self-update.

    A unit the package ships and nobody can read is a failure with no account
    of itself on a host with no shell.
    """

    services = appliance(tmp_path)

    log = services.status.read_log(source, 20)

    assert log["source"] == source
    journalled = [
        args
        for tool, args, _ in services.host.calls
        if tool == "journalctl" and "-u" in args and args[args.index("-u") + 1] == unit
    ]
    assert journalled, f"{source} never asked the journal for {unit}"


def test_no_log_source_falls_through_to_the_package_log(tmp_path):
    """read_log ends on an else, so an unrouted source silently serves dpkg."""

    from appliance import validation

    services = appliance(tmp_path)
    dpkg = services.status.probe.root / "var/log/dpkg.log"
    dpkg.parent.mkdir(parents=True, exist_ok=True)
    dpkg.write_text("this-is-the-package-log\n", encoding="utf-8")

    for source in validation.LOG_SOURCES:
        if source == validation.LOG_SOURCE_PACKAGES:
            continue
        assert "this-is-the-package-log" not in services.status.read_log(source, 20)["text"], source


def test_a_readable_unit_is_not_thereby_a_controllable_one():
    """Reading a journal and starting a unit are different authorities.

    `ssh.socket` is on the controllable list beside `ssh.service` because on a
    host that has one it *is* SSH: it holds port 22 and starts sshd per
    connection, so turning SSH off without it leaves the port open behind a
    console that says it is closed.
    """

    from appliance.systemd import (
        CONTROLLABLE_UNITS,
        READABLE_UNITS,
        UNIT_DOCKER,
        UNIT_SSH,
        UNIT_SSH_SOCKET,
    )

    assert CONTROLLABLE_UNITS == (UNIT_DOCKER, UNIT_SSH, UNIT_SSH_SOCKET)
    for unit in APPLIANCE_UNIT_SOURCES.values():
        assert unit in READABLE_UNITS
        assert unit not in CONTROLLABLE_UNITS


def test_the_archive_says_a_log_could_not_be_read(tmp_path):
    """The bundle is read by somebody who cannot ask the host; an empty file
    there says the unit logged nothing, which is not what happened."""

    services = appliance(tmp_path)
    services.host.tools.discard("journalctl")
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

    with tarfile.open(operation.result["path"], "r:gz") as archive:
        text = archive.extractfile("logs/appliance_agent.log").read().decode("utf-8")

    assert text.startswith("unavailable: CommandError"), text


def test_the_support_archive_carries_every_declared_log_source(tmp_path):
    """The bundle is what an operator sends when they cannot read the host.

    A second hand-kept list of sources beside LOG_SOURCES is a bundle that
    silently stops carrying whatever was added to the appliance last.
    """

    from appliance import validation

    services = appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    plan = handlers.dispatch({"operation": "support.plan_archive"})["plan"]

    for source in validation.LOG_SOURCES:
        assert f"logs/{source}.log" in plan["members"], source


# --- a count is only as old as the index behind it --------------------------


def age_the_package_index(tmp_path, services, seconds):
    """Age the index against the service's own clock, not the wall clock.

    The harness injects a fixed time, so an mtime derived from time.time() is
    off by whatever the two differ by. Reading the reported age of an epoch-zero
    mtime gives the service's "now" without reaching into it.
    """

    lists = host_files(tmp_path) / "var" / "lib" / "apt" / "lists"
    lists.mkdir(parents=True, exist_ok=True)
    os.utime(lists, (0, 0))
    now = services.status.overview()["updates"]["index_age_seconds"]
    stamp = now - seconds
    os.utime(lists, (stamp, stamp))
    return lists


def test_an_index_nobody_refreshed_is_not_an_empty_list(tmp_path):
    """The check never runs apt-get update, on purpose: a status poll must not
    change the machine it reports on. The cost is that "0 security updates" is
    only as old as the last refresh, and a live Pi 3B+ was found reporting
    exactly that against an index untouched for twenty-three days. Nothing read
    index_age_seconds, so nothing said so."""

    services = appliance(tmp_path)
    age_the_package_index(tmp_path, services, 23 * 24 * 60 * 60)

    warnings = services.status.overview()["health"]["warnings"]
    stale = [item for item in warnings if item["code"] == "package_index_stale"]

    assert stale, [item["code"] for item in warnings]
    assert "23 days" in stale[0]["message"]
    assert stale[0]["severity"] == FINDING_WARNING


def test_a_fresh_index_says_nothing(tmp_path):
    services = appliance(tmp_path)
    age_the_package_index(tmp_path, services, 3600)

    codes = [item["code"] for item in services.status.overview()["health"]["warnings"]]

    assert "package_index_stale" not in codes
def test_a_stopped_ems_container_is_not_a_healthy_appliance(tmp_path):
    """The whole point of the box stands still and it calls itself healthy.

    `_health` reads the Docker daemon, the Admin container, update counts, the
    reboot flag, package-manager health and two fill levels. It never looks at
    `docker.containers` -- the list that carries the EMS itself. So with Docker
    up and Admin healthy, an exited `ems-solarflow-api-control` leaves the
    headline at "This appliance is healthy." and the findings panel at "Nothing
    needs your attention", with only a small "exited" on one tile.
    """

    services = appliance(tmp_path)
    services.host.run_container(
        services.config.ems_container, f"{ADMIN_REPOSITORY}:v1.0.0", state="exited"
    )

    health = services.status.overview()["health"]

    assert health["level"] != HEALTH_HEALTHY, health
    assert "ems_not_running" in [item["code"] for item in health["warnings"]], health["warnings"]


def test_a_running_ems_container_is_not_a_finding(tmp_path):
    services = appliance(tmp_path)
    services.host.run_container(services.config.ems_container, f"{ADMIN_REPOSITORY}:v1.0.0")

    health = services.status.overview()["health"]

    assert "ems_not_running" not in [item["code"] for item in health["warnings"]]


def test_an_appliance_with_no_ems_installed_is_not_accused_of_a_stopped_one(tmp_path):
    """No EMS yet is the state a fresh appliance is in, and Admin already says so."""

    services = appliance(tmp_path)

    codes = [item["code"] for item in services.status.overview()["health"]["warnings"]]

    assert "ems_not_running" not in codes
