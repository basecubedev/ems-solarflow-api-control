# SPDX-License-Identifier: AGPL-3.0-or-later
"""The durable operation model.

The operation record — not a thread, a request or a browser tab — is the
authority for what is running. These tests pin the state machine, the
single-active-mutation rule, durability across a restart, and the confirmation
token that binds a confirmation to the plan the operator saw.
"""

import json

import pytest

from appliance.operations import (
    STATE_AWAITING_CONFIRMATION,
    STATE_CANCELLED,
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    STATE_PLANNED,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_VERIFYING,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransitionError,
    OperationConflictError,
    OperationError,
    OperationStore,
    UnknownOperationError,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation]


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        self.now += 1
        return self.now


def store_at(tmp_path, **kwargs):
    return OperationStore(tmp_path / "operations", time_fn=Clock(), **kwargs)


def planned(store, kind="admin.install"):
    operation = store.create(kind, {"tag": "v1.0.0"})
    return store.await_confirmation(operation.operation_id, {"type": kind})


# --- state machine ---------------------------------------------------------


def test_declared_states_and_transitions_are_consistent():
    for state, targets in TRANSITIONS.items():
        for target in targets:
            assert target in TRANSITIONS, f"{state} -> {target} is not a declared state"
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_plan_confirm_run_succeed(tmp_path):
    store = store_at(tmp_path)
    operation = store.create("admin.install", {"tag": "v1.0.0"})
    assert operation.state == STATE_PLANNED

    awaiting = store.await_confirmation(operation.operation_id, {"type": "admin.install"})
    assert awaiting.state == STATE_AWAITING_CONFIRMATION

    running = store.confirm(operation.operation_id, awaiting.confirmation_token)
    assert running.state == STATE_RUNNING

    verifying = store.advance(operation.operation_id, "waiting_for_health", state=STATE_VERIFYING)
    assert verifying.state == STATE_VERIFYING

    done = store.finish(operation.operation_id, STATE_SUCCEEDED, result={"installed": "v1.0.0"})
    assert done.state == STATE_SUCCEEDED
    assert done.terminal is True


def test_rollback_path_is_a_declared_transition(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.advance(record.operation_id, "rolling_back", state=STATE_ROLLING_BACK)
    rolled = store.finish(record.operation_id, STATE_ROLLED_BACK)
    assert rolled.state == STATE_ROLLED_BACK


def test_undeclared_transition_is_refused(tmp_path):
    store = store_at(tmp_path)
    operation = store.create("admin.install")
    with pytest.raises(InvalidTransitionError):
        store.finish(operation.operation_id, STATE_SUCCEEDED)


def test_terminal_operation_cannot_be_restarted(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.finish(record.operation_id, STATE_SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        store.advance(record.operation_id, "running", state=STATE_RUNNING)


# --- confirmation token ----------------------------------------------------


def test_confirmation_requires_the_token_of_this_plan(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    with pytest.raises(OperationError) as excinfo:
        store.confirm(record.operation_id, "a-different-token-000000")
    assert excinfo.value.code == "confirmation_token_mismatch"
    assert store.get(record.operation_id).state == STATE_AWAITING_CONFIRMATION


def test_token_is_not_exposed_in_the_default_record(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    assert "confirmation_token" not in record.to_dict()
    assert store.get(record.operation_id).confirmation_token == ""


def test_unknown_operation_id_is_refused(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(UnknownOperationError):
        store.get("f" * 32)


# --- single active mutation ------------------------------------------------


def test_a_second_conflicting_mutation_is_refused(tmp_path):
    store = store_at(tmp_path)
    first = store.create("admin.install")
    with pytest.raises(OperationConflictError) as excinfo:
        store.create("updates.install")
    assert excinfo.value.active_id == first.operation_id
    assert excinfo.value.active_type == "admin.install"


def test_a_finished_operation_releases_the_lock(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.finish(record.operation_id, STATE_SUCCEEDED)
    assert store.create("updates.install").state == STATE_PLANNED


def test_a_recoverable_failure_still_blocks_a_second_mutation(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.finish(record.operation_id, STATE_FAILED_RECOVERABLE)
    with pytest.raises(OperationConflictError):
        store.create("updates.install")


def test_cancelling_a_recoverable_failure_releases_the_lock(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.finish(record.operation_id, STATE_FAILED_RECOVERABLE)
    store.cancel(record.operation_id)
    assert store.create("updates.install").state == STATE_PLANNED


def test_a_recoverable_failure_can_be_retried_with_the_token(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.finish(record.operation_id, STATE_FAILED_RECOVERABLE)
    retried = store.retry(record.operation_id, record.confirmation_token)
    assert retried.state == STATE_RUNNING


# --- durability ------------------------------------------------------------


def test_operations_survive_a_new_store_instance(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.advance(record.operation_id, "pulling_image", detail="v1.0.0")

    reopened = store_at(tmp_path)
    restored = reopened.get(record.operation_id)
    assert restored.state == STATE_RUNNING
    assert restored.stage == "pulling_image"
    assert restored.progress[-1]["stage"] == "pulling_image"


def test_a_restart_turns_a_running_operation_into_a_visible_failure(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)

    reopened = store_at(tmp_path)
    recovered = reopened.recover_interrupted()
    assert [item.state for item in recovered] == [STATE_FAILED_RECOVERABLE]
    assert reopened.get(record.operation_id).error["code"] == "operation_interrupted"


def test_a_restart_expires_an_unconfirmed_plan(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)

    reopened = store_at(tmp_path)
    reopened.recover_interrupted()
    assert reopened.get(record.operation_id).state == STATE_CANCELLED
    assert reopened.create("updates.install").state == STATE_PLANNED


def test_a_corrupt_record_is_reported_not_ignored(tmp_path):
    store = store_at(tmp_path)
    operation = store.create("admin.install")
    (tmp_path / "operations" / f"{operation.operation_id}.json").write_text("{", encoding="utf-8")
    with pytest.raises(OperationError) as excinfo:
        store.get(operation.operation_id)
    assert excinfo.value.code == "operation_record_invalid"


def test_records_are_written_as_readable_json(tmp_path):
    store = store_at(tmp_path)
    operation = store.create("admin.install", {"tag": "v1.0.0"})
    payload = json.loads(
        (tmp_path / "operations" / f"{operation.operation_id}.json").read_text(encoding="utf-8")
    )
    assert payload["type"] == "admin.install"
    assert payload["requested_target"]["tag"] == "v1.0.0"


# --- acknowledgement -------------------------------------------------------


def test_terminal_results_stay_visible_until_acknowledged(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    store.confirm(record.operation_id, record.confirmation_token)
    store.finish(record.operation_id, STATE_FAILED_TERMINAL, error={"code": "boom", "message": "x"})

    assert [item.operation_id for item in store.unacknowledged()] == [record.operation_id]
    store.acknowledge(record.operation_id)
    assert store.unacknowledged() == []


def test_only_a_finished_operation_can_be_acknowledged(tmp_path):
    store = store_at(tmp_path)
    record = planned(store)
    with pytest.raises(OperationError) as excinfo:
        store.acknowledge(record.operation_id)
    assert excinfo.value.code == "operation_not_terminal"


def test_secret_looking_target_values_never_leave_the_store(tmp_path):
    store = store_at(tmp_path)
    operation = store.create("network.wifi", {"ssid": "HomeNet", "passphrase": "hunter2hunter2"})
    assert operation.to_dict()["requested_target"]["passphrase"] == "***"


def test_an_unexpected_planner_error_still_releases_the_operation_lock(tmp_path):
    """A planner that raises something nobody anticipated must not wedge the
    appliance: the record it created holds the lock every later operation needs.
    """

    from tests.helpers.appliance import build_test_services
    from appliance.agent import AgentHandlers

    services = build_test_services(tmp_path)

    def explode(*args, **kwargs):
        raise TypeError("a bug nobody wrote a handler for")

    services.admin.plan_repair = explode
    handlers = AgentHandlers(services, executor=lambda target: target())

    with pytest.raises(TypeError):
        handlers.dispatch({"operation": "admin.plan_repair"})

    assert services.operations.active() is None
