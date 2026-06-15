# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from types import SimpleNamespace

import pytest

from dashboard.auth import SessionStore, write_password_file
from dashboard.server import (
    MAX_JSON_BODY_BYTES,
    SECURITY_HEADERS,
    DashboardRequestHandler,
    JsonBodyLengthError,
    JsonBodyTooLarge,
    SSEConnectionLimiter,
    start_dashboard_server,
)
from dashboard.auth import LoginRateLimiter
from dashboard.runtime_write import build_validation_context


class StoreStub:
    def __init__(self):
        self.history_ranges = []

    def latest(self):
        return {
            "timestamp": "2026-06-03T12:00:00+00:00",
            "pv_total_w": 1200,
        }

    def history(self, range_name):
        self.history_ranges.append(range_name)
        return [
            {
                "timestamp": "2026-06-03T11:55:00+00:00",
                "pv_total_w": 1000,
            }
        ]

    def energy_summary(self):
        return {
            "enabled": True,
            "currency": "EUR",
            "today": {"inverter_output_wh": 1000},
            "yesterday": {"inverter_output_wh": 800},
        }


class RuntimeStateStub:
    def __init__(self):
        self.data = {
            "system": {
                "enabled": True,
                "max_total_power": 900,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {
                "enabled": True,
                "control_enabled": True,
            },
            "winter": {
                "enabled": False,
            },
            "devices": {
                "WR1": {
                    "enabled": True,
                    "max_power": 800,
                    "offgrid_socket_mode": "off",
                    "pv_priority_factor": 1.0,
                }
            },
        }
        self.saved = 0

    def save_atomic(self):
        self.saved += 1


def direct_handler(path, *, method="GET", body=b"", auth_file=None, runtime_state=None):
    sent_headers = {}
    runtime_state = runtime_state or RuntimeStateStub()
    handler = SimpleNamespace(
        path=path,
        headers={
            "Content-Length": str(len(body)),
        },
        rfile=BytesIO(body),
        wfile=BytesIO(),
        client_address=("127.0.0.1", 12345),
        server=SimpleNamespace(
            store=StoreStub(),
            runtime_state=runtime_state,
            runtime_validation=build_validation_context(runtime_state=runtime_state),
            auth_file=str(auth_file) if auth_file else "",
            sessions=SessionStore(),
            login_limiter=LoginRateLimiter(),
            https_active=False,
            sse_limiter=SSEConnectionLimiter(8, 2),
            sse_max_connection_seconds=1,
        ),
    )

    def send_response(status):
        handler.status = status

    def send_header(key, value):
        sent_headers[key] = value

    def end_headers():
        pass

    def send_error(status):
        handler.status = status

    handler.send_response = send_response
    handler.send_header = send_header
    handler._send_security_headers = lambda: DashboardRequestHandler._send_security_headers(handler)
    handler.end_headers = end_headers
    handler.send_error = send_error
    handler.log_message = lambda *args: None
    for name in (
        "_send_json",
        "_send_static",
        "_handle_runtime_patch",
        "_json_body_preflight",
        "_json_body_length",
        "_read_json_body",
        "_require_write_auth",
        "_auth_configured",
        "_current_session",
        "_session_cookie_value",
    ):
        setattr(
            handler,
            name,
            lambda *args, _name=name, **kwargs: getattr(
                DashboardRequestHandler,
                _name,
            )(handler, *args, **kwargs),
        )

    if method == "GET":
        DashboardRequestHandler.do_GET(handler)
    elif method == "PATCH":
        DashboardRequestHandler.do_PATCH(handler)
    else:
        raise AssertionError(f"unsupported direct handler method {method}")

    raw_body = handler.wfile.getvalue()
    return handler.status, sent_headers, raw_body


def read_response(url, method="GET", payload=None, headers=None):
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def json_response(url, method="GET", payload=None, headers=None):
    status, headers, body = read_response(
        url,
        method=method,
        payload=payload,
        headers=headers,
    )
    return status, headers, json.loads(body.decode("utf-8"))


def with_server(store, **kwargs):
    try:
        server = start_dashboard_server(store, host="127.0.0.1", port=0, **kwargs)
    except PermissionError as exc:
        pytest.skip(f"local socket creation is not permitted: {exc}")

    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_dashboard_server_serves_read_only_api_endpoints():
    store = StoreStub()
    server, base_url = with_server(store)

    try:
        status, headers, live = json_response(f"{base_url}/api/live")
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        assert headers["Cache-Control"] == "no-store"
        assert live["pv_total_w"] == 1200

        status, _, history = json_response(f"{base_url}/api/history?range=1h")
        assert status == 200
        assert history["range"] == "1h"
        assert history["items"][0]["pv_total_w"] == 1000
        assert store.history_ranges == ["1h"]

        status, _, energy = json_response(f"{base_url}/api/energy-stats")
        assert status == 200
        assert energy["yesterday"]["inverter_output_wh"] == 800
    finally:
        server.shutdown()
        server.server_close()


def test_read_only_gets_remain_public_when_auth_is_configured(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")

    status, headers, body = direct_handler("/", auth_file=auth_file)
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b'id="loginModal"' in body

    status, headers, body = direct_handler("/api/live", auth_file=auth_file)
    assert status == 200
    assert "application/json" in headers["Content-Type"]
    assert json.loads(body.decode("utf-8"))["pv_total_w"] == 1200

    status, headers, body = direct_handler("/api/runtime", auth_file=auth_file)
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["system"]["max_total_power"] == 900
    assert "_limits" in payload


def test_runtime_patch_without_session_is_rejected_when_auth_is_configured(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    body = json.dumps({"max_total_power": 700}).encode("utf-8")

    status, _, raw_body = direct_handler(
        "/api/runtime/system",
        method="PATCH",
        body=body,
        auth_file=auth_file,
    )

    assert status == 401
    assert json.loads(raw_body.decode("utf-8"))["error"] == "not_authenticated"


def test_dashboard_server_rejects_invalid_history_range_and_write_methods():
    store = StoreStub()
    server, base_url = with_server(store)

    try:
        status, _, payload = json_response(f"{base_url}/api/history?range=bad")
        assert status == 400
        assert payload["error"] == "unsupported_range"
        assert "1h" in payload["supported"]

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            if method == "PATCH":
                url = f"{base_url}/api/live"
            else:
                url = f"{base_url}/api/live"
            status, _, payload = json_response(url, method=method)
            assert status == 405
            assert payload == {"error": "read_only"}
    finally:
        server.shutdown()
        server.server_close()


def test_auth_status_login_logout_and_cookie_flags(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    store = StoreStub()
    server, base_url = with_server(store, auth_file=str(auth_file))

    try:
        status, _, payload = json_response(f"{base_url}/api/auth/status")
        assert status == 200
        assert payload == {
            "auth_configured": True,
            "authenticated": False,
            "write_mode_available": True,
            "write_mode_active": False,
        }

        status, _, payload = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "wrong"},
        )
        assert status == 403
        assert payload["error"] == "invalid_password"

        status, headers, payload = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "secret-password"},
        )
        assert status == 200
        assert payload["authenticated"] is True
        assert payload["write_mode_active"] is True
        assert payload["csrf_token"]
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Secure" not in cookie

        status, headers, payload = json_response(
            f"{base_url}/api/auth/logout",
            method="POST",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert "Max-Age=0" in headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()


def test_login_fails_when_auth_is_not_configured(tmp_path):
    server, base_url = with_server(StoreStub(), auth_file=str(tmp_path / "missing.json"))

    try:
        status, _, payload = json_response(f"{base_url}/api/auth/status")
        assert status == 200
        assert payload["auth_configured"] is False
        assert payload["write_mode_available"] is False

        status, headers, payload = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "secret-password"},
        )
        assert status == 403
        assert payload["error"] == "invalid_password"
        assert "Set-Cookie" not in headers
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_write_requires_auth_and_csrf_and_validates_payload(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    runtime_state = RuntimeStateStub()
    server, base_url = with_server(
        StoreStub(),
        runtime_state=runtime_state,
        auth_file=str(auth_file),
    )

    try:
        status, _, payload = json_response(
            f"{base_url}/api/runtime/system",
            method="PATCH",
            payload={"max_total_power": 700},
        )
        assert status == 401
        assert payload["error"] == "not_authenticated"

        _, login_headers, login = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "secret-password"},
        )
        cookie = login_headers["Set-Cookie"]
        csrf = login["csrf_token"]

        status, _, payload = json_response(
            f"{base_url}/api/runtime/system",
            method="PATCH",
            payload={"max_total_power": 700},
            headers={"Cookie": cookie},
        )
        assert status == 403
        assert payload["error"] == "csrf_failed"

        status, _, payload = json_response(
            f"{base_url}/api/runtime/system",
            method="PATCH",
            payload={"unknown": 700},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "unknown field" in payload["message"]

        status, _, payload = json_response(
            f"{base_url}/api/runtime/system",
            method="PATCH",
            payload={"max_total_power": -1},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400

        status, _, payload = json_response(
            f"{base_url}/api/runtime/system",
            method="PATCH",
            payload={"max_total_power": 700},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["updated"] is True
        assert runtime_state.data["system"]["max_total_power"] == 700
        assert runtime_state.saved == 1

        device_name = urllib.parse.quote("WR1")
        status, _, payload = json_response(
            f"{base_url}/api/runtime/device/{device_name}",
            method="PATCH",
            payload={"offgrid_socket_mode": "eco"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert runtime_state.data["devices"]["WR1"]["offgrid_socket_mode"] == "eco"
    finally:
        server.shutdown()
        server.server_close()


def test_repeated_failed_login_attempts_are_rate_limited(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    server, base_url = with_server(StoreStub(), auth_file=str(auth_file))

    try:
        status = None
        payload = None
        for _ in range(6):
            status, _, payload = json_response(
                f"{base_url}/api/auth/login",
                method="POST",
                payload={"password": "wrong"},
            )
        assert status == 429
        assert payload["error"] == "login_rate_limited"
    finally:
        server.shutdown()
        server.server_close()


def test_secure_cookie_flag_is_set_when_https_active(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    server, base_url = with_server(
        StoreStub(),
        auth_file=str(auth_file),
        ssl_enabled=False,
    )
    server.https_active = True

    try:
        status, headers, _ = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "secret-password"},
        )
        assert status == 200
        assert "Secure" in headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()


def test_json_body_limit_and_invalid_content_length_are_rejected_before_read():
    def bind_body_helpers(fake):
        fake._json_body_length = lambda: DashboardRequestHandler._json_body_length(fake)
        return fake

    oversized = SimpleNamespace(
        headers={"Content-Length": str(MAX_JSON_BODY_BYTES + 1)},
        rfile=BytesIO(b""),
    )
    with pytest.raises(JsonBodyTooLarge):
        DashboardRequestHandler._read_json_body(bind_body_helpers(oversized))

    invalid = SimpleNamespace(
        headers={"Content-Length": "not-an-int"},
        rfile=BytesIO(b""),
    )
    with pytest.raises(JsonBodyLengthError):
        DashboardRequestHandler._read_json_body(bind_body_helpers(invalid))

    empty = SimpleNamespace(
        headers={},
        rfile=BytesIO(b""),
    )
    assert DashboardRequestHandler._read_json_body(bind_body_helpers(empty)) == {}


def test_json_responses_include_no_store_and_security_headers():
    sent_headers = {}

    class Handler:
        wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            sent_headers[key] = value

        def _send_security_headers(self):
            DashboardRequestHandler._send_security_headers(self)

        def end_headers(self):
            pass

    DashboardRequestHandler._send_json(Handler(), {"ok": True})

    assert sent_headers["Cache-Control"] == "no-store"
    assert sent_headers["Pragma"] == "no-cache"
    assert sent_headers["X-Content-Type-Options"] == "nosniff"
    assert sent_headers["X-Frame-Options"] == "DENY"


def test_failed_login_does_not_set_cookie_without_socket(tmp_path):
    captured = {}
    fake = SimpleNamespace(
        client_address=("127.0.0.1", 12345),
        server=SimpleNamespace(
            auth_file=str(tmp_path / "missing-auth.json"),
            login_limiter=LoginRateLimiter(),
        ),
    )
    fake._json_body_preflight = lambda: None
    fake._auth_configured = lambda: True
    fake._read_json_body = lambda: {"password": "wrong"}
    fake._send_json = lambda payload, status=200, headers=None: captured.update(
        {"payload": payload, "status": status, "headers": headers or {}}
    )

    DashboardRequestHandler._handle_login(fake)

    assert captured["status"] == 403
    assert captured["payload"]["error"] == "invalid_password"
    assert "Set-Cookie" not in captured["headers"]


def test_sse_connection_limiter_enforces_per_ip_and_global_limits():
    limiter = SSEConnectionLimiter(max_global=3, max_per_ip=2)

    assert limiter.acquire("127.0.0.1")
    assert limiter.acquire("127.0.0.1")
    assert not limiter.acquire("127.0.0.1")

    assert limiter.acquire("127.0.0.2")
    assert not limiter.acquire("127.0.0.3")

    limiter.release("127.0.0.1")
    assert limiter.acquire("127.0.0.3")
    limiter.release("127.0.0.1")
    limiter.release("127.0.0.2")
    limiter.release("127.0.0.3")
    assert limiter.total == 0
    assert limiter.by_ip == {}


def test_security_headers_do_not_require_unsafe_inline():
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"


def test_dashboard_server_serves_static_index_and_blocks_missing_paths():
    store = StoreStub()
    server, base_url = with_server(store)

    try:
        status, headers, body = read_response(f"{base_url}/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert b"Energy" in body or b"EMS" in body

        status, _, _ = read_response(f"{base_url}/../config.json")
        assert status == 404

        status, _, _ = read_response(f"{base_url}/does-not-exist.js")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_auth_refresh_requires_session_and_csrf_and_slides(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    server, base_url = with_server(StoreStub(), auth_file=str(auth_file))

    try:
        # No session at all -> not authenticated.
        status, _, payload = json_response(
            f"{base_url}/api/auth/refresh", method="POST"
        )
        assert status == 401
        assert payload["error"] == "not_authenticated"

        _, login_headers, login = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "secret-password"},
        )
        cookie = login_headers["Set-Cookie"]
        csrf = login["csrf_token"]

        # Valid session but no CSRF token (as a background poll would send) must
        # NOT be able to renew the session.
        status, _, payload = json_response(
            f"{base_url}/api/auth/refresh",
            method="POST",
            headers={"Cookie": cookie},
        )
        assert status == 403
        assert payload["error"] == "csrf_failed"

        # Session + CSRF (a genuine-activity heartbeat) renews and reports
        # remaining lifetime.
        status, _, payload = json_response(
            f"{base_url}/api/auth/refresh",
            method="POST",
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["authenticated"] is True
        assert payload["session_expires_in_seconds"] >= 0
    finally:
        server.shutdown()
        server.server_close()


def test_auth_refresh_when_auth_not_configured_is_forbidden(tmp_path):
    server, base_url = with_server(
        StoreStub(), auth_file=str(tmp_path / "missing.json")
    )

    try:
        status, _, payload = json_response(
            f"{base_url}/api/auth/refresh", method="POST"
        )
        assert status == 403
        assert payload["error"] == "auth_not_configured"
    finally:
        server.shutdown()
        server.server_close()
