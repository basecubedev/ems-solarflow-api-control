# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import serve_dashboard_preview  # noqa: E402
from dashboard_preview_data import FLOW_VIEWS, SCENARIOS, build_scenario  # noqa: E402
from serve_dashboard_preview import (  # noqa: E402
    DEFAULT_CAPTURE_VIEWS,
    normalize_views,
    parse_args,
    resolve_scenario,
    start_server,
)


@pytest.fixture
def preview_server():
    servers = []

    def _start(scenario="normal"):
        server = start_server("127.0.0.1", 0, scenario)
        servers.append(server)
        host, port = server.server_address
        return server, f"http://{host}:{port}"

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


def _get(base, path):
    return urllib.request.urlopen(base + path, timeout=5)


def _get_json(base, path):
    with _get(base, path) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def test_server_starts_on_free_port(preview_server):
    _, base = preview_server()
    with _get(base, "/") as response:
        assert response.status == 200
        assert b"<html" in response.read().lower()


def test_default_host_is_loopback(preview_server):
    server, _ = preview_server()
    host, _port = server.server_address
    assert host == "127.0.0.1"


@pytest.mark.parametrize("view", FLOW_VIEWS)
def test_preview_views_return_html(preview_server, view):
    _, base = preview_server()
    with _get(base, f"/preview/{view}") as response:
        assert response.status == 200
        body = response.read().decode("utf-8")
    assert response.headers.get("Content-Type", "").startswith("text/html")
    assert "<html" in body.lower()
    # The injected bootstrap opens the requested view and still loads app.js.
    assert "dashboard.flowView" in body
    assert json.dumps(view) in body
    assert '<script src="/app.js"></script>' in body


def test_unknown_preview_view_is_404(preview_server):
    _, base = preview_server()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/preview/does-not-exist")
    assert exc.value.code == 404


def test_static_assets_served(preview_server):
    _, base = preview_server()
    for path, content_type in (("/app.js", "javascript"), ("/styles.css", "css")):
        with _get(base, path) as response:
            assert response.status == 200
            assert content_type in response.headers.get("Content-Type", "")


def test_api_live_returns_devices(preview_server):
    _, base = preview_server("firmware-status")
    payload = _get_json(base, "/api/live")
    assert payload["devices"]
    # Firmware-status fields must be present so the frontend can render labels.
    wr4 = payload["devices"]["WR4"]
    for field in ("ac_mode", "ac_status", "soc_limit", "pack_state", "dc_status", "grid_state"):
        assert field in wr4
    assert wr4["ac_status"] == 9  # unknown value preserved for label fallback


def test_api_history_returns_items(preview_server):
    _, base = preview_server()
    payload = _get_json(base, "/api/history?range=6h")
    assert payload["range"] == "6h"
    assert isinstance(payload["items"], list)
    assert len(payload["items"]) > 0
    assert "timestamp" in payload["items"][0]


def test_api_runtime_returns_runtime_state(preview_server):
    _, base = preview_server()
    payload = _get_json(base, "/api/runtime")
    assert "system" in payload
    assert "devices" in payload
    assert "_limits" in payload


def test_api_diagnose_returns_report(preview_server):
    _, base = preview_server()
    payload = _get_json(base, "/api/diagnose")
    assert payload["schema_version"] == 1
    assert "diagnosis" in payload
    assert "sections" in payload["diagnosis"]


def test_api_logs_returns_lines(preview_server):
    _, base = preview_server()
    payload = _get_json(base, "/api/logs?after=0")
    assert isinstance(payload["lines"], list)
    assert payload["lines"]
    first = payload["lines"][0]
    assert {"seq", "ts", "level", "message"} <= set(first)


def test_maintenance_preview_uses_current_upgrade_contract(preview_server):
    _, base = preview_server("write-mode")
    payload = _get_json(base, "/api/maintenance/config-upgrade")
    assert payload["apply_available"] is True
    assert payload["plan_id"]
    assert {"add", "migrate", "comment_add", "comment_refresh"} <= {
        item["kind"] for item in payload["items"]
    }

    with _get(base, "/preview/maintenance") as response:
        html = response.read().decode("utf-8")
    assert "setFlowView('maintenance',false)" in html


def test_auth_status_changes_with_scenario(preview_server):
    _, base_normal = preview_server("normal")
    assert _get_json(base_normal, "/api/auth/status")["authenticated"] is False
    assert _get_json(base_normal, "/api/auth/status")["auth_configured"] is False

    _, base_readonly = preview_server("auth-readonly")
    readonly = _get_json(base_readonly, "/api/auth/status")
    assert readonly["auth_configured"] is True
    assert readonly["authenticated"] is False

    _, base_write = preview_server("write-mode")
    write = _get_json(base_write, "/api/auth/status")
    assert write["authenticated"] is True
    assert write["csrf_token"]


def test_offline_scenario_marks_device_offline(preview_server):
    _, base = preview_server("offline-device")
    payload = _get_json(base, "/api/live")
    assert payload["devices"]["WR1"]["online"] is True
    assert payload["devices"]["WR2"]["online"] is False


def test_write_endpoints_are_preview_only(preview_server):
    _, base = preview_server("write-mode")
    request = urllib.request.Request(
        base + "/api/runtime/system",
        data=b"{}",
        method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["applied"] is False
    assert payload["status"] == "preview-only"


@pytest.mark.parametrize("attack", ["/../server.py", "/%2e%2e/serve_dashboard_preview.py", "/../../config.json"])
def test_static_path_traversal_blocked(preview_server, attack):
    _, base = preview_server()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, attack)
    assert exc.value.code == 404


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        build_scenario("not-a-scenario")


def test_all_scenarios_build():
    for scenario in SCENARIOS:
        data = build_scenario(scenario)
        assert data["snapshot"]["devices"]
        assert "auth" in data
        assert "diagnose" in data


@pytest.mark.parametrize("path", ["/preview", "/preview/"])
def test_landing_page_lists_all_views(preview_server, path):
    _, base = preview_server("firmware-status")
    with _get(base, path) as response:
        assert response.status == 200
        assert response.headers.get("Content-Type", "").startswith("text/html")
        body = response.read().decode("utf-8")
    # Names the current scenario and links every flow view.
    assert "firmware-status" in body
    for view in FLOW_VIEWS:
        assert f'href="/preview/{view}"' in body
    # Makes the safety guarantee explicit.
    assert "Synthetic data only" in body


def test_landing_page_is_not_the_real_dashboard(preview_server):
    _, base = preview_server()
    with _get(base, "/preview") as response:
        landing = response.read().decode("utf-8")
    with _get(base, "/") as response:
        index = response.read().decode("utf-8")
    # The landing page must not replace the real dashboard index.html.
    assert '<script src="/app.js"></script>' not in landing
    assert '<script src="/app.js"></script>' in index


def test_normalize_views_expands_all():
    assert normalize_views(["all"]) == list(FLOW_VIEWS)


def test_normalize_views_keeps_explicit_lists():
    assert normalize_views(["devices", "control"]) == ["devices", "control"]


def test_normalize_views_none_is_none():
    assert normalize_views(None) is None


def test_normalize_views_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_views(["nope"])


def test_interactive_default_scenario_is_normal():
    assert resolve_scenario(parse_args([])) == "normal"


def test_capture_default_scenario_is_write_mode():
    assert resolve_scenario(parse_args(["--capture"])) == "write-mode"


def test_explicit_scenario_wins_in_capture_mode():
    assert resolve_scenario(parse_args(["--capture", "--scenario", "firmware-status"])) == "firmware-status"


def test_explicit_scenario_wins_in_interactive_mode():
    assert resolve_scenario(parse_args(["--scenario", "write-mode"])) == "write-mode"


def test_capture_assets_returns_written_paths(tmp_path, monkeypatch):
    import capture_dashboard_previews as capture

    monkeypatch.setattr(capture, "require_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: None)

    written = capture.capture_assets(
        "127.0.0.1", 8767, str(tmp_path), "write-mode", ["diagnose", "logs"]
    )
    assert written == [
        os.path.join(str(tmp_path), "preview-diagnose.jpg"),
        os.path.join(str(tmp_path), "preview-logs.jpg"),
    ]


def test_capture_mode_uses_write_mode_by_default(tmp_path, monkeypatch, capsys):
    captured = {}

    def fake_capture_assets(host, port, output_dir, scenario, views):
        captured["scenario"] = scenario
        captured["views"] = views
        return [os.path.join(output_dir, "preview-diagnose.jpg")]

    import capture_dashboard_previews as capture

    monkeypatch.setattr(capture, "capture_assets", fake_capture_assets)
    serve_dashboard_preview.main(
        ["--capture", "--port", "0", "--output-dir", str(tmp_path)]
    )

    assert captured["scenario"] == "write-mode"
    assert captured["views"] is None  # default capture views resolved downstream
    assert "maintenance" in DEFAULT_CAPTURE_VIEWS
    assert "Captured preview screenshots:" in capsys.readouterr().out


def test_list_flags_exit_without_serving(capsys):
    serve_dashboard_preview.main(["--list-scenarios"])
    out = capsys.readouterr().out
    assert "Scenarios:" in out
    for name in SCENARIOS:
        assert name in out

    serve_dashboard_preview.main(["--list-views"])
    out = capsys.readouterr().out
    assert "Views:" in out
    for view in FLOW_VIEWS:
        assert view in out
