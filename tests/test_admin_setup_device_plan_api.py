# SPDX-License-Identifier: AGPL-3.0-or-later
"""``POST /api/setup/device-plan`` — the Setup identity/planning boundary.

The browser posts only what it persisted. Candidates, issued ids and the
keep/replace/add/block verdict are all read and decided server-side, so the
response is the only thing Setup may act on. This module pins the boundary
itself: authentication, CSRF, the shape of the answer, and that no raw identity
evidence crosses it in either direction.
"""

import json
import threading

import pytest

from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import authenticate, request

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]

SERIAL = "EOD1AAA111"


class _Devices:
    """A minimal mDNS provider stand-in holding one verified inverter."""

    def __init__(self, devices):
        self._devices = devices

    def devices(self):
        return [dict(device) for device in self._devices]

    def ignored_devices(self):
        return []

    def status(self):
        return {"enabled": False}

    def refresh(self):
        return {}


@pytest.fixture()
def server(tmp_path, monkeypatch, isolated_install_root):
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry)
    srv.mdns_provider = _Devices(
        [
            {
                "id": "device-1",
                "role_suggestion": "inverter",
                "device_type": "solarflow",
                "api_family": "solarflow",
                "ip": "10.0.0.11",
                "port": 8080,
                "serial_number": SERIAL,
                "verified": True,
                "usable_for_config": True,
            }
        ]
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def _plan(server, state, **extra):
    status, _, payload = request(
        f"{server}/api/setup/device-plan",
        method="POST",
        body=dict({"state": state}, **extra),
    )
    assert status == 200, payload
    return payload


def test_legacy_draft_is_rehydrated_with_issued_identities(server):
    plan = _plan(
        server,
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + SERIAL,
                    "role": "inverter",
                    "serial_number": SERIAL,
                    "ip": "10.0.0.11",
                    "port": 8080,
                }
            ]
        },
    )

    entry = plan["draft_items"][0]
    assert entry["observation_id"] == plan["observations"][0]["observation_id"]
    assert entry["physical_device_id"].startswith("opaque:v1:")
    assert entry["unresolved"] is False
    # The legacy entry *is* the live observation, so nothing is adopted next to it.
    assert plan["operations"]["adopt_observations"] == []


def test_an_unknown_device_is_adopted_exactly_once(server):
    plan = _plan(server, {})
    adopted = plan["operations"]["adopt_observations"]
    assert len(adopted) == 1
    assert adopted[0]["observation_id"] == plan["observations"][0]["observation_id"]

    second = _plan(
        server,
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "role": "inverter",
                    "serial_number": SERIAL,
                    "ip": "10.0.0.11",
                    "port": 8080,
                }
            ]
        },
    )
    assert second["operations"]["adopt_observations"] == []


def test_a_dismissed_observation_is_never_adopted(server):
    first = _plan(server, {})
    observation_id = first["observations"][0]["observation_id"]
    plan = _plan(server, {"observation_dismissals": [observation_id]})

    assert plan["operations"]["adopt_observations"] == []
    dismissed = plan["dismissals"]["observations"]
    # Both references come back: the issued id names what the card is, the
    # caller's handle names which card its own state is keyed on.
    assert [entry["observation_id"] for entry in dismissed] == [observation_id]
    assert dismissed[0]["observation_ref"] == plan["observations"][0]["observation_ref"]


def test_the_response_carries_no_raw_identity_evidence(server):
    plan = _plan(
        server,
        {
            "draft_items": [
                {
                    "draft_item_id": "item-1",
                    "source_id": "solarflow:" + SERIAL,
                    "role": "inverter",
                    "serial_number": SERIAL,
                    "ip": "10.0.0.11",
                }
            ],
            "physical_dismissals": [SERIAL],
        },
    )

    encoded = json.dumps(plan)
    for secret in (SERIAL, SERIAL.lower(), "10.0.0.11"):
        assert secret not in encoded


def test_switch_intent_returns_the_canonical_pairwise_plan(server):
    current = {
        "draft_item_id": "item-1",
        "role": "inverter",
        "serial_number": SERIAL,
        "ip": "10.0.0.99",
        "port": 8080,
    }
    discovered = _plan(server, {})
    plan = _plan(
        server,
        {"draft_items": [current]},
        switch={
            "current_ref": "item-1",
            "candidate_id": discovered["observations"][0]["observation_id"],
        },
    )

    verdict = plan["switch"]["plan"]
    assert verdict["same_physical_device"] is True
    assert verdict["action"] in ("use_candidate", "replace_with_confirmation")
    assert verdict["candidate_connection_id"].startswith("conn:v1:")


def test_switch_refuses_a_candidate_the_server_does_not_offer(server):
    plan = _plan(
        server,
        {"draft_items": [{"draft_item_id": "item-1", "role": "inverter"}]},
        switch={"current_ref": "item-1", "candidate_id": "obs:v1:forged"},
    )

    assert plan["switch"]["error"] == "unknown_candidate_connection"
    assert "plan" not in plan["switch"]


def test_device_plan_requires_authentication(tmp_path, monkeypatch, isolated_install_root):
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, _, _ = request(
            f"{base}/api/setup/device-plan", method="POST", body={"state": {}}
        )
        assert status in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_device_plan_rejects_a_non_object_state(server):
    status, _, payload = request(
        f"{server}/api/setup/device-plan", method="POST", body={"state": []}
    )
    assert status == 400
    assert payload["error"]


def test_a_browser_supplied_candidate_body_is_not_a_candidate(server):
    """A handle names a server-owned record; a body describes nothing."""

    plan = _plan(
        server,
        {},
        candidates={
            "observations": [
                {
                    "observation_id": "obs:v1:forged",
                    "physical_device_id": "opaque:v1:forged",
                    "identity_status": "confirmed",
                    "role_suggestion": "inverter",
                    "device_type": "solarflow",
                    "ip": "10.0.0.77",
                    "port": 8080,
                    "verified": True,
                    "usable_for_config": True,
                }
            ]
        },
    )

    assert [entry["observation_id"] for entry in plan["observations"]] != ["obs:v1:forged"]
    assert all(
        entry["observation_id"] != "obs:v1:forged" for entry in plan["observations"]
    )
    assert plan["unresolved_references"] == [
        {"kind": "observation", "handle": "obs:v1:forged"}
    ]
    # The one trusted device is still planned normally.
    assert len(plan["operations"]["adopt_observations"]) == 1


def test_a_stored_mqtt_selection_for_no_current_proposal_is_unresolved(server):
    """A selection is a hint: without a current proposal it resolves to nothing."""

    plan = _plan(
        server,
        {
            "mqtt_selections": [
                {
                    "id": "cloud-1",
                    "connection_source": "zendure_cloud_mqtt",
                    "broker_ref": "zendure_cloud",
                    "selection_origin": "manual",
                }
            ]
        },
    )

    entry = plan["mqtt_selections"][0]
    assert entry["unresolved"] is True
    assert entry["physical_device_id"] is None
    assert entry["legacy_match"] == "unmatched"
    assert plan["operations"]["drop_mqtt_selections"] == []
