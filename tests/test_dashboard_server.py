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
    _external_mqtt_status_payload,
    _replace_external_device_names,
    _resolve_external_device_name,
    start_dashboard_server,
)
from dashboard.auth import LoginRateLimiter
from dashboard.runtime_write import build_validation_context

pytestmark = [
    pytest.mark.integration,
]


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


class CloudStatusStore(StoreStub):
    route = "DASHBOARD_CLOUD_ROUTE_7501"
    product = "DASHBOARD_PRODUCT_ACCOUNT"

    def latest(self):
        return {
            "timestamp": "2026-06-03T12:00:00+00:00",
            "devices": {
                self.route: {
                    "broker_ref": "cloud_a",
                    "name": f"SolarFlow {self.route}",
                    "detail": f"account product {self.product}",
                    "write_topic": (
                        f"iot/{self.product}/{self.route}/properties/write"
                    ),
                }
            },
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


def test_ui_config_reports_animation_mode():
    store = StoreStub()
    server, base_url = with_server(store, animation_mode="reduced")
    try:
        status, headers, payload = json_response(f"{base_url}/api/ui-config")
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        assert payload == {"animation_mode": "reduced"}
    finally:
        server.shutdown()
        server.server_close()


def test_ui_config_defaults_to_normal_and_sanitizes_invalid():
    store = StoreStub()
    server, base_url = with_server(store, animation_mode="bogus")
    try:
        _, _, payload = json_response(f"{base_url}/api/ui-config")
        assert payload == {"animation_mode": "normal"}
    finally:
        server.shutdown()
        server.server_close()


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


def test_live_endpoint_masks_cloud_routes_using_config_scope(tmp_path):
    store = CloudStatusStore()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {
                            "source": "zendure_cloud_mqtt",
                            "host": "mqtt.example.invalid",
                        }
                    }
                },
                "devices": [
                    {
                        "type": "zendure_mqtt",
                        "mqtt": {
                            "broker_ref": "cloud_a",
                            "product_key": store.product,
                            "device_id": store.route,
                        },
                    }
                ],
            }
        )
    )
    server, base_url = with_server(store, config_path=str(config_path))

    try:
        status, _, payload = json_response(f"{base_url}/api/live")
        flattened = json.dumps(payload)
        assert status == 200
        assert store.route not in flattened
        assert store.product not in flattened
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


class SeriesStoreStub(StoreStub):
    """Store stub exposing a ``path`` to a minimal snapshots SQLite database."""

    def __init__(self, path):
        super().__init__()
        self.path = path


def _seed_snapshots(path, device_name="WR1"):
    import sqlite3
    from datetime import datetime, timedelta, timezone

    con = sqlite3.connect(path)
    con.execute("CREATE TABLE snapshots (timestamp TEXT PRIMARY KEY, payload TEXT)")
    now = datetime.now(timezone.utc)
    for index in range(3):
        ts = (now - timedelta(minutes=10 * (2 - index))).isoformat()
        payload = json.dumps(
            {
                "timestamp": ts,
                "pv_total_w": 1000 + index * 100,
                "inverter_output_w": 400 + index * 50,
                "battery_power_w": 200 - index * 50,
                "devices": {device_name: {"pv_input_w": 600 + index * 60}},
            }
        )
        con.execute(
            "INSERT INTO snapshots(timestamp, payload) VALUES (?, ?)", (ts, payload)
        )
    con.commit()
    con.close()


def test_history_series_endpoint_returns_columnar_sqlite_data(tmp_path):
    db_path = str(tmp_path / "dashboard.sqlite")
    _seed_snapshots(db_path)
    store = SeriesStoreStub(db_path)
    server, base_url = with_server(store)

    try:
        status, _, payload = json_response(
            f"{base_url}/api/history/series?range=24h&series=pv,output,battery"
        )
        assert status == 200
        assert payload["range"] == "24h"
        assert payload["source"] == "sqlite"
        assert len(payload["time"]) == 3
        assert payload["series"]["pv"][0] == 1000
        assert payload["series"]["output"][2] == 500
        assert set(payload["series"].keys()) == {"pv", "output", "battery"}

        # Device filter reads per-device snapshot fields.
        status, _, filtered = json_response(
            f"{base_url}/api/history/series?range=24h&series=pv&devices=WR1"
        )
        assert status == 200
        assert filtered["devices"] == ["WR1"]
        assert filtered["series"]["pv"][0] == 600

        status, _, bad = json_response(f"{base_url}/api/history/series?range=bad")
        assert status == 400
        assert bad["error"] == "unsupported_range"
        assert "365d" in bad["supported"]

        # Custom date range via explicit start/end epoch bounds.
        import time as _time

        now = int(_time.time())
        status, _, custom = json_response(
            f"{base_url}/api/history/series?start={now - 3600}&end={now + 3600}&series=pv"
        )
        assert status == 200
        assert custom["range"] == "custom"
        assert len(custom["time"]) == 3

        status, _, invalid = json_response(
            f"{base_url}/api/history/series?start={now}&end={now - 3600}"
        )
        assert status == 400
        assert invalid["error"] == "invalid_range"
    finally:
        server.shutdown()
        server.server_close()


def test_masked_cloud_device_alias_round_trips_runtime_and_history(tmp_path):
    route = "DASHBOARD_ACCOUNT_ROUTE_7501"
    product = "DASHBOARD_ACCOUNT_PRODUCT"
    raw_name = f"Secret Roof {route}"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {"source": "zendure_cloud_mqtt"}
                    }
                },
                "devices": [
                    {
                        "name": raw_name,
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
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    runtime_state = RuntimeStateStub()
    runtime_state.data["devices"] = {
        raw_name: {
            "enabled": True,
            "max_power": 800,
            "offgrid_socket_mode": "off",
            "pv_priority_factor": 1.0,
        }
    }
    database_path = str(tmp_path / "dashboard.sqlite")
    _seed_snapshots(database_path, raw_name)
    server, base_url = with_server(
        SeriesStoreStub(database_path),
        runtime_state=runtime_state,
        runtime_validation=build_validation_context(
            config=json.loads(config_path.read_text())
        ),
        auth_file=str(auth_file),
        config_path=str(config_path),
    )

    try:
        status, _, runtime = json_response(f"{base_url}/api/runtime")
        assert status == 200
        aliases = list(runtime["devices"])
        assert len(aliases) == 1
        alias = aliases[0]
        assert route not in alias
        assert set(runtime["_limits"]["devices"]) == {alias}

        _, login_headers, login = json_response(
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"password": "secret-password"},
        )
        status, _, updated = json_response(
            f"{base_url}/api/runtime/device/{urllib.parse.quote(alias)}",
            method="PATCH",
            payload={"offgrid_socket_mode": "eco"},
            headers={
                "Cookie": login_headers["Set-Cookie"],
                "X-CSRF-Token": login["csrf_token"],
            },
        )
        assert status == 200
        assert route not in json.dumps(updated)
        assert runtime_state.data["devices"][raw_name]["offgrid_socket_mode"] == "eco"

        del runtime_state.data["devices"][raw_name]
        status, _, rejected = json_response(
            f"{base_url}/api/runtime/device/{urllib.parse.quote(alias)}",
            method="PATCH",
            payload={"offgrid_socket_mode": "standard"},
            headers={
                "Cookie": login_headers["Set-Cookie"],
                "X-CSRF-Token": login["csrf_token"],
            },
        )
        assert status == 400
        assert route not in json.dumps(rejected)

        status, _, history = json_response(
            f"{base_url}/api/history/series?range=24h&series=pv&devices="
            f"{urllib.parse.quote(alias)}"
        )
        assert status == 200
        assert history["devices"] == [alias]
        assert history["series"]["pv"][0] == 600
        assert route not in json.dumps(history)
    finally:
        server.shutdown()
        server.server_close()


def test_colliding_masked_cloud_device_names_receive_distinct_aliases(tmp_path):
    routes = ["DASHBOARD_ACCOUNT_ALPHA_7501", "DASHBOARD_ACCOUNT_BETA_7501"]
    product = "DASHBOARD_ACCOUNT_PRODUCT"
    raw_names = [f"Roof {route}" for route in routes]
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {"source": "zendure_cloud_mqtt"}
                    }
                },
                "devices": [
                    {
                        "name": name,
                        "type": "zendure_mqtt",
                        "mqtt": {
                            "broker_ref": "cloud_a",
                            "product_key": product,
                            "device_id": route,
                        },
                    }
                    for name, route in zip(raw_names, routes)
                ],
            }
        )
    )
    runtime_state = RuntimeStateStub()
    template = next(iter(runtime_state.data["devices"].values()))
    runtime_state.data["devices"] = {
        name: dict(template) for name in raw_names
    }
    server, base_url = with_server(
        StoreStub(),
        runtime_state=runtime_state,
        runtime_validation=build_validation_context(
            config=json.loads(config_path.read_text())
        ),
        config_path=str(config_path),
    )

    try:
        status, _, runtime = json_response(f"{base_url}/api/runtime")
        assert status == 200
        assert len(runtime["devices"]) == 2
        assert len(set(runtime["devices"])) == 2
        assert not any(route in json.dumps(runtime) for route in routes)
        assert set(runtime["devices"]) == set(runtime["_limits"]["devices"])
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_status_redaction_fails_closed_when_config_is_missing(tmp_path):
    route = "DASHBOARD_ACCOUNT_ROUTE_7501"
    product = "DASHBOARD_ACCOUNT_PRODUCT"
    raw_name = f"Roof {route}"
    missing_config_path = tmp_path / "missing-config.json"
    config = {
        "zendure_mqtt": {
            "brokers": {"cloud_a": {"source": "zendure_cloud_mqtt"}}
        },
        "devices": [
            {
                "name": raw_name,
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "product_key": product,
                    "device_id": route,
                },
            }
        ],
    }

    class MissingConfigStore(StoreStub):
        def latest(self):
            return {
                "devices": {
                    raw_name: {
                        "name": raw_name,
                        "device_id": route,
                        "product_key": product,
                        "detail": f"route={route} product={product}",
                        "write_topic": f"iot/{product}/{route}/properties/write",
                    }
                }
            }

    server, base_url = with_server(
        MissingConfigStore(),
        runtime_validation=build_validation_context(config=config),
        config_path=str(missing_config_path),
    )
    try:
        status, _, payload = json_response(f"{base_url}/api/live")
        assert status == 200
        flattened = json.dumps(payload)
        assert route not in flattened
        assert product not in flattened
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_status_redaction_fails_closed_with_empty_config(tmp_path):
    route = "DASHBOARD_ACCOUNT_ROUTE_7501"
    product = "DASHBOARD_ACCOUNT_PRODUCT"
    diagnostic_route = "DASHBOARD_DIAGNOSTIC_ROUTE_7502"
    metric_route = "DASHBOARD_METRIC_ROUTE_7503"
    source_route = "DASHBOARD_SOURCE_ROUTE_7504"
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    class PartialConfigStore(StoreStub):
        def latest(self):
            return {
                "device_id": route,
                "product_key": product,
                "detail": f"device_id={route} product_key={product}",
                "devices": {
                    route: {
                        "device_id": route,
                        "product_key": product,
                    }
                },
                "diagnostic_by_route": {diagnostic_route: {"ok": False}},
                "metrics": {metric_route: 1},
                "sources": {source_route: {"status": "stale"}},
            }

    server, base_url = with_server(
        PartialConfigStore(),
        config_path=str(config_path),
    )
    try:
        status, _, payload = json_response(f"{base_url}/api/live")
        assert status == 200
        flattened = json.dumps(payload)
        assert route not in flattened
        assert product not in flattened
        assert diagnostic_route not in flattened
        assert metric_route not in flattened
        assert source_route not in flattened
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_status_redaction_uses_last_good_config_after_read_failure(tmp_path):
    route = "DASHBOARD_ACCOUNT_ROUTE_7501"
    product = "DASHBOARD_ACCOUNT_PRODUCT"
    raw_name = f"Roof {route}"
    config_path = tmp_path / "config.json"
    config = {
        "zendure_mqtt": {
            "brokers": {"cloud_a": {"source": "zendure_cloud_mqtt"}}
        },
        "devices": [
            {
                "name": raw_name,
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "product_key": product,
                    "device_id": route,
                },
            }
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    class CachedConfigStore(StoreStub):
        def latest(self):
            return {
                "devices": {
                    raw_name: {
                        "detail": f"route={route} product={product}",
                    }
                }
            }

    server, base_url = with_server(
        CachedConfigStore(),
        runtime_validation=build_validation_context(config=config),
        config_path=str(config_path),
    )
    try:
        config_path.unlink()
        status, _, payload = json_response(f"{base_url}/api/live")
        assert status == 200
        flattened = json.dumps(payload)
        assert route not in flattened
        assert product not in flattened
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_status_redaction_retains_old_context_during_config_replacement(tmp_path):
    old_route = "DASHBOARD_OLD_ACCOUNT_ROUTE_7501"
    new_route = "DASHBOARD_NEW_ACCOUNT_ROUTE_7502"
    product = "DASHBOARD_ACCOUNT_PRODUCT"

    def config_for(route):
        return {
            "zendure_mqtt": {
                "brokers": {"cloud_a": {"source": "zendure_cloud_mqtt"}}
            },
            "devices": [
                {
                    "name": f"Roof {route}",
                    "type": "zendure_mqtt",
                    "mqtt": {
                        "broker_ref": "cloud_a",
                        "product_key": product,
                        "device_id": route,
                    },
                }
            ],
        }

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_for(old_route)), encoding="utf-8")

    class ReplacedConfigStore(StoreStub):
        def latest(self):
            return {
                "devices": {
                    f"Roof {old_route}": {
                        "detail": f"stale route {old_route}",
                    }
                }
            }

    server, base_url = with_server(
        ReplacedConfigStore(), config_path=str(config_path)
    )
    try:
        config_path.write_text(json.dumps(config_for(new_route)), encoding="utf-8")
        status, _, payload = json_response(f"{base_url}/api/live")
        assert status == 200
        assert old_route not in json.dumps(payload)
    finally:
        server.shutdown()
        server.server_close()


def test_external_alias_scalar_replacement_is_single_pass():
    first = "Roof ROUTE_ALPHA_7501"
    second = "Roof …7501"
    aliases = {
        first: "Roof …7501 [1]",
        second: "Roof …7501 [2]",
    }

    assert _replace_external_device_names(
        {"devices": [first, second]}, aliases
    ) == {"devices": ["Roof …7501 [1]", "Roof …7501 [2]"]}


def test_fail_closed_learned_device_alias_round_trips_without_config(tmp_path):
    raw_name = "Roof MISSING_CONFIG_CLOUD_ROUTE_7501"
    server = SimpleNamespace(
        config_path=str(tmp_path / "missing.json"),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server, {"devices": {raw_name: {"name": raw_name}}}
    )
    alias = next(iter(safe["devices"]))

    assert raw_name not in json.dumps(safe)
    assert _resolve_external_device_name(server, alias) == raw_name


def test_valid_cloud_context_preserves_ordinary_status_reason_and_detail(tmp_path):
    route = "DASHBOARD_STATUS_ROUTE_7501"
    raw_name = f"Roof {route}"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {"source": "zendure_cloud_mqtt"}
                    }
                },
                "devices": [
                    {
                        "name": raw_name,
                        "type": "zendure_mqtt",
                        "mqtt": {
                            "broker_ref": "cloud_a",
                            "product_key": "DASHBOARD_STATUS_PRODUCT",
                            "device_id": route,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    server = SimpleNamespace(
        config_path=str(config_path),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {
            "devices": {
                raw_name: {
                    "reason": "Broker delivery is still pending",
                    "detail": "HTTP telemetry is available",
                }
            }
        },
    )
    status = next(iter(safe["devices"].values()))

    assert status["reason"] == "Broker delivery is still pending"
    assert status["detail"] == "HTTP telemetry is available"
    assert route not in json.dumps(safe)


def test_valid_local_config_preserves_device_name_reason_and_detail(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "name": "Living Room",
                        "ip": "192.0.2.10",
                        "sn": "LOCAL-SERIAL",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    server = SimpleNamespace(
        config_path=str(config_path),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {
            "devices": {
                "Living Room": {
                    "reason": "Broker delivery is still pending",
                    "detail": "Everything is healthy",
                }
            }
        },
    )

    assert safe == {
        "devices": {
            "Living Room": {
                "reason": "Broker delivery is still pending",
                "detail": "Everything is healthy",
            }
        }
    }


def test_valid_local_route_like_name_stays_consistent_in_keys_and_scalars(tmp_path):
    trusted_name = "Local WR_ALPHA_7501"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "name": trusted_name,
                        "ip": "192.0.2.10",
                        "sn": "LOCAL-SERIAL",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    server = SimpleNamespace(
        config_path=str(config_path),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {
            "devices": {
                trusted_name: {
                    "name": trusted_name,
                    "reason": "Broker delivery is still pending",
                }
            },
            "checks": [
                {
                    "device": trusted_name,
                    "message": f"Zendure device {trusted_name} is healthy",
                }
            ],
        },
    )

    assert list(safe["devices"]) == [trusted_name]
    assert safe["devices"][trusted_name]["name"] == trusted_name
    assert safe["checks"][0]["device"] == trusted_name
    assert trusted_name in safe["checks"][0]["message"]


def test_valid_local_config_masks_unknown_stale_cloud_device_name(tmp_path):
    stale_route = "ACCOUNT_ROUTE_7501"
    stale_name = f"Roof {stale_route}"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "name": "Living Room",
                        "ip": "192.0.2.10",
                        "sn": "LOCAL-SERIAL",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    server = SimpleNamespace(
        config_path=str(config_path),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {
            "devices": {
                stale_name: {
                    "detail": f"stale route {stale_route}",
                }
            }
        },
    )

    assert stale_route not in json.dumps(safe)
    assert "stale route" in next(iter(safe["devices"].values()))["detail"]


def test_valid_local_config_masks_unknown_stale_diagnostic_device(tmp_path):
    stale_route = "ACCOUNT_ROUTE_7501"
    stale_name = f"Roof-{stale_route}"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "name": "Living Room",
                        "ip": "192.0.2.10",
                        "sn": "LOCAL-SERIAL",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    server = SimpleNamespace(
        config_path=str(config_path),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {
            "checks": [
                {
                    "device": stale_name,
                    "message": f"Zendure device {stale_name} read failed",
                }
            ]
        },
    )

    assert stale_route not in json.dumps(safe)
    assert "read failed" in safe["checks"][0]["message"]


def test_mixed_partial_cloud_config_masks_name_only_cloud_route(tmp_path):
    route = "SECRET_CLOUD_ROUTE_7501"
    raw_name = f"Rejected Cloud {route}"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {"source": "zendure_cloud_mqtt"},
                        "local_a": {"source": "local_mqtt"},
                    }
                },
                "devices": [
                    {
                        "name": "Local inverter",
                        "type": "zendure_mqtt",
                        "mqtt": {
                            "broker_ref": "local_a",
                            "device_id": "LOCAL_ROUTE_1234",
                        },
                    },
                    {
                        "name": raw_name,
                        "type": "zendure_mqtt",
                        "mqtt": {"broker_ref": "cloud_a"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    server = SimpleNamespace(
        config_path=str(config_path),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {"devices": {raw_name: {"name": raw_name, "status": "invalid"}}},
    )

    assert route not in json.dumps(safe)


def test_fail_closed_diagnostic_device_field_masks_matching_free_text(tmp_path):
    raw_name = "Roof-MISSING_CONFIG_DIAG_ROUTE_9501"
    server = SimpleNamespace(
        config_path=str(tmp_path / "missing.json"),
        runtime_validation={},
    )

    safe = _external_mqtt_status_payload(
        server,
        {
            "checks": [
                {
                    "device": raw_name,
                    "message": f"Zendure device {raw_name} read failed",
                }
            ]
        },
    )

    assert raw_name not in json.dumps(safe)
    assert "read failed" in safe["checks"][0]["message"]


def test_analytics_unavailable_without_influx(tmp_path):
    # With no InfluxDB configured, the Analytics tab must get a clean, explicit
    # unavailable response (HTTP 200, not an error) so it renders an info state.
    store = SeriesStoreStub(str(tmp_path / "dashboard.sqlite"))
    server, base_url = with_server(store)

    try:
        status, _, advertised = json_response(f"{base_url}/api/analytics/status")
        assert status == 200
        assert advertised["available"] is False
        assert advertised["reason"] == "not_configured"

        status, _, series = json_response(
            f"{base_url}/api/analytics/series?range=24h&series=pv"
        )
        assert status == 200
        assert series["available"] is False
        assert series["reason"] == "not_configured"
    finally:
        server.shutdown()
        server.server_close()


def test_analytics_unreachable_hint(tmp_path):
    # InfluxDB configured but unreachable: both endpoints must return a clean
    # unavailable payload with an actionable hint pointing at 'influx init'.
    store = SeriesStoreStub(str(tmp_path / "dashboard.sqlite"))
    server, base_url = with_server(store)

    class _UnreachableProvider:
        def available(self):
            return False

    # Inject a configured-but-unreachable analytics provider.
    server._analytics_provider = _UnreachableProvider()
    server._analytics_built = True

    try:
        status, _, advertised = json_response(f"{base_url}/api/analytics/status")
        assert status == 200
        assert advertised["available"] is False
        assert advertised["reason"] == "unreachable"
        assert "influx init" in advertised["hint"]

        status, _, series = json_response(
            f"{base_url}/api/analytics/series?range=24h&series=pv"
        )
        assert status == 200
        assert series["available"] is False
        assert series["reason"] == "unreachable"
        assert "influx init" in series["hint"]
    finally:
        server.shutdown()
        server.server_close()


def test_history_series_stays_sqlite_when_influx_enabled(tmp_path):
    # Enabling InfluxDB must never silently replace the operational SQLite
    # history: /api/history/series stays SQLite-backed, while /api/analytics/*
    # is the only InfluxDB-backed surface.
    db_path = str(tmp_path / "dashboard.sqlite")
    _seed_snapshots(db_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "influxdb": {
                    "enabled": True,
                    "url": "http://127.0.0.1:8086",
                    "org": "ems",
                    "token": "test-token",
                }
            }
        ),
        encoding="utf-8",
    )

    store = SeriesStoreStub(db_path)
    server, base_url = with_server(store, config_path=str(config_path))

    try:
        status, _, payload = json_response(
            f"{base_url}/api/history/series?range=24h&series=pv,output,battery"
        )
        assert status == 200
        assert payload["source"] == "sqlite"
        assert payload["series"]["pv"][0] == 1000

        # The analytics provider is the InfluxDB one (advertised available).
        status, _, advertised = json_response(f"{base_url}/api/analytics/status")
        assert status == 200
        assert advertised["available"] is True
        assert advertised["provider"] == "influxdb"
    finally:
        server.shutdown()
        server.server_close()


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
