# SPDX-License-Identifier: AGPL-3.0-or-later
"""A password rewritten from a container does not reach a root shell.

The shared password file sits in the EMS deployment root, which the EMS and
Admin containers mount read-write. Whoever runs code in one can write a password
of their own into it, or delete it to reopen first-run setup, and then sign in
to the Appliance Manager, whose SSH, shell-access and key actions hand out a
root shell. The agent keeps a root-owned copy of the password the Appliance
Manager itself confirmed, and those actions ask for that one.
"""

import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from appliance import cli, persistent_state
from appliance.agent import AgentHandlers
from appliance.agent_client import InProcessAgentClient
from appliance.auth import (
    RECORD_FIELDS,
    AuthError,
    AuthStore,
    ConfirmedPassword,
    hash_password,
    record_generation,
)
from appliance.protocol import (
    ROOT_ACCESS_PLANS,
    SESSION_GENERATION_FIELD,
    ProtocolError,
    ValidationContext,
    validate_request,
)
from appliance.web import ApplianceWebApp, ApplianceWebServer
from tests.helpers.appliance import appliance_config, build_test_services
from tests.test_appliance_web_api import Client

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

PASSWORD = "the-operators-secret-1"
ELSEWHERE = "set-with-emsctl-secret-2"
FOREIGN = "written-from-a-container-3"
ED25519 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf "
    "appliance-test@example.invalid"
)

ROOT_ACCESS_ROUTES = (
    ("/api/ssh/enable", {}),
    ("/api/ssh/shell-access/enable", {}),
    ("/api/ssh/keys", {"account": "ems-backup", "public_key": ED25519}),
    ("/api/manager/plan-update", {"release_id": "ems-appliance-manager-0.1.0-arm64"}),
    ("/api/manager/plan-revert", {}),
)


@pytest.fixture
def appliance(tmp_path):
    """The real web service in front of the real agent handlers, the way an
    appliance runs them: the web process asks the agent about the password."""

    services = build_test_services(tmp_path)
    services.auth.iterations = 1000
    home = tmp_path / "home" / "ems-backup"
    home.mkdir(parents=True, exist_ok=True)
    services.host.add_account("ems-backup", home)

    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    app = ApplianceWebApp(paths=services.paths, config=services.config, agent=agent)
    server = ApplianceWebServer(app, ("127.0.0.1", 0))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield services, app, Client(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _set_up(client, password=PASSWORD):
    status, payload, _ = client.post(
        "/api/session/setup", {"password": password, "confirmation": password}
    )
    assert status == 200, payload


def _sign_in(client, password):
    client.cookie = client.csrf = ""
    status, payload, _ = client.post("/api/session/login", {"password": password})
    assert status == 200, payload
    return payload


def _written_elsewhere(services, password):
    """What `emsctl dashboard set-password`, the Admin console or any process in
    a container can put into the shared file."""

    record = hash_password(password, iterations=1000)
    services.paths.auth_file.write_text(json.dumps(record), encoding="utf-8")
    return record


def _confirmed(services):
    return ConfirmedPassword(services.paths.confirmed_password_file)


# --- a password written from a container ------------------------------------


@pytest.mark.parametrize(("path", "body"), ROOT_ACCESS_ROUTES)
def test_a_password_written_from_a_container_opens_no_root_access(appliance, path, body):
    services, _, client = appliance
    _set_up(client)
    _written_elsewhere(services, FOREIGN)
    _sign_in(client, FOREIGN)

    status, payload, _ = client.post(path, body)

    assert status == 403, payload
    assert payload["error"] == "password_unconfirmed"
    assert services.operations.list() == [], "a plan was made before the refusal"


CLOSING_ROUTES = (
    ("/api/ssh/disable", {}),
    ("/api/ssh/shell-access/disable", {}),
    ("/api/ssh/keys/remove-plan", {"account": "ems-backup", "fingerprint": "SHA256:" + "A" * 43}),
    ("/api/ssh/keys/revoke", {"account": "ems-backup"}),
)


@pytest.mark.parametrize(("path", "body"), CLOSING_ROUTES)
def test_closing_access_needs_no_confirmation(appliance, path, body):
    """Switching SSH or shell access off and taking keys away take nothing from
    the operator, and an unconfirmed session must still be able to close a door.
    Whatever the plan says about the host, it is not refused for the password."""

    services, _, client = appliance
    _set_up(client)
    _written_elsewhere(services, FOREIGN)
    _sign_in(client, FOREIGN)

    status, payload, _ = client.post(path, body)

    assert payload.get("error") != "password_unconfirmed", payload
    assert status != 403, payload


def test_a_refused_root_access_plan_is_audited(appliance):
    services, _, client = appliance
    _set_up(client)
    _written_elsewhere(services, FOREIGN)
    _sign_in(client, FOREIGN)

    client.post("/api/ssh/enable", {})

    denied = [
        entry
        for entry in services.audit.tail()
        if entry["action"] == "ssh.service" and entry["result"] == "denied"
    ]
    assert denied, services.audit.tail()


def test_a_deleted_file_does_not_reopen_first_run_setup(appliance):
    services, _, client = appliance
    _set_up(client)
    services.paths.auth_file.unlink()
    client.cookie = client.csrf = ""

    session = client.get("/api/session")[1]
    status, payload, _ = client.post(
        "/api/session/setup", {"password": FOREIGN, "confirmation": FOREIGN}
    )

    assert session["password_configured"] is True
    assert session["password_file_missing"] is True
    assert status == 409, payload
    assert payload["error"] == "password_already_configured"
    assert not services.paths.auth_file.exists()


def test_the_agent_refuses_first_run_setup_too(tmp_path):
    """The web service asks first, but the agent is the boundary."""

    services = build_test_services(tmp_path)
    services.auth.iterations = 1000
    services.auth.create(PASSWORD, PASSWORD)
    services.paths.auth_file.unlink()

    with pytest.raises(AuthError) as error:
        services.auth.create(FOREIGN, FOREIGN)

    assert error.value.code == "password_already_configured"
    assert "password-reset" in error.value.message


class _SwapAfterVerify:
    """Puts a record back into the shared file the moment the agent has
    answered a sign-in -- the interleaving a container writing in a loop wins."""

    def __init__(self, inner, swap):
        self.inner = inner
        self.swap = swap

    def call(self, operation, **fields):
        answer = self.inner.call(operation, **fields)
        if operation == "auth.verify" and answer.get("ok"):
            self.swap()
        return answer

    def available(self):
        return True


def test_a_session_is_bound_to_the_record_its_password_matched(appliance):
    """Protected interleaving: the sign-in is verified against a record written
    from a container, and the operator's own record is put back before the web
    service asks which generation to bind the session to. Bound to that second
    answer, the session would carry the confirmed generation and pass the
    root-access check with a password the appliance never confirmed."""

    services, app, client = appliance
    _set_up(client)
    app.sessions.destroy_all()
    own = services.paths.auth_file.read_text(encoding="utf-8")
    foreign = _written_elsewhere(services, FOREIGN)

    swapping = _SwapAfterVerify(
        app.agent, lambda: services.paths.auth_file.write_text(own, encoding="utf-8")
    )
    app.agent = swapping
    app.auth.agent = swapping
    _sign_in(client, FOREIGN)

    generations = {session.generation for session in app.sessions.sessions.values()}
    assert generations == {record_generation(foreign)}

    status, _, _ = client.post("/api/ssh/enable", {})
    assert status == 401
    assert services.operations.list() == []


class _SwapAfter:
    """Writes a record into the shared file the moment the agent has answered
    one operation -- the interleaving a container writing in a loop wins."""

    def __init__(self, inner, operation, swap):
        self.inner = inner
        self.operation = operation
        self.swap = swap

    def call(self, operation, **fields):
        answer = self.inner.call(operation, **fields)
        if operation == self.operation:
            self.swap()
        return answer

    def available(self):
        return True


def test_the_first_password_session_is_bound_to_the_record_it_wrote(appliance):
    """Protected interleaving: a container writes its own record right after the
    agent stored the first password. Bound to a second read, the session would
    carry the foreign generation, and confirming it with the password just set
    would make the foreign record the confirmed one."""

    services, app, client = appliance
    foreign = hash_password(FOREIGN, iterations=1000)
    swapping = _SwapAfter(
        app.agent,
        "auth.create",
        lambda: services.paths.auth_file.write_text(json.dumps(foreign), encoding="utf-8"),
    )
    app.agent = swapping
    app.auth.agent = swapping

    _set_up(client)

    generations = {session.generation for session in app.sessions.sessions.values()}
    assert generations == {_confirmed(services).generation()}
    assert record_generation(foreign) not in generations


def test_a_first_password_whose_confirmation_cannot_be_written_is_audited(
    appliance, monkeypatch
):
    """The password is in force from that moment on, so it must not be set
    without a trace even though the request failed."""

    import errno

    from appliance import auth as appliance_auth

    services, _, client = appliance

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(appliance_auth, "atomic_write", full)
    status, payload, _ = client.post(
        "/api/session/setup", {"password": PASSWORD, "confirmation": PASSWORD}
    )

    assert status == 503, payload
    assert payload["error"] == "confirmed_password_unwritten"
    assert services.paths.auth_file.exists()
    assert [
        entry["target"]
        for entry in services.audit.tail()
        if entry["action"] == "password.change" and entry["result"] == "failure"
    ] == ["confirmed_password_unwritten"]


# --- a password set elsewhere by the operator -------------------------------


def test_a_password_set_elsewhere_signs_in_and_waits_for_the_previous_one(appliance):
    services, _, client = appliance
    _set_up(client)
    _written_elsewhere(services, ELSEWHERE)

    signed_in = _sign_in(client, ELSEWHERE)
    assert signed_in["password_confirmed"] is False
    assert client.get("/api/session")[1]["password_confirmed"] is False
    assert client.post("/api/ssh/enable", {})[0] == 403

    status, payload, _ = client.post("/api/settings/password/confirm", {"password": "not-it"})
    assert status == 403, payload
    assert payload["error"] == "previous_password_invalid"

    status, payload, _ = client.post("/api/settings/password/confirm", {"password": PASSWORD})
    assert status == 200, payload

    assert client.get("/api/status")[0] == 200, "confirming must not sign the session out"
    assert client.get("/api/session")[1]["password_confirmed"] is True
    status, payload, _ = client.post("/api/ssh/enable", {})
    assert status == 200, payload
    assert _confirmed(services).verify(ELSEWHERE)


def test_a_wrong_previous_password_counts_like_a_wrong_sign_in(appliance):
    services, app, client = appliance
    _set_up(client)
    _written_elsewhere(services, ELSEWHERE)
    _sign_in(client, ELSEWHERE)

    for _ in range(app.rate_limiter.max_failures):
        assert client.post("/api/settings/password/confirm", {"password": "guess"})[0] == 403
    status, payload, _ = client.post("/api/settings/password/confirm", {"password": PASSWORD})

    assert status == 429, payload
    assert payload["error"] == "login_rate_limited"
    failures = [
        entry
        for entry in services.audit.tail()
        if entry["action"] == "password.confirm" and entry["result"] == "failure"
    ]
    assert len(failures) == app.rate_limiter.max_failures


def test_a_confirmation_audits_its_success(appliance):
    services, _, client = appliance
    _set_up(client)
    _written_elsewhere(services, ELSEWHERE)
    _sign_in(client, ELSEWHERE)

    client.post("/api/settings/password/confirm", {"password": PASSWORD})

    assert any(
        entry["action"] == "password.confirm" and entry["result"] == "success"
        for entry in services.audit.tail()
    )


def test_confirming_takes_only_the_record_the_session_signed_in_with(tmp_path):
    """Protected interleaving: a session signed in with the operator's new
    password asks for confirmation, and a container has written its own record
    in the meantime. The previous password is right, but the record now in the
    file is not the one the session signed in with, so it is not adopted."""

    services = build_test_services(tmp_path)
    services.auth.iterations = 1000
    services.auth.create(PASSWORD, PASSWORD)
    elsewhere = _written_elsewhere(services, ELSEWHERE)
    _written_elsewhere(services, FOREIGN)
    before = _confirmed(services).generation()

    with pytest.raises(AuthError) as error:
        services.auth.confirm(PASSWORD, record_generation(elsewhere))

    assert error.value.code == "password_changed"
    assert _confirmed(services).generation() == before


def test_a_record_changed_during_confirmation_is_refused_and_audited(appliance):
    """Protected interleaving: the previous password is right, and a container
    writes its own record while the agent is asked to confirm. The refusal is
    the sign of that, so it is audited, and it costs the operator no attempt."""

    services, app, client = appliance
    _set_up(client)
    _written_elsewhere(services, ELSEWHERE)
    _sign_in(client, ELSEWHERE)
    foreign = hash_password(FOREIGN, iterations=1000)

    class _SwapBeforeConfirm:
        def __init__(self, inner):
            self.inner = inner

        def call(self, operation, **fields):
            if operation == "auth.confirm":
                services.paths.auth_file.write_text(json.dumps(foreign), encoding="utf-8")
            return self.inner.call(operation, **fields)

        def available(self):
            return True

    swapping = _SwapBeforeConfirm(app.agent)
    app.agent = swapping
    app.auth.agent = swapping

    status, payload, _ = client.post("/api/settings/password/confirm", {"password": PASSWORD})

    assert status == 409, payload
    assert payload["error"] == "password_changed"
    assert _confirmed(services).verify(PASSWORD)
    assert [
        entry["target"]
        for entry in services.audit.tail()
        if entry["action"] == "password.confirm" and entry["result"] == "failure"
    ] == ["password_changed"]
    assert not app.rate_limiter.failures


@pytest.mark.parametrize("record", ["missing", "unreadable"])
def test_without_a_readable_confirmed_record_the_way_back_is_named(appliance, record):
    services, app, client = appliance
    _set_up(client)
    _written_elsewhere(services, ELSEWHERE)
    _sign_in(client, ELSEWHERE)
    if record == "missing":
        services.paths.confirmed_password_file.unlink()
    else:
        services.paths.confirmed_password_file.write_text("{", encoding="utf-8")

    for _ in range(app.rate_limiter.max_failures + 1):
        status, payload, _ = client.post(
            "/api/settings/password/confirm", {"password": PASSWORD}
        )
        assert status == 409, payload
        assert payload["error"] == "confirmed_password_unavailable"
        assert "password-reset" in payload["message"]

    assert not app.rate_limiter.failures


def test_a_change_judges_the_record_its_current_password_matched(tmp_path, monkeypatch):
    """Protected interleaving: a change is asked for with a password written from
    a container, and the operator's confirmed record is put back right after
    that password was verified. Judged by a second read, the change would look
    like one made with the confirmed password and make the new one confirmed."""

    from appliance import auth as appliance_auth

    services = build_test_services(tmp_path)
    services.auth.iterations = 1000
    services.auth.create(PASSWORD, PASSWORD)
    own = services.paths.auth_file.read_text(encoding="utf-8")
    _written_elsewhere(services, FOREIGN)
    real_verify = appliance_auth.verify_password_record

    def verify_then_put_back(password, record):
        verified = real_verify(password, record)
        services.paths.auth_file.write_text(own, encoding="utf-8")
        return verified

    monkeypatch.setattr(appliance_auth, "verify_password_record", verify_then_put_back)
    services.auth.change(FOREIGN, ELSEWHERE, ELSEWHERE)
    monkeypatch.undo()

    assert _confirmed(services).verify(PASSWORD)
    assert not _confirmed(services).verify(ELSEWHERE)


def test_a_change_in_the_manager_keeps_root_access_open(appliance):
    services, _, client = appliance
    _set_up(client)
    status, payload, _ = client.post(
        "/api/settings/password",
        {"current_password": PASSWORD, "password": ELSEWHERE, "confirmation": ELSEWHERE},
    )
    assert status == 200, payload

    _sign_in(client, ELSEWHERE)
    status, payload, _ = client.post("/api/ssh/enable", {})

    assert status == 200, payload


def test_a_change_from_an_unconfirmed_session_does_not_confirm_it(appliance):
    """Whoever wrote the file can change the password from there; that must not
    turn their password into the confirmed one."""

    services, _, client = appliance
    _set_up(client)
    _written_elsewhere(services, FOREIGN)
    _sign_in(client, FOREIGN)
    status, payload, _ = client.post(
        "/api/settings/password",
        {"current_password": FOREIGN, "password": ELSEWHERE, "confirmation": ELSEWHERE},
    )
    assert status == 200, payload

    _sign_in(client, ELSEWHERE)

    assert client.post("/api/ssh/enable", {})[0] == 403
    assert _confirmed(services).verify(PASSWORD)


# --- an appliance updated from a Manager that kept no confirmed record -------


def test_the_shared_password_is_adopted_once_where_none_was_confirmed(tmp_path):
    services = build_test_services(tmp_path)
    AuthStore(services.paths.auth_file, iterations=1000).create(PASSWORD, PASSWORD)

    assert services.auth.adopt() is True
    assert _confirmed(services).verify(PASSWORD)

    _written_elsewhere(services, FOREIGN)

    assert services.auth.adopt() is False
    assert _confirmed(services).verify(PASSWORD)


def test_nothing_is_adopted_without_a_shared_password(tmp_path):
    services = build_test_services(tmp_path)

    assert services.auth.adopt() is False
    assert not services.paths.confirmed_password_file.exists()
    assert services.auth.configured() is False


def test_a_pipe_in_place_of_the_shared_file_does_not_hold_the_agent(tmp_path):
    """The agent reads the file as root before it serves anyone; a FIFO planted
    there from a container blocked that read for ever."""

    services = build_test_services(tmp_path)
    os.mkfifo(services.paths.auth_file)
    answers = []
    reader = threading.Thread(target=lambda: answers.append(services.auth.adopt()), daemon=True)

    reader.start()
    reader.join(timeout=10)

    assert answers == [False], "adopting the shared password waited on a pipe"
    assert services.auth.configured() is True
    assert services.auth.verify(PASSWORD) is False


@pytest.mark.parametrize(
    "content",
    ["[" * 60000, json.dumps({"pad": "x" * (64 * 1024)})],
    ids=["nested", "oversized"],
)
def test_a_shared_file_that_does_not_parse_within_bounds_is_refused(tmp_path, content):
    services = build_test_services(tmp_path)
    services.paths.auth_file.write_text(content, encoding="utf-8")

    assert services.auth.status()["configured"] is True
    assert services.auth.status()["generation"] == ""
    assert services.auth.verify(PASSWORD) is False
    assert services.auth.adopt() is False


def test_a_link_in_place_of_the_shared_file_is_not_followed(tmp_path):
    services = build_test_services(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(hash_password(FOREIGN, iterations=1000)), encoding="utf-8")
    services.paths.auth_file.symlink_to(target)

    assert services.auth.verify(FOREIGN) is False
    assert services.auth.adopt() is False


def test_a_failed_adoption_does_not_stop_the_agent(tmp_path, monkeypatch):
    services = build_test_services(tmp_path)
    served = []

    def refuse():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "resolve_paths", lambda: services.paths)
    monkeypatch.setattr(cli, "migrate_state", lambda paths: None)
    monkeypatch.setattr(cli, "write_report", lambda paths, report: None)
    monkeypatch.setattr(cli, "build_services", lambda paths: services)
    monkeypatch.setattr(cli, "serve_agent", lambda *args, **kwargs: served.append(True))
    monkeypatch.setattr(services.auth, "adopt", refuse)

    assert cli.command_agent(SimpleNamespace(socket=None)) == cli.EXIT_OK
    assert served == [True]


@pytest.mark.parametrize("lock", ["held", "pipe"])
def test_a_lock_a_container_holds_fails_a_change_instead_of_hanging(tmp_path, lock):
    import fcntl

    from appliance.auth import lock_path

    store = AuthStore(tmp_path / "dashboard-auth.json", iterations=1000, lock_wait=0)
    store.create(PASSWORD, PASSWORD)
    path = lock_path(store.path)
    holder = None
    if lock == "held":
        holder = os.open(str(path), os.O_WRONLY)
        fcntl.flock(holder, fcntl.LOCK_EX)
    else:
        path.unlink()
        os.mkfifo(path)
    try:
        with pytest.raises(AuthError) as error:
            store.change(PASSWORD, ELSEWHERE, ELSEWHERE)
    finally:
        if holder is not None:
            os.close(holder)

    assert error.value.code == "password_store_unavailable"
    assert ("held by another process" if lock == "held" else "remove it") in error.value.message
    assert store.verify(PASSWORD)


def test_a_writer_waits_for_a_lock_that_is_let_go(tmp_path, monkeypatch):
    """Protected interleaving: another writer holds the lock when a change asks
    for it, and lets go during the first wait. The agent and the console reset
    are two such writers, and the second must not be told to stop containers."""

    import fcntl

    from appliance import auth as appliance_auth
    from appliance.auth import lock_path

    store = AuthStore(tmp_path / "dashboard-auth.json", iterations=1000, lock_wait=10)
    store.create(PASSWORD, PASSWORD)
    holder = os.open(str(lock_path(store.path)), os.O_WRONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    waits = []

    def let_go(seconds):
        waits.append(seconds)
        os.close(holder)

    monkeypatch.setattr(appliance_auth.time, "sleep", let_go)

    store.change(PASSWORD, ELSEWHERE, ELSEWHERE)

    assert len(waits) == 1
    assert store.verify(ELSEWHERE)


def test_a_held_lock_is_a_503_and_costs_no_attempt(appliance):
    import fcntl

    from appliance.auth import lock_path

    services, app, client = appliance
    _set_up(client)
    services.auth.lock_wait = 0
    holder = os.open(str(lock_path(services.paths.auth_file)), os.O_WRONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        status, payload, _ = client.post(
            "/api/settings/password",
            {"current_password": PASSWORD, "password": ELSEWHERE, "confirmation": ELSEWHERE},
        )
    finally:
        os.close(holder)

    assert status == 503, payload
    assert payload["error"] == "password_store_unavailable"
    assert "held by another process" in payload["message"]
    assert str(services.paths.install_root) not in payload["message"]
    assert not app.rate_limiter.failures


def test_the_agent_adopts_before_it_serves_anyone(tmp_path, monkeypatch):
    services = build_test_services(tmp_path)
    AuthStore(services.paths.auth_file, iterations=1000).create(PASSWORD, PASSWORD)
    seen = {}

    def serve(served, socket_path, *, after_ready=None):
        seen["confirmed"] = _confirmed(served).verify(PASSWORD)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "resolve_paths", lambda: services.paths)
    monkeypatch.setattr(cli, "migrate_state", lambda paths: None)
    monkeypatch.setattr(cli, "write_report", lambda paths, report: None)
    monkeypatch.setattr(cli, "build_services", lambda paths: services)
    monkeypatch.setattr(cli, "serve_agent", serve)

    assert cli.command_agent(SimpleNamespace(socket=None)) == cli.EXIT_OK
    assert seen == {"confirmed": True}
    assert any(
        entry["action"] == "password.confirm" and entry["target"] == "agent_start"
        for entry in services.audit.tail()
    )


# --- the console reset --------------------------------------------------------


@pytest.fixture
def appliance_env(tmp_path, monkeypatch):
    from appliance.paths import (
        ENV_CONFIG_DIR,
        ENV_INSTALL_ROOT,
        ENV_LOG_DIR,
        ENV_RUNTIME_DIR,
        ENV_STATE_DIR,
        resolve_paths,
    )

    for variable, name in (
        (ENV_INSTALL_ROOT, "opt"),
        (ENV_CONFIG_DIR, "etc"),
        (ENV_STATE_DIR, "state"),
        (ENV_LOG_DIR, "log"),
        (ENV_RUNTIME_DIR, "run"),
    ):
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(variable, str(directory))
    return resolve_paths()


def test_a_reset_as_root_makes_the_new_password_the_confirmed_one(appliance_env, monkeypatch):
    paths = appliance_env
    confirmed = ConfirmedPassword(paths.confirmed_password_file)
    AuthStore(paths.auth_file, iterations=1000, confirmed=confirmed).create(PASSWORD, PASSWORD)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    assert cli.command_password_reset(SimpleNamespace(password=ELSEWHERE, json=False)) == 0

    assert confirmed.verify(ELSEWHERE)
    assert AuthStore(paths.auth_file).verify(ELSEWHERE)


def test_a_reset_without_root_leaves_root_access_waiting(appliance_env, capsys, monkeypatch):
    paths = appliance_env
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    confirmed = ConfirmedPassword(paths.confirmed_password_file)
    AuthStore(paths.auth_file, iterations=1000, confirmed=confirmed).create(PASSWORD, PASSWORD)

    assert cli.command_password_reset(SimpleNamespace(password=ELSEWHERE, json=False)) == 0

    assert confirmed.verify(PASSWORD)
    assert "confirmed in the Appliance Manager" in capsys.readouterr().err


def test_a_directory_at_the_shared_path_fails_the_reset_with_a_reason(
    appliance_env, monkeypatch, capsys
):
    paths = appliance_env
    paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.auth_file.mkdir()
    (paths.auth_file / "planted").write_text("x", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    assert cli.command_password_reset(SimpleNamespace(password=ELSEWHERE, json=False)) == cli.EXIT_ERROR

    assert "is a directory; remove it" in capsys.readouterr().err
    assert not list(paths.auth_file.parent.glob(".*.tmp"))


def test_a_lock_that_cannot_be_opened_says_why(tmp_path, monkeypatch):
    """A card gone read-only is not a planted entry, and removing the lock would
    not help."""

    import errno

    from appliance import auth as appliance_auth

    store = AuthStore(tmp_path / "dashboard-auth.json", iterations=1000, lock_wait=0)
    store.create(PASSWORD, PASSWORD)
    real_open = appliance_auth.os.open

    def read_only(path, flags, *args):
        if str(path).endswith(".lock"):
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        return real_open(path, flags, *args)

    monkeypatch.setattr(appliance_auth.os, "open", read_only)
    with pytest.raises(AuthError) as error:
        store.change(PASSWORD, ELSEWHERE, ELSEWHERE)
    monkeypatch.undo()

    assert error.value.code == "password_store_unavailable"
    assert "cannot be opened: Read-only file system" in error.value.message


@pytest.mark.parametrize("step", ["create", "reset"])
def test_a_record_that_could_not_be_finished_is_not_left_behind(tmp_path, monkeypatch, step):
    """A half-written record reads as a set password nobody can sign in with,
    and a leftover temporary file stops a fresh deployment root from being
    adopted."""

    import errno

    from appliance import auth as appliance_auth

    store = AuthStore(tmp_path / "dashboard-auth.json", iterations=1000)
    if step == "reset":
        store.create(PASSWORD, PASSWORD)

    def full(descriptor):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(appliance_auth.os, "fsync", full)
    with pytest.raises(OSError):
        getattr(store, step)(ELSEWHERE, ELSEWHERE)
    monkeypatch.undo()

    assert not list(tmp_path.glob(".*.tmp"))
    if step == "reset":
        assert store.verify(PASSWORD)
    else:
        assert not store.path.exists()


def test_a_reset_whose_confirmation_cannot_be_written_says_so_and_is_audited(
    appliance_env, monkeypatch, capsys
):
    import errno

    from appliance import auth as appliance_auth
    from appliance.audit import AuditLog

    paths = appliance_env
    confirmed = ConfirmedPassword(paths.confirmed_password_file)
    AuthStore(paths.auth_file, iterations=1000, confirmed=confirmed).create(PASSWORD, PASSWORD)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(appliance_auth, "atomic_write", full)

    assert cli.command_password_reset(SimpleNamespace(password=ELSEWHERE, json=False)) == (
        cli.EXIT_ERROR
    )

    assert "could not be recorded as the confirmed one" in capsys.readouterr().err
    assert AuthStore(paths.auth_file).verify(ELSEWHERE)
    assert confirmed.verify(PASSWORD)
    assert "password.reset" in [entry["action"] for entry in AuditLog(paths.audit_log).tail()]


@pytest.mark.parametrize("iterations", [10**20, 10 * 600000 + 1], ids=["overflow", "too-many"])
def test_a_record_with_absurd_iterations_costs_the_agent_nothing(
    tmp_path, monkeypatch, iterations
):
    """Written from a container, it turned every sign-in attempt into an hour of
    PBKDF2 in the root agent, or into an exception."""

    from appliance import auth as appliance_auth

    services = build_test_services(tmp_path)
    record = dict(hash_password(FOREIGN, iterations=1000), iterations=iterations)
    services.paths.auth_file.write_text(json.dumps(record), encoding="utf-8")
    derivations = []
    real_pbkdf2 = appliance_auth.hashlib.pbkdf2_hmac

    def counted(*args):
        derivations.append(args[-1])
        return real_pbkdf2(*args)

    monkeypatch.setattr(appliance_auth.hashlib, "pbkdf2_hmac", counted)

    assert services.auth.check(FOREIGN) == ""
    assert services.auth.adopt() is False
    assert derivations == [], "a PBKDF2 derivation ran for a record no writer produces"


# --- where the record lives and what it is ----------------------------------


def test_the_confirmed_record_is_root_state_shaped_like_the_shared_one(tmp_path):
    services = build_test_services(tmp_path)
    services.auth.iterations = 1000
    services.auth.create(PASSWORD, PASSWORD)
    path = services.paths.confirmed_password_file

    assert path.parent == services.paths.agent_state_dir
    assert services.paths.install_root not in path.parents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    shared = json.loads(services.paths.auth_file.read_text(encoding="utf-8"))
    assert json.loads(path.read_text(encoding="utf-8")) == shared
    assert set(shared) == set(RECORD_FIELDS)


def test_the_confirmed_record_is_no_state_format_an_older_manager_must_know():
    """A new axis in the state-schema stamp makes every older Manager package
    uninstallable on an appliance that recorded it. The record has the shared
    file's four fields and no version, so an older Manager just leaves it."""

    axes = set(persistent_state.implemented_schemas())

    assert not [axis for axis in axes if "password" in axis]


def test_a_generation_covers_the_whole_record():
    """Two records with the same salt and hash but other iterations verify
    differently, so they must not pass for one another."""

    record = hash_password(PASSWORD, iterations=1000)
    changed = dict(record, iterations=1001)

    assert record_generation(record) != record_generation(changed)
    assert record_generation(None) == ""
    assert record_generation({}) == ""


# --- the protocol ---------------------------------------------------------------


def test_exactly_the_plans_that_can_hand_out_a_root_shell_carry_the_session_generation():
    """The three that open one, and the two that install a Manager which may
    not ask for the confirmed password before it does."""

    assert ROOT_ACCESS_PLANS == {
        "ssh.plan_service",
        "ssh.plan_shell_access",
        "ssh.plan_key_add",
        "manager.plan_update",
        "manager.plan_revert",
    }


def test_a_malformed_session_generation_is_refused_before_any_handler():
    context = ValidationContext(appliance_config())

    with pytest.raises(ProtocolError) as error:
        validate_request(
            {"operation": "ssh.plan_service", "enabled": True, SESSION_GENERATION_FIELD: "zz"},
            context,
        )

    assert error.value.code == "invalid_password_generation"
