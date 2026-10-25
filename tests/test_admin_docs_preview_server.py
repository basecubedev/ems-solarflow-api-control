# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin docs preview behind every Admin Console documentation screenshot.

``scripts/serve_admin_docs_preview.py`` answers the Modern console's API calls
with demo payloads, picked per page by the ``?scenario=`` of that page's own
address, and answers the settings, their check, the telemetry view and the
guided setup's search and plan through the shipped Admin builders. These tests
hold the parts a capture silently depends on.
"""

import json
import os
import re
import sys
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import serve_admin_docs_preview  # noqa: E402
from serve_admin_docs_preview import SCENARIOS, build_scenarios, demo_config, start_server  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.admin,
]


@pytest.fixture
def preview():
    server = start_server("127.0.0.1", 0)
    host, port = server.server_address
    yield server, f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _request(base, path, *, scenario=None, body=None):
    page = f"{base}/?scenario={scenario}" if scenario else f"{base}/"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Referer": page, "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        return json.loads(response.read())


def test_every_named_scenario_is_built():
    assert set(build_scenarios()) == set(SCENARIOS)


def test_the_capture_script_names_only_served_scenarios():
    with open(os.path.join(SCRIPTS_DIR, "capture_admin_docs.mjs"), encoding="utf-8") as handle:
        used = set(re.findall(r'scenario: "([a-z-]+)"', handle.read()))
    assert used and used <= set(SCENARIOS)


def test_a_page_scenario_changes_only_its_own_answers(preview):
    _, base = preview
    assert _request(base, "/api/admin/auth/status")["authenticated"] is True
    assert _request(base, "/api/admin/auth/status", scenario="signed-out")["authenticated"] is False
    assert _request(base, "/api/admin/maintenance/overview", scenario="fresh")["install_state"]["state"] == "none"
    assert _request(base, "/api/admin/maintenance/overview", scenario="signed-out")["install_state"]["state"] == "standard_install"


def test_an_unknown_scenario_is_the_installed_system(preview):
    _, base = preview
    overview = _request(base, "/api/admin/maintenance/overview", scenario="no-such-scenario")
    assert overview["install_state"]["state"] == "standard_install"


def test_the_session_heartbeat_keeps_the_scenarios_sign_in(preview):
    """Every click sends a heartbeat whose answer the console adopts as its sign-in state."""

    _, base = preview
    assert _request(base, "/api/admin/auth/refresh", body={})["authenticated"] is True
    assert _request(base, "/api/admin/auth/refresh", scenario="first-start", body={})["requires_initial_password"] is True


def test_the_settings_come_from_the_shipped_builder_over_the_demo_config(preview):
    _, base = preview
    config = _request(base, "/api/admin/maintenance/config")
    assert config["status"] == "ok"
    assert config["config_path"] == "/opt/ems/config/config.json"
    names = [device["name"] for device in config["draft"]["devices"]]
    assert names == [device["name"] for device in demo_config()["devices"]]
    assert config["summary"]["control"]["placeholder_fields"] == []
    assert config["catalog"], "the shipped settings catalog is served"


def test_an_unchanged_draft_checks_clean_and_a_change_is_named(preview):
    _, base = preview
    draft = _request(base, "/api/admin/maintenance/config")["draft"]
    assert _request(base, "/api/admin/maintenance/config/preview", body={"draft": draft})["changed"] is False
    draft["features"]["winter.winter_min_soc"] = 30
    checked = _request(base, "/api/admin/maintenance/config/preview", body={"draft": draft})
    assert checked["validation"]["ok"] is True
    assert [row["path"] for row in checked["diff"]["changes"]] == ["winter.winter_min_soc"]


def test_settings_and_search_agree_on_which_device_is_which(preview):
    _, base = preview
    configured = {
        device["sn"]: device["physical_device_id"]
        for device in _request(base, "/api/admin/maintenance/config")["draft"]["devices"]
    }
    found = {
        device["serial_number"]: device["physical_device_id"]
        for device in _request(base, "/api/discovery/devices")["devices"]
    }
    for serial, identity in configured.items():
        assert found[serial] == identity


def test_the_guided_setup_search_and_plan_put_the_demo_inverters_in_the_draft(preview):
    _, base = preview
    run = _request(base, "/api/setup/discovery/run", scenario="setup", body={"refresh": True})
    roles = sorted(device.get("role") or device.get("role_suggestion") or "" for device in run["devices"])
    assert roles.count("inverter") == 2
    plan = _request(base, "/api/setup/device-plan", scenario="setup", body={"state": {}})
    assert plan["plan_id"]


def test_the_demo_installation_is_removed_when_the_preview_closes():
    server = start_server("127.0.0.1", 0)
    root = server.installation.root
    assert os.path.isdir(root)
    server.shutdown()
    server.server_close()
    assert not os.path.exists(root)


def test_the_demo_config_names_no_template_placeholder():
    text = json.dumps(demo_config())
    assert "YOUR_SN" not in text
    assert "DEMO-" in text
    assert serve_admin_docs_preview.DEMO_IDENTITY_KEY.startswith(b"ems-admin-docs-preview")
