# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin comes back after a reboot.

Docker starts a container at boot only when its restart policy says so. The
Admin installer used to write ``restart: "no"``, so a rebooted appliance came
up with EMS running and the Admin console gone until somebody pressed Start in
the Appliance Manager. New installations get ``unless-stopped``. An existing
one shows a repair finding, and a boot unit runs exactly that one repair
through the agent -- plan, confirmation, lock and audit like any other.
"""

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from appliance import admin_transition
from appliance import cli
from appliance.admin_deployment import (
    ADMIN_RESTART_POLICY,
    INSTALLER_MARKER,
    DeploymentError,
    apply_restart_policy,
    enable_service_restart,
    read_service_restart,
)
from appliance.admin_lifecycle import (
    ACTION_SET_RESTART_POLICY,
    TYPE_REPAIR,
    run_restart_policy_repair,
)
from appliance.agent import AgentHandlers
from appliance.agent_client import (
    AgentCallError,
    AgentClient,
    AgentUnavailableError,
    InProcessAgentClient,
)
from appliance.operations import STATE_CANCELLED, STATE_SUCCEEDED
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    StaticCatalogue,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]


def installed_before_the_fix(
    tmp_path, *, restart='"no"', container_policy="no", state="running", by_installer=True
):
    services = build_test_services(tmp_path, catalogue=StaticCatalogue(["v1.0.0"]))
    host = services.host
    compose = host.write_deployment(tag="v1.0.0", restart=restart)
    if not by_installer:
        (services.paths.install_root / "docker-compose.admin.yml").write_text(
            "".join(line for line in compose.splitlines(True) if INSTALLER_MARKER not in line),
            encoding="utf-8",
        )
    host.publish_image("v1.0.0")
    host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    host.run_container(
        ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", restart_policy=container_policy, state=state
    )
    return services


def compose_path(services):
    return services.paths.install_root / "docker-compose.admin.yml"


def compose_text(services):
    return compose_path(services).read_text(encoding="utf-8")


def container_policy(services):
    return services.host.containers[ADMIN_CONTAINER]["HostConfig"]["RestartPolicy"]["Name"]


def docker_calls(services, verb):
    return [
        call for call in services.host.calls if call[0] == "docker" and call[1][:1] == (verb,)
    ]


def restart_finding(services):
    found = [item for item in services.admin.inspect_repair() if item.check == "restart_policy"]
    assert len(found) == 1, found
    return found[0]


def boot_run(services):
    handlers = AgentHandlers(services, executor=lambda target: target())
    return run_restart_policy_repair(InProcessAgentClient(handlers))


def write_live_transition(services):
    path = admin_transition.transition_path(services.paths, services.admin.deployment())
    path.parent.mkdir(parents=True, exist_ok=True)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    path.write_text(
        json.dumps(
            {
                "state_version": 2,
                "operation_id": "a" * 32,
                "mode": "guided_upgrade",
                "stage": "admin_update_pending",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ),
        encoding="utf-8",
    )


# --- the compose edit --------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ('"no"', "unless-stopped"),
        ("'no'", "unless-stopped"),
        ("no", "unless-stopped"),
        ('"no"  # set by the installer', "unless-stopped  # set by the installer"),
    ],
)
def test_an_explicit_no_becomes_the_admin_policy_and_nothing_else_changes(written, expected):
    template = (
        "services:\n"
        "  ems-solarflow-admin:\n"
        "    image: example/admin:v1\n"
        "    restart: {}\n"
        "    read_only: true\n"
    )

    after = enable_service_restart(template.format(written), "ems-solarflow-admin")

    assert after == template.format(expected)
    assert read_service_restart(after, "ems-solarflow-admin") == ADMIN_RESTART_POLICY


def test_only_the_admin_service_is_touched():
    before = (
        "services:\n"
        "  ems-solarflow-admin:\n"
        "    image: example/admin:v1\n"
        "    environment:\n"
        '      restart: "no"\n'
        '    restart: "no"\n'
        "  mosquitto:\n"
        "    image: eclipse-mosquitto\n"
        '    restart: "no"\n'
    )

    after = enable_service_restart(before, "ems-solarflow-admin")

    assert after.splitlines() == [
        "services:",
        "  ems-solarflow-admin:",
        "    image: example/admin:v1",
        "    environment:",
        '      restart: "no"',
        "    restart: unless-stopped",
        "  mosquitto:",
        "    image: eclipse-mosquitto",
        '    restart: "no"',
    ]


def test_a_crlf_file_keeps_its_line_endings():
    before = 'services:\r\n  ems-solarflow-admin:\r\n    image: a:b\r\n    restart: "no"\r\n'

    after = enable_service_restart(before, "ems-solarflow-admin")

    assert after == before.replace('restart: "no"', "restart: unless-stopped")


@pytest.mark.parametrize("policy", ["always", "on-failure", "unless-stopped"])
def test_a_policy_other_than_no_is_left_as_written(policy):
    before = f"services:\n  ems-solarflow-admin:\n    image: a:b\n    restart: {policy}\n"

    assert enable_service_restart(before, "ems-solarflow-admin") == before


def test_a_service_without_a_restart_key_is_not_given_one():
    before = "services:\n  ems-solarflow-admin:\n    image: a:b\n"

    assert enable_service_restart(before, "ems-solarflow-admin") == before
    assert read_service_restart(before, "ems-solarflow-admin") is None


def test_the_rewrite_keeps_the_file_mode_and_owner(tmp_path):
    services = installed_before_the_fix(tmp_path)
    path = compose_path(services)
    os.chmod(path, 0o644)
    before = os.stat(path)

    assert apply_restart_policy(services.admin.deployment()) is True

    after = os.stat(path)
    assert stat.S_IMODE(after.st_mode) == 0o644
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_a_no_in_a_file_the_installer_did_not_write_is_the_operators(tmp_path):
    services = installed_before_the_fix(tmp_path, by_installer=False)
    before = compose_text(services)

    assert apply_restart_policy(services.admin.deployment()) is False
    assert compose_text(services) == before
    assert restart_finding(services).ok is True
    assert services.admin.detect()["restart_policy"]["update_due"] is False


def test_a_compose_file_that_is_not_utf8_is_a_typed_error(tmp_path):
    services = installed_before_the_fix(tmp_path)
    deployment = services.admin.deployment()
    compose_path(services).write_bytes(compose_text(services).encode("utf-8") + b"# M\xfcller\n")

    with pytest.raises(DeploymentError) as excinfo:
        apply_restart_policy(deployment)

    assert excinfo.value.code == "compose_file_unreadable"


# --- what the Manager shows --------------------------------------------------


def test_an_installation_from_before_the_fix_shows_a_repair_finding(tmp_path):
    services = installed_before_the_fix(tmp_path)

    finding = restart_finding(services)

    assert finding.ok is False
    assert finding.action == ACTION_SET_RESTART_POLICY
    assert services.admin.detect()["restart_policy"] == {
        "compose": "no",
        "container": "no",
        "update_due": True,
    }


def test_a_fixed_file_with_an_old_container_is_still_due(tmp_path):
    services = installed_before_the_fix(tmp_path, restart="unless-stopped", container_policy="no")

    assert restart_finding(services).action == ACTION_SET_RESTART_POLICY
    assert services.admin.detect()["restart_policy"]["update_due"] is True


def test_a_current_installation_has_nothing_to_repair(tmp_path):
    services = installed_before_the_fix(
        tmp_path, restart="unless-stopped", container_policy="unless-stopped"
    )

    assert restart_finding(services).ok is True
    assert services.admin.detect()["restart_policy"]["update_due"] is False


def test_a_policy_the_operator_chose_is_not_a_finding(tmp_path):
    services = installed_before_the_fix(tmp_path, restart="always", container_policy="no")

    finding = restart_finding(services)

    assert finding.ok is True
    assert finding.action == ""
    assert services.admin.detect()["restart_policy"]["update_due"] is False


# --- the boot run --------------------------------------------------------------


def test_the_boot_run_moves_file_and_container_through_one_audited_repair(tmp_path):
    services = installed_before_the_fix(tmp_path)
    before = compose_text(services)

    result = boot_run(services)

    assert result["ran"] is True
    assert compose_text(services) == before.replace('restart: "no"', "restart: unless-stopped")
    assert container_policy(services) == "unless-stopped"
    operation = services.operations.get(result["operation_id"])
    assert operation.type == TYPE_REPAIR
    assert operation.state == STATE_SUCCEEDED
    assert operation.requested_target["actions"] == [ACTION_SET_RESTART_POLICY]


def test_the_boot_run_neither_stops_nor_starts_nor_recreates_the_admin(tmp_path):
    services = installed_before_the_fix(tmp_path)

    boot_run(services)

    for verb in ("stop", "start", "restart", "rm", "compose"):
        assert docker_calls(services, verb) == [], verb
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True


def test_an_admin_that_is_down_is_not_started_by_the_boot_run(tmp_path):
    """Only the policy is automatic.

    Docker cannot say whether a stopped Admin was lost to a reboot or stopped
    by its operator, so starting it here could undo a deliberate Stop. The
    repair preview still offers Start to the operator.
    """

    services = installed_before_the_fix(tmp_path, state="exited")

    result = boot_run(services)

    assert result["ran"] is True
    assert container_policy(services) == "unless-stopped"
    assert docker_calls(services, "start") == []
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is False
    operation = services.operations.get(result["operation_id"])
    assert operation.requested_target["actions"] == [ACTION_SET_RESTART_POLICY]
    assert operation.state == STATE_SUCCEEDED


def test_the_boot_run_on_a_current_installation_leaves_no_record(tmp_path):
    services = installed_before_the_fix(
        tmp_path, restart="unless-stopped", container_policy="unless-stopped"
    )

    result = boot_run(services)

    assert result == {"ran": False, "reason": "nothing_to_do"}
    assert services.operations.list() == []
    assert docker_calls(services, "update") == []


def test_the_boot_run_is_idempotent(tmp_path):
    services = installed_before_the_fix(tmp_path)
    boot_run(services)
    settled = compose_text(services)
    services.host.calls.clear()

    assert boot_run(services) == {"ran": False, "reason": "nothing_to_do"}
    assert compose_text(services) == settled
    assert docker_calls(services, "update") == []


def test_the_boot_run_keeps_a_policy_the_operator_chose(tmp_path):
    services = installed_before_the_fix(tmp_path, restart="always", container_policy="no")
    before = compose_text(services)

    assert boot_run(services) == {"ran": False, "reason": "nothing_to_do"}
    assert compose_text(services) == before
    assert container_policy(services) == "no"


def test_the_boot_run_waits_for_an_operation_the_operator_started(tmp_path):
    services = installed_before_the_fix(tmp_path)
    busy = services.operations.create("admin.lifecycle", actor="operator")
    before = compose_text(services)

    result = boot_run(services)

    assert result["ran"] is False
    assert result["reason"] == "busy"
    assert compose_text(services) == before
    assert services.operations.get(busy.operation_id).state != STATE_CANCELLED


def test_the_boot_run_does_not_fight_a_live_admin_transition(tmp_path):
    services = installed_before_the_fix(tmp_path)
    write_live_transition(services)
    before = compose_text(services)

    result = boot_run(services)

    assert result["ran"] is False
    assert result["reason"] == "busy"
    assert compose_text(services) == before
    assert container_policy(services) == "no"


def test_the_boot_run_keeps_an_operators_no(tmp_path):
    services = installed_before_the_fix(tmp_path, by_installer=False)
    before = compose_text(services)

    assert boot_run(services) == {"ran": False, "reason": "nothing_to_do"}
    assert compose_text(services) == before
    assert container_policy(services) == "no"


def test_docker_not_answering_defers_the_run_instead_of_calling_it_done(tmp_path):
    services = installed_before_the_fix(tmp_path)
    services.host.docker_running = False
    before = compose_text(services)

    result = boot_run(services)

    assert result == {"ran": False, "reason": "deferred"}
    assert compose_text(services) == before
    assert [item.state for item in services.operations.list()] == [STATE_CANCELLED]


def test_a_refused_docker_update_fails_the_repair_it_does_not_pass(tmp_path):
    services = installed_before_the_fix(tmp_path)
    original = services.host._docker

    def refuse_update(args):
        if args[:1] == ["update"]:
            return services.host._result("docker", args, 1, "", "permission denied")
        return original(args)

    services.host._docker = refuse_update

    result = boot_run(services)

    assert result["ran"] is False
    assert result["reason"] == "repair_failed"
    operation = services.operations.get(result["operation_id"])
    assert operation.state != STATE_SUCCEEDED
    assert container_policy(services) == "no"


def test_an_agent_that_is_not_running_is_reported(tmp_path):
    result = run_restart_policy_repair(AgentClient(tmp_path / "missing.sock"))

    assert result["ran"] is False
    assert result["reason"] == "agent_unavailable"


def test_the_unit_command_never_executes_the_repair_in_its_own_process(
    tmp_path, monkeypatch, capsys
):
    """Without the agent it gives up; it does not build a privileged stack.

    An in-process agent would run the confirmed repair on a daemon thread that
    this short-lived command does not outlive, leaving a half-applied policy
    and a record stuck in "running" until the next agent start.
    """

    services = installed_before_the_fix(tmp_path)
    monkeypatch.setattr(cli, "resolve_paths", lambda: services.paths)

    code = cli.command_admin_restart_policy(type("Args", (), {"json": False})())

    assert code == cli.EXIT_ERROR
    assert "agent_unavailable" in capsys.readouterr().out
    assert services.operations.list() == []
    assert container_policy(services) == "no"


class GivesUpOn:
    """A client that loses one call the way a socket timeout loses it.

    ``after`` lets the agent finish the call before the caller gives up, which
    is exactly how an unconfirmed plan outlives the caller that asked for it.
    """

    def __init__(self, inner, operation, *, after, error):
        self.inner, self.operation, self.after, self.error = inner, operation, after, error

    def call(self, operation, **fields):
        if operation == self.operation:
            if self.after:
                self.inner.call(operation, **fields)
            raise self.error
        return self.inner.call(operation, **fields)


def boot_client(services):
    return InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))


def test_a_plan_the_caller_gave_up_on_does_not_hold_the_lock(tmp_path):
    services = installed_before_the_fix(tmp_path)
    client = GivesUpOn(
        boot_client(services),
        "admin.plan_repair",
        after=True,
        error=AgentUnavailableError("timed out"),
    )

    result = run_restart_policy_repair(client)

    assert result["reason"] == "agent_unavailable"
    assert services.operations.active() is None
    assert [item.state for item in services.operations.list()] == [STATE_CANCELLED]


def test_a_refused_confirmation_does_not_leave_the_plan_holding_the_lock(tmp_path):
    services = installed_before_the_fix(tmp_path)
    client = GivesUpOn(
        boot_client(services),
        "operations.execute",
        after=False,
        error=AgentCallError("confirmation_token_mismatch", "the token does not match"),
    )

    result = run_restart_policy_repair(client)

    assert result["reason"] == "repair_failed"
    assert services.operations.active() is None
    assert container_policy(services) == "no"


def test_an_execute_whose_answer_was_lost_is_not_reported_as_a_failure(tmp_path):
    services = installed_before_the_fix(tmp_path)
    client = GivesUpOn(
        boot_client(services),
        "operations.execute",
        after=True,
        error=AgentUnavailableError("connection reset"),
    )

    result = run_restart_policy_repair(client)

    assert result["reason"] == "outcome_unknown"
    operation = services.operations.get(result["operation_id"])
    assert operation.state == STATE_SUCCEEDED


def test_the_boot_run_does_not_probe_the_admin_or_the_ports(tmp_path):
    """It holds the operation lock; the full inspection is the console's."""

    services = installed_before_the_fix(tmp_path)

    boot_run(services)

    assert services.admin.health.probes == 0
    assert [call for call in services.host.calls if call[0] == "ss"] == []
