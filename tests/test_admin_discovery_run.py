# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin discovery preparation + unified run endpoints (no real network)."""

import base64
import threading
import time

import pytest

from ems import paths
from admin.models import DiscoveredDevice
from admin.secret_store import ZendureTokenStore
from admin.zendure_cloud_mqtt import FakeCloudMqttListener, ZendureCloudDiscovery
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import authenticate, request

pytestmark = pytest.mark.simulation

SHARED_SERIAL = "SN123456"
# base64 of "<api_url>.<app_key>"; the fetcher is faked so only decodability matters.
VALID_TOKEN = base64.b64encode(b"https://app.zendure.tech.APP-KEY-SECRET").decode("ascii")


def _fake_scan(cidr, timeout_ms=600, max_workers=32, progress_callback=None):
    if progress_callback is not None:
        progress_callback(
            {
                "total_hosts": 2,
                "checked_hosts": 1,
                "found_devices": 0,
                "failed_hosts": 0,
                "current_ip": "192.168.178.41",
            }
        )
    device = DiscoveredDevice(
        ip="192.168.178.42",
        api_family="zendure_local_http",
        device_type="zendure_solarflow_800_pro_2",
        role_suggestion="inverter",
        display_name="SolarFlow 800 Pro 2",
        model="SolarFlow 800 Pro 2",
        serial_number=SHARED_SERIAL,
        confidence=0.95,
        config_ready=True,
    )
    return [device], []


def _zendure_device_list(_token, _timeout):
    return {
        "devices": [
            {
                "productKey": "PK-SECRET-AAA",
                "deviceKey": "DK-SECRET-BBB",
                "productModel": "SolarFlow 800 Pro 2",
                "snNumber": SHARED_SERIAL,
                "deviceName": "Balcony battery",
            }
        ],
        "mqtt": {
            "host": "mqtt.example.invalid",
            "port": 8883,
            "username": "mqtt-user",
            "password": "mqtt-secret",
            "client_id": "client-xyz",
        },
        "api_url": "https://app.zendure.tech",
        "app_key": "APP-KEY-SECRET",
    }


@pytest.fixture()
def server(tmp_path, monkeypatch, isolated_install_root):
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path))

    registry = ScanRegistry(scan_runner=_fake_scan)
    record = registry.start("192.168.178.0/24", [80], 600, 32)
    scan = record
    for _ in range(100):
        scan = registry.get(record["scan_id"])
        if scan["status"] in ("finished", "failed"):
            break
        time.sleep(0.02)
    if scan["status"] == "failed":
        pytest.fail(f"background scan failed: {scan.get('errors')}")
    assert scan["status"] == "finished"

    token_store = ZendureTokenStore(tmp_path)
    zendure = ZendureCloudDiscovery(
        store=token_store,
        device_list_fetcher=_zendure_device_list,
        listener_factory=lambda conn: FakeCloudMqttListener(conn),
    )
    zendure.save_token(VALID_TOKEN)
    zendure.refresh()

    srv = create_server(
        "127.0.0.1",
        0,
        registry=registry,
        zendure_cloud_discovery=zendure,
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


def test_preparation_defaults_are_returned(server):
    status, _, payload = request(f"{server}/api/discovery/preparation")
    assert status == 200
    assert payload["discovery_priority"] == ["local_api", "local_mqtt", "zendure_mqtt"]
    assert all(payload["sources"][s]["enabled"] for s in payload["sources"])


def test_preparation_priority_round_trips(server):
    new_priority = ["zendure_mqtt", "local_mqtt", "local_api"]
    status, _, saved = request(
        f"{server}/api/discovery/preparation",
        method="POST",
        body={
            "discovery_priority": new_priority,
            "sources": {"local_mqtt": {"enabled": False}},
        },
    )
    assert status == 200
    assert saved["discovery_priority"] == new_priority
    assert saved["sources"]["local_mqtt"]["enabled"] is False

    _, _, reloaded = request(f"{server}/api/discovery/preparation")
    assert reloaded["discovery_priority"] == new_priority
    assert reloaded["sources"]["local_mqtt"]["enabled"] is False


def test_run_unifies_duplicate_serial_and_selects_local_api_by_default(server):
    status, _, payload = request(f"{server}/api/discovery/run", method="POST")
    assert status == 200
    assert payload["priority"] == ["local_api", "local_mqtt", "zendure_mqtt"]

    unified = [d for d in payload["devices"] if d["serial_number"] == SHARED_SERIAL]
    assert len(unified) == 1, "duplicate serial must collapse into one device"
    device = unified[0]
    assert device["selected_source"] == "local_api"
    assert set(device["sources"]) == {"local_api", "zendure_mqtt"}
    assert device["confidence"] == "high"

    # Details keep every source's own candidates.
    assert payload["details"]["local_api"]["device_count"] >= 1
    assert payload["details"]["zendure_mqtt"]["device_count"] >= 1


def test_run_selects_zendure_when_priority_changes(server):
    request(
        f"{server}/api/discovery/preparation",
        method="POST",
        body={"discovery_priority": ["zendure_mqtt", "local_mqtt", "local_api"]},
    )
    _, _, payload = request(f"{server}/api/discovery/run", method="POST")
    device = next(d for d in payload["devices"] if d["serial_number"] == SHARED_SERIAL)
    assert device["selected_source"] == "zendure_mqtt"


def test_run_lazily_seeds_zendure_when_not_refreshed(
    tmp_path, monkeypatch, isolated_install_root
):
    # Reproduces a fresh restart: an API key is saved but the cloud source was
    # never manually refreshed, so its in-memory candidates are empty. The run
    # must still seed them so priority can select Zendure over Local API.
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path))
    registry = ScanRegistry(scan_runner=_fake_scan)
    record = registry.start("192.168.178.0/24", [80], 600, 32)
    scan = record
    for _ in range(100):
        scan = registry.get(record["scan_id"])
        if scan["status"] in ("finished", "failed"):
            break
        time.sleep(0.02)
    assert scan["status"] == "finished"

    zendure = ZendureCloudDiscovery(
        store=ZendureTokenStore(tmp_path),
        device_list_fetcher=_zendure_device_list,
        listener_factory=lambda conn: FakeCloudMqttListener(conn),
    )
    zendure.save_token(VALID_TOKEN)
    assert zendure.candidates() == []  # never refreshed

    srv = create_server(
        "127.0.0.1", 0, registry=registry, zendure_cloud_discovery=zendure
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        request(
            f"{base}/api/discovery/preparation",
            method="POST",
            body={"discovery_priority": ["zendure_mqtt", "local_mqtt", "local_api"]},
        )
        _, _, payload = request(f"{base}/api/discovery/run", method="POST")
        device = next(
            d for d in payload["devices"] if d["serial_number"] == SHARED_SERIAL
        )
        assert device["selected_source"] == "zendure_mqtt"
        assert set(device["sources"]) == {"local_api", "zendure_mqtt"}
    finally:
        srv.shutdown()
        srv.server_close()


def test_disabled_source_is_excluded_from_unified_list(server):
    request(
        f"{server}/api/discovery/preparation",
        method="POST",
        body={"sources": {"zendure_mqtt": {"enabled": False}}},
    )
    _, _, payload = request(f"{server}/api/discovery/run", method="POST")
    device = next(d for d in payload["devices"] if d["serial_number"] == SHARED_SERIAL)
    # Zendure is disabled, so only local_api contributes now.
    assert device["sources"] == ["local_api"]
    # But the detail panel still reports Zendure's collected state.
    assert "settings" in payload["details"]["zendure_mqtt"]


def test_run_never_leaks_token_or_secret_values(server):
    import json as _json

    _, _, payload = request(f"{server}/api/discovery/run", method="POST")
    blob = _json.dumps(payload)
    for secret in ("APP-KEY-SECRET", "PK-SECRET-AAA", "DK-SECRET-BBB", "mqtt-secret"):
        assert secret not in blob
    zendure_settings = payload["details"]["zendure_mqtt"]["settings"]
    assert "token" not in zendure_settings or not zendure_settings.get("token")
    assert zendure_settings["token_saved"] is True


def test_source_refresh_endpoint_validates_source(server):
    status, _, payload = request(
        f"{server}/api/discovery/source/bogus/refresh", method="POST"
    )
    assert status == 404
    status, _, payload = request(
        f"{server}/api/discovery/source/zendure_mqtt/refresh", method="POST"
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["source"] == "zendure_mqtt"


def test_discovery_endpoints_write_no_ems_config(server):
    request(
        f"{server}/api/discovery/preparation",
        method="POST",
        body={"discovery_priority": ["zendure_mqtt", "local_api", "local_mqtt"]},
    )
    request(f"{server}/api/discovery/run", method="POST")
    request(f"{server}/api/discovery/source/local_api/refresh", method="POST")
    # The isolated_install_root fixture repoints paths.BASE_DIR at an empty root;
    # discovery preparation/run must never create an EMS config there.
    assert not paths.standard_config_path().exists()
    assert not paths.legacy_config_path().exists()
