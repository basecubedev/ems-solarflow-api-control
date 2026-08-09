# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactional EMS Admin installation, rollback and repair.

The appliance runs outside Docker, so it must be able to replace the Admin
container and put the previous known-good version back when the new one does
not become healthy. These tests drive the real service against a scripted
Docker engine — no container is started.
"""

import pytest

from appliance.admin_lifecycle import TYPE_INSTALL, TYPE_ROLLBACK
from appliance.agent import AgentHandlers
from appliance.operations import (
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    STATE_MANUAL_ACTION_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_SUCCEEDED,
)
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    ADMIN_SERVICE,
    IMAGE_SOURCE,
    StaticCatalogue,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation]


def healthy_appliance(tmp_path, *, tag="v1.0.0", variable_tag=True, catalogue=None):
    services = build_test_services(
        tmp_path, catalogue=catalogue or StaticCatalogue(["v1.1.0", "v1.0.0"])
    )
    host = services.host
    host.write_deployment(tag=tag, variable_tag=variable_tag)
    host.publish_image(tag)
    host.pull_local(f"{ADMIN_REPOSITORY}:{tag}")
    host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:{tag}")
    return services


def plan_and_execute(services, **fields):
    """Plan and confirm an install exactly the way the web layer does.

    Planning failures surface as an agent error; execution failures are
    recorded on the operation record instead of raised, because the agent runs
    execution detached from the request.
    """

    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_install", **fields})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


def refused(services, **fields):
    """Return the error code the agent reports when a plan is refused."""

    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_install", **fields})
    return getattr(excinfo.value, "code", "")


# --- detection -------------------------------------------------------------


def test_healthy_admin_is_detected_with_version_image_and_health(tmp_path):
    services = healthy_appliance(tmp_path)
    detected = services.admin.detect()

    assert detected["installed"] is True
    assert detected["healthy"] is True
    assert detected["version"] == "v1.0.0"
    assert detected["container"]["state"] == "running"
    assert detected["container"]["health"] == "healthy"
    assert detected["digest"].startswith("sha256:")
    assert detected["deployment"]["service_defined"] is True


def test_stopped_admin_is_detected_as_installed_but_unhealthy(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.containers[ADMIN_CONTAINER]["State"].update(
        {"Running": False, "Status": "exited"}
    )
    detected = services.admin.detect()
    assert detected["installed"] is True
    assert detected["healthy"] is False
    assert detected["container"]["state"] == "exited"


def test_missing_admin_container_is_detected(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.containers.pop(ADMIN_CONTAINER)
    detected = services.admin.detect()
    assert detected["installed"] is False
    assert detected["container"]["exists"] is False


def test_missing_admin_image_degrades_without_raising(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.images.clear()
    detected = services.admin.detect()
    assert detected["installed"] is True
    assert detected["image"]["exists"] is False
    assert detected["version"] == ""


def test_docker_down_is_reported_not_crashed(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.docker_running = False
    detected = services.admin.detect()
    assert detected["docker"]["state"] == "stopped"
    assert detected["installed"] is False


# --- target validation -----------------------------------------------------


def test_install_plan_pulls_validates_and_waits_for_confirmation(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    handlers = AgentHandlers(services, executor=lambda target: target())

    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    plan = planned["plan"]

    assert plan["type"] == TYPE_INSTALL
    assert plan["target_tag"] == "v1.1.0"
    assert plan["target_digest"].startswith("sha256:")
    assert plan["target_architecture"] == "arm64"
    assert planned["operation"]["state"] == "awaiting_confirmation"
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True


def test_install_plan_refuses_an_image_with_a_foreign_source_label(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", source="https://github.com/attacker/evil")
    assert refused(services, channel="exact", tag="v1.1.0") == "image_source_mismatch"


def test_install_plan_refuses_a_version_label_conflict(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", version="v2.0.0")
    assert refused(services, channel="exact", tag="v1.1.0") == "image_version_mismatch"


def test_install_plan_refuses_a_foreign_architecture(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", architecture="amd64")
    assert refused(services, channel="exact", tag="v1.1.0") == "architecture_mismatch"


def test_install_plan_refuses_an_image_without_oci_labels(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", labels={})
    assert refused(services, channel="exact", tag="v1.1.0") == "image_labels_missing"


def test_failed_target_image_pull_is_reported(tmp_path):
    services = healthy_appliance(tmp_path)
    assert refused(services, channel="exact", tag="v9.9.9") == "image_pull_failed"
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True


def test_identical_target_is_refused_unless_reinstall_was_requested(tmp_path):
    services = healthy_appliance(tmp_path)
    assert refused(services, channel="exact", tag="v1.0.0") == "target_identical"


def test_docker_down_blocks_an_install_plan(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.docker_running = False
    assert refused(services, channel="exact", tag="v1.1.0") == "docker_unavailable"


def test_missing_compose_file_blocks_an_install_plan(tmp_path):
    services = healthy_appliance(tmp_path)
    (services.paths.install_root / "docker-compose.admin.yml").unlink()
    assert refused(services, channel="exact", tag="v1.1.0") == "compose_file_missing"


# --- successful install ----------------------------------------------------


def test_successful_install_replaces_the_container_and_records_known_good(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")

    operation, plan = plan_and_execute(services, channel="exact", tag="v1.1.0")

    assert operation.state == STATE_SUCCEEDED
    assert operation.result["installed_version"] == "v1.1.0"
    assert services.admin.detect()["version"] == "v1.1.0"

    current = services.known_good.current()
    previous = services.known_good.previous()
    assert current["admin_version"] == "v1.1.0"
    assert current["admin_digest"] == plan["target_digest"]
    assert current["healthcheck"] == "passed"
    assert previous["admin_version"] == "v1.0.0"


def test_install_progresses_through_the_documented_stages(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    operation, _ = plan_and_execute(services, channel="exact", tag="v1.1.0")
    stages = [entry["stage"] for entry in operation.progress]
    for expected in (
        "preflight",
        "pulling_image",
        "inspecting_image",
        "recording_known_good",
        "stopping_admin",
        "recreating_admin",
        "waiting_for_health",
    ):
        assert expected in stages, stages


def test_reinstall_of_the_current_version_is_allowed(tmp_path):
    services = healthy_appliance(tmp_path)
    operation, _ = plan_and_execute(services, channel="exact", tag="v1.0.0", reinstall=True)
    assert operation.state == STATE_SUCCEEDED
    assert services.admin.detect()["version"] == "v1.0.0"


def test_install_uses_the_latest_stable_channel_without_a_tag(tmp_path):
    services = healthy_appliance(tmp_path, catalogue=StaticCatalogue(["v1.1.0", "v1.0.0"]))
    services.host.publish_image("v1.1.0")
    operation, plan = plan_and_execute(services, channel="latest_stable")
    assert plan["target_tag"] == "v1.1.0"
    assert operation.state == STATE_SUCCEEDED


def test_install_writes_the_tag_into_the_environment_file_when_compose_uses_a_variable(tmp_path):
    services = healthy_appliance(tmp_path, variable_tag=True)
    services.host.publish_image("v1.1.0")
    plan_and_execute(services, channel="exact", tag="v1.1.0")
    env = (services.paths.install_root / ".env.admin").read_text(encoding="utf-8")
    assert "EMS_ADMIN_TAG=v1.1.0" in env


def test_install_pins_the_compose_image_to_the_resolved_digest(tmp_path):
    services = healthy_appliance(tmp_path, variable_tag=False)
    services.host.publish_image("v1.1.0")
    _, plan = plan_and_execute(services, channel="exact", tag="v1.1.0")
    compose = (services.paths.install_root / "docker-compose.admin.yml").read_text(encoding="utf-8")
    assert plan["target_reference"] in compose
    assert f"{ADMIN_REPOSITORY}:v1.1.0" not in compose


def test_install_never_touches_ems_config_data_or_backups(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    config_file = services.paths.ems_config_dir / "config.json"
    config_file.write_text('{"devices": []}', encoding="utf-8")
    backup = services.paths.ems_backups_dir / "backup.tar.gz"
    backup.write_bytes(b"backup-bytes")

    plan_and_execute(services, channel="exact", tag="v1.1.0")

    assert config_file.read_text(encoding="utf-8") == '{"devices": []}'
    assert backup.read_bytes() == b"backup-bytes"
    assert services.paths.ems_data_dir.is_dir()


def test_install_does_not_remove_unrelated_containers(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    services.host.run_container("some-other-app", f"{ADMIN_REPOSITORY}:v1.0.0")
    plan_and_execute(services, channel="exact", tag="v1.1.0")
    assert "some-other-app" in services.host.containers


# --- automatic rollback ----------------------------------------------------


def test_unhealthy_target_rolls_back_to_the_previous_known_good(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)

    operation, _ = plan_and_execute(services, channel="exact", tag="v1.1.0")

    assert operation.state == STATE_ROLLED_BACK
    assert operation.error["code"] == "admin_unhealthy"
    assert operation.result["restored_version"] == "v1.0.0"
    assert services.admin.detect()["version"] == "v1.0.0"
    assert services.admin.detect()["healthy"] is True


def test_rollback_restores_the_previous_deployment_and_repins_its_digest(tmp_path):
    """The saved bytes come back first, then the known-good digest is re-pinned."""

    services = healthy_appliance(tmp_path)
    restored_digest = services.admin.detect()["digest"]
    services.host.publish_image("v1.1.0", healthy=False)

    plan_and_execute(services, channel="exact", tag="v1.1.0")

    compose = (services.paths.install_root / "docker-compose.admin.yml").read_text(encoding="utf-8")
    env = (services.paths.install_root / ".env.admin").read_text(encoding="utf-8")
    assert restored_digest in compose
    assert "EMS_ADMIN_TAG=v1.0.0" in env
    assert services.admin.detect()["digest"] == restored_digest


def test_compose_up_failure_also_rolls_back(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    services.host.compose_up_fails = True

    operation, _ = plan_and_execute(services, channel="exact", tag="v1.1.0")
    assert operation.state in (STATE_ROLLED_BACK, STATE_FAILED_TERMINAL)


def test_rollback_failure_is_reported_as_terminal(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)
    previous_digest = services.admin.detect()["digest"]

    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    # The immutable reference of the previously verified image disappears from
    # the local store and from the registry while the replacement runs, so
    # rollback cannot re-pin the recovery digest. That must end as a terminal
    # failure, never as a silent success.
    services.host.images.pop(f"{ADMIN_REPOSITORY}@{previous_digest}", None)
    services.host.registry.pop(f"{ADMIN_REPOSITORY}@{previous_digest}", None)

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] in ("rollback_failed", "admin_unhealthy")


# --- explicit rollback -----------------------------------------------------


def test_explicit_rollback_restores_the_previous_known_good_digest(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    plan_and_execute(services, channel="exact", tag="v1.1.0")
    services.operations.acknowledge(services.operations.list()[0].operation_id)

    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    assert planned["plan"]["type"] == TYPE_ROLLBACK
    assert planned["plan"]["target"]["admin_version"] == "v1.0.0"

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_SUCCEEDED
    assert services.admin.detect()["version"] == "v1.0.0"


def test_rollback_without_a_previous_version_is_refused(tmp_path):
    services = healthy_appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_rollback"})
    assert getattr(excinfo.value, "code", "") == "no_previous_known_good"


def test_known_good_history_keeps_current_and_previous(tmp_path):
    services = healthy_appliance(tmp_path)
    for tag in ("v1.1.0", "v1.2.0"):
        services.host.publish_image(tag)
        plan_and_execute(services, channel="exact", tag=tag)
        services.operations.acknowledge(services.operations.list()[0].operation_id)

    entries = services.known_good.entries()
    assert entries[0]["admin_version"] == "v1.2.0"
    assert entries[1]["admin_version"] == "v1.1.0"
    assert len(entries) >= 2
    assert all(entry["admin_digest"].startswith("sha256:") for entry in entries)


# --- lifecycle actions -----------------------------------------------------


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_actions_execute(tmp_path, action):
    services = healthy_appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_lifecycle", "action": action})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_SUCCEEDED
    assert operation.result["action"] == action


# --- lifecycle verification -------------------------------------------------


def lifecycle(services, action):
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_lifecycle", "action": action})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"])


def record_known_good(services, tag="v1.0.0"):
    entry = services.host.registry[f"{ADMIN_REPOSITORY}:{tag}"]
    return services.known_good.record(
        admin_image=f"{ADMIN_REPOSITORY}:{tag}",
        admin_digest=entry["_digest"],
        admin_version=tag,
    )


@pytest.mark.parametrize("action", ["start", "restart"])
def test_start_and_restart_verify_the_http_endpoint_and_the_version(tmp_path, action):
    services = healthy_appliance(tmp_path)
    record_known_good(services)

    operation = lifecycle(services, action)

    verification = operation.result["verification"]
    assert operation.state == STATE_SUCCEEDED
    assert verification["verified"] is True
    assert verification["api_reachable"] is True
    assert verification["version"] == "v1.0.0"
    assert verification["version_matches"] is True
    assert verification["digest_matches"] is True


@pytest.mark.parametrize("action", ["start", "restart"])
def test_a_running_but_unreachable_admin_is_not_a_successful_lifecycle_action(tmp_path, action):
    """docker start returning 0 says nothing about the Admin being usable."""

    services = healthy_appliance(tmp_path)
    record_known_good(services)
    services.host.publish_image("v1.0.0", healthy=False)
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", health="none")

    operation = lifecycle(services, action)

    assert operation.state == STATE_FAILED_RECOVERABLE, operation.result
    assert operation.error["code"] == "api_unreachable"
    assert "HTTP endpoint did not answer" in operation.error["message"]
    assert operation.result["verification"]["container_state"] == "running"


def test_a_container_without_a_docker_health_check_is_not_verified_by_running_alone(tmp_path):
    services = healthy_appliance(tmp_path)
    record_known_good(services)
    services.host.publish_image("v1.0.0", healthy=False)
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", health="none")

    verification = services.admin.verify_admin()

    assert verification["container_state"] == "running"
    assert verification["container_health"] == "none"
    assert verification["verified"] is False
    assert "api_unreachable" in verification["failures"]


def test_the_wrong_admin_image_is_rejected(tmp_path):
    services = healthy_appliance(tmp_path)
    record_known_good(services, "v1.0.0")
    services.host.publish_image("v1.1.0")
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.1.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.1.0")

    operation = lifecycle(services, "restart")

    assert operation.state == STATE_MANUAL_ACTION_REQUIRED, operation.result
    assert operation.error["code"] == "image_mismatch"
    verification = operation.result["verification"]
    assert verification["digest_matches"] is False
    assert verification["active_digest"] != verification["expected_digest"]


def test_a_version_that_disagrees_with_the_expected_target_is_rejected(tmp_path):
    services = healthy_appliance(tmp_path)
    entry = record_known_good(services, "v1.0.0")
    # Same digest, but the running Admin reports a different version.
    services.host.publish_image("v1.0.0", version="v0.4.2", digest=entry["admin_digest"])
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")

    operation = lifecycle(services, "restart")

    assert operation.state == STATE_FAILED_RECOVERABLE, operation.result
    assert operation.error["code"] == "version_mismatch"
    assert operation.result["verification"]["version"] == "v0.4.2"


def test_an_unreadable_version_is_reported_rather_than_assumed(tmp_path):
    from appliance.health import HealthResult
    from tests.helpers.appliance import FakeHealthChecker

    services = healthy_appliance(tmp_path)
    services.admin.health = FakeHealthChecker(
        services.host, forced=HealthResult(reachable=True, status_code=200, version="")
    )
    services.host.publish_image("v1.0.0", labels={})
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")

    verification = services.admin.verify_admin()

    assert verification["api_reachable"] is True
    assert verification["verified"] is False
    assert verification["failures"] == ["version_unreadable"]


def test_a_version_missing_from_the_api_falls_back_to_the_image_label(tmp_path):
    from appliance.health import HealthResult
    from tests.helpers.appliance import FakeHealthChecker

    services = healthy_appliance(tmp_path)
    services.admin.health = FakeHealthChecker(
        services.host, forced=HealthResult(reachable=True, status_code=200, version="")
    )

    verification = services.admin.verify_admin(expected_version="v1.0.0")

    assert verification["version"] == "v1.0.0"
    assert verification["version_source"] == "image_label"
    assert verification["verified"] is True


def test_stop_verifies_that_the_container_really_stopped(tmp_path):
    services = healthy_appliance(tmp_path)

    operation = lifecycle(services, "stop")

    assert operation.state == STATE_SUCCEEDED
    verification = operation.result["verification"]
    assert verification["expect_running"] is False
    assert verification["container_state"] != "running"
    assert verification["verified"] is True


def test_a_stop_that_leaves_the_container_running_is_not_a_success(tmp_path):
    services = healthy_appliance(tmp_path)
    # docker stop returns 0 but the container comes straight back up.
    services.host.stop_container_sticks = False

    operation = lifecycle(services, "stop")

    assert operation.state == STATE_FAILED_RECOVERABLE, operation.result
    assert operation.error["code"] == "container_still_running"


def test_a_missing_container_needs_manual_action_rather_than_a_retry(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.containers.pop(ADMIN_CONTAINER)

    operation = lifecycle(services, "start")

    assert operation.state == STATE_MANUAL_ACTION_REQUIRED, operation.result
    assert operation.error["code"] in ("container_missing", "admin_lifecycle_failed")


# --- repair ----------------------------------------------------------------


def test_repair_preview_reports_a_healthy_deployment(tmp_path):
    services = healthy_appliance(tmp_path)
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["docker_daemon"].ok is True
    assert findings["compose_file"].ok is True
    assert findings["admin_service"].ok is True
    assert findings["admin_container"].ok is True


def test_repair_preview_suggests_starting_docker(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.docker_running = False
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["docker_daemon"].ok is False
    assert findings["docker_daemon"].action == "start_docker"


def test_repair_preview_suggests_reinstall_when_the_container_is_missing(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.containers.pop(ADMIN_CONTAINER)
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["admin_container"].action == "recreate_admin"


def test_repair_preview_suggests_start_when_the_container_is_stopped(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.containers[ADMIN_CONTAINER]["State"].update({"Running": False, "Status": "exited"})
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["admin_container"].action == "start_admin"


def test_repair_preview_reports_a_missing_compose_file_as_manual(tmp_path):
    services = healthy_appliance(tmp_path)
    (services.paths.install_root / "docker-compose.admin.yml").unlink()
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["compose_file"].ok is False
    assert findings["compose_file"].manual is True
    assert findings["compose_file"].action == ""


def test_repair_preview_reports_a_missing_bind_path(tmp_path):
    services = healthy_appliance(tmp_path)
    import shutil

    shutil.rmtree(services.paths.ems_backups_dir)
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["bind_path_backups"].ok is False
    assert findings["bind_path_backups"].action == "create_bind_path:backups"


def test_repair_reports_a_port_conflict_without_killing_anything(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.listening_ports = (
        'LISTEN 0 4096 0.0.0.0:8090 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
    )
    findings = {item.check: item for item in services.admin.inspect_repair()}
    assert findings["admin_port"].ok is False
    assert "nginx" in findings["admin_port"].detail
    assert findings["admin_port"].action == ""


def test_repair_execution_recreates_a_missing_bind_path(tmp_path):
    services = healthy_appliance(tmp_path)
    import shutil

    shutil.rmtree(services.paths.ems_backups_dir)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_repair"})
    assert "create_bind_path:backups" in planned["plan"]["actions"]

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    assert services.paths.ems_backups_dir.is_dir()
    assert services.operations.get(planned["operation"]["operation_id"]).state == STATE_SUCCEEDED


def test_repair_execution_starts_a_stopped_container(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.containers[ADMIN_CONTAINER]["State"].update({"Running": False, "Status": "exited"})
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_repair"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True


# --- logs ------------------------------------------------------------------


def test_admin_logs_are_bounded_and_redacted(tmp_path):
    services = healthy_appliance(tmp_path)
    log = services.status.read_log("admin_container", 50)
    assert "supersecret" not in log["text"]
    assert log["source"] == "admin_container"
    assert log["lines"] <= 50


# --- deployment discovery --------------------------------------------------


def test_deployment_discovery_finds_the_admin_compose_file(tmp_path):
    services = healthy_appliance(tmp_path)
    deployment = services.admin.deployment()
    assert deployment.compose_file.name == "docker-compose.admin.yml"
    assert deployment.env_file.name == ".env.admin"
    assert deployment.service == ADMIN_SERVICE
    assert deployment.tag_source == "environment"


def test_deployment_discovery_detects_a_literal_image_tag(tmp_path):
    services = healthy_appliance(tmp_path, variable_tag=False)
    assert services.admin.deployment().tag_source == "compose"


def test_expected_source_label_comes_from_host_configuration(tmp_path):
    services = healthy_appliance(tmp_path)
    assert services.config.images.expected_source == IMAGE_SOURCE


# --- port inspection truthfulness -------------------------------------------


def test_a_free_port_is_reported_as_available(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.listening_ports = 'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'

    findings = {item.check: item for item in services.admin.inspect_repair()}

    assert findings["admin_port"].ok is True
    assert findings["admin_port"].indeterminate is False
    assert "available" in findings["admin_port"].detail


def test_a_failed_port_inspection_is_never_reported_as_available(tmp_path):
    """``ss`` under a sandbox that denies AF_NETLINK fails; it finds nothing."""

    services = healthy_appliance(tmp_path)
    services.host.ss_exit_code = 1
    services.host.ss_stderr = "Cannot open netlink socket: Address family not supported by protocol"

    findings = {item.check: item for item in services.admin.inspect_repair()}

    assert findings["admin_port"].indeterminate is True, findings["admin_port"]
    assert "available" not in findings["admin_port"].detail.lower()
    assert findings["admin_port"].action == ""


def test_a_missing_port_tool_is_reported_as_indeterminate(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.tools.discard("ss")

    findings = {item.check: item for item in services.admin.inspect_repair()}

    assert findings["admin_port"].indeterminate is True
    assert "available" not in findings["admin_port"].detail.lower()


# --- port ownership is proven, never inferred from a process name ------------

ADMIN_LISTENER = (
    'LISTEN 0 4096 0.0.0.0:8090 0.0.0.0:* users:(("docker-proxy",pid=901,fd=4))\n'
)
FOREIGN_LISTENER = (
    'LISTEN 0 4096 0.0.0.0:8090 0.0.0.0:* users:(("nginx",pid=812,fd=6))\n'
)


def port_finding(services):
    return {item.check: item for item in services.admin.inspect_repair()}["admin_port"]


def test_a_listener_the_admin_container_publishes_is_owned(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_port(ADMIN_CONTAINER, 8090)
    services.host.listening_ports = ADMIN_LISTENER

    finding = port_finding(services)

    assert finding.ok is True, finding
    assert finding.indeterminate is False, finding


def test_an_unrelated_docker_proxy_listener_is_a_conflict(tmp_path):
    """A docker-proxy the Admin container did not create is not Admin's."""

    services = healthy_appliance(tmp_path)
    services.host.run_container("unrelated", f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.publish_port("unrelated", 8090)
    services.host.listening_ports = ADMIN_LISTENER

    finding = port_finding(services)

    assert finding.ok is False, finding
    assert finding.indeterminate is False, finding
    assert finding.action == ""


def test_an_admin_listener_next_to_an_unrelated_listener_is_a_conflict(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_port(ADMIN_CONTAINER, 8090)
    services.host.listening_ports = ADMIN_LISTENER + FOREIGN_LISTENER

    finding = port_finding(services)

    assert finding.ok is False, finding


def test_a_native_listener_on_the_admin_port_is_a_conflict(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.listening_ports = FOREIGN_LISTENER

    finding = port_finding(services)

    assert finding.ok is False, finding
    assert "nginx" in finding.detail


def test_unprovable_ownership_is_indeterminate_never_available(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.listening_ports = ADMIN_LISTENER
    services.host.docker_running = False

    finding = port_finding(services)

    assert finding.indeterminate is True, finding
    assert "available" not in finding.detail.lower(), finding
