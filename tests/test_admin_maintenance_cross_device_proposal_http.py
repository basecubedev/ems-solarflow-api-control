# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance Preview/Apply HTTP contract for cross-device connection selections.

Discovery here is the real one: two physical inverters are observed on two local
brokers and the server issues its own proposal ids and identity tokens. Attaching
the *second* inverter's valid proposal to the *first* inverter's stored entry must
be refused by both handlers before a broker profile, a backup or a config write
happens — the proposal is current and trusted, it simply belongs to another
device.

The counterpart, a proposal for the same physical inverter on another broker,
must keep applying.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]

BROKER_B1_REF = "local_b1"
BROKER_B1_HOST = "10.0.0.10"
BROKER_B2_HOST = "10.0.0.20"
BROKER_B3_HOST = "10.0.0.30"
SERIAL_A = "SN-A"
SERIAL_OTHER = "SN-OTHER"
MISMATCH_CODE = "mqtt_proposal_identity_mismatch"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _request(url, method="GET", body=None):
    data = None
    headers = dict(auth_headers(url, method))
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _stored_config():
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            {
                "name": "INV_1",
                "type": "zendure_mqtt",
                "enabled": True,
                "serial_number": SERIAL_A,
                "max_power": 800,
                "min_soc": 10,
                "mqtt": {
                    "broker_ref": BROKER_B1_REF,
                    "source": "local_mqtt",
                    "topic_family": "zensdk_ha_scalar",
                    "base_topic": "Zendure",
                    "device_id": "ROUTE-A",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            },
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "zendure_mqtt": {
            "brokers": {
                BROKER_B1_REF: {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": BROKER_B1_HOST,
                    "port": 1883,
                    "tls": False,
                }
            }
        },
        "dashboard": {"enabled": True, "port": 8080},
    }


def _observation(host, serial, device_id):
    return {
        "broker_host": host,
        "broker_port": 1883,
        "source_type": "local_mqtt",
        "topic_family": "zensdk_ha_scalar",
        "serial_number": serial,
        "device_id": device_id,
        "metrics_seen": ["electricLevel", "outputHomePower"],
        "topics_seen": [f"Zendure/sensor/{device_id}/electricLevel"],
    }


def _broker(host, observations):
    return {
        "id": f"mqtt:{host}:1883",
        "host": host,
        "port": 1883,
        "devices": observations,
    }


def _discovery():
    """Real discovery state: the configured inverter, a foreign one, and a
    same-serial alternative for the configured inverter on a third broker."""

    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    generation = store.begin_refresh()
    store.complete_refresh(
        generation,
        [
            _broker(
                BROKER_B1_HOST, [_observation(BROKER_B1_HOST, SERIAL_A, "ROUTE-A")]
            ),
            _broker(
                BROKER_B2_HOST,
                [_observation(BROKER_B2_HOST, SERIAL_OTHER, "ROUTE-OTHER")],
            ),
            _broker(
                BROKER_B3_HOST, [_observation(BROKER_B3_HOST, SERIAL_A, "ROUTE-A2")]
            ),
        ],
        success=True,
    )
    return MqttBrokerDiscovery(store=store, topic_discoverer=None)


@pytest.fixture
def admin_server(tmp_path, isolated_install_root):
    root = isolated_install_root
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(_stored_config()), encoding="utf-8")
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=_discovery(),
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base, config_path
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _proposal_for(base, host):
    status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200, payload
    matches = [
        proposal
        for proposal in payload["proposals"]
        if proposal.get("broker_host") == host
    ]
    assert len(matches) == 1, payload["proposals"]
    proposal = matches[0]
    # A server-issued id and identity token, not a hand-written stub.
    assert str(proposal.get("id") or "").strip()
    assert str(proposal.get("physical_identity_token") or "").startswith("opaque:v1:")
    return proposal


def _switch_draft(base, host):
    """Attach the proposal discovered on ``host`` to the stored INV_1 entry."""

    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    proposal = _proposal_for(base, host)
    fragment = proposal["config_fragment"]
    mqtt = fragment.get("mqtt") or {}
    route = mqtt.get("device_id") or proposal.get("device_id") or ""
    item = next(d for d in draft["devices"] if d.get("original_name") == "INV_1")
    item["proposal_id"] = proposal["id"]
    item["proposal_broker_ref"] = proposal["broker_ref"]
    item["serial_number"] = proposal.get("serial_number") or ""
    item["device_id"] = route
    item["physical_identity_token"] = proposal["physical_identity_token"]
    item["mqtt"] = {
        "broker_ref": mqtt.get("broker_ref") or "",
        "source": mqtt.get("source") or "",
        "topic_family": mqtt.get("topic_family") or "",
        "base_topic": mqtt.get("base_topic"),
        "device_id": route,
    }
    item["broker"] = {
        "ref": proposal.get("broker_ref") or "",
        "host": proposal.get("broker_host") or "",
        "port": proposal.get("broker_port"),
        "tls": proposal.get("broker_tls") is True,
        "tls_insecure": proposal.get("broker_tls_insecure") is True,
        "tls_mode": proposal.get("broker_tls_mode") or "",
        "credentials_ref": proposal.get("credentials_ref") or "",
        "source": proposal.get("connection_source") or "",
    }
    return draft, loaded["revision"], proposal


def _assert_cross_device(payload):
    assert payload["status"] == "invalid", payload
    assert payload["validation"]["ok"] is False
    codes = [issue["code"] for issue in payload["validation"]["errors"]]
    assert MISMATCH_CODE in codes, payload["validation"]
    message = " ".join(
        issue["message"] for issue in payload["validation"]["errors"]
    ).lower()
    assert "different inverter" in message, message


def test_preview_rejects_a_proposal_for_another_inverter(admin_server):
    base, _ = admin_server
    draft, _, _ = _switch_draft(base, BROKER_B2_HOST)

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft}
    )

    assert status == 400, payload
    _assert_cross_device(payload)
    assert "preview" not in payload


def test_apply_rejects_a_proposal_for_another_inverter(admin_server):
    base, config_path = admin_server
    before = config_path.read_text(encoding="utf-8")
    draft, revision, _ = _switch_draft(base, BROKER_B2_HOST)

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": revision, "confirm": True},
    )

    assert status == 400, payload
    _assert_cross_device(payload)
    assert "payload" not in payload
    assert config_path.read_text(encoding="utf-8") == before
    assert list(config_path.parent.glob("config.json.*")) == []


def test_a_rejected_cross_device_switch_provisions_no_broker_profile(admin_server):
    base, config_path = admin_server
    draft, revision, proposal = _switch_draft(base, BROKER_B2_HOST)

    _request(f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft})
    _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": revision, "confirm": True},
    )

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(stored["zendure_mqtt"]["brokers"]) == {BROKER_B1_REF}
    assert proposal["broker_ref"] not in stored["zendure_mqtt"]["brokers"]
    assert BROKER_B2_HOST not in json.dumps(stored)
    device = next(d for d in stored["devices"] if d.get("type") == "zendure_mqtt")
    assert device == _stored_config()["devices"][1]


def test_the_same_inverter_on_another_broker_still_applies(admin_server):
    """The mirror case: only the physical identity decides, not the broker."""

    base, config_path = admin_server
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200, loaded
    # Re-home INV_1 onto the broker that carries its own serial: seed the second
    # broker with SN-A so a same-device alternative exists.
    draft, revision, proposal = _switch_draft(base, BROKER_B3_HOST)

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft}
    )
    assert status == 200, payload
    assert payload["validation"]["ok"] is True, payload["validation"]["errors"]

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": revision, "confirm": True},
    )
    assert status == 200, payload

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    device = next(d for d in stored["devices"] if d.get("type") == "zendure_mqtt")
    assert device["serial_number"] == SERIAL_A
    assert device["mqtt"]["device_id"] == "ROUTE-A2"
    assert device["mqtt"]["broker_ref"] == proposal["broker_ref"]
    assert stored["zendure_mqtt"]["brokers"][proposal["broker_ref"]]["host"] == (
        BROKER_B3_HOST
    )
