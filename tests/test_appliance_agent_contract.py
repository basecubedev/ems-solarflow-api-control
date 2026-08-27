# SPDX-License-Identifier: AGPL-3.0-or-later
"""The agent's fixed operation allowlist.

This is the privilege boundary between the unprivileged web process and root.
Anything that is not a declared operation with declared, typed fields must be
refused before a handler runs — no shell command, no path, no image reference,
no second concurrent mutation.
"""

import json
import os
import socket
import threading

import pytest

from appliance.agent import AgentError, AgentHandlers, AgentServer
from appliance.agent_client import AgentCallError, AgentClient
from appliance.protocol import (
    MUTATING_OPERATIONS,
    OPERATIONS,
    READ_ONLY_OPERATIONS,
    ProtocolError,
    ValidationContext,
    validate_request,
)
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    appliance_config,
    build_test_services,
)

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]


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


@pytest.fixture
def context():
    return ValidationContext(appliance_config())


# --- allowlist shape -------------------------------------------------------


def test_every_operation_name_is_lowercase_and_dotted():
    for name in OPERATIONS:
        assert name == name.lower()
        assert " " not in name


def test_read_only_operations_never_take_the_mutation_lock():
    for spec in READ_ONLY_OPERATIONS:
        assert spec.mutating is False
        assert spec.takes_lock is False


# Mutating operations that deliberately do not serialise against host changes:
# operation control drives the lock itself, and an authentication audit append
# must still succeed while an install is running.
# Bookkeeping, not host mutation: neither writes anything a running operation
# could conflict with, so neither may block on the mutation lock.
# The lock serialises *host* mutations -- an image write, an apt run. Setting a
# password is not one, and must not queue behind one: an operator who cannot set
# their first password while an OS update runs is locked out of their own box.
LOCK_EXEMPT_MUTATIONS = frozenset(
    {"audit.record_web_event", "auth.create", "auth.change"}
)


def test_mutating_plan_operations_take_the_mutation_lock():
    for spec in MUTATING_OPERATIONS:
        assert spec.mutating is True
        if spec.name.startswith("operations.") or spec.name in LOCK_EXEMPT_MUTATIONS:
            continue
        assert spec.takes_lock is True, spec.name


def test_the_audit_append_never_blocks_on_a_running_host_mutation():
    spec = OPERATIONS["audit.record_web_event"]
    assert spec.mutating is True
    assert spec.takes_lock is False


def test_no_operation_declares_a_command_or_path_field():
    forbidden = {"command", "argv", "shell", "path", "file", "image", "repository", "registry"}
    for spec in OPERATIONS.values():
        assert not forbidden & {field.name for field in spec.fields}, spec.name


# --- rejection contract ----------------------------------------------------


def test_unknown_operation_is_rejected(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request({"operation": "admin.destroy"}, context)
    assert excinfo.value.code == "unknown_operation"


def test_a_shell_command_is_not_an_operation(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request({"command": "docker pull evil/image"}, context)
    assert excinfo.value.code == "invalid_request"


def test_a_command_field_smuggled_into_a_valid_operation_is_rejected(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.0.0",
             "command": "rm -rf /"},
            context,
        )
    assert excinfo.value.code == "unknown_field"
    assert excinfo.value.field == "command"


def test_an_arbitrary_path_field_is_rejected(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request({"operation": "logs.read", "source": "audit", "path": "/etc/shadow"}, context)
    assert excinfo.value.code == "unknown_field"


def test_an_arbitrary_image_repository_cannot_be_requested(context):
    # The repository is host configuration; the request may only carry a tag.
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {"operation": "admin.plan_install", "channel": "exact",
             "tag": "ghcr.io/attacker/image:v1.0.0"},
            context,
        )
    assert excinfo.value.code == "invalid_release_tag"


def test_malformed_arguments_are_rejected(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request({"operation": "admin.plan_install", "channel": "sideways"}, context)
    assert excinfo.value.code == "invalid_release_channel"


def test_missing_required_field_is_rejected(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request({"operation": "admin.plan_install"}, context)
    assert excinfo.value.code == "missing_field"
    assert excinfo.value.field == "channel"


def test_a_non_object_request_is_rejected(context):
    with pytest.raises(ProtocolError):
        validate_request(["admin.plan_install"], context)


def test_prerelease_tags_need_an_explicit_host_opt_in(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.0.0-rc1"}, context
        )
    assert excinfo.value.code == "prerelease_not_allowed"

    permissive = ValidationContext(
        appliance_config(
            images=appliance_config().images.__class__(
                repositories=(ADMIN_REPOSITORY,), allow_prerelease=True
            )
        )
    )
    spec, args = validate_request(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.0.0-rc1"}, permissive
    )
    assert args["tag"] == "v1.0.0-rc1"


def test_account_must_be_appliance_managed(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {"operation": "ssh.plan_revoke_all", "account": "root"}, context
        )
    assert excinfo.value.code == "account_not_allowed"


def test_valid_operation_is_accepted(context):
    spec, args = validate_request(
        {"operation": "logs.read", "source": "audit", "lines": 50}, context
    )
    assert spec.name == "logs.read"
    assert args == {"source": "audit", "lines": 50}


def test_optional_fields_get_their_declared_defaults(context):
    _, args = validate_request({"operation": "logs.read", "source": "audit"}, context)
    assert args["lines"] == 200


# --- web audit events ------------------------------------------------------


@pytest.mark.parametrize(
    "event", ["login.success", "login.failure", "logout", "password.change", "password.reset"]
)
def test_every_allowed_web_audit_event_validates(context, event):
    _, args = validate_request(
        {"operation": "audit.record_web_event", "event": event, "result": "success"}, context
    )
    assert args["event"] == event
    assert args["reason"] == ""


@pytest.mark.parametrize(
    "event", ["admin.install", "system.reboot", "arbitrary", "login.success.extra", ""]
)
def test_an_audit_event_outside_the_fixed_set_is_rejected(context, event):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {"operation": "audit.record_web_event", "event": event, "result": "success"}, context
        )
    assert excinfo.value.code == "invalid_audit_event"


def test_a_free_form_audit_reason_is_rejected(context):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {
                "operation": "audit.record_web_event",
                "event": "login.failure",
                "result": "failure",
                "reason": "password=hunter2",
            },
            context,
        )
    assert excinfo.value.code == "invalid_audit_reason"


@pytest.mark.parametrize("field", ["password", "session", "csrf_token", "public_key", "detail"])
def test_no_extra_audit_field_can_be_smuggled_in(context, field):
    with pytest.raises(ProtocolError) as excinfo:
        validate_request(
            {
                "operation": "audit.record_web_event",
                "event": "login.success",
                "result": "success",
                field: "value",
            },
            context,
        )
    assert excinfo.value.code == "unknown_field"


def test_a_web_audit_event_is_written_to_the_audit_log(handlers, services):
    payload = handlers.dispatch(
        {"operation": "audit.record_web_event", "event": "login.failure", "result": "failure",
         "reason": "invalid_password"},
        actor="appliance-admin",
        source_ip="192.168.1.20",
    )
    assert payload["recorded"] is True

    entry = services.audit.tail()[-1]
    assert entry["action"] == "login.failure"
    assert entry["result"] == "failure"
    assert entry["target"] == "invalid_password"
    assert entry["source_ip"] == "192.168.1.20"
    assert entry["user"] == "appliance-admin"


def test_an_audit_event_creates_no_operation_record(handlers, services):
    handlers.dispatch(
        {"operation": "audit.record_web_event", "event": "logout", "result": "success"}
    )
    assert services.operations.list() == []
    assert services.operations.active() is None


def test_a_hostile_source_address_never_reaches_the_audit_log(tmp_path, services):
    server = AgentServer(
        services,
        socket_path=tmp_path / "agent.sock",
        handlers=AgentHandlers(services, executor=lambda target: target()),
        allowed_uids=(os.getuid(),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AgentClient(server.socket_path, timeout=5)
        client.call(
            "audit.record_web_event",
            actor="appliance-admin\nadmin.install",
            source_ip="10.0.0.1 password=secret",
            event="login.success",
            result="success",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    entry = services.audit.tail()[-1]
    assert entry["source_ip"] == ""
    assert entry["user"] == ""
    assert "secret" not in json.dumps(entry)


# --- dispatch behaviour ----------------------------------------------------


def test_read_only_dispatch_needs_no_operation_record(handlers, services):
    payload = handlers.dispatch({"operation": "status.get"})
    assert payload["system"]["status"] == "ok"
    assert services.operations.list() == []


def test_concurrent_mutation_is_refused(handlers):
    handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "restart"})
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "stop"})
    assert getattr(excinfo.value, "code", "") == "operation_conflict"


def test_read_only_calls_stay_available_during_a_mutation(handlers):
    handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "restart"})
    assert handlers.dispatch({"operation": "admin.get"})["installed"] is True
    assert handlers.dispatch({"operation": "operations.list"})["active"] is not None


def test_execution_requires_the_confirmation_token(handlers):
    planned = handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "restart"})
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {
                "operation": "operations.execute",
                "operation_id": planned["operation"]["operation_id"],
                "confirmation_token": "wrong-token-0000000000",
            }
        )
    assert getattr(excinfo.value, "code", "") == "confirmation_token_mismatch"


def test_a_stale_plan_cannot_start_a_second_operation(handlers):
    first = handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "restart"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": first["operation"]["operation_id"],
            "confirmation_token": first["confirmation_token"],
        }
    )
    handlers.dispatch(
        {"operation": "operations.acknowledge", "operation_id": first["operation"]["operation_id"]}
    )
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {
                "operation": "operations.execute",
                "operation_id": first["operation"]["operation_id"],
                "confirmation_token": first["confirmation_token"],
            }
        )
    assert getattr(excinfo.value, "code", "") == "invalid_operation_transition"


def test_a_failed_plan_releases_the_lock(handlers):
    with pytest.raises(AgentError):
        handlers.dispatch({"operation": "admin.plan_install", "channel": "exact", "tag": "v9.9.9"})
    assert handlers.dispatch({"operation": "operations.list"})["active"] is None


def test_sensitive_operations_are_audited(handlers, services):
    planned = handlers.dispatch({"operation": "admin.plan_lifecycle", "action": "restart"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    actions = [entry["action"] for entry in services.audit.tail()]
    assert actions == []  # a lifecycle restart is audited by the web layer, not the plan table

    install_planned = None
    services.operations.acknowledge(planned["operation"]["operation_id"])
    with pytest.raises(AgentError):
        install_planned = handlers.dispatch(
            {"operation": "admin.plan_install", "channel": "exact", "tag": "v9.9.9"}
        )
    assert install_planned is None
    assert "admin.install" in [entry["action"] for entry in services.audit.tail()]


# --- socket transport ------------------------------------------------------


def test_socket_refuses_a_peer_that_is_not_root_or_the_web_user(tmp_path, services):
    server = AgentServer(
        services,
        socket_path=tmp_path / "agent.sock",
        handlers=AgentHandlers(services, executor=lambda target: target()),
        allowed_uids=(999999,),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AgentClient(server.socket_path, timeout=5)
        with pytest.raises(AgentCallError) as excinfo:
            client.call("status.get")
        assert excinfo.value.code == "peer_not_allowed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_refusal_that_lost_the_write_race_is_still_delivered(tmp_path):
    """The refusal is written before the request is read, so the close races the
    caller's write. Here the race is decided rather than waited for: the agent
    has already answered and closed before the client sends its first byte, so
    ``sendall`` cannot win. What used to happen then was ``BrokenPipeError`` ->
    "the appliance agent closed the connection", and the reason -- already
    sitting in this socket's receive buffer -- was never read.
    """

    path = tmp_path / "agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    answered = threading.Event()
    refusal = {
        "ok": False,
        "error": {
            "code": "peer_not_allowed",
            "message": "this local user may not use the appliance agent",
        },
    }

    def refuse_without_reading():
        connection, _ = listener.accept()
        connection.sendall(json.dumps(refusal).encode("utf-8") + b"\n")
        connection.close()
        answered.set()

    def connect_after_the_agent_has_gone():
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(path))
        assert answered.wait(10), "the agent never answered"
        return connection

    server = threading.Thread(target=refuse_without_reading, daemon=True)
    server.start()
    try:
        client = AgentClient(path, timeout=10, connect=connect_after_the_agent_has_gone)
        with pytest.raises(AgentCallError) as excinfo:
            client.call("status.get")

        assert excinfo.value.code == "peer_not_allowed"
    finally:
        server.join(timeout=10)
        listener.close()


def test_socket_serves_an_allowed_peer(tmp_path, services):
    import os

    server = AgentServer(
        services,
        socket_path=tmp_path / "agent.sock",
        handlers=AgentHandlers(services, executor=lambda target: target()),
        allowed_uids=(os.getuid(),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AgentClient(server.socket_path, timeout=5)
        assert client.call("system.get")["appliance_version"]
        with pytest.raises(AgentCallError) as excinfo:
            client.call("admin.destroy")
        assert excinfo.value.code == "unknown_operation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_socket_is_not_a_network_listener(tmp_path, services):
    server = AgentServer(services, socket_path=tmp_path / "agent.sock")
    try:
        assert server.socket.family == socket.AF_UNIX
        assert str(server.socket_path).startswith(str(tmp_path))
    finally:
        server.server_close()


def test_oversized_request_is_refused(tmp_path, services):
    import os

    server = AgentServer(
        services,
        socket_path=tmp_path / "agent.sock",
        handlers=AgentHandlers(services, executor=lambda target: target()),
        allowed_uids=(os.getuid(),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(str(server.socket_path))
        payload = json.dumps({"operation": "status.get", "pad": "x" * 200000}) + "\n"
        connection.sendall(payload.encode("utf-8"))
        reply = json.loads(connection.recv(65536).decode("utf-8"))
        connection.close()
        assert reply["ok"] is False
        assert reply["error"]["code"] == "request_too_large"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_malformed_json_is_refused(tmp_path, services):
    import os

    server = AgentServer(
        services,
        socket_path=tmp_path / "agent.sock",
        handlers=AgentHandlers(services, executor=lambda target: target()),
        allowed_uids=(os.getuid(),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(str(server.socket_path))
        connection.sendall(b"{not json\n")
        reply = json.loads(connection.recv(65536).decode("utf-8"))
        connection.close()
        assert reply["error"]["code"] == "invalid_request"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_no_host_process_is_started_by_a_refused_request(handlers, services):
    services.host.calls.clear()
    with pytest.raises(ProtocolError):
        handlers.dispatch({"operation": "admin.plan_install", "channel": "exact", "tag": "latest"})
    assert services.host.calls == []


# --- how long a caller waits ------------------------------------------------


def test_a_planner_that_pulls_an_image_may_take_longer_than_the_default():
    """The 30s default cut off planners that pull, and the caller giving up
    left the operation it had already created holding the lock."""

    from appliance.agent_client import DEFAULT_TIMEOUT, operation_timeout

    for name in ("admin.plan_install", "admin.plan_rollback", "manager.plan_update"):
        assert operation_timeout(name) > DEFAULT_TIMEOUT, name


def test_a_cheap_read_only_call_keeps_the_short_timeout():
    """Cheap is the rule; the exceptions are the calls that shell out to apt or
    nmcli, and those declare their own budget rather than inheriting this one."""

    from appliance.agent_client import DEFAULT_TIMEOUT, operation_timeout

    for name in ("admin.get", "manager.status", "docker.get", "ssh.get"):
        assert operation_timeout(name) == DEFAULT_TIMEOUT, name


def test_the_registry_is_the_only_place_a_timeout_is_declared():
    """One owner: the spec that already carries mutating and takes_lock."""

    from appliance import protocol
    from appliance.agent_client import operation_timeout

    for name, spec in protocol.OPERATIONS.items():
        assert operation_timeout(name) == spec.timeout_seconds, name


def test_an_unreachable_agent_still_fails_fast(tmp_path):
    """Patience for a working agent must not become patience for a dead one."""

    from appliance.agent_client import AgentClient, AgentUnavailableError

    client = AgentClient(tmp_path / "missing.sock")

    with pytest.raises(AgentUnavailableError):
        client.call("admin.plan_install", channel="latest_stable", reinstall=False)


def test_no_operation_times_out_below_what_it_may_legitimately_spend():
    """A client timeout under the server's own subprocess budget abandons a call
    that is still running, and for a planner it strands the operation lock."""

    from appliance import protocol

    slow = {
        "status.get": protocol.SLOW_PROBE_TIMEOUT,
        "updates.get": protocol.SLOW_PROBE_TIMEOUT,
        "support.plan_archive": protocol.SLOW_PROBE_TIMEOUT,
        "network.get": protocol.WIFI_OPERATION_TIMEOUT,
        "network.wifi.scan": protocol.WIFI_OPERATION_TIMEOUT,
        "network.wifi.plan": protocol.WIFI_OPERATION_TIMEOUT,
    }
    specs = protocol.OPERATIONS

    for name, expected in slow.items():
        assert name in specs, name
        assert specs[name].timeout_seconds >= expected, name
        assert specs[name].timeout_seconds > protocol.DEFAULT_OPERATION_TIMEOUT, name


def test_setting_a_password_never_queues_behind_a_host_mutation():
    """An operator who cannot set their first password while an OS update runs
    is locked out of the only interface they have."""

    from appliance import protocol

    for name in ("auth.create", "auth.change"):
        spec = protocol.OPERATIONS[name]
        assert spec.mutating is True
        assert spec.takes_lock is False, name


def test_a_password_never_travels_as_an_ordinary_argument():
    """It crosses the agent socket, so it has to be a kind the validator knows
    not to alter -- stripping whitespace would change a password into one the
    operator cannot type back."""

    from appliance import protocol
    from appliance.validation import validate_secret

    for name in ("auth.verify", "auth.create", "auth.change"):
        for field in protocol.OPERATIONS[name].fields:
            if "password" in field.name or field.name == "confirmation":
                assert field.kind == protocol.KIND_SECRET, (name, field.name)

    assert validate_secret("  padded  ") == "  padded  "
