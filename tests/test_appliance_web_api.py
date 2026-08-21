# SPDX-License-Identifier: AGPL-3.0-or-later
"""The unprivileged web service: authentication, CSRF, sessions and routing.

The web process holds no privilege of its own. These tests run the real HTTP
server against an in-process agent, so an authorisation mistake here is visible
as a request that reaches — or fails to reach — the agent.
"""

import http.client
import json
import threading

import pytest

from appliance.agent import AgentHandlers
from appliance.agent_client import AgentUnavailableError, InProcessAgentClient
from appliance.auth import MIN_PASSWORD_LENGTH, SESSION_COOKIE_NAME, AuthStore
from appliance.web import ApplianceWebApp, ApplianceWebServer
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

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
    app.auth = AuthStore(services.paths.auth_file, iterations=1000)
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
    assert set(unauthenticated) <= {"authenticated", "password_configured", "appliance_version"}

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


def test_first_password_must_be_confirmed_and_long_enough(appliance):
    _, _, client = appliance
    status, payload, _ = client.post("/api/session/setup", {"password": "short", "confirmation": "short"})
    assert status == 400
    assert payload["error"] == "password_too_short"
    assert MIN_PASSWORD_LENGTH >= 12

    status, payload, _ = client.post(
        "/api/session/setup", {"password": PASSWORD, "confirmation": "different-one-here"}
    )
    assert payload["error"] == "password_mismatch"


def test_setup_is_refused_once_a_password_exists(appliance):
    _, app, client = appliance
    app.auth.create(PASSWORD, PASSWORD)
    status, payload, _ = client.post(
        "/api/session/setup", {"password": "another-password-1", "confirmation": "another-password-1"}
    )
    assert status == 409
    assert payload["error"] == "password_already_configured"


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
