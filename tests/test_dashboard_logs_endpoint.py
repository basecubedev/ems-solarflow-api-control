# SPDX-License-Identifier: AGPL-3.0-or-later
import logging

from dashboard.auth import write_password_file
from ems.log_buffer import RingBufferLogHandler
from test_dashboard_server import StoreStub, json_response, with_server


def make_buffer(name, lines):
    handler = RingBufferLogHandler(capacity=1000)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    logger.propagate = False
    for level, message in lines:
        logger.log(level, message)
    return handler


def logs_server(tmp_path, buffer=None, configured=True, **kwargs):
    auth_file = tmp_path / "dashboard-auth.json"
    if configured:
        write_password_file(auth_file, "secret-password")
    server, base_url = with_server(
        StoreStub(),
        auth_file=str(auth_file),
        log_buffer=buffer,
        **kwargs,
    )
    return server, base_url


def login(base_url):
    _, headers, payload = json_response(
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"password": "secret-password"},
    )
    return headers["Set-Cookie"]


def test_logs_require_authentication(tmp_path):
    buffer = make_buffer("logs.auth", [(logging.INFO, "hello")])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        status, _, payload = json_response(f"{base_url}/api/logs")
        assert status == 401
        assert payload["error"] == "not_authenticated"

        status, _, payload = json_response(
            f"{base_url}/api/logs",
            headers={"Cookie": "ems_dashboard_session=forged"},
        )
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()


def test_logs_when_auth_not_configured_is_forbidden(tmp_path):
    server, base_url = logs_server(tmp_path, make_buffer("logs.cfg", []), configured=False)
    try:
        status, _, payload = json_response(f"{base_url}/api/logs")
        assert status == 403
        assert payload["error"] == "auth_not_configured"
    finally:
        server.shutdown()
        server.server_close()


def test_logs_return_lines_and_headers(tmp_path):
    buffer = make_buffer(
        "logs.lines",
        [(logging.INFO, "first"), (logging.WARNING, "second")],
    )
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, headers, payload = json_response(
            f"{base_url}/api/logs", headers={"Cookie": cookie}
        )
        assert status == 200
        assert [line["message"] for line in payload["lines"]] == ["first", "second"]
        assert payload["cursor"] == 2
        assert payload["dropped"] is False
        assert headers["Cache-Control"] == "no-store"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
    finally:
        server.shutdown()
        server.server_close()


def test_logs_after_cursor_returns_only_newer(tmp_path):
    buffer = make_buffer(
        "logs.cursor",
        [(logging.INFO, "a"), (logging.INFO, "b"), (logging.INFO, "c")],
    )
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs?after=1", headers={"Cookie": cookie}
        )
        assert status == 200
        assert [line["seq"] for line in payload["lines"]] == [2, 3]
    finally:
        server.shutdown()
        server.server_close()


def test_logs_limit_is_clamped(tmp_path):
    buffer = make_buffer("logs.limit", [(logging.INFO, f"l{i}") for i in range(10)])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs?limit=99999", headers={"Cookie": cookie}
        )
        assert status == 200
        assert len(payload["lines"]) == 10  # capped at MAX_LOG_LINES, all returned
    finally:
        server.shutdown()
        server.server_close()


def test_logs_invalid_params_are_bad_request(tmp_path):
    buffer = make_buffer("logs.invalid", [(logging.INFO, "x")])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        for query in ("after=-1", "after=abc", "limit=-5", "level=LOUD"):
            status, _, payload = json_response(
                f"{base_url}/api/logs?{query}", headers={"Cookie": cookie}
            )
            assert status == 400, query
            assert payload["error"] == "bad_request"
    finally:
        server.shutdown()
        server.server_close()


def test_logs_level_filter(tmp_path):
    buffer = make_buffer(
        "logs.level",
        [(logging.INFO, "i"), (logging.WARNING, "w"), (logging.ERROR, "e")],
    )
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs?level=WARNING", headers={"Cookie": cookie}
        )
        assert status == 200
        assert [line["message"] for line in payload["lines"]] == ["w", "e"]
    finally:
        server.shutdown()
        server.server_close()


def test_logs_reject_non_get(tmp_path):
    buffer = make_buffer("logs.method", [(logging.INFO, "x")])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs",
            method="POST",
            payload={},
            headers={"Cookie": cookie},
        )
        assert status == 405
        assert payload["error"] == "read_only"
    finally:
        server.shutdown()
        server.server_close()


def test_logs_neutralize_injection_and_optional_redaction(tmp_path):
    buffer = make_buffer(
        "logs.xss",
        [(logging.INFO, "evil\r\ninjected line"), (logging.INFO, "token=supersecret123")],
    )
    # redaction on: secret-looking values masked end-to-end
    server, base_url = logs_server(tmp_path, buffer, log_redaction=True)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs", headers={"Cookie": cookie}
        )
        assert status == 200
        messages = [line["message"] for line in payload["lines"]]
        # CR/LF injection neutralized by the buffer (no embedded newline)
        assert "\n" not in messages[0] and "\r" not in messages[0]
        # redaction masked the secret
        assert "supersecret123" not in messages[1]
    finally:
        server.shutdown()
        server.server_close()
