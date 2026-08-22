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
from datetime import datetime, timedelta, timezone

import pytest

from appliance import admin_transition
from appliance.agent import AgentHandlers
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
