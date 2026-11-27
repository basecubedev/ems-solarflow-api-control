# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two management layers, one Admin container.

The appliance edits the Admin deployment to install, roll back, repair or
restart it. The Admin console replaces *itself* through System Build and Guided
Upgrade, writing the same files. Both are correct alone and neither knows about
the other, so one has to yield -- and it is the appliance, because Admin is the
only side that can be halfway through with a worker running.

The asymmetry that matters is in the other direction: the appliance is the tool
an operator reaches for when Admin is broken. A transition that has expired, or
one that cannot be read, must therefore *not* block it. Those are the wedged
states the appliance exists to fix.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from appliance import admin_deployment, admin_transition
from appliance.agent import AgentHandlers
from appliance.operations import STATE_FAILED_TERMINAL, STATE_SUCCEEDED
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    StaticCatalogue,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

MUTATING_PLANS = (
    ("admin.plan_install", {"channel": "exact", "tag": "v1.1.0"}),
    ("admin.plan_rollback", {}),
    ("admin.plan_repair", {}),
    ("admin.plan_lifecycle", {"action": "restart"}),
)


def stamp(offset_seconds):
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def healthy_appliance(tmp_path):
    services = build_test_services(tmp_path, catalogue=StaticCatalogue(["v1.1.0", "v1.0.0"]))
    host = services.host
    host.write_deployment(tag="v1.0.0")
    host.publish_image("v1.0.0")
    host.publish_image("v1.1.0")
    host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    # Two records: a rollback needs a *previous* known-good, and one record
    # only ever becomes the current one.
    for version, fill in (("v0.9.0", "9"), ("v1.0.0", "1")):
        services.known_good.record(
            admin_image=f"{ADMIN_REPOSITORY}:{version}",
            admin_digest="sha256:" + fill * 64,
            admin_version=version,
            admin_reference=f"{ADMIN_REPOSITORY}@sha256:" + fill * 64,
        )
    return services


def write_transition(services, payload):
    path = admin_transition.transition_path(services.paths, services.admin.deployment())
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, str)):
        path.write_bytes(payload if isinstance(payload, bytes) else payload.encode())
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def live_transition(stage="admin_update_pending"):
    return {
        "state_version": 2,
        "operation_id": "a" * 32,
        "mode": "guided_upgrade",
        "stage": stage,
        "created_at": stamp(-60),
        "expires_at": stamp(3600),
    }


def refuse_code(services, operation, fields):
    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as caught:
        handlers.dispatch({"operation": operation, **fields})
    return getattr(caught.value, "code", "")


def plan_code(services, operation, fields):
    """The plan's error code, or "" when it planned successfully."""

    handlers = AgentHandlers(services, executor=lambda target: target())
    try:
        handlers.dispatch({"operation": operation, **fields})
    except Exception as exc:  # noqa: BLE001 - the code is what is under test
        return getattr(exc, "code", exc.__class__.__name__)
    return ""


# --- where the record lives --------------------------------------------------


def test_the_state_directory_comes_from_the_deployment_not_from_a_guess(tmp_path):
    services = healthy_appliance(tmp_path)
    env_file = services.paths.install_root / ".env.admin"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "EMS_ADMIN_DATA_DIR=/srv/elsewhere/admin\n",
        encoding="utf-8",
    )

    path = admin_transition.transition_path(services.paths, services.admin.deployment())

    assert str(path) == "/srv/elsewhere/admin/state/pending-transition.json"


def test_the_compose_environment_the_container_runs_with_outranks_the_env_file(tmp_path):
    """The installer bakes the value into the compose file and keeps .env.admin
    for reference; a value changed in the compose only had the Manager watch a
    file the Admin never writes."""

    from appliance.admin_deployment import set_service_environment

    services = healthy_appliance(tmp_path)
    deployment = services.admin.deployment()
    env_file = services.paths.install_root / ".env.admin"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "EMS_ADMIN_DATA_DIR=/srv/stale/admin\n",
        encoding="utf-8",
    )
    compose = deployment.compose_file.read_text(encoding="utf-8")
    deployment.compose_file.write_text(
        set_service_environment(
            compose, deployment.service, {"EMS_ADMIN_DATA_DIR": "/srv/current/admin"}
        ),
        encoding="utf-8",
    )

    path = admin_transition.transition_path(services.paths, services.admin.deployment())

    assert str(path) == "/srv/current/admin/state/pending-transition.json"


def test_a_compose_value_taken_from_the_env_file_reads_the_env_file(tmp_path):
    from appliance.admin_deployment import set_service_environment

    services = healthy_appliance(tmp_path)
    deployment = services.admin.deployment()
    env_file = services.paths.install_root / ".env.admin"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "EMS_ADMIN_DATA_DIR=/srv/elsewhere/admin\n",
        encoding="utf-8",
    )
    compose = deployment.compose_file.read_text(encoding="utf-8")
    deployment.compose_file.write_text(
        set_service_environment(
            compose, deployment.service, {"EMS_ADMIN_DATA_DIR": "${EMS_ADMIN_DATA_DIR}"}
        ),
        encoding="utf-8",
    )

    path = admin_transition.transition_path(services.paths, services.admin.deployment())

    assert str(path) == "/srv/elsewhere/admin/state/pending-transition.json"


def test_a_deployment_that_names_no_data_dir_falls_back_to_the_shipped_layout(tmp_path):
    services = healthy_appliance(tmp_path)

    path = admin_transition.transition_path(services.paths, services.admin.deployment())

    assert path == services.paths.install_root / "data" / "admin" / "state" / (
        "pending-transition.json"
    )


def test_a_relative_data_dir_is_not_taken_from_the_environment_file(tmp_path):
    """It would resolve against whatever the agent's cwd happens to be."""

    services = healthy_appliance(tmp_path)
    env_file = services.paths.install_root / ".env.admin"
    env_file.write_text("EMS_ADMIN_DATA_DIR=relative/admin\n", encoding="utf-8")

    path = admin_transition.transition_path(services.paths, services.admin.deployment())

    assert path.is_absolute()
    assert path == services.paths.install_root / "data" / "admin" / "state" / (
        "pending-transition.json"
    )


# --- classification ----------------------------------------------------------


def test_no_file_means_no_transition(tmp_path):
    services = healthy_appliance(tmp_path)

    record = admin_transition.read_transition(
        admin_transition.transition_path(services.paths, services.admin.deployment())
    )

    assert record["state"] == admin_transition.STATE_NONE
    assert admin_transition.blocks_admin_mutation(record) is False


def test_a_transition_inside_its_own_expiry_is_live(tmp_path):
    services = healthy_appliance(tmp_path)
    path = write_transition(services, live_transition())

    record = admin_transition.read_transition(path)

    assert record["state"] == admin_transition.STATE_LIVE
    assert admin_transition.blocks_admin_mutation(record) is True


def test_a_transition_past_its_own_expiry_is_not_protected(tmp_path):
    services = healthy_appliance(tmp_path)
    payload = live_transition()
    payload["expires_at"] = stamp(-60)
    path = write_transition(services, payload)

    record = admin_transition.read_transition(path)

    assert record["state"] == admin_transition.STATE_EXPIRED
    assert admin_transition.blocks_admin_mutation(record) is False


@pytest.mark.parametrize(
    "payload",
    [
        b"{ not json",
        json.dumps(["a", "list"]),
        json.dumps({"operation_id": "x", "stage": "y"}),
        json.dumps({"expires_at": "not-a-timestamp"}),
    ],
)
def test_a_record_that_cannot_be_classified_never_blocks(tmp_path, payload):
    """A corrupt transition is not a running one, and Admin's resume is already
    broken. Blocking on it would make the recovery tool unusable."""

    services = healthy_appliance(tmp_path)
    path = write_transition(services, payload)

    record = admin_transition.read_transition(path)

    assert record["state"] == admin_transition.STATE_UNREADABLE
    assert admin_transition.blocks_admin_mutation(record) is False
    assert record["reason"]


def test_an_implausibly_large_record_is_not_parsed(tmp_path):
    services = healthy_appliance(tmp_path)
    path = write_transition(services, b"{}" + b" " * (admin_transition.MAX_TRANSITION_BYTES + 1))

    record = admin_transition.read_transition(path)

    assert record["state"] == admin_transition.STATE_UNREADABLE


# --- what the appliance does with it -----------------------------------------


@pytest.mark.parametrize("operation,fields", MUTATING_PLANS)
def test_every_admin_mutating_plan_yields_to_a_live_transition(tmp_path, operation, fields):
    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition())

    assert refuse_code(services, operation, fields) == "admin_transition_in_flight"


def test_a_transition_that_starts_after_the_plan_still_stops_the_execution(tmp_path):
    """The yield has to hold at confirmation, not only at plan time.

    Execution already re-reads the image and the deployment file because "a plan
    can be confirmed minutes later" -- and those are exactly the minutes in which
    the Admin console can start replacing itself. Its own updater pulls before it
    touches a file, so every other binding still holds while the record is
    already live, and the two layers would then write the same file with no lock
    between them. An install is what this drives, because it is the operation this
    fixture can carry all the way to success; the rollback executor is given the
    same check.
    """

    services = healthy_appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    compose = services.paths.install_root / "docker-compose.admin.yml"
    before = compose.read_bytes()

    write_transition(services, live_transition(stage="admin_reconnect_pending"))
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )

    record = services.operations.get(planned["operation"]["operation_id"])
    assert record.state != STATE_SUCCEEDED, record.result
    assert record.error["code"] == "admin_transition_in_flight"
    assert compose.read_bytes() == before


@pytest.mark.parametrize("operation,fields", MUTATING_PLANS)
def test_no_admin_plan_is_blocked_by_an_expired_transition(tmp_path, operation, fields):
    """The wedged case: this is precisely when the appliance must still work."""

    services = healthy_appliance(tmp_path)
    payload = live_transition()
    payload["expires_at"] = stamp(-60)
    write_transition(services, payload)

    assert plan_code(services, operation, fields) == ""


@pytest.mark.parametrize("operation,fields", MUTATING_PLANS)
def test_no_admin_plan_is_blocked_by_an_unreadable_transition(tmp_path, operation, fields):
    services = healthy_appliance(tmp_path)
    write_transition(services, b"{ corrupt")

    assert plan_code(services, operation, fields) == ""


def test_starting_admin_is_refused_too_while_a_replacement_is_in_flight(tmp_path):
    """Racing the other side is not safer in the helpful direction."""

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition())

    assert refuse_code(services, "admin.plan_lifecycle", {"action": "start"}) == (
        "admin_transition_in_flight"
    )


def test_the_refusal_says_what_to_wait_for_and_how_to_clear_it(tmp_path):
    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition(stage="admin_reconnect_pending"))
    handlers = AgentHandlers(services, executor=lambda target: target())

    with pytest.raises(Exception) as caught:
        handlers.dispatch({"operation": "admin.plan_repair"})

    message = str(getattr(caught.value, "message", caught.value))
    assert "admin_reconnect_pending" in message
    assert "pending-transition.json" in message


def test_reading_the_state_never_writes_to_it(tmp_path):
    """That record belongs to Admin; clearing it is not the appliance's call."""

    services = healthy_appliance(tmp_path)
    path = write_transition(services, live_transition())
    before = path.read_bytes(), path.stat().st_mtime_ns

    refuse_code(services, "admin.plan_repair", {})

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_read_only_status_still_works_while_a_transition_is_live(tmp_path):
    """Refusing to mutate is not refusing to look."""

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition())
    handlers = AgentHandlers(services, executor=lambda target: target())

    payload = handlers.dispatch({"operation": "admin.get"})

    assert payload["installed"] is True


def test_the_status_payload_says_a_transition_is_live_so_the_page_can(tmp_path):
    """The page must be able to explain the refusal before one happens."""

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition())
    handlers = AgentHandlers(services, executor=lambda target: target())

    payload = handlers.dispatch({"operation": "admin.get"})

    assert payload["transition"]["state"] == admin_transition.STATE_LIVE
    assert payload["transition"]["stage"] == "admin_update_pending"


def test_the_status_payload_carries_no_transition_when_there_is_none(tmp_path):
    services = healthy_appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())

    payload = handlers.dispatch({"operation": "admin.get"})

    assert payload["transition"]["state"] == admin_transition.STATE_NONE


# --- a transition Admin has finished ----------------------------------------


def test_a_finished_transition_is_not_one_in_flight(tmp_path):
    """Admin keeps the record after it finishes, with the clock moved forward.

    `_progressed` renews `expires_at` on every durable write, the completing
    one included, so a *successful* self-upgrade leaves a record naming up to
    an hour in the future. Nothing removes it: `PendingTransitionStore.clear()`
    has no production caller. Classifying by the clock alone therefore made the
    appliance stand back from its own repair tools for that hour, right after
    the upgrade those tools exist to recover from.
    """

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition(stage="completed"))

    record = admin_transition.read_transition(
        admin_transition.transition_path(services.paths, services.admin.deployment())
    )

    assert record["state"] == admin_transition.STATE_FINISHED
    assert admin_transition.blocks_admin_mutation(record) is False


@pytest.mark.parametrize("operation,fields", MUTATING_PLANS)
@pytest.mark.parametrize("stage", ["completed", "cancelled"])
def test_no_admin_plan_is_blocked_after_admin_finished(tmp_path, stage, operation, fields):
    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition(stage=stage))

    assert plan_code(services, operation, fields) != "admin_transition_in_flight"


def test_a_stage_this_side_does_not_know_still_blocks(tmp_path):
    """Fail closed: only the two stages Admin itself calls terminal release it."""

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition(stage="some_future_stage"))

    record = admin_transition.read_transition(
        admin_transition.transition_path(services.paths, services.admin.deployment())
    )

    assert record["state"] == admin_transition.STATE_LIVE
    assert admin_transition.blocks_admin_mutation(record) is True


def test_a_stage_admin_may_still_act_on_keeps_blocking(tmp_path):
    """`failed_recoverable` is cancellable on Admin's side, so it is not over."""

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition(stage="failed_recoverable"))

    record = admin_transition.read_transition(
        admin_transition.transition_path(services.paths, services.admin.deployment())
    )

    assert admin_transition.blocks_admin_mutation(record) is True


def test_the_appliance_stands_back_for_exactly_the_stages_admin_calls_unfinished():
    """One authority for which stages are terminal, copied and pinned."""

    from admin.admin_update import TERMINAL_TRANSITION_STAGES, VALID_TRANSITION_STAGES

    assert set(admin_transition.FINISHED_STAGES) == set(TERMINAL_TRANSITION_STAGES)
    assert set(admin_transition.FINISHED_STAGES) <= set(VALID_TRANSITION_STAGES)


# --- a file the EMS container can write --------------------------------------------


def _transition_file(tmp_path):
    path = tmp_path / "state" / admin_transition.TRANSITION_FILE
    path.parent.mkdir(parents=True)
    return path


def test_a_fifo_at_the_transition_path_is_unreadable_not_a_hang(tmp_path):
    """The EMS container mounts this tree read-write. A FIFO there hung every
    status read and every Admin plan, the plan while it held the Admin lock."""

    import os

    path = _transition_file(tmp_path)
    os.mkfifo(path)

    assert admin_transition.read_transition(path)["state"] == admin_transition.STATE_UNREADABLE


def test_a_link_at_the_transition_path_is_not_followed(tmp_path):
    """A link to /dev/zero was read as root until memory ran out."""

    path = _transition_file(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(live_transition()), encoding="utf-8")
    path.symlink_to(target)

    assert admin_transition.read_transition(path)["state"] == admin_transition.STATE_UNREADABLE


def test_an_oversized_transition_is_refused_without_reading_it_whole(tmp_path, monkeypatch):
    import os

    path = _transition_file(tmp_path)
    path.write_bytes(b"{" + b" " * (4 * admin_transition.MAX_TRANSITION_BYTES) + b"}")
    read = []
    real = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: read.append(n) or real(fd, n))
    monkeypatch.setattr(type(path), "read_bytes", lambda self: pytest.fail("the whole file was read"))

    assert admin_transition.read_transition(path)["state"] == admin_transition.STATE_UNREADABLE
    assert sum(read) <= admin_transition.MAX_TRANSITION_BYTES + 1, sum(read)


def test_an_expiry_no_admin_would_write_does_not_block_for_ever(tmp_path):
    """Admin renews its expiry an hour ahead. One years ahead held every
    Install, Rollback, Repair, Start, Stop and Restart until then."""

    path = _transition_file(tmp_path)
    path.write_text(json.dumps({**live_transition(), "expires_at": "9999-12-31T00:00:00Z"}), encoding="utf-8")

    record = admin_transition.read_transition(path)

    assert record["state"] == admin_transition.STATE_UNREADABLE
    assert "9999-12-31" in record["reason"]


def test_the_expiry_ceiling_is_the_admins_own_transition_lifetime():
    from admin.admin_update import DEFAULT_TRANSITION_TTL_SECONDS

    assert admin_transition.MAX_TRANSITION_TTL_SECONDS == DEFAULT_TRANSITION_TTL_SECONDS


def test_the_refusal_says_when_the_transition_stops_blocking(monkeypatch):
    """"Clear <path> if it never will" gave no time and asked for a shell the
    operator of the console may not have."""

    monkeypatch.setenv("TZ", "Europe/Berlin")
    import time

    time.tzset()
    try:
        message = admin_transition.refusal_message(
            {
                "path": "/opt/ems/data/admin/transition.json",
                "stage": "admin_update_pending",
                "expires_at": "2026-11-05T13:45:00Z",
            }
        )
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    assert "14:45" in message, message
    assert "at the latest" in message, message


def _execute_with_transition_starting(services, planned, when):
    """Execute ``planned``, letting Admin start replacing itself at ``when``."""

    original = getattr(services.admin, when)

    def starting(*args, **kwargs):
        write_transition(services, live_transition())
        return original(*args, **kwargs)

    setattr(services.admin, when, starting)
    handlers = AgentHandlers(services, executor=lambda target: target())
    try:
        handlers.dispatch(
            {
                "operation": "operations.execute",
                "operation_id": planned["operation"]["operation_id"],
                "confirmation_token": planned["confirmation_token"],
            }
        )
    except Exception:  # noqa: BLE001 - the record is what is under test
        pass
    finally:
        delattr(services.admin, when)
    return services.operations.get(planned["operation"]["operation_id"])


def test_an_install_stands_back_from_a_transition_that_started_after_its_preflight(tmp_path):
    """The record was read once when execution began; one that went live while
    the target was verified was then fought: the Admin stopped and recreated."""

    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    compose_before = services.admin.deployment().compose_file.read_text(encoding="utf-8")
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )

    operation = _execute_with_transition_starting(services, planned, "_record_running_admin")

    assert operation.error["code"] == "admin_transition_in_flight", operation.to_dict()
    assert operation.result["admin_untouched"] is True
    assert services.admin.deployment().compose_file.read_text(encoding="utf-8") == compose_before
    assert services.admin.detect()["container"]["state"] == "running"


def test_a_failed_install_does_not_recover_over_a_transition_the_new_admin_started(tmp_path):
    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )

    operation = _execute_with_transition_starting(services, planned, "_verify_admin")

    assert operation.state == "manual_action_required", operation.to_dict()
    assert operation.stage == "admin_transition_in_flight"
    assert "nothing was restored" in operation.error["message"]


# --- restarting or shutting down the host -------------------------------------

POWER_PLANS = (("system.plan_reboot", "reboot"), ("system.plan_shutdown", "poweroff"))


@pytest.mark.parametrize("plan", [plan for plan, _ in POWER_PLANS])
def test_a_restart_or_shutdown_is_not_planned_while_admin_replaces_itself(tmp_path, plan):
    """Issue #416: a restart in the middle of an Admin update interrupts the update."""

    services = healthy_appliance(tmp_path)
    write_transition(services, live_transition())
    handlers = AgentHandlers(services, executor=lambda target: target())

    planned = handlers.dispatch({"operation": plan})

    assert [blocker["code"] for blocker in planned["plan"]["blockers"]] == ["admin_transition_in_flight"]
    assert "replacing itself" in planned["plan"]["blockers"][0]["message"]


@pytest.mark.parametrize(
    "transition",
    [
        {**live_transition(), "expires_at": stamp(-60)},
        live_transition(stage="completed"),
        b"{not json",
    ],
    ids=["expired", "finished", "unreadable"],
)
@pytest.mark.parametrize("plan", [plan for plan, _ in POWER_PLANS])
def test_a_transition_that_is_not_live_does_not_hold_the_host_up(tmp_path, transition, plan):
    """A wedged Admin record is a reason to restart, not a reason to refuse one."""

    services = healthy_appliance(tmp_path)
    write_transition(services, transition)
    handlers = AgentHandlers(services, executor=lambda target: target())

    planned = handlers.dispatch({"operation": plan})

    assert planned["plan"]["blockers"] == []


@pytest.mark.parametrize(("plan", "call"), POWER_PLANS)
def test_an_admin_update_started_after_the_plan_still_stops_the_power_action(tmp_path, plan, call):
    """The Admin can begin replacing itself between the plan and Confirm."""

    services = healthy_appliance(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": plan})
    assert planned["plan"]["blockers"] == []
    write_transition(services, live_transition())
    services.host.calls.clear()

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )

    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.error["code"] == "admin_transition_in_flight"
    assert not [c for c in services.host.calls if c[0] == "systemctl" and call in c[1]], services.host.calls


def _undecodable_compose(services):
    services.admin.deployment().compose_file.write_bytes(b"services:\n  admin:\n    image: \xff\xfe\n")


def _undecodable_env(services):
    services.admin.deployment().env_file.write_bytes(b"EMS_ADMIN_TAG=\xff\xfe\n")


def _nul_in_the_data_dir(services):
    env_file = services.admin.deployment().env_file
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "EMS_ADMIN_DATA_DIR=/srv/ad\x00min\n", encoding="utf-8"
    )


def _fifo_where_the_compose_file_would_be(services):
    for name in admin_deployment.COMPOSE_CANDIDATES:
        (services.paths.install_root / name).unlink(missing_ok=True)
    os.mkfifo(services.paths.install_root / admin_deployment.COMPOSE_CANDIDATES[0])


@pytest.mark.parametrize(
    "damage",
    [_undecodable_compose, _undecodable_env, _nul_in_the_data_dir, _fifo_where_the_compose_file_would_be],
    ids=["compose_not_utf8", "env_not_utf8", "nul_in_data_dir", "fifo_compose"],
)
@pytest.mark.parametrize("plan", [plan for plan, _ in POWER_PLANS])
def test_a_damaged_admin_deployment_does_not_take_the_restart_away(tmp_path, damage, plan):
    """The restart is the way out of a broken Admin deployment, so reading it may not fail it.

    The planner runs on a thread joined with a deadline only so that a read
    which blocks fails the test instead of hanging the run.
    """

    services = healthy_appliance(tmp_path)
    damage(services)
    handlers = AgentHandlers(services, executor=lambda target: target())
    answers = []
    planner = threading.Thread(
        target=lambda: answers.append(handlers.dispatch({"operation": plan})), daemon=True
    )

    planner.start()
    planner.join(timeout=30)

    assert not planner.is_alive(), "planning blocked on the damaged deployment"
    assert answers and answers[0]["plan"]["blockers"] == []


def test_a_transition_path_with_a_nul_is_unreadable_not_an_exception(tmp_path):
    """Status and the Admin operations read the record without the power path's guard."""

    record = admin_transition.read_transition(tmp_path / "ad\x00min" / "pending-transition.json")

    assert record["state"] == admin_transition.STATE_UNREADABLE
    assert not admin_transition.blocks_admin_mutation(record)


def test_the_deployment_is_read_only_from_a_regular_file_of_bounded_size(tmp_path):
    regular = tmp_path / "compose.yml"
    regular.write_text("services: {}\n", encoding="utf-8")
    linked = tmp_path / "linked.yml"
    linked.symlink_to(regular)
    fifo = tmp_path / "fifo.yml"
    os.mkfifo(fifo)
    large = tmp_path / "large.yml"
    large.write_bytes(b"#" * (admin_transition.MAX_DEPLOYMENT_FILE_BYTES + 1))
    undecodable = tmp_path / "latin1.yml"
    undecodable.write_bytes(b"image: \xff\n")

    read = admin_transition._regular_text

    assert read(regular) == "services: {}\n"
    assert read(linked) == "services: {}\n"
    assert _read_without_blocking(read, fifo) == ""
    assert read(large) == ""
    assert read(undecodable) == ""
    assert read(tmp_path / "missing.yml") == ""
    assert read(tmp_path) == ""


def test_a_file_of_exactly_the_bound_is_read_and_a_pipe_holding_text_is_not(tmp_path):
    exact = tmp_path / "exact.yml"
    exact.write_bytes(b"#" * admin_transition.MAX_DEPLOYMENT_FILE_BYTES)
    read_end, write_end = os.pipe()
    os.write(write_end, b"services: {}\n")
    os.close(write_end)
    try:
        fed = _read_without_blocking(admin_transition._regular_text, Path(f"/proc/self/fd/{read_end}"))
    finally:
        os.close(read_end)

    assert admin_transition._regular_text(exact) == "#" * admin_transition.MAX_DEPLOYMENT_FILE_BYTES
    assert fed == ""


def _read_without_blocking(read, fifo):
    """``read(fifo)``, failing instead of hanging when the reader blocks on the FIFO.

    A reader that blocks is released by opening the FIFO's write end, so the
    suite finishes either way; the protected interleaving is "read returns
    before anything writes".
    """

    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("text", read(fifo)), daemon=True)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        release = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(release)
        worker.join(timeout=10)
        pytest.fail(f"reading {fifo.name} blocked")
    return result["text"]
