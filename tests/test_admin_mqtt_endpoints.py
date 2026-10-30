# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin's MQTT routes: broker switch, accounts, mapping file and live topics.

Behind the same session and CSRF gate as every other discovery route; Docker
is a fake and the live-topic watcher is fed by the test, so nothing leaves the
process.
"""

import json
import threading

import pytest

from admin import server as admin_server
from admin.credential_store import CredentialStore
from admin.mqtt_broker import BuiltinBroker
from admin.mqtt_live_topics import LiveTopicSessions
from admin.server import ScanRegistry, create_server
from ems import paths
from tests.admin_auth_helpers import authenticate, raw_request, request
from tests.test_admin_mqtt_broker import FakeCompose

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
]


class FakeWatcher:
    instances = []

    def __init__(self, endpoint, tree):
        self.endpoint = endpoint
        self.tree = tree
        self.state = "listening"
        self.stopped = False
        FakeWatcher.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True


@pytest.fixture()
def server(tmp_path, monkeypatch, isolated_install_root):
    root = paths.BASE_DIR
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    compose = FakeCompose()

    def broker_for(credential_store, **_kwargs):
        return BuiltinBroker(
            install_root=root,
            config_dir=f"{root}/config",
            data_dir=f"{root}/data",
            credential_store=CredentialStore(f"{root}/config"),
            compose=compose,
            admin_image="ghcr.io/basecubedev/ems-solarflow-admin:test",
            runtime_identity=(1000, 1000),
            port_free=lambda port: True,
            port_answers=lambda port: True,
        )

    monkeypatch.setattr(admin_server, "builtin_broker_for_install", broker_for)
    monkeypatch.setattr(admin_server, "host_address", lambda: "192.0.2.10")
    FakeWatcher.instances = []
    srv = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=lambda *a, **k: ([], [])))
    srv.mqtt_live_topics = LiveTopicSessions(watcher_factory=FakeWatcher, spawn=lambda target: None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base, compose, root
    finally:
        srv.shutdown()
        srv.server_close()


def _post(base, path, body):
    return request(f"{base}{path}", method="POST", body=body)


def test_the_routes_need_a_session_and_writes_need_the_csrf_token(server):
    base, compose, _root = server

    assert raw_request(f"{base}/api/discovery/mqtt-broker")[0] == 401
    assert raw_request(f"{base}/api/discovery/mqtt-broker/enable", method="POST", body={"confirm": True})[0] in (401, 403)
    assert compose.calls == []


def test_switching_on_needs_a_confirmation_and_then_starts_the_broker(server):
    base, compose, _root = server

    assert _post(base, "/api/discovery/mqtt-broker/enable", {})[0] == 400
    status, _headers, payload = _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})

    assert status == 200 and payload["ok"] and payload["created_account"] == "devices"
    view = request(f"{base}/api/discovery/mqtt-broker")[2]
    assert view["running"] is True
    assert view["host_address"] == "192.0.2.10"
    assert view["accounts"] == [
        {"name": "devices", "topic_prefix": None, "bridge": False, "zendure": False, "password_saved": True}
    ]
    assert view["stop_impact"] == {"external_devices": [], "zendure_devices": [], "grid_meter": False}


def test_a_password_is_revealed_only_on_a_post_and_never_cached(server):
    base, _compose, _root = server
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})

    status, headers, payload = _post(base, "/api/discovery/mqtt-broker/reveal", {"name": "devices"})

    assert status == 200 and len(payload["password"]) >= 16
    assert headers.get("Cache-Control") == "no-store"
    assert payload["password"] not in json.dumps(request(f"{base}/api/discovery/mqtt-broker")[2])
    assert _post(base, "/api/discovery/mqtt-broker/reveal", {"name": "nobody"})[0] == 404


def test_settings_accounts_and_stop_go_through_the_broker(server):
    base, compose, _root = server
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})

    assert _post(base, "/api/discovery/mqtt-broker/settings", {"anonymous": True})[0] == 200
    status, _h, payload = _post(base, "/api/discovery/mqtt-broker/settings", {"accounts": []})
    assert status == 200 and payload["ok"]
    status, _h, payload = _post(base, "/api/discovery/mqtt-broker/settings", {"port": 0})
    assert status == 422 and payload["issues"][0]["code"] == "mqtt_broker_port_invalid"
    assert _post(base, "/api/discovery/mqtt-broker/settings", {"colour": "red"})[0] == 400
    status, _h, payload = _post(base, "/api/discovery/mqtt-broker/stop", {"confirm": True})
    assert status == 200 and payload["status"] == "stopped"


def test_a_mapping_is_previewed_applied_and_shown(server):
    base, _compose, root = server
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    document = {
        "schema_version": 1,
        "devices": [
            {
                "id": "balkon",
                "broker_ref": "ems-mqtt",
                "values": {"inverterPower": {"sources": [{"topic": "plug/status", "format": "json", "path": "apower"}]}},
            }
        ],
    }
    broken = json.loads(json.dumps(document))
    broken["devices"][0]["broker_ref"] = "nowhere"

    assert _post(base, "/api/discovery/mqtt-mapping/preview", {"document": broken})[2]["issues"][0]["code"] == (
        "mqtt_mapping_broker_invalid"
    )
    assert _post(base, "/api/discovery/mqtt-mapping/apply", {"document": document})[0] == 400
    status, _h, payload = _post(base, "/api/discovery/mqtt-mapping/apply", {"document": document, "confirm": True})

    assert status == 200 and payload["ok"]
    assert json.loads(open(f"{root}/config/mqtt/mappings.json", encoding="utf-8").read()) == document
    view = request(f"{base}/api/discovery/mqtt-mapping")[2]
    assert view["document"] == document and view["issues"] == []
    assert "ems-mqtt" in view["broker_refs"]


def test_a_value_preview_answers_with_the_services_own_calculation(server):
    base, _compose, _root = server
    device = {
        "id": "balkon",
        "broker_ref": "ems-mqtt",
        "values": {
            "inverterPower": {"sources": [{"topic": "plug", "format": "json", "path": "apower", "invert": True}]}
        },
    }

    payload = _post(
        base, "/api/discovery/mqtt-mapping/value-preview", {"device": device, "payloads": {"plug": '{"apower": -612.3}'}}
    )[2]
    refused = _post(
        base, "/api/discovery/mqtt-mapping/value-preview", {"device": device, "payloads": {"plug": '{"apower": 5}'}}
    )[2]

    assert payload["evaluation"]["state"] == {"inverterPower": 612.3}
    assert refused["evaluation"]["reason"] == "catalog_refused"


def test_live_topics_need_a_running_broker_and_show_what_arrives(server):
    base, _compose, _root = server

    assert _post(base, "/api/discovery/mqtt-topics/sessions", {"broker_ref": "ems-mqtt"})[0] == 409
    assert _post(base, "/api/discovery/mqtt-topics/sessions", {"broker_ref": "unknown"})[0] == 404

    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    status, _h, payload = _post(base, "/api/discovery/mqtt-topics/sessions", {"broker_ref": "ems-mqtt"})
    assert status == 200
    (watcher,) = FakeWatcher.instances
    assert watcher.endpoint["username"] == "ems-admin" and watcher.endpoint["host"] == "127.0.0.1"
    watcher.tree.observe("shellyplusplugs-balkon/status/switch:0", b'{"apower": -612.3}', True)

    snapshot = request(f"{base}/api/discovery/mqtt-topics/sessions/{payload['session']}")[2]

    (group,) = snapshot["groups"]
    assert (group["label"], group["key"], group["mappable"]) == ("shelly_gen2", "shellyplusplugs-balkon", True)
    assert group["topics"][0]["values"][0]["path"] == "apower"
    assert _post(base, f"/api/discovery/mqtt-topics/sessions/{payload['session']}/stop", {})[2]["ok"]
    assert watcher.stopped
    assert request(f"{base}/api/discovery/mqtt-topics/sessions/{payload['session']}")[0] == 404
