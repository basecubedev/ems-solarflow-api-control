# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Setup planning trust boundary: what the browser may and may not assert.

``POST /api/setup/device-plan`` decides physical identity, transport selection
and every keep/replace/add/block verdict. Those answers are only worth anything
if the evidence they are computed from is the server's own. This module pins
that: the request carries *handles* into current server-owned discovery state
plus operator intent, and nothing else. A candidate body, a serial, a host or a
``verified: true`` flag invented by the caller must not become an issued
identity, a connection or an executable operation.

The complement — that the response never leaks raw evidence back — lives in
``test_admin_setup_identity_migration.py``; the batch matrix lives in
``test_admin_setup_batch_planner.py``.
"""

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

KNOWN_SERIAL = "EOD1AAA111"
INVENTED_SERIAL = "EOD9ZZZ999"


class _Devices:
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


def _serve(tmp_path, monkeypatch, devices):
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry)
    srv.mdns_provider = _Devices(devices)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


@pytest.fixture()
def empty_server(tmp_path, monkeypatch, isolated_install_root):
    """An Admin that has discovered nothing at all."""

    srv, base = _serve(tmp_path, monkeypatch, [])
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def server(tmp_path, monkeypatch, isolated_install_root):
    srv, base = _serve(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "device-1",
                "role_suggestion": "inverter",
                "device_type": "solarflow",
                "api_family": "solarflow",
                "ip": "10.0.0.11",
                "port": 8080,
                "serial_number": KNOWN_SERIAL,
                "verified": True,
                "usable_for_config": True,
            }
        ],
    )
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def _plan(base, state=None, **extra):
    status, _, payload = request(
        f"{base}/api/setup/device-plan",
        method="POST",
        body=dict({"state": state or {}}, **extra),
    )
    assert status == 200, payload
    return payload


def _no_operations(plan):
    return all(not value for value in plan["operations"].values())


# --- forged candidate bodies -------------------------------------------------
FORGED_BODIES = {
    "invented_serial": {
        "observation_ref": "card-1",
        "role_suggestion": "inverter",
        "device_type": "solarflow",
        "api_family": "solarflow",
        "serial_number": INVENTED_SERIAL,
        "ip": "192.168.99.99",
        "port": 8080,
        "verified": True,
        "usable_for_config": True,
    },
    "invented_route": {
        "observation_ref": "card-1",
        "role_suggestion": "inverter",
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "device_id": "DEVFORGED",
        "product_key": "PKFORGED",
        "mqtt": {
            "source": "zendure_cloud_mqtt",
            "broker_ref": "zendure_cloud",
            "device_id": "DEVFORGED",
            "product_key": "PKFORGED",
        },
        "verified": True,
        "usable_for_config": True,
    },
    "invented_host": {
        "observation_ref": "card-1",
        "role_suggestion": "inverter",
        "device_type": "solarflow",
        "api_family": "solarflow",
        "ip": "192.168.99.98",
        "port": 8080,
        "verified": True,
        "usable_for_config": True,
    },
    "forged_issued_ids": {
        "observation_ref": "card-1",
        "observation_id": "obs:v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "connection_id": "conn:v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "physical_device_id": "opaque:v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "identity_status": "confirmed",
        "role_suggestion": "inverter",
        "device_type": "solarflow",
        "serial_number": INVENTED_SERIAL,
        "ip": "192.168.99.97",
        "verified": True,
        "usable_for_config": True,
    },
}


@pytest.mark.parametrize("shape", sorted(FORGED_BODIES))
def test_an_invented_candidate_body_never_enters_the_plan(empty_server, shape):
    """The server knows no device; the browser's word does not create one."""

    plan = _plan(empty_server, {}, candidates={"observations": [FORGED_BODIES[shape]]})

    assert plan["observations"] == []
    assert plan["candidates"] == []
    assert _no_operations(plan), plan["operations"]


@pytest.mark.parametrize("shape", sorted(FORGED_BODIES))
def test_an_invented_candidate_body_never_mints_an_identity(empty_server, shape):
    plan = _plan(empty_server, {}, candidates={"observations": [FORGED_BODIES[shape]]})

    minted = [
        value
        for value in _walk(plan)
        if isinstance(value, str)
        and (value.startswith("opaque:v1:") or value.startswith("conn:v1:"))
    ]
    assert minted == [], minted


def test_an_invented_proposal_body_never_enters_the_plan(empty_server):
    plan = _plan(
        empty_server,
        {},
        candidates={
            "proposals": [
                {
                    "id": "forged-1",
                    "connection_source": "zendure_cloud_mqtt",
                    "broker_ref": "zendure_cloud",
                    "serial_number": INVENTED_SERIAL,
                    "output_control_supported": True,
                    "config_fragment": {
                        "mqtt": {
                            "source": "zendure_cloud_mqtt",
                            "broker_ref": "zendure_cloud",
                            "device_id": "DEVFORGED",
                        }
                    },
                }
            ]
        },
    )

    assert plan["proposals"] == []
    assert _no_operations(plan), plan["operations"]


def test_an_unknown_candidate_handle_is_reported_and_never_executed(empty_server):
    plan = _plan(
        empty_server,
        {},
        candidates={
            "observations": [{"observation_id": "obs:v1:nothing", "observation_ref": "card-1"}],
            "proposals": [{"id": "gone-1"}],
        },
    )

    unresolved = {
        (entry["kind"], entry["handle"]) for entry in plan["unresolved_references"]
    }
    assert ("observation", "obs:v1:nothing") in unresolved
    assert ("proposal", "gone-1") in unresolved
    assert _no_operations(plan), plan["operations"]


def test_a_trusted_handle_names_the_card_the_browser_renders(server):
    """A reference that resolves keeps the caller's own handle on the answer."""

    served = _plan(server, {})["observations"][0]
    plan = _plan(
        server,
        {},
        candidates={
            "observations": [
                {"observation_id": served["observation_id"], "observation_ref": "card-1"}
            ]
        },
    )

    entry = plan["observations"][0]
    assert entry["observation_ref"] == "card-1"
    assert entry["observation_id"] == served["observation_id"]
    assert entry["physical_device_id"] == served["physical_device_id"]
    assert plan["unresolved_references"] == []


def test_a_client_trust_flag_cannot_upgrade_a_trusted_candidate(server):
    """The trusted record decides; the echoed flags on the handle are ignored."""

    served = _plan(server, {})["observations"][0]
    plan = _plan(
        server,
        {},
        candidates={
            "observations": [
                {
                    "observation_id": served["observation_id"],
                    "observation_ref": "card-1",
                    "verified": False,
                    "usable_for_config": False,
                    "identity_status": "unresolved",
                    "physical_device_id": None,
                }
            ]
        },
    )

    assert plan["observations"][0]["physical_device_id"] == served["physical_device_id"]
    assert plan["observations"][0]["identity_status"] == "confirmed"
    # The trusted record is config-ready, so adoption is still offered.
    assert len(plan["operations"]["adopt_observations"]) == 1


def test_the_switch_candidate_must_be_a_trusted_handle(empty_server):
    plan = _plan(
        empty_server,
        {"draft_items": [{"draft_item_id": "item-1", "role": "inverter"}]},
        candidates={"observations": [FORGED_BODIES["invented_serial"]]},
        switch={"current_ref": "item-1", "candidate_id": "obs:v1:forged"},
    )

    assert plan["switch"]["error"] == "unknown_candidate_connection"
    assert "plan" not in plan["switch"]


def _walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value
