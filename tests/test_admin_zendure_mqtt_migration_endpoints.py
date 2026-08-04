# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5: Admin HTTP endpoints for the Zendure MQTT control migration.

The read-only review and the confirmed apply are reachable through the
authenticated Admin API. Apply requires a valid session + CSRF (the shared write
gate), an explicit confirmation, and a review fingerprint that still matches;
backup is on by default and the write is atomic. A stale preview is rejected and
the original config is left active. No broker secret appears in any response.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin.server import ScanRegistry, create_server
from admin.releases import ReleaseManager
from tests.admin_auth_helpers import auth_headers, authenticate, raw_request
from tests.test_admin_server import _fake_gateway_prober, _fake_scan

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _request(url, method="GET", body=None, headers=None):
    data = None
    hdrs = dict(auth_headers(url, method))
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


@pytest.fixture()
def server(isolated_install_root):
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        release_manager=ReleaseManager(data_dir=isolated_install_root / "admin-data"),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base, isolated_install_root
    finally:
        srv.shutdown()
        srv.server_close()


def _write_config(root, data):
    cfg_dir = root / "config"
    cfg_dir.mkdir(exist_ok=True)
    path = cfg_dir / "config.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _legacy_control_config():
    return {
        "config_schema_version": 3,
        "zendure_mqtt": {
            "brokers": {
                "local_a": {
                    "host": "10.0.0.9",
                    "port": 1883,
                    "username": "mqtt",
                    "password": "s3cr3t-broker-pass",
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Legacy",
                "product": "Hyper 2000",
                "mqtt": {
                    "broker_ref": "local_a",
                    "source": "local_mqtt",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV",
                    "product_key": "PK",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


def _cloud_legacy_control_config():
    route = "ADMIN_MIGRATION_CLOUD_ROUTE_7501"
    product = "ADMIN_MIGRATION_PRODUCT_ACCOUNT"
    topic = f"iot/{product}/{route}/properties/write"
    config = _legacy_control_config()
    config["zendure_mqtt"]["brokers"] = {
        "cloud_a": {
            "source": "zendure_cloud_mqtt",
            "host": "mqtt.example.invalid",
            "password": "MIGRATION_BROKER_PASSWORD",
        }
    }
    device = config["devices"][0]
    device["name"] = f"Cloud {route} via {topic}"
    device["mqtt"]["broker_ref"] = "cloud_a"
    device["mqtt"]["device_id"] = route
    device["mqtt"]["product_key"] = product
    return config, route, product, topic


REVIEW = "/api/admin/maintenance/zendure-mqtt/migration-review"
APPLY = "/api/admin/maintenance/zendure-mqtt/migration-apply"


def test_review_endpoint_returns_plan_and_fingerprint(server):
    base, root = server
    _write_config(root, _legacy_control_config())
    status, payload = _request(f"{base}{REVIEW}")
    assert status == 200, payload
    assert payload["status"] == "ok"
    assert payload["review"]["needs_migration"] is True
    assert payload["confirmation_required"] is True
    assert payload["revision"]
    assert "s3cr3t-broker-pass" not in json.dumps(payload)


def test_migration_review_and_apply_never_expose_cloud_route_or_topic(server):
    base, root = server
    config, route, product, topic = _cloud_legacy_control_config()
    _write_config(root, config)

    review_status, review = _request(f"{base}{REVIEW}")
    review_flattened = json.dumps(review)
    assert review_status == 200
    assert review["review"]["needs_migration"] is True
    for raw in (route, product, topic, "MIGRATION_BROKER_PASSWORD"):
        assert raw not in review_flattened

    apply_status, applied = _request(
        f"{base}{APPLY}",
        "POST",
        {"confirm": True, "revision": review["revision"], "backup": True},
    )
    applied_flattened = json.dumps(applied)
    assert apply_status == 200
    for raw in (route, product, topic, "MIGRATION_BROKER_PASSWORD"):
        assert raw not in applied_flattened


def test_review_requires_authentication(server):
    base, root = server
    _write_config(root, _legacy_control_config())
    status, _headers, _payload = raw_request(f"{base}{REVIEW}")
    assert status == 401


def test_apply_endpoint_migrates_and_pins_profile(server):
    base, root = server
    path = _write_config(root, _legacy_control_config())
    _, review = _request(f"{base}{REVIEW}")
    status, payload = _request(
        f"{base}{APPLY}",
        "POST",
        {"confirm": True, "revision": review["revision"], "backup": True},
    )
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["changed"] is True
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["devices"][0]["hardware_profile"] == "hyper_2000"
    # Idempotent: a second review reports nothing to migrate.
    _, review2 = _request(f"{base}{REVIEW}")
    assert review2["review"]["needs_migration"] is False


def test_apply_requires_explicit_confirmation(server):
    base, root = server
    _write_config(root, _legacy_control_config())
    _, review = _request(f"{base}{REVIEW}")
    status, payload = _request(
        f"{base}{APPLY}", "POST", {"revision": review["revision"]}
    )
    assert status == 400
    assert "confirmation" in json.dumps(payload).lower()


def test_apply_rejects_stale_fingerprint(server):
    base, root = server
    _write_config(root, _legacy_control_config())
    status, payload = _request(
        f"{base}{APPLY}",
        "POST",
        {"confirm": True, "revision": "stale-revision-value"},
    )
    assert status == 409
    assert payload["status"] == "conflict"


def test_apply_requires_csrf(server):
    base, root = server
    _write_config(root, _legacy_control_config())
    _, review = _request(f"{base}{REVIEW}")
    # A valid session but a wrong CSRF token must be rejected by the write gate.
    status, payload = _request(
        f"{base}{APPLY}",
        "POST",
        {"confirm": True, "revision": review["revision"]},
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert status == 403
