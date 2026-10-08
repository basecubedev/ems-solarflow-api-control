# SPDX-License-Identifier: AGPL-3.0-or-later
"""The unprivileged web service: authentication, CSRF, sessions and routing.

The web process holds no privilege of its own. These tests run the real HTTP
server against an in-process agent, so an authorisation mistake here is visible
as a request that reaches — or fails to reach — the agent.
"""

import contextlib
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from appliance.agent import AgentHandlers
from appliance.agent_client import AgentUnavailableError, InProcessAgentClient
from appliance.auth import SESSION_COOKIE_NAME, AuthStore, ConfirmedPassword
from appliance.web import AgentAuth, ApplianceWebApp, ApplianceWebServer
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

PASSWORD = "appliance-secret-1"


class _OfflineAgent:
    """An agent socket that is simply not there."""

    def call(self, operation, **kwargs):
        raise AgentUnavailableError("the appliance agent is not reachable")

    def available(self):
        return False


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, body=None, *, headers=None, csrf=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Accept": "application/json"}
        if self.cookie:
            sent["Cookie"] = self.cookie
        if csrf and self.csrf and method != "GET":
            sent["X-Appliance-CSRF"] = self.csrf
        if body is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        status = response.status
        headers_out = dict(response.getheaders())
        connection.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            parsed = {"_raw": raw.decode("utf-8", errors="replace")}
        if isinstance(parsed, dict) and parsed.get("csrf_token"):
            self.csrf = parsed["csrf_token"]
        return status, parsed, headers_out

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body if body is not None else {}, **kwargs)

    def login(self, password=PASSWORD):
        return self.post("/api/session/login", {"password": password})


@pytest.fixture
def appliance(tmp_path):
    services = build_test_services(tmp_path)
    services.host.write_deployment(tag="v1.0.0")
    services.host.publish_image("v1.0.0")
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    home = tmp_path / "home" / "ems-backup"
    home.mkdir(parents=True, exist_ok=True)
    services.host.add_account("ems-backup", home)

    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    app = ApplianceWebApp(paths=services.paths, config=services.config, agent=agent)
    app.auth = AuthStore(
        services.paths.auth_file,
        iterations=1000,
        confirmed=ConfirmedPassword(services.paths.confirmed_password_file),
    )
    server = ApplianceWebServer(app, ("127.0.0.1", 0))
    # A short poll interval keeps shutdown() from adding half a second per test.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield services, app, Client(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def signed_in(appliance):
    services, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    status, payload, _ = client.login()
    assert status == 200, payload
    return services, app, client


# --- first run -------------------------------------------------------------


def test_first_run_reports_that_no_password_exists(appliance):
    _, _, client = appliance
    status, payload, _ = client.get("/api/session")
    assert status == 200
    assert payload["password_configured"] is False
    assert payload["authenticated"] is False


def test_no_system_information_is_exposed_before_authentication(appliance):
    _, _, client = appliance
    unauthenticated = client.get("/api/session")[1]
    assert set(unauthenticated) <= {
        "authenticated",
        "password_configured",
        "password_file_missing",
        "appliance_version",
    }

    for path in ("/api/status", "/api/system", "/api/admin", "/api/updates", "/api/network",
                 "/api/docker", "/api/ssh/keys", "/api/logs/audit", "/api/settings"):
        status, payload, _ = client.get(path)
        assert status == 401, path
        assert payload["error"] == "authentication_required"


def test_first_password_creates_a_session(appliance):
    _, _, client = appliance
    status, payload, headers = client.post(
        "/api/session/setup", {"password": PASSWORD, "confirmation": PASSWORD}
    )
    assert status == 200
    assert payload["authenticated"] is True
    assert SESSION_COOKIE_NAME in headers["Set-Cookie"]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Strict" in headers["Set-Cookie"]


def test_first_password_must_be_confirmed_and_not_empty(appliance):
    """One password opens the appliance, Admin and the dashboard, so one rule
    holds for all three: the other two have always accepted any non-empty one,
    and a stricter rule here would refuse to change a password set from there."""

    _, _, client = appliance
    status, payload, _ = client.post("/api/session/setup", {"password": "", "confirmation": ""})
    assert status == 400
    assert payload["error"] == "password_required"

    status, payload, _ = client.post(
        "/api/session/setup", {"password": "short", "confirmation": "short"}
    )
    assert status == 200, payload

    status, payload, _ = client.post(
        "/api/session/setup", {"password": PASSWORD, "confirmation": "different-one-here"}
    )
    assert payload["error"] in ("password_mismatch", "password_already_configured")


def test_setup_is_refused_once_a_password_exists(appliance):
    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    status, payload, _ = client.post(
        "/api/session/setup", {"password": "another-password-1", "confirmation": "another-password-1"}
    )
    assert status == 409
    assert payload["error"] == "password_already_configured"


def test_a_foreign_page_cannot_set_the_first_password(appliance):
    """The documented first-start window, reachable from any page on the LAN.

    `/api/session/setup` is routed before any check runs, so a page the
    operator happens to open could send a simple cross-origin POST -- text/plain
    needs no preflight -- and claim the password that opens the Appliance
    Manager, the Admin console and the dashboard. The operator is then locked
    out of the only browser recovery UI and needs a console or SSH.
    """

    _, app, client = appliance
    status, payload, _ = client.post(
        "/api/session/setup",
        {"password": "pwn-by-a-web-page", "confirmation": "pwn-by-a-web-page"},
        headers={"Origin": "http://evil.example"},
    )

    assert status == 403, payload
    assert payload["error"] == "csrf_origin_rejected"
    assert not app.auth.configured(), "a foreign page enrolled this appliance"


def test_a_rebound_host_cannot_set_the_first_password(appliance):
    _, app, client = appliance
    status, payload, _ = client.post(
        "/api/session/setup",
        {"password": "pwn-by-a-web-page", "confirmation": "pwn-by-a-web-page"},
        headers={"Host": "attacker.example", "Origin": "http://attacker.example"},
    )

    assert status == 403, payload
    assert payload["error"] == "csrf_host_rejected"
    assert not app.auth.configured()


def test_a_foreign_page_cannot_burn_the_operators_login_attempts(appliance):
    """Five failures from the operator's own IP lock them out for five minutes."""

    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)

    status, payload, _ = client.post(
        "/api/session/login",
        {"password": "wrong-on-purpose"},
        headers={"Origin": "http://evil.example"},
    )

    assert status == 403, payload
    assert payload["error"] == "csrf_origin_rejected"


def test_enrolment_and_login_still_work_without_an_origin_header(appliance):
    """A non-browser caller sends no Origin, and always sends a Host."""

    _, _, client = appliance
    status, _, _ = client.post(
        "/api/session/setup", {"password": PASSWORD, "confirmation": PASSWORD}
    )
    assert status == 200

    status, _, _ = client.post("/api/session/login", {"password": PASSWORD})
    assert status == 200


def test_there_is_no_unauthenticated_password_reset_endpoint(appliance):
    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    for path in ("/api/session/reset", "/api/password/reset", "/api/settings/password"):
        status, _, _ = client.post(path, {"password": "attacker-password-1"})
        assert status in (401, 403, 404), path


# --- login -----------------------------------------------------------------


def test_login_and_logout(signed_in):
    _, _, client = signed_in
    assert client.get("/api/session")[1]["authenticated"] is True
    assert client.post("/api/session/logout")[1]["authenticated"] is False
    assert client.get("/api/status")[0] == 401


def test_wrong_password_is_refused(appliance):
    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    status, payload, _ = client.login("not-the-password")
    assert status == 401
    assert payload["error"] == "invalid_credentials"


def test_repeated_failures_are_rate_limited(appliance):
    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    codes = [client.login("wrong-password-here")[0] for _ in range(6)]
    assert codes[-1] == 429
    # A correct password is refused as well while the limiter is engaged.
    assert client.login()[0] == 429


def test_login_failures_and_successes_are_audited(signed_in):
    services, app, client = signed_in
    client.post("/api/session/logout")
    client.login("wrong-password-here")
    actions = [entry["action"] for entry in services.audit.tail()]
    assert "login.success" in actions
    assert "login.failure" in actions
    assert not any("wrong-password-here" in json.dumps(entry) for entry in services.audit.tail())


# --- the audit trail belongs to the agent ----------------------------------


def test_the_web_module_never_opens_the_audit_log_itself():
    import appliance.web as web_module

    assert not hasattr(web_module, "AuditLog"), (
        "the web service must report audit events to the agent, not write the log"
    )


def test_every_authentication_event_is_an_allowlisted_agent_operation(signed_in):
    services, app, client = signed_in
    recorded = []
    original = app.agent.call

    def spy(operation, **kwargs):
        recorded.append((operation, kwargs))
        return original(operation, **kwargs)

    app.agent.call = spy
    client.post("/api/session/logout")
    client.login()
    client.post(
        "/api/settings/password",
        {"current_password": PASSWORD, "password": "another-secret-1", "confirmation": "another-secret-1"},
    )

    audit_calls = [entry for entry in recorded if entry[0] == "audit.record_web_event"]
    assert [entry[1]["event"] for entry in audit_calls] == [
        "logout",
        "login.success",
        "password.change",
    ]
    for _, fields in audit_calls:
        assert set(fields) <= {"actor", "source_ip", "event", "result", "reason"}


def test_authentication_survives_an_unreachable_agent(appliance):
    services, app, client = appliance
    app.agent = _OfflineAgent()
    app.audit.agent = app.agent

    status, payload, _ = client.post(
        "/api/session/setup", {"password": PASSWORD, "confirmation": PASSWORD}
    )
    assert status == 200, payload
    assert payload["authenticated"] is True
    assert payload["security_audit"]["degraded"] is True
    assert payload["security_audit"]["authoritative"] is False
    assert payload["security_audit"]["last_error"] == "agent_unavailable"


def test_a_degraded_audit_is_visible_on_the_session_and_settings_endpoints(appliance):
    services, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    app.agent = _OfflineAgent()
    app.audit.agent = app.agent
    client.login()

    _, session, _ = client.get("/api/session")
    assert session["security_audit"]["state"] == "degraded"
    assert session["security_audit"]["unrecorded_events"] >= 1
    assert session["security_audit"]["message"]

    _, settings, _ = client.get("/api/settings")
    assert settings["security_audit"]["degraded"] is True


def test_an_unrecorded_audit_event_is_written_to_the_web_owned_log(appliance):
    services, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    app.agent = _OfflineAgent()
    app.audit.agent = app.agent
    client.login("wrong-password-here")

    entries = app.web_log.tail()
    assert entries, "the web service must record that an audit event was lost"
    assert entries[-1]["event"] == "audit_unavailable"
    assert entries[-1]["audit_event"] == "login.failure"
    assert not any("wrong-password-here" in json.dumps(entry) for entry in entries)


def test_the_web_log_stays_bounded(tmp_path):
    from appliance.audit import WebLog

    log = WebLog(tmp_path / "appliance.log", max_bytes=2048)
    for index in range(400):
        log.warn("audit_unavailable", audit_event="login.failure", error=f"attempt-{index}")

    assert log.path.stat().st_size <= 2048 + 512
    assert log.path.with_name("appliance.log.1").is_file()


def test_a_reachable_agent_reports_a_healthy_audit(signed_in):
    services, app, client = signed_in
    _, session, _ = client.get("/api/session")
    assert session["security_audit"]["state"] == "healthy"
    assert session["security_audit"]["authoritative"] is True
    assert session["security_audit"]["unrecorded_events"] == 0


def test_a_password_change_invalidates_every_session(signed_in):
    services, app, client = signed_in
    status, payload, _ = client.post(
        "/api/settings/password",
        {
            "current_password": PASSWORD,
            "password": "a-brand-new-secret",
            "confirmation": "a-brand-new-secret",
        },
    )
    assert status == 200
    assert payload["sessions_invalidated"] is True
    assert client.get("/api/status")[0] == 401


def test_a_password_change_needs_the_current_password(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.post(
        "/api/settings/password",
        {"current_password": "wrong", "password": "a-brand-new-secret", "confirmation": "a-brand-new-secret"},
    )
    assert status == 400
    assert payload["error"] == "current_password_invalid"


def test_a_cli_password_reset_invalidates_browser_sessions(signed_in):
    services, app, client = signed_in
    assert client.get("/api/status")[0] == 200
    AuthStore(services.paths.auth_file, iterations=1000).reset("console-reset-secret")
    assert client.get("/api/status")[0] == 401


# --- CSRF ------------------------------------------------------------------


def test_a_mutation_without_a_csrf_token_is_refused(signed_in):
    services, _, client = signed_in
    status, payload, _ = client.post("/api/admin/restart", {}, csrf=False)
    assert status == 403
    assert payload["error"] == "csrf_token_invalid"
    assert services.operations.list() == []


def test_a_mutation_with_a_foreign_csrf_token_is_refused(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.post(
        "/api/admin/restart", {}, headers={"X-Appliance-CSRF": "forged"}, csrf=False
    )
    assert status == 403


def test_a_lookalike_origin_is_refused(signed_in):
    _, _, client = signed_in
    # "evil-<host>" ends with the real host, so a suffix comparison would let
    # an attacker-controlled origin through.
    status, payload, _ = client.request(
        "POST",
        "/api/admin/restart",
        {},
        headers={"Origin": f"http://evil-127.0.0.1:{client.port}"},
    )
    assert status == 403
    assert payload["error"] == "csrf_origin_rejected"


def test_a_foreign_origin_is_refused(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.post(
        "/api/admin/restart", {}, headers={"Origin": "http://evil.example"}
    )
    assert status == 403
    assert payload["error"] == "csrf_origin_rejected"


# --- read-only API ---------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_key",
    [
        ("/api/status", "health"),
        ("/api/system", "hardware"),
        ("/api/network", "hostname"),
        ("/api/docker", "daemon"),
        ("/api/admin", "installed"),
        ("/api/updates", "security_count"),
        ("/api/ssh/keys", "accounts"),
        ("/api/backup", "paths"),
        ("/api/operations", "recent"),
        ("/api/settings", "appliance_version"),
    ],
)
def test_read_only_endpoints_answer(signed_in, path, expected_key):
    _, _, client = signed_in
    status, payload, _ = client.get(path)
    assert status == 200, payload
    assert expected_key in payload


def test_log_endpoint_is_bounded_and_redacted(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.get("/api/logs/admin_container?lines=20")
    assert status == 200
    assert payload["source"] == "admin_container"
    assert payload["lines"] <= 20
    assert "supersecret" not in payload["text"]


def test_an_unknown_log_source_is_refused(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.get("/api/logs/%2Fetc%2Fshadow")
    assert status == 400
    assert payload["error"] == "invalid_log_source"


def test_the_console_is_offered_every_log_source_it_may_read(signed_in):
    """One list. The browser held a copy of nine sources while the backend
    declared sixteen, so the journals the manager card itself points at --
    manager_verify among them -- could not be opened from the console."""

    from appliance import validation

    _services, _app, client = signed_in

    status, payload, _ = client.get("/api/settings")

    assert status == 200
    assert payload["log_sources"] == list(validation.LOG_SOURCES)


def test_settings_never_expose_a_host_secret(signed_in):
    _, _, client = signed_in
    payload = client.get("/api/settings")[1]
    assert "password" not in json.dumps(payload).lower()


# --- mutations -------------------------------------------------------------


def test_every_mutation_returns_a_plan_and_a_confirmation_token(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.post("/api/admin/restart")
    assert status == 200
    assert payload["operation"]["state"] == "awaiting_confirmation"
    assert payload["plan"]["action"] == "restart"
    assert payload["confirmation_token"]


def test_confirmation_executes_the_planned_operation(signed_in):
    services, _, client = signed_in
    planned = client.post("/api/admin/restart")[1]
    status, payload, _ = client.post(
        "/api/operations/confirm",
        {
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        },
    )
    assert status == 200
    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == "succeeded"


def test_a_wrong_confirmation_token_is_refused_with_403(signed_in):
    _, _, client = signed_in
    planned = client.post("/api/admin/restart")[1]
    status, payload, _ = client.post(
        "/api/operations/confirm",
        {
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": "wrong-token-000000000",
        },
    )
    assert status == 403
    assert payload["error"] == "confirmation_token_mismatch"


def test_a_conflicting_second_mutation_returns_409(signed_in):
    _, _, client = signed_in
    client.post("/api/admin/restart")
    status, payload, _ = client.post("/api/updates/plan", {"scope": "security"})
    assert status == 409
    assert payload["error"] == "operation_conflict"


def test_a_running_operation_is_visible_after_a_browser_reload(signed_in):
    _, _, client = signed_in
    planned = client.post("/api/admin/restart")[1]
    reloaded = client.get("/api/operations")[1]
    assert reloaded["active"]["operation_id"] == planned["operation"]["operation_id"]
    assert reloaded["active"]["stage"] == "awaiting_confirmation"


def test_a_terminal_result_stays_until_acknowledged(signed_in):
    _, _, client = signed_in
    planned = client.post("/api/admin/restart")[1]
    client.post(
        "/api/operations/confirm",
        {
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        },
    )
    listing = client.get("/api/operations")[1]
    assert listing["unacknowledged"][0]["operation_id"] == planned["operation"]["operation_id"]

    client.post(
        "/api/operations/acknowledge",
        {"operation_id": planned["operation"]["operation_id"]},
    )
    assert client.get("/api/operations")[1]["unacknowledged"] == []


def test_a_plan_can_be_cancelled(signed_in):
    services, _, client = signed_in
    planned = client.post("/api/admin/restart")[1]
    client.post("/api/operations/cancel", {"operation_id": planned["operation"]["operation_id"]})
    assert services.operations.active() is None


def test_the_browser_cannot_choose_an_image_repository(signed_in):
    services, _, client = signed_in
    services.host.publish_image("v1.1.0")
    # The web layer builds the agent request from named fields only, so an
    # extra "repository" key is dropped instead of forwarded. The plan uses the
    # repository from the host allowlist.
    status, payload, _ = client.post(
        "/api/admin/plan-install",
        {"channel": "exact", "tag": "v1.1.0", "repository": "ghcr.io/attacker/evil"},
    )
    assert status == 200, payload
    assert payload["plan"]["repository"] == ADMIN_REPOSITORY
    assert not any(
        "attacker" in " ".join(args) for _, args, _ in services.host.calls
    )


def test_the_browser_cannot_send_a_command(signed_in):
    services, _, client = signed_in
    services.host.calls.clear()
    status, payload, _ = client.post("/api/admin/plan-install", {"command": "docker run evil"})
    assert status == 400
    assert payload["error"] == "invalid_release_channel"
    assert services.host.calls == []


def test_reboot_plan_lists_blockers_and_running_state(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.post("/api/system/reboot")
    assert status == 200
    assert payload["plan"]["action"] == "reboot"
    assert "docker" in payload["plan"]
    assert isinstance(payload["plan"]["blockers"], list)


def test_ssh_key_deployment_through_the_api(signed_in):
    services, _, client = signed_in
    key = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf "
        "appliance-test@example.invalid"
    )
    planned = client.post("/api/ssh/keys", {"account": "ems-backup", "public_key": key})[1]
    assert planned["plan"]["key"]["key_type"] == "ssh-ed25519"

    client.post(
        "/api/operations/confirm",
        {
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        },
    )
    assert len(services.ssh.keystore("ems-backup").list()) == 1


def test_a_private_key_upload_is_refused_by_the_api(signed_in):
    _, _, client = signed_in
    status, payload, _ = client.post(
        "/api/ssh/keys",
        {
            "account": "ems-backup",
            "public_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----",
        },
    )
    assert status == 400
    assert payload["error"] == "private_key_rejected"


# --- transport hardening ---------------------------------------------------


def test_security_headers_are_present(signed_in):
    _, _, client = signed_in
    _, _, headers = client.get("/api/status")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"


def test_static_assets_are_an_explicit_allowlist(signed_in):
    _, _, client = signed_in
    assert client.get("/static/app.js")[0] == 200
    assert client.get("/static/styles.css")[0] == 200
    assert client.get("/static/../appliance/web.py")[0] == 404
    assert client.get("/static/../../etc/passwd")[0] == 404


def test_the_index_page_is_served_without_authentication(appliance):
    _, _, client = appliance
    status, payload, _ = client.get("/")
    assert status == 200
    assert "Appliance Manager" in payload["_raw"]


def test_the_browser_test_reset_endpoint_does_not_exist_outside_test_mode(signed_in):
    services, app, client = signed_in
    assert app.test_mode is False
    assert client.post("/api/test/reset", {})[0] == 404
    assert client.get("/api/status")[0] == 200


def test_the_browser_test_reset_endpoint_needs_authentication_free_gating(appliance):
    _, app, client = appliance
    # Without test mode the path is simply unknown, authenticated or not.
    assert client.post("/api/test/reset", {})[0] == 404


def test_an_unreachable_agent_is_reported_as_service_unavailable(tmp_path):
    from appliance.agent_client import AgentClient
    from appliance.paths import resolve_paths

    services = build_test_services(tmp_path)
    app = ApplianceWebApp(
        paths=services.paths,
        config=services.config,
        agent=AgentClient(services.paths.runtime_dir / "absent.sock", timeout=1),
    )
    app.auth = AuthStore(services.paths.auth_file, iterations=1000)
    app.auth.create(PASSWORD, PASSWORD)
    server = ApplianceWebServer(app, ("127.0.0.1", 0))
    # A short poll interval keeps shutdown() from adding half a second per test.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        client = Client(server.server_address[1])
        client.login()
        status, payload, _ = client.get("/api/status")
        assert status == 503
        assert payload["error"] == "agent_unavailable"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert resolve_paths is not None


# --- hardening at the network edge --------------------------------------------


def test_a_non_string_password_is_a_refusal_not_a_traceback(appliance):
    """Unauthenticated input must not be able to crash the login route."""

    _services, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)

    status, payload, _ = client.post("/api/session/login", {"password": {"not": "a string"}})

    assert status in (400, 401)
    assert "_raw" not in payload


def test_a_non_string_password_is_refused_by_the_store_itself():
    from appliance.auth import hash_password, verify_password_record

    record = hash_password(PASSWORD, iterations=1000)

    assert verify_password_record(PASSWORD, record) is True
    assert verify_password_record({"not": "a string"}, record) is False
    assert verify_password_record(["also", "not"], record) is False
    assert verify_password_record(None, record) is False


def test_a_request_naming_a_foreign_host_is_refused(signed_in):
    """DNS rebinding makes Origin and Host agree on the attacker's name.

    Comparing one against the other therefore proves nothing unless the Host is
    itself checked against what this appliance answers to.
    """

    _services, _app, client = signed_in

    status, payload, _ = client.post(
        "/api/network/scan",
        headers={"Host": "attacker.example", "Origin": "http://attacker.example"},
    )

    assert status == 403
    assert payload["error"] == "csrf_host_rejected"


def test_a_hex_word_is_not_an_address_literal(signed_in):
    """'cafe' is a name somebody on the LAN can register, not an address.

    The literal branch matched any run of hex digits and colons, or of digits
    and dots, so a single-label name an attacker registers over DHCP passed
    the host half of the rebinding check that exists to exclude it.
    """

    _services, app, client = signed_in
    app.probe_hostname = lambda: "ems-appliance"

    status, payload, _ = client.post(
        "/api/network/scan", headers={"Host": "cafe", "Origin": "http://cafe"}
    )

    assert status == 403, payload
    assert payload["error"] == "csrf_host_rejected"
    # The literals the branch exists for keep working, and only those.
    assert app.names_this_appliance("192.168.1.5") is True
    assert app.names_this_appliance("192.168.1.5:8443") is True
    assert app.names_this_appliance("[fd00::1]:8080") is True
    assert app.names_this_appliance("fd00::1") is True
    assert app.names_this_appliance("1.2.3.4.5") is False
    assert app.names_this_appliance("abcdef") is False


def test_a_renamed_appliance_answers_to_its_new_name_without_a_restart(
    signed_in, tmp_path, monkeypatch
):
    """ProtectHostname= gives the web unit a UTS namespace of its own, copied
    when it starts. After `hostnamectl set-hostname` gethostname() in this
    process went on answering the old name, so every POST under the new one --
    login included -- was refused until the unit was restarted. The static
    hostname file hostnamectl writes is the host's own, and the unit can read it.
    """

    import socket

    services, app, client = signed_in
    monkeypatch.setattr(socket, "gethostname", lambda: "ems-solarflow")
    hostname_file = tmp_path / "etc-hostname"
    hostname_file.write_text("ems-solarflow\n", encoding="utf-8")
    app.hostname_file = hostname_file

    planned = client.post("/api/network/hostname", {"hostname": "garage-pi"})[1]
    status, payload, _ = client.post(
        "/api/operations/confirm",
        {
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        },
    )
    assert status == 200, payload
    assert services.host.hostname == "garage-pi"
    hostname_file.write_text(f"{services.host.hostname}\n", encoding="utf-8")

    renamed = {"Host": "garage-pi.local:8088", "Origin": "http://garage-pi.local:8088"}
    status, payload, _ = client.post("/api/admin/restart", headers=renamed)
    assert status == 200, payload
    status, payload, _ = client.post(
        "/api/session/login", {"password": PASSWORD}, headers=renamed
    )
    assert status == 200, payload

    former = {"Host": "ems-solarflow.local:8088", "Origin": "http://ems-solarflow.local:8088"}
    status, payload, _ = client.post("/api/admin/restart", headers=former)
    assert status == 403, payload
    assert payload["error"] == "csrf_host_rejected"
    assert "IP address" in payload["message"]


def test_the_kernel_name_answers_only_without_a_static_hostname(appliance, tmp_path, monkeypatch):
    import socket

    _, app, _ = appliance
    monkeypatch.setattr(socket, "gethostname", lambda: "kernel-name")

    app.hostname_file = tmp_path / "absent"
    assert app.probe_hostname() == "kernel-name"

    blank = tmp_path / "blank"
    blank.write_text("# written by the image\n\n", encoding="utf-8")
    app.hostname_file = blank
    assert app.probe_hostname() == "kernel-name"

    named = tmp_path / "named"
    named.write_text("# written by the image\n  Garage-Pi  \n", encoding="utf-8")
    app.hostname_file = named
    assert app.probe_hostname() == "Garage-Pi"


@pytest.mark.parametrize(
    "content, expected",
    [
        (b"garage-pi.\n", "garage-pi"),
        (b"garage pi\n", "garagepi"),
        (b"garage_pi\n", "garagepi"),
        (b"\xef\xbb\xbfgarage-pi\n", "garage-pi"),
        (b"\xef\xbb\xbf# a comment\ngarage-pi\n", "acomment"),
        (b"\xef\xbb\xbf\ngarage-pi\n", "kernel-name"),
        (b"\xc2\xa0# set by the image\ngarage-pi\n", "setbytheimage"),
        (b"# reachable as pi.localhost\ngarage-pi\n", "garage-pi"),
        (b"\ngarage-pi\n", "garage-pi"),
        (b"   \ngarage-pi\n", "garage-pi"),
        (b"  # a comment\ngarage-pi\n", "garage-pi"),
        (b"\t# a comment\ngarage-pi\n", "garage-pi"),
        (b"\x0b# a comment\ngarage-pi\n", "acomment"),
        (b"garage-pi\rjunk\n", "garage-pi"),
        (b"garage\x00pi\n", "garage"),
        (b"#" * 4090 + b"\ngarage-pi\n", "garage-pi"),
        (b"mylocalhost\n", "mylocalhost"),
        (b"pi.localhost.localdomain\n", "kernel-name"),
        (b"localhost-\n", "kernel-name"),
        (b"...\n", "kernel-name"),
        (b"!!!\ngarage-pi\n", "kernel-name"),
        (b"\xff\xfe\ngarage-pi\n", "kernel-name"),
        ("münchen-pi\n".encode(), "mnchen-pi"),
        (b"garage..pi\n", "garage.pi"),
        (b".garage-pi\n", "garage-pi"),
        (b"garage-pi-\n", "garage-pi"),
        (b"-garage-pi\n", "garage-pi"),
        (b"garage.-pi\n", "garage.pi"),
        (b"garage-.pi\n", "garage-pi"),
        (b"garage-pi--\n", "kernel-name"),
        (b"-.-\n", "kernel-name"),
        (b"a" * 70 + b"\n", "a" * 64),
        (b"a" * 63 + b".b\n", "a" * 63),
        (b"a" * 63 + b"-x\n", "a" * 63),
    ],
)
def test_the_hostname_file_is_read_as_systemd_reads_it(appliance, tmp_path, monkeypatch, content, expected):
    """The values systemd 257 gives these files (read_etc_hostname with its
    cleanup); read raw, a hand-edited file with a trailing dot refused every
    sign-in under the name the host answers to."""

    import socket

    _, app, _ = appliance
    monkeypatch.setattr(socket, "gethostname", lambda: "kernel-name")
    hostname_file = tmp_path / "hostname"
    hostname_file.write_bytes(content)
    app.hostname_file = hostname_file

    assert app.probe_hostname() == expected


def test_the_hostname_comes_from_the_hosts_static_hostname_file():
    from appliance import web

    assert web.HOSTNAME_FILE == "/etc/hostname"


def test_a_name_on_the_first_line_counts_whatever_follows_it(appliance, tmp_path, monkeypatch):
    import socket

    _, app, _ = appliance
    monkeypatch.setattr(socket, "gethostname", lambda: "kernel-name")
    named = tmp_path / "named"
    named.write_bytes(b"garage-pi\n\xff\xfe\n")
    app.hostname_file = named

    assert app.probe_hostname() == "garage-pi"


@pytest.mark.parametrize(
    "placeholder", ["localhost", "localhost.localdomain", "LOCALHOST", "localhost.", "pi.localhost"]
)
def test_localhost_in_the_hostname_file_is_no_static_name(appliance, tmp_path, monkeypatch, placeholder):
    """systemd treats it as unset, so the name the host goes by comes from DHCP;
    a name on a later line does not count either."""

    import socket

    _, app, _ = appliance
    monkeypatch.setattr(socket, "gethostname", lambda: "garage-pi")
    placeholder_file = tmp_path / "placeholder"
    placeholder_file.write_text(f"{placeholder}\nother-name\n", encoding="utf-8")
    app.hostname_file = placeholder_file

    assert app.probe_hostname() == "garage-pi"
    assert app.names_this_appliance("garage-pi.local:8088") is True


def test_a_support_archive_can_actually_be_retrieved(signed_in):
    """The docs tell an operator to attach it, and on an A/B image there is no
    shell and the file lives in root-owned agent state."""

    services, _app, client = signed_in
    planned = client.post("/api/support/archive")[1]
    operation_id = planned["operation"]["operation_id"]
    confirmed = client.post(
        "/api/operations/confirm",
        {"operation_id": operation_id, "confirmation_token": planned["confirmation_token"]},
    )
    assert confirmed[0] == 200, confirmed[1]

    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    connection.request(
        "GET", f"/api/support/archive/{operation_id}", headers={"Cookie": client.cookie}
    )
    response = connection.getresponse()
    body = response.read()
    disposition = response.getheader("Content-Disposition") or ""
    connection.close()

    assert response.status == 200, body[:200]
    assert body[:2] == b"\x1f\x8b", "not a gzip stream"
    assert operation_id in disposition


def test_a_support_archive_download_needs_a_session(appliance):
    _services, _app, client = appliance

    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    connection.request("GET", "/api/support/archive/op-1")
    status = connection.getresponse().status
    connection.close()

    assert status == 401


def test_a_support_archive_path_cannot_name_anything_but_an_operation(signed_in):
    _services, _app, client = signed_in

    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    connection.request(
        "GET", "/api/support/archive/..%2f..%2fetc%2fpasswd", headers={"Cookie": client.cookie}
    )
    status = connection.getresponse().status
    connection.close()

    assert status in (400, 404)


def test_an_archive_that_was_never_created_is_a_404_not_a_traceback(signed_in):
    _services, _app, client = signed_in

    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    connection.request(
        "GET", "/api/support/archive/" + "0" * 32, headers={"Cookie": client.cookie}
    )
    status = connection.getresponse().status
    connection.close()

    assert status == 404


def test_a_wildcard_listener_answers_over_ipv6_too():
    """The documented first-contact address is an mDNS name, and avahi
    publishes an AAAA alongside the A on any LAN with IPv6. A browser may try
    that first, and an IPv4-only listener refuses it."""

    import socket

    app = ApplianceWebApp(paths=None, config=None, agent=_OfflineAgent())
    server = ApplianceWebServer(app, ("0.0.0.0", 0))
    try:
        assert server.address_family == socket.AF_INET6
        assert server.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    finally:
        server.server_close()


def test_an_explicit_address_is_still_honoured():
    """An operator who pinned a loopback listener keeps it."""

    import socket

    app = ApplianceWebApp(paths=None, config=None, agent=_OfflineAgent())
    server = ApplianceWebServer(app, ("127.0.0.1", 0))
    try:
        assert server.address_family == socket.AF_INET
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_an_unreachable_agent_is_not_reported_as_a_wrong_password(appliance):
    """The shared password lives root-owned in the deployment root, so the
    unprivileged web process cannot check it without the agent. That is the
    cost of one secret for all three interfaces, and it is accepted -- but it
    must be stated. Answering 401 sends an operator hunting for a password that
    is in fact correct, spends the rate limiter on a transport failure, and
    hides the one fact that explains every other symptom on the page.
    """

    services, app, client = appliance
    app.auth = AgentAuth(_OfflineAgent())

    status, payload, _ = client.post("/api/session/login", {"password": PASSWORD})

    assert status == 503, payload
    assert payload["error"] == "agent_unavailable"
    assert "not correct" not in payload["message"]


def test_a_transport_failure_does_not_count_against_the_rate_limiter(appliance):
    """An agent that flaps must not lock the operator out once it returns."""

    services, app, client = appliance
    app.auth = AgentAuth(_OfflineAgent())

    for _ in range(8):
        status, _, _ = client.post("/api/session/login", {"password": PASSWORD})
        assert status == 503

    app.auth = AuthStore(services.paths.auth_file, iterations=1000)
    app.auth.create(PASSWORD, PASSWORD)
    status, payload, _ = client.post("/api/session/login", {"password": PASSWORD})

    assert status == 200, payload


# --- the rate limiter under concurrency --------------------------------------


def test_concurrent_logins_cannot_spend_more_than_the_documented_budget(appliance):
    """Protected interleaving: every caller reaches the limit check before any
    of them has recorded a failure against it.

    `login` reads the limiter under the lock, releases it, derives the password
    hash -- hundreds of milliseconds on a Pi, deliberately outside the lock --
    and only then records the failure. Until it does, every concurrent attempt
    sees a clean slate, so the documented budget of five per five minutes
    becomes "as many as the attacker holds open connections". Each of those
    derivations is 600 000 PBKDF2 rounds in the root agent, which is the one
    process an operator recovers through.

    The gate is forced open deterministically rather than by timing: `verify`
    blocks until the test has seen all callers pass the check point. The cap on
    concurrent password checks would hold the derivations under the budget on
    its own, so it is opened as wide as the callers: this pins the count.
    """

    import threading

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)

    callers = 10
    app._password_checks = threading.BoundedSemaphore(callers)
    budget = app.rate_limiter.max_failures
    reached_check = threading.Semaphore(0)
    release = threading.Event()
    verifications = []
    counter = threading.Lock()

    real_limited = app.rate_limiter.limited

    def counting_limited(key):
        answer = real_limited(key)
        reached_check.release()
        return answer

    def blocking_verify(password):
        with counter:
            verifications.append(password)
        assert release.wait(timeout=10), "the test never released the verifications"
        return ""

    app.rate_limiter.limited = counting_limited
    app.auth.check = blocking_verify

    refused = []
    threads = [
        threading.Thread(target=lambda: _attempt_login(app, refused)) for _ in range(callers)
    ]
    for thread in threads:
        thread.start()
    for _ in range(callers):
        assert reached_check.acquire(timeout=10), "not every caller reached the limit check"
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert len(verifications) <= budget, (
        f"{len(verifications)} password derivations for a budget of {budget}"
    )
    assert len(refused) >= callers - budget


def _attempt_login(app, refused):
    from appliance.auth import AuthError

    try:
        app.login("wrong-on-purpose", source_ip="203.0.113.7")
    except AuthError as exc:
        if exc.code == "login_rate_limited":
            refused.append(exc.code)


def test_an_attempt_the_agent_could_not_judge_is_still_not_counted(appliance):
    """The password was never read, so it must not push towards a lockout."""

    from appliance.auth import AuthError

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)

    def unavailable(password):
        raise AuthError("agent_unavailable", "the agent is not reachable")

    app.auth.check = unavailable
    for _ in range(app.rate_limiter.max_failures + 2):
        with pytest.raises(AuthError) as excinfo:
            app.login(PASSWORD, source_ip="203.0.113.8")
        assert excinfo.value.code == "agent_unavailable"

    assert not app.rate_limiter.limited("203.0.113.8")


def test_a_correct_password_clears_what_the_attempt_itself_recorded(appliance):
    from appliance.auth import AuthError

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)

    for _ in range(app.rate_limiter.max_failures - 1):
        with pytest.raises(AuthError):
            app.login("wrong-on-purpose", source_ip="203.0.113.9")

    assert app.login(PASSWORD, source_ip="203.0.113.9") is not None
    assert not app.rate_limiter.limited("203.0.113.9")


def _counting_verify(app):
    checks = []
    real_check = app.auth.check

    def check(password):
        checks.append(password)
        return real_check(password)

    app.auth.check = check
    return checks


def _wrong_login(app, source_ip):
    from appliance.auth import AuthError

    try:
        app.login("wrong-on-purpose", source_ip=source_ip)
    except AuthError:
        pass


def _login_code(app, password, source_ip):
    from appliance.auth import AuthError

    try:
        app.login(password, source_ip=source_ip)
    except AuthError as exc:
        return exc.code
    return "ok"


def test_five_typos_on_one_ipv6_device_do_not_lock_out_its_network(appliance):
    """Every host on a SLAAC network shares one /64. Counted by the /64 alone,
    one device's five typos locked every IPv6 client on the LAN out of the
    appliance for five minutes.
    """

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)

    for _ in range(app.rate_limiter.max_failures):
        _wrong_login(app, "2001:db8:5:7::a")

    assert _login_code(app, PASSWORD, "2001:db8:5:7::a") == "login_rate_limited"
    assert _login_code(app, PASSWORD, "2001:db8:5:7::b") == "ok"


def test_a_link_local_device_keeps_its_typos_to_itself(appliance):
    """Every link-local client shares fe80::/64, on every link. The zone index
    names this appliance's interface, not the client, so it is no part of the
    source.
    """

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)

    for _ in range(app.rate_limiter.max_failures):
        _wrong_login(app, "fe80::a%eth0")

    assert _login_code(app, PASSWORD, "fe80::a%wlan0") == "login_rate_limited"
    assert _login_code(app, PASSWORD, "fe80::b%eth0") == "ok"


def test_rotating_addresses_inside_one_ipv6_64_share_its_budget(appliance):
    """A host chooses its own addresses inside its /64, so its address alone is
    a budget it renews at will -- and every attempt is 600 000 PBKDF2 rounds in
    the root agent. The /64 is the bound it cannot renew.
    """

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    checks = _counting_verify(app)
    budget = app.rate_limiter.max_network_failures
    attempts = 2 * budget

    for n in range(1, attempts + 1):
        _wrong_login(app, f"2001:db8:5:7::{n:x}")

    assert len(checks) == budget, (
        f"{len(checks)} password checks for {attempts} addresses in one /64"
    )
    assert _login_code(app, PASSWORD, "2001:db8:5:7:abcd::1") == "login_rate_limited"
    assert _login_code(app, PASSWORD, "2001:db8:5:8::1") == "ok"


def test_a_sign_in_clears_its_own_address_never_its_network(appliance):
    """A correct password proves something about one device, not about the
    other addresses in its /64. Its own attempt is not a failure, though, and
    is not left counted against the /64 either.
    """

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    limiter = app.rate_limiter
    operator = "2001:db8:5:7::a"
    typos = limiter.max_failures - 1

    for _ in range(typos):
        _wrong_login(app, operator)
    assert _login_code(app, PASSWORD, operator) == "ok"

    for n in range(1, limiter.max_network_failures - typos):
        _wrong_login(app, f"2001:db8:5:7::1:{n:x}")
    assert not limiter.limited(operator), "the sign-in itself was counted against the /64"

    _wrong_login(app, "2001:db8:5:7::2:1")
    assert _login_code(app, PASSWORD, operator) == "login_rate_limited"


def test_an_ipv4_client_on_the_dual_stack_listener_keeps_its_own_budget(appliance):
    """The listener binds `::`, so an IPv4 client arrives as ::ffff:a.b.c.d.

    Taken by its /64, every IPv4 client on the network would share one budget
    and any of them could lock all the others out.
    """

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)

    for _ in range(app.rate_limiter.max_failures):
        _wrong_login(app, "::ffff:192.0.2.10")

    assert app.rate_limiter.limited("192.0.2.10")
    assert _login_code(app, PASSWORD, "::ffff:192.0.2.11") == "ok"


def test_failures_from_other_sources_never_lock_out_an_address_with_budget_left(appliance):
    """One dual-stack host holds four sources: its IPv4 address and a global,
    a unique-local and a link-local IPv6 address. A ceiling across all sources
    that it can fill on its own keeps the operator out for as long as it goes
    on failing, with the physical console the only way back -- and protects
    nothing, since Admin and the dashboard check the same password.
    """

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    one_host = ("192.0.2.66", "2001:db8:1:2::66", "fd00:1:2:3::66", "fe80::66%eth0")

    for source in one_host:
        for _ in range(app.rate_limiter.max_failures):
            _wrong_login(app, source)

    assert all(app.rate_limiter.limited(source) for source in one_host)
    assert _login_code(app, PASSWORD, "192.0.2.50") == "ok"
    assert _login_code(app, PASSWORD, "2001:db8:1:2::50") == "ok"


def test_a_wall_clock_step_neither_extends_nor_ends_a_lockout(monkeypatch):
    """The window is measured on the monotonic clock. On the wall clock, a step
    back -- NTP correcting a Pi that booted without a real-time clock -- kept
    every failure in the window for as long as the step, and a step forward
    ended a lockout at once.
    """

    import time

    from appliance.auth import LoginRateLimiter

    wall = [1_800_000_000.0]
    monotonic = [5_000.0]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    monkeypatch.setattr(time, "monotonic", lambda: monotonic[0])
    limiter = LoginRateLimiter()

    for _ in range(limiter.max_failures):
        limiter.record_failure("192.0.2.30")
    wall[0] -= 86_400
    monotonic[0] += limiter.window_seconds + 1
    assert not limiter.limited("192.0.2.30"), "a backward wall-clock step kept the lockout"

    for _ in range(limiter.max_failures):
        limiter.record_failure("192.0.2.30")
    wall[0] += 2 * 86_400
    monotonic[0] += 1
    assert limiter.limited("192.0.2.30"), "a forward wall-clock step ended the lockout"


@contextlib.contextmanager
def _two_password_checks_held(app):
    """Two sign-ins from other sources, held inside the password check. A
    further check is recorded and answered at once."""

    entered = threading.Semaphore(0)
    release = threading.Event()
    counter = threading.Lock()
    checks = []

    def held_verify(password):
        with counter:
            checks.append(password)
            held = len(checks) <= 2
        if held:
            entered.release()
            assert release.wait(timeout=10), "the test never released the held checks"
        return ""

    app.auth.check = held_verify
    holders = [
        threading.Thread(target=_wrong_login, args=(app, f"198.51.100.{n}")) for n in (1, 2)
    ]
    try:
        for thread in holders:
            thread.start()
        for _ in holders:
            assert entered.acquire(timeout=10), "a held password check never started"
        yield checks
    finally:
        release.set()
        for thread in holders:
            thread.join(timeout=10)


def test_a_third_concurrent_password_check_is_refused_rather_than_queued(appliance):
    """Protected interleaving: two password checks are held inside the
    derivation when a third attempt arrives from a source with budget left.

    Every attempt the limiter admitted ran its PBKDF2 derivation at once in the
    root agent, as many in parallel as the attacker had sources, and that agent
    is the process an operator recovers the appliance through. The third is
    refused at once and told to try again; its password was never read, so it
    is not counted either.
    """

    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)

    with _two_password_checks_held(app) as checks:
        status, payload, _ = client.login("wrong-on-purpose")

    assert len(checks) == 2, f"{len(checks)} password checks ran at once"
    assert status == 503, payload
    assert payload["error"] == "login_busy"
    assert "try again" in payload["message"]
    assert "127.0.0.1" not in app.rate_limiter.failures


# --- the current-password check of a password change -------------------------


def _change(client, current, new_password="next-secret-1", confirmation=None):
    return client.post(
        "/api/settings/password",
        {
            "current_password": current,
            "password": new_password,
            "confirmation": new_password if confirmation is None else confirmation,
        },
    )


def test_a_wrong_current_password_spends_the_same_budget_as_a_sign_in(signed_in):
    """The password change reads the current password with the same derivation
    a sign-in does. Outside the limiter, it was a password check any session
    could run without a budget.
    """

    _, app, client = signed_in

    for _ in range(app.rate_limiter.max_failures):
        status, payload, _ = _change(client, "not-the-password")
        assert status == 400, payload
        assert payload["error"] == "current_password_invalid"

    status, payload, _ = _change(client, "not-the-password")
    assert status == 429, payload
    assert payload["error"] == "login_rate_limited"
    assert client.login()[0] == 429


def test_only_a_wrong_current_password_is_counted(signed_in):
    """A refusal after the current password was read and found right -- a new
    password that does not match its confirmation -- is no failed guess."""

    _, app, client = signed_in

    for _ in range(app.rate_limiter.max_failures + 2):
        status, payload, _ = _change(client, PASSWORD, confirmation="something-else")
        assert status == 400, payload
        assert payload["error"] == "password_mismatch"

    assert not app.rate_limiter.limited("127.0.0.1")
    assert _audited(app, "password.change") == []


def test_a_successful_password_change_leaves_no_failure_behind(appliance):
    """Only a wrong current password counts; a change that went through leaves
    neither its address nor its /64 charged."""

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    source = "2001:db8:1:2::5"
    passwords = [PASSWORD, "next-secret-1", "next-secret-2", "next-secret-3"]

    for current, new_password in zip(passwords, passwords[1:]):
        app.change_password(current, new_password, new_password, source_ip=source)

    assert app.rate_limiter.failures == {}


def test_a_password_change_that_goes_through_clears_the_typos_before_it(appliance):
    from appliance.auth import AuthError

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    source = "198.51.100.9"
    for _ in range(3):
        with pytest.raises(AuthError):
            app.change_password("not-the-password", "next-secret-1", "next-secret-1", source_ip=source)

    app.change_password(PASSWORD, "next-secret-1", "next-secret-1", source_ip=source)

    assert source not in app.rate_limiter.failures


def test_a_change_refused_after_a_correct_current_password_leaves_the_typos(appliance):
    """Only a change that goes through clears its address."""

    from appliance.auth import AuthError

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    source = "198.51.100.10"
    for _ in range(2):
        with pytest.raises(AuthError):
            app.change_password("not-the-password", "next-secret-1", "next-secret-1", source_ip=source)

    with pytest.raises(AuthError):
        app.change_password(PASSWORD, "next-secret-1", "something-else", source_ip=source)

    assert len(app.rate_limiter.failures[source]) == 2


def test_the_documented_login_budgets_are_the_ones_in_force():
    """The security model and the troubleshooting guide name these numbers."""

    from appliance.auth import LoginRateLimiter

    limiter = LoginRateLimiter()

    assert (limiter.max_failures, limiter.max_network_failures, limiter.window_seconds) == (5, 20, 300)


def test_a_password_change_waits_in_no_queue_either(signed_in):
    """Protected interleaving: two sign-ins are held inside the password check
    when a password change arrives; its current-password check is a third."""

    _, app, client = signed_in

    with _two_password_checks_held(app) as checks:
        status, payload, _ = _change(client, PASSWORD)

    assert len(checks) == 2, f"{len(checks)} password checks ran at once"
    assert status == 503, payload
    assert payload["error"] == "login_busy"
    assert "127.0.0.1" not in app.rate_limiter.failures
    assert _audited(app, "password.change") == [("failure", "busy")]


def test_a_password_change_the_agent_could_not_check_is_audited(signed_in):
    from appliance.auth import AuthError

    _, app, client = signed_in

    def unavailable(*_args, **_kwargs):
        raise AuthError("agent_unavailable", "the appliance agent is not answering")

    app.auth.change = unavailable
    status, payload, _ = _change(client, PASSWORD)

    assert status == 503, payload
    assert "127.0.0.1" not in app.rate_limiter.failures
    assert _audited(app, "password.change") == [("failure", "agent_unavailable")]


def _audited(app, action):
    return [
        (entry["result"], entry["target"])
        for entry in app.agent.handlers.services.audit.tail()
        if entry["action"] == action
    ]


def test_a_busy_refusal_is_audited_without_breaking_the_audit(appliance):
    """A reason the agent does not accept marks the audit as broken until the
    web service restarts; a busy refusal is recorded with one it does.

    Protected interleaving: two sign-ins are held inside the password check
    when a third arrives and is refused as busy.
    """

    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)

    with _two_password_checks_held(app):
        status, payload, _ = client.login("whatever")

    assert payload["error"] == "login_busy", payload
    assert ("failure", "busy") in _audited(app, "login.failure")
    assert app.audit_status()["degraded"] is False


def test_every_audit_event_and_reason_the_web_sends_is_one_the_agent_accepts():
    """Two lists that must agree: what web.py records and what the agent takes.

    Read from the source: a reason passed in a dict or by position would get
    past it, so web.py passes every reason by keyword.
    """

    from appliance import validation, web

    source = (Path(__file__).resolve().parents[1] / "appliance" / "web.py").read_text(encoding="utf-8")
    events = set(re.findall(r'audit\.record\(\s*"([^"]+)"', source))
    literal = set(re.findall(r'reason="([^"]*)"', source))
    computed = re.findall(r"reason=(?!\")([A-Za-z_][\w.]*)", source)

    assert events and literal
    assert events <= set(validation.WEB_AUDIT_EVENTS)
    assert literal <= set(validation.WEB_AUDIT_REASONS)
    assert set(computed) <= {"audit_reason"}, computed
    assert set(web._AUDIT_REASON_FOR_CODE.values()) <= set(validation.WEB_AUDIT_REASONS)
    assert set(web._AUDIT_REASON_FOR_CODE) == {"login_busy"}
    assert web.audit_reason("login_busy") == "busy"
    assert web.audit_reason("agent_unavailable") == "agent_unavailable"
    assert web.audit_reason("anything_else") == ""


def test_a_lockout_held_by_the_network_says_so_and_names_the_way_out(appliance):
    """Only an address its own failures did not lock is told the network did."""

    from appliance.auth import AuthError

    _, app, _ = appliance
    app.auth.create(PASSWORD, PASSWORD)
    own = "2001:db8:5:7::a"
    for _ in range(app.rate_limiter.max_failures):
        _wrong_login(app, own)
    for n in range(1, app.rate_limiter.max_network_failures - app.rate_limiter.max_failures + 1):
        _wrong_login(app, f"2001:db8:5:7::1:{n:x}")

    with pytest.raises(AuthError) as network:
        app.login(PASSWORD, source_ip="2001:db8:5:7:abcd::1")
    with pytest.raises(AuthError) as own_lock:
        app.login(PASSWORD, source_ip=own)

    assert network.value.code == own_lock.value.code == "login_rate_limited"
    assert "IPv4" in network.value.message
    assert "IPv4" not in own_lock.value.message


def test_a_password_change_refusal_is_audited(signed_in):
    services, app, client = signed_in

    for _ in range(app.rate_limiter.max_failures + 1):
        _change(client, "not-the-password")

    changes = [entry for entry in services.audit.tail() if entry["action"] == "password.change"]
    assert [(entry["result"], entry["target"]) for entry in changes][-2:] == [
        ("failure", "invalid_password"),
        ("denied", "rate_limited"),
    ]
    assert app.audit_status()["degraded"] is False
