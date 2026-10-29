# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

import pytest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import serve_dashboard_preview  # noqa: E402
from dashboard.server import DashboardRequestHandler  # noqa: E402
from dashboard_preview_data import (  # noqa: E402
    DIAGNOSE_PROFILES,
    FLOW_VIEWS,
    SCENARIOS,
    build_scenario,
)
from serve_dashboard_preview import (  # noqa: E402
    SCENARIO_COOKIE,
    parse_args,
    start_server,
)

CAPTURE_SPEC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests",
    "e2e-dashboard",
    "capture-docs.spec.ts",
)

pytestmark = [
    pytest.mark.integration,
]


@pytest.fixture
def preview_server():
    servers = []

    def _start(scenario="normal", fixed_now=None):
        server = start_server("127.0.0.1", 0, scenario, fixed_now=fixed_now)
        servers.append(server)
        host, port = server.server_address
        return server, f"http://{host}:{port}"

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


def _get(base, path, cookie=None):
    headers = {"Cookie": cookie} if cookie is not None else {}
    return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=5)


def _get_json(base, path, cookie=None):
    with _get(base, path, cookie) as response:
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
    assert '<script src="/classic/app.js"></script>' in body


def test_unknown_preview_view_is_404(preview_server):
    _, base = preview_server()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/preview/does-not-exist")
    assert exc.value.code == 404


def test_static_assets_served(preview_server):
    _, base = preview_server()
    for path, content_type in (("/classic/app.js", "javascript"), ("/classic/styles.css", "css")):
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
    payload = _get_json(base, "/api/diagnose?profile=hardware")
    assert payload["schema_version"] == 1
    assert "diagnosis" in payload
    assert "sections" in payload["diagnosis"]


def test_the_preview_diagnoses_the_profiles_the_dashboard_does():
    assert DIAGNOSE_PROFILES == DashboardRequestHandler.DIAGNOSE_PROFILES


@pytest.mark.parametrize("profile", DIAGNOSE_PROFILES)
def test_each_diagnose_profile_answers_as_itself(preview_server, profile):
    _, base = preview_server("offline-device")
    payload = _get_json(base, f"/api/diagnose?profile={profile}")
    sections = [section["id"] for section in payload["diagnosis"]["sections"]]
    assert payload["profile"] == profile
    assert ("hardware" in sections) == (profile in ("deep", "hardware"))
    assert ("control" in sections) == (profile in ("control", "control_quality"))
    assert payload["status"] == ("error" if profile in ("deep", "hardware") else "warning")


@pytest.mark.parametrize("query", ["", "?profile=", "?profile=everything"])
def test_a_diagnose_without_a_known_profile_is_refused_as_the_dashboard_refuses_it(preview_server, query):
    _, base = preview_server()
    with pytest.raises(urllib.error.HTTPError) as refused:
        _get(base, f"/api/diagnose{query}")
    assert refused.value.code == 400
    assert json.loads(refused.value.read().decode("utf-8")) == {
        "error": "invalid_profile",
        "supported": list(DIAGNOSE_PROFILES),
    }


def test_a_pinned_clock_gives_every_answer_the_same_instant(preview_server):
    pinned = datetime.fromisoformat("2026-06-17T18:30:00+02:00").timestamp()
    _, base = preview_server(fixed_now=pinned)
    first = _get_json(base, "/api/history/series?range=24h&series=pv,battery")
    assert first == _get_json(base, "/api/history/series?range=24h&series=pv,battery")
    assert pinned - 900 <= first["time"][-1] <= pinned
    assert _get_json(base, "/api/diagnose?profile=install")["diagnosis"]["timestamp"] == "2026-06-17T16:30:00+00:00"
    assert _get_json(base, "/api/logs?after=0")["lines"][-1]["ts"] == pinned


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_preview_device_s_firmware_labels_agree_with_its_power(scenario):
    for name, device in build_scenario(scenario)["snapshot"]["devices"].items():
        battery = device.get("battery_power_w")
        if not device.get("online") or battery is None:
            continue
        direction = 1 if battery > 0 else 2 if battery < 0 else 0
        if device["pack_state"] in (0, 1, 2):
            assert device["pack_state"] == direction, (scenario, name)
        if device["dc_status"] in (0, 1, 2):
            assert device["dc_status"] == direction, (scenario, name)
        if device["soc_limit"] == 1:
            assert battery <= 0, (scenario, name)
        if device["soc_limit"] == 2:
            assert battery >= 0, (scenario, name)
        assert sum(device["pv_inputs_w"]) == device["pv_input_w"], (scenario, name)


def test_a_scaled_preview_keeps_each_device_s_solar_inputs_adding_up():
    for name, device in build_scenario("normal", device_count=5)["snapshot"]["devices"].items():
        assert sum(device["pv_inputs_w"]) == device["pv_input_w"], name


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
    with _get(base, "/classic/") as response:
        index = response.read().decode("utf-8")
    # The landing page must not replace the real dashboard index.html.
    assert '<script src="/classic/app.js"></script>' not in landing
    assert '<script src="/classic/app.js"></script>' in index


def test_default_scenario_is_normal():
    assert parse_args([]).scenario == "normal"


def test_explicit_scenario_is_kept():
    assert parse_args(["--scenario", "write-mode"]).scenario == "write-mode"


@pytest.mark.parametrize("flag", ["--capture", "--views", "--output-dir"])
def test_capture_flags_are_retired(flag):
    with pytest.raises(SystemExit):
        parse_args([flag, "x"] if flag != "--capture" else [flag])


def test_a_scenario_cookie_answers_that_request_from_its_scenario(preview_server):
    _, base = preview_server("normal")
    signed_in = _get_json(base, "/api/auth/status", f"{SCENARIO_COOKIE}=write-mode")
    assert signed_in["authenticated"] is True
    offline = _get_json(base, "/api/live", f"{SCENARIO_COOKIE}=offline-device")
    assert offline["devices"]["WR2"]["online"] is False


def test_a_scenario_cookie_leaves_other_requests_on_the_server_scenario(preview_server):
    _, base = preview_server("normal")
    _get_json(base, "/api/auth/status", f"{SCENARIO_COOKIE}=write-mode")
    assert _get_json(base, "/api/auth/status")["authenticated"] is False
    assert _get_json(base, "/api/live")["devices"]["WR2"]["online"] is True


@pytest.mark.parametrize(
    "cookie",
    [f"{SCENARIO_COOKIE}=not-a-scenario", f'{SCENARIO_COOKIE}="unterminated', "garbage;;;=="],
)
def test_an_unknown_or_malformed_scenario_cookie_falls_back(preview_server, cookie):
    _, base = preview_server("normal")
    assert _get_json(base, "/api/auth/status", cookie)["authenticated"] is False


def test_the_modern_capture_spec_names_the_cookie_the_server_reads():
    with open(CAPTURE_SPEC, encoding="utf-8") as handle:
        spec = handle.read()
    assert f'"{SCENARIO_COOKIE}"' in spec


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
