import json
import urllib.error
import urllib.request

import pytest

from dashboard.server import start_dashboard_server


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


def read_response(url, method="GET"):
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def json_response(url, method="GET"):
    status, headers, body = read_response(url, method=method)
    return status, headers, json.loads(body.decode("utf-8"))


def with_server(store):
    try:
        server = start_dashboard_server(store, host="127.0.0.1", port=0)
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


def test_dashboard_server_rejects_invalid_history_range_and_write_methods():
    store = StoreStub()
    server, base_url = with_server(store)

    try:
        status, _, payload = json_response(f"{base_url}/api/history?range=bad")
        assert status == 400
        assert payload["error"] == "unsupported_range"
        assert "1h" in payload["supported"]

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, _, payload = json_response(f"{base_url}/api/live", method=method)
            assert status == 405
            assert payload == {"error": "read_only"}
    finally:
        server.shutdown()
        server.server_close()


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
