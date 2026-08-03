# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging

import pytest

from dashboard.auth import write_password_file
from ems.log_buffer import RingBufferLogHandler
from test_dashboard_server import StoreStub, json_response, with_server

pytestmark = [
    pytest.mark.integration,
]


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


def test_logs_mask_cloud_route_and_product_using_config_scope(tmp_path):
    route = "DASHBOARD_LOG_CLOUD_ROUTE_7501"
    product = "DASHBOARD_LOG_PRODUCT_ACCOUNT"
    credential_probe = "DASHBOARD_LOG_CLOUD_PASSWORD_7501"
    app_key = "DASHBOARD_LOG_CLOUD_APP_KEY_7501"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {
                            "source": "zendure_cloud_mqtt",
                            "host": "mqtt.example.invalid",
                            "password": credential_probe,
                            "app_key": app_key,
                        }
                    }
                },
                "devices": [
                    {
                        "type": "zendure_mqtt",
                        "mqtt": {
                            "broker_ref": "cloud_a",
                            "product_key": product,
                            "device_id": route,
                        },
                    }
                ],
            }
        )
    )
    buffer = make_buffer(
        "logs.cloud-route",
        [
            (
                logging.INFO,
                f"pending route {route} product {product} auth {credential_probe} {app_key}",
            )
        ],
    )
    server, base_url = logs_server(
        tmp_path,
        buffer,
        config_path=str(config_path),
    )

    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs", headers={"Cookie": cookie}
        )
        flattened = json.dumps(payload)
        assert status == 200
        assert route not in flattened
        assert product not in flattened
        assert credential_probe not in flattened
        assert app_key not in flattened
    finally:
        server.shutdown()
        server.server_close()


def test_logs_mask_labeled_cloud_identifiers_without_config_context(tmp_path):
    route = "DASHBOARD_LOG_CLOUD_ROUTE_7501"
    product = "DASHBOARD_LOG_PRODUCT_ACCOUNT"
    credential_probe = "DASHBOARD_LOG_PASSWORD_7502"
    app_key = "DASHBOARD_LOG_APP_KEY_7503"
    buffer = make_buffer(
        "logs.cloud-route-no-config",
        [
            (
                logging.INFO,
                (
                    f"rejected device=Roof-{route} product={product} "
                    f"password={credential_probe} app_key={app_key} "
                    f"url=mqtt://cloud-user:{credential_probe}@broker.invalid"
                ),
            )
        ],
    )
    server, base_url = logs_server(
        tmp_path,
        buffer,
        config_path=str(tmp_path / "missing-config.json"),
    )

    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs", headers={"Cookie": cookie}
        )
        flattened = json.dumps(payload)
        assert status == 200
        assert route not in flattened
        assert product not in flattened
        assert credential_probe not in flattened
        assert app_key not in flattened
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


def test_logs_initial_fetch_returns_newest_limited_lines(tmp_path):
    buffer = make_buffer("logs.initial", [(logging.INFO, f"l{i}") for i in range(5)])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs?after=0&limit=2", headers={"Cookie": cookie}
        )
        assert status == 200
        assert [line["message"] for line in payload["lines"]] == ["l3", "l4"]
        assert payload["cursor"] == 5
    finally:
        server.shutdown()
        server.server_close()


def test_logs_endpoint_limit_cap_keeps_newest_window(tmp_path):
    buffer = make_buffer("logs.limit.cap", [(logging.INFO, f"l{i}") for i in range(1205)])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs?after=0&limit=99999", headers={"Cookie": cookie}
        )
        assert status == 200
        assert len(payload["lines"]) == 1000
        assert payload["lines"][0]["message"] == "l205"
        assert payload["lines"][-1]["message"] == "l1204"
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


def login_with_csrf(base_url):
    _, headers, payload = json_response(
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"password": "secret-password"},
    )
    return headers["Set-Cookie"], payload["csrf_token"]


def test_set_log_level_requires_session_and_csrf(tmp_path):
    buffer = make_buffer("logs.level.auth", [(logging.INFO, "x")])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        # no session
        status, _, payload = json_response(
            f"{base_url}/api/logs/level", method="POST", payload={"level": "DEBUG"}
        )
        assert status == 401
        assert payload["error"] == "not_authenticated"

        # session but no CSRF (background-style request) is rejected
        cookie, _ = login_with_csrf(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs/level",
            method="POST",
            payload={"level": "DEBUG"},
            headers={"Cookie": cookie},
        )
        assert status == 403
        assert payload["error"] == "csrf_failed"
    finally:
        server.shutdown()
        server.server_close()


def test_set_log_level_auth_not_configured(tmp_path):
    server, base_url = logs_server(tmp_path, make_buffer("logs.level.cfg", []), configured=False)
    try:
        status, _, payload = json_response(
            f"{base_url}/api/logs/level", method="POST", payload={"level": "DEBUG"}
        )
        assert status == 403
        assert payload["error"] == "auth_not_configured"
    finally:
        server.shutdown()
        server.server_close()


def test_set_log_level_changes_root_logger_and_validates(tmp_path):
    buffer = make_buffer("logs.level.set", [(logging.INFO, "x")])
    server, base_url = logs_server(tmp_path, buffer)
    original_level = logging.getLogger().level
    try:
        cookie, csrf = login_with_csrf(base_url)

        status, _, payload = json_response(
            f"{base_url}/api/logs/level",
            method="POST",
            payload={"level": "DEBUG"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["service_level"] == "DEBUG"
        assert logging.getLogger().level == logging.DEBUG

        # unknown level is rejected
        status, _, payload = json_response(
            f"{base_url}/api/logs/level",
            method="POST",
            payload={"level": "LOUD"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "bad_request"
    finally:
        logging.getLogger().setLevel(original_level)
        server.shutdown()
        server.server_close()


def test_logs_response_includes_service_level(tmp_path):
    buffer = make_buffer("logs.level.report", [(logging.INFO, "x")])
    server, base_url = logs_server(tmp_path, buffer)
    try:
        cookie = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/logs", headers={"Cookie": cookie}
        )
        assert status == 200
        assert "service_level" in payload
    finally:
        server.shutdown()
        server.server_close()
