# SPDX-License-Identifier: AGPL-3.0-or-later
"""A plan that outlasts the browser is handed to the console that started it.

An Admin install plan may pull for 35 minutes and an update plan refresh an
index for 22, while Firefox gives up on a response after five. The agent still
finishes the plan into ``awaiting_confirmation`` with the operation lock held,
and its confirmation token went to a caller that is gone. The console sends a
random request id with every plan; the agent keeps its digest on the operation
record, and hands the plan and its token to whoever presents that id again, for
a plan waiting for confirmation and nothing else.
"""

import json

import pytest

from appliance.agent import AgentError, AgentHandlers
from appliance.operations import OperationError
from appliance.protocol import OPERATIONS, ProtocolError
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    build_test_services,
)

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

PLAN_REQUEST_FIELD = "plan_request"
REQUEST = "0123456789abcdef0123456789abcdef"
OTHER_REQUEST = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def services(tmp_path):
    built = build_test_services(tmp_path)
    built.host.write_deployment(tag="v1.0.0")
    built.host.publish_image("v1.0.0")
    built.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    built.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    return built


@pytest.fixture
def handlers(services):
    return AgentHandlers(services, executor=lambda target: target())


def plan(handlers, **fields):
    return handlers.dispatch(
        {"operation": "admin.plan_lifecycle", "action": "restart", **fields},
        actor="appliance-admin",
    )


def resume(handlers, operation_id, request=REQUEST):
    return handlers.dispatch(
        {"operation": "operations.resume_plan", "operation_id": operation_id,
         PLAN_REQUEST_FIELD: request},
        actor="appliance-admin",
    )


def refused_code(call):
    with pytest.raises((AgentError, OperationError, ProtocolError)) as refused:
        call()
    return refused.value.code


def test_a_plan_whose_caller_is_gone_is_handed_to_the_console_that_started_it(
    handlers, services
):
    lost = plan(handlers, **{PLAN_REQUEST_FIELD: REQUEST})
    operation_id = lost["operation"]["operation_id"]

    picked_up = resume(handlers, operation_id)

    assert picked_up["operation"]["operation_id"] == operation_id
    assert picked_up["operation"]["state"] == "awaiting_confirmation"
    assert picked_up["plan"] == lost["plan"]
    assert picked_up["confirmation_token"] == lost["confirmation_token"]
    executed = handlers.dispatch(
        {"operation": "operations.execute", "operation_id": operation_id,
         "confirmation_token": picked_up["confirmation_token"]},
        actor="appliance-admin",
    )
    assert executed["operation"]["state"] != "awaiting_confirmation"


def test_a_plan_is_not_handed_to_a_caller_that_did_not_start_it(handlers):
    operation_id = plan(handlers, **{PLAN_REQUEST_FIELD: REQUEST})["operation"]["operation_id"]

    assert refused_code(lambda: resume(handlers, operation_id, OTHER_REQUEST)) == (
        "plan_request_mismatch"
    )


def test_a_plan_made_without_a_request_id_is_handed_to_nobody(handlers):
    operation_id = plan(handlers)["operation"]["operation_id"]

    assert refused_code(lambda: resume(handlers, operation_id)) == "plan_request_mismatch"


def test_a_plan_still_being_made_is_not_handed_out(handlers, services):
    record = services.operations.create("admin.lifecycle", plan_request=REQUEST)

    assert refused_code(lambda: resume(handlers, record.operation_id)) == (
        "plan_not_awaiting_confirmation"
    )


def test_a_cancelled_plan_is_not_handed_out(handlers):
    operation_id = plan(handlers, **{PLAN_REQUEST_FIELD: REQUEST})["operation"]["operation_id"]
    handlers.dispatch({"operation": "operations.cancel", "operation_id": operation_id})

    assert refused_code(lambda: resume(handlers, operation_id)) == (
        "plan_not_awaiting_confirmation"
    )


def test_the_request_id_is_kept_only_as_a_digest_and_never_shown(handlers, services):
    lost = plan(handlers, **{PLAN_REQUEST_FIELD: REQUEST})
    operation_id = lost["operation"]["operation_id"]

    stored = (services.operations.directory / f"{operation_id}.json").read_text(encoding="utf-8")
    assert REQUEST not in stored
    shown = json.dumps(services.operations.get(operation_id).to_dict())
    assert REQUEST not in shown
    assert "plan_request" not in shown
    assert "plan_request" not in json.dumps(lost["operation"])


def test_a_malformed_request_id_is_refused_before_a_plan_is_made(handlers, services):
    assert refused_code(lambda: plan(handlers, **{PLAN_REQUEST_FIELD: "not hex"})) == (
        "invalid_plan_request"
    )
    assert services.operations.active() is None


def test_every_operation_that_takes_the_lock_accepts_a_request_id():
    """The console sends one with every plan, so a plan that refused the field
    would fail as unknown_field rather than plan."""

    for spec in OPERATIONS.values():
        if spec.takes_lock:
            assert PLAN_REQUEST_FIELD in {field.name for field in spec.fields}, spec.name
