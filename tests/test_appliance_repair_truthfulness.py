# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repair must report what actually happened.

An operator acts on the result of a repair. A repair that reports ``succeeded``
while Docker is still stopped, while the compose file is still missing or while
no automatic action was performed at all is worse than no repair at all, so
every outcome here is pinned to the state the host is really in afterwards.
"""

import pytest

from appliance.agent import AgentHandlers
from appliance.operations import (
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    STATE_MANUAL_ACTION_REQUIRED,
    STATE_SUCCEEDED,
)
from tests.helpers.appliance import ADMIN_CONTAINER, ADMIN_REPOSITORY, build_test_services

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]


def appliance(tmp_path, *, running=True):
    services = build_test_services(tmp_path)
    host = services.host
    host.write_deployment(tag="v1.0.0")
    host.publish_image("v1.0.0")
    host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    if running:
        host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    return services


def repair(services):
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_repair"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


# --- docker start ----------------------------------------------------------


def test_docker_start_repair_actually_starts_the_service(tmp_path):
    services = appliance(tmp_path)
    services.host.docker_running = False
    services.host.units["docker.service"] = {"active": "inactive", "enabled": "enabled"}
    services.host.start_docker_succeeds = True

    operation, plan = repair(services)

    assert "start_docker" in plan["actions"]
    assert ("systemctl", ("start", "docker.service"), None) in services.host.calls
    assert services.host.docker_running is True
    assert operation.state == STATE_SUCCEEDED


def test_docker_start_repair_that_does_not_start_docker_is_not_a_success(tmp_path):
    services = appliance(tmp_path)
    services.host.docker_running = False
    services.host.start_docker_succeeds = False

    operation, _ = repair(services)

    assert operation.state != STATE_SUCCEEDED
    assert operation.state in (STATE_FAILED_RECOVERABLE, STATE_FAILED_TERMINAL)
    # The reported code names the action that failed, not a generic outcome.
    assert operation.error["code"] in ("repair_incomplete", "docker_unavailable", "start_failed")
    assert "start_docker" in operation.error["message"]


def test_docker_start_repair_verifies_the_api_not_only_the_unit(tmp_path):
    services = appliance(tmp_path)
    services.host.docker_running = False
    # The unit reports active but the API stays unreachable.
    services.host.start_docker_succeeds = True
    services.host.docker_api_broken = True

    operation, _ = repair(services)

    assert operation.state == STATE_FAILED_RECOVERABLE
    applied = {item["action"]: item["result"] for item in operation.result["applied"]}
    assert applied["start_docker"] != "verified"


# --- container lifecycle ---------------------------------------------------


def test_starting_a_stopped_admin_is_verified_before_reporting_success(tmp_path):
    services = appliance(tmp_path)
    services.host.containers[ADMIN_CONTAINER]["State"].update(
        {"Running": False, "Status": "exited"}
    )

    operation, plan = repair(services)

    assert "start_admin" in plan["actions"]
    assert operation.state == STATE_SUCCEEDED
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True
    applied = {item["action"]: item["result"] for item in operation.result["applied"]}
    assert applied["start_admin"] == "verified"


def test_a_container_that_will_not_stay_running_is_not_a_success(tmp_path):
    services = appliance(tmp_path)
    services.host.containers[ADMIN_CONTAINER]["State"].update(
        {"Running": False, "Status": "exited"}
    )
    services.host.container_start_sticks = False

    operation, _ = repair(services)

    assert operation.state == STATE_FAILED_RECOVERABLE


def test_an_unhealthy_admin_restart_is_reported_as_remaining_problem(tmp_path):
    services = appliance(tmp_path)
    services.host.containers[ADMIN_CONTAINER]["State"]["Health"]["Status"] = "unhealthy"

    operation, plan = repair(services)

    assert "restart_admin" in plan["actions"]
    assert operation.state == STATE_FAILED_RECOVERABLE
    assert operation.error["code"] in ("repair_incomplete", "container_unhealthy")
    assert "restart_admin" in operation.error["message"]


# --- manual actions --------------------------------------------------------


def test_a_missing_compose_file_is_manual_action_required_not_success(tmp_path):
    services = appliance(tmp_path)
    (services.paths.install_root / "docker-compose.admin.yml").unlink()

    operation, plan = repair(services)

    assert operation.state == STATE_MANUAL_ACTION_REQUIRED
    assert operation.state != STATE_SUCCEEDED
    assert operation.result["manual_actions"]
    assert any("compose" in item for item in operation.result["manual_actions"])


def test_a_missing_environment_file_is_manual_action_required(tmp_path):
    services = appliance(tmp_path)
    (services.paths.install_root / ".env.admin").unlink()

    operation, _ = repair(services)

    assert operation.state == STATE_MANUAL_ACTION_REQUIRED
    assert any("environment" in item for item in operation.result["manual_actions"])


def test_manual_actions_are_not_offered_as_executable_repair_actions(tmp_path):
    services = appliance(tmp_path)
    (services.paths.install_root / "docker-compose.admin.yml").unlink()

    handlers = AgentHandlers(services, executor=lambda target: target())
    plan = handlers.dispatch({"operation": "admin.plan_repair"})["plan"]

    assert "regenerate_admin_compose" not in plan["actions"]
    assert plan["manual_actions"]


# --- post-repair verification ----------------------------------------------


def test_a_healthy_appliance_repairs_to_succeeded(tmp_path):
    services = appliance(tmp_path)
    operation, plan = repair(services)
    assert plan["healthy"] is True
    assert operation.state == STATE_SUCCEEDED
    assert operation.result["remaining_findings"] == []


def test_a_repair_that_fixes_one_of_two_problems_is_recoverable(tmp_path):
    services = appliance(tmp_path)
    import shutil

    services.host.containers[ADMIN_CONTAINER]["State"].update(
        {"Running": False, "Status": "exited"}
    )
    shutil.rmtree(services.paths.ems_backups_dir)
    (services.paths.install_root / ".env.admin").unlink()

    operation, _ = repair(services)

    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True
    assert services.paths.ems_backups_dir.is_dir()
    assert operation.state in (STATE_FAILED_RECOVERABLE, STATE_MANUAL_ACTION_REQUIRED)
    assert operation.state != STATE_SUCCEEDED


def test_post_repair_findings_are_reported_with_the_result(tmp_path):
    services = appliance(tmp_path)
    services.host.docker_running = False
    services.host.start_docker_succeeds = False

    operation, _ = repair(services)

    remaining = operation.result["remaining_findings"]
    assert remaining
    assert any(item["check"] == "docker_daemon" for item in remaining)


def test_repair_probes_the_admin_http_endpoint_not_only_the_container(tmp_path):
    """A running container with no Docker health check proves nothing."""

    services = appliance(tmp_path)
    services.host.publish_image("v1.0.0", healthy=False)
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", health="none")

    handlers = AgentHandlers(services, executor=lambda target: target())
    plan = handlers.dispatch({"operation": "admin.plan_repair"})["plan"]

    api = [item for item in plan["findings"] if item["check"] == "admin_api"][0]
    assert api["ok"] is False, plan["findings"]
    assert plan["healthy"] is False
    assert "restart_admin" in plan["actions"]


def test_a_repair_that_leaves_the_admin_unreachable_never_reports_success(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.0.0", healthy=False)
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", health="none")

    operation, _ = repair(services)

    assert operation.state != STATE_SUCCEEDED
    assert operation.state == STATE_FAILED_RECOVERABLE, operation.result
    applied = {item["action"]: item for item in operation.result["applied"]}
    assert applied["restart_admin"]["result"] == "api_unreachable", applied
    assert applied["restart_admin"]["verification"]["api_reachable"] is False
    assert any(item["check"] == "admin_api" for item in operation.result["remaining_findings"])


def test_a_repair_that_starts_the_wrong_image_reports_the_mismatch(tmp_path):
    services = appliance(tmp_path, running=False)
    entry = services.host.publish_image("v1.0.0")
    services.known_good.record(
        admin_image=f"{ADMIN_REPOSITORY}:v1.0.0",
        admin_digest=entry["_digest"],
        admin_version="v1.0.0",
    )
    services.host.publish_image("v1.1.0")
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.1.0")
    services.host.run_container(
        ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.1.0", state="exited", health="none"
    )

    operation, _ = repair(services)

    applied = {item["action"]: item for item in operation.result["applied"]}
    assert applied["start_admin"]["result"] == "image_mismatch", applied
    assert applied["start_admin"]["verification"]["digest_matches"] is False
    assert operation.state != STATE_SUCCEEDED


def test_the_terminal_state_matches_the_final_findings(tmp_path):
    """succeeded means every finding is clear; nothing weaker may claim it."""

    services = appliance(tmp_path)
    operation, _ = repair(services)
    assert operation.state == STATE_SUCCEEDED
    assert operation.result["remaining_findings"] == []

    broken = appliance(tmp_path / "broken")
    broken.host.publish_image("v1.0.0", healthy=False)
    broken.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    broken.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", health="none")

    operation, _ = repair(broken)
    assert operation.result["remaining_findings"]
    assert operation.state != STATE_SUCCEEDED


def test_a_port_conflict_cannot_be_repaired_automatically(tmp_path):
    services = appliance(tmp_path)
    services.host.listening_ports = (
        'LISTEN 0 4096 0.0.0.0:8090 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
    )

    operation, _ = repair(services)

    assert operation.state == STATE_FAILED_RECOVERABLE
    assert any(item["check"] == "admin_port" for item in operation.result["remaining_findings"])
