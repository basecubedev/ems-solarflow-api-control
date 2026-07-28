# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance Preview/Apply HTTP contract for untrusted MQTT connection switches.

A configured MQTT inverter may only be moved to another concrete MQTT connection
when the server resolves a current trusted discovery proposal. A browser-supplied
broker endpoint block is not proof that such a proposal exists, so a draft that
carries one without a resolvable ``proposal_id`` must be refused by both handlers
before any backup or config write happens.
"""

import copy
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

pytestmark = pytest.mark.simulation

BROKER_B1_REF = "local_b1"
BROKER_B1_HOST = "10.0.0.10"
FOREIGN_REF = "local_mqtt_10_9_9_9_61c5b0c5"
FOREIGN_HOST = "10.9.9.9"
SERIAL = "SN-A"


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
                "serial_number": SERIAL,
                "max_power": 800,
                "min_soc": 10,
                "mqtt": {
                    "broker_ref": BROKER_B1_REF,
                    "source": "local_mqtt",
                    "topic_family": "zensdk_ha_scalar",
                    "base_topic": "Zendure",
                    "device_id": "ROUTE-B1",
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
        mqtt_discovery=MqttBrokerDiscovery(
            store=MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900),
            topic_discoverer=None,
        ),
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


def _untrusted_switch_draft(base):
    """Load the Maintenance draft and re-home INV_1 with a bare broker block."""

    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    item = next(d for d in draft["devices"] if d.get("original_name") == "INV_1")
    item.pop("proposal_id", None)
    item.pop("proposal_broker_ref", None)
    item["broker"] = {
        "ref": FOREIGN_REF,
        "host": FOREIGN_HOST,
        "port": 1883,
        "tls": False,
        "tls_insecure": False,
        "tls_mode": "",
        "credentials_ref": "",
        "source": "local_mqtt",
    }
    item["mqtt"] = {
        "broker_ref": FOREIGN_REF,
        "source": "local_mqtt",
        "topic_family": "zensdk_ha_scalar",
        "base_topic": "Zendure",
        "device_id": "EVIL-ROUTE",
    }
    item["device_id"] = "EVIL-ROUTE"
    return draft, loaded["revision"]


def _assert_untrusted(payload):
    assert payload["status"] == "invalid", payload
    errors = payload["validation"]["errors"]
    assert errors[0]["code"] == "mqtt_proposal_untrusted", errors
    assert payload["validation"]["ok"] is False


def test_preview_rejects_a_broker_switch_without_a_proposal(admin_server):
    base, _ = admin_server
    draft, _ = _untrusted_switch_draft(base)

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft}
    )

    assert status == 400, payload
    _assert_untrusted(payload)
    assert "preview" not in payload


def test_apply_rejects_a_broker_switch_without_a_proposal(admin_server):
    base, config_path = admin_server
    before = config_path.read_text(encoding="utf-8")
    draft, revision = _untrusted_switch_draft(base)

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": revision, "confirm": True},
    )

    assert status == 400, payload
    _assert_untrusted(payload)
    assert "payload" not in payload
    assert config_path.read_text(encoding="utf-8") == before
    backups = list((config_path.parent).glob("config.json.*"))
    assert backups == [], backups


def test_rejected_switch_provisions_no_broker_profile(admin_server):
    base, config_path = admin_server
    draft, revision = _untrusted_switch_draft(base)

    _request(
        f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft}
    )
    _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": revision, "confirm": True},
    )

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(stored["zendure_mqtt"]["brokers"]) == {BROKER_B1_REF}
    assert FOREIGN_HOST not in json.dumps(stored)
    device = next(d for d in stored["devices"] if d.get("type") == "zendure_mqtt")
    assert device["mqtt"] == _stored_config()["devices"][1]["mqtt"]


def test_an_ordinary_edit_without_a_broker_block_still_applies(admin_server):
    base, config_path = admin_server
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200, loaded
    draft = copy.deepcopy(loaded["draft"])
    item = next(d for d in draft["devices"] if d.get("original_name") == "INV_1")
    item["max_power"] = 750

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft}
    )
    assert status == 200, payload
    assert payload["validation"]["ok"] is True, payload["validation"]

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": loaded["revision"], "confirm": True},
    )
    assert status == 200, payload

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    device = next(d for d in stored["devices"] if d.get("type") == "zendure_mqtt")
    assert device["max_power"] == 750
    assert device["mqtt"] == _stored_config()["devices"][1]["mqtt"]


def test_the_browser_cannot_supply_the_server_trust_marker(admin_server):
    base, config_path = admin_server
    draft, revision = _untrusted_switch_draft(base)
    item = next(d for d in draft["devices"] if d.get("original_name") == "INV_1")
    item["trusted_connection_selection"] = True

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/preview", "POST", {"draft": draft}
    )
    assert status == 400, payload
    _assert_untrusted(payload)

    status, payload = _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": revision, "confirm": True},
    )
    assert status == 400, payload
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(stored["zendure_mqtt"]["brokers"]) == {BROKER_B1_REF}
