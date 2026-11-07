# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin's MQTT routes: broker switch, accounts, mapping file and live topics.

Behind the same session and CSRF gate as every other discovery route; Docker
is a fake and the live-topic watcher is fed by the test, so nothing leaves the
process.
"""

import functools
import json
import threading

import pytest

from admin import server as admin_server
from admin.credential_store import CredentialStore
from admin.mqtt_broker import BuiltinBroker
from admin.mqtt_live_topics import LiveTopicSessions
from admin.mqtt_mapping_brokers import MappingBrokers
from admin.server import ScanRegistry, create_server
from admin.test_support import write_started_ems_snapshot
from ems import paths
from tests.admin_auth_helpers import authenticate, raw_request, request
from tests.test_admin_mqtt_broker import FakeCompose

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
]


def fake_login(endpoint):
    """192.0.2.50 lets anyone in; 192.0.2.51 only ``iob`` with ``iob-password``; nothing else answers."""

    if endpoint["host"] == "192.0.2.50":
        return "accepted"
    if endpoint["host"] == "192.0.2.51":
        return "accepted" if (endpoint.get("username"), endpoint.get("password")) == ("iob", "iob-password") else "refused"
    return "unreachable"


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
            host_addresses=lambda: (),
        )

    monkeypatch.setattr(admin_server, "builtin_broker_for_install", broker_for)
    monkeypatch.setattr(admin_server, "host_address", lambda: "192.0.2.10")
    monkeypatch.setattr(
        admin_server, "MappingBrokers", functools.partial(MappingBrokers, login=fake_login, networks=lambda: {"networks": []})
    )
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


def test_a_mapping_of_many_devices_fits_the_request(server):
    base, _compose, root = server
    devices = [
        {
            "id": f"balcony-{index}",
            "label": f"Balcony plant {index}",
            "broker_ref": "ems-mqtt",
            "values": {
                target: {"sources": [{"topic": f"shellyplusplugs-a8032ab1234{index}/status/switch:0", "format": "json", "path": "apower", "invert": True, "negative": "zero", "max_age_seconds": 600}]}
                for target in ("inverterPower", "solarPower")
            },
        }
        for index in range(24)
    ]
    document = {"schema_version": 1, "devices": devices}
    assert len(json.dumps({"document": document, "confirm": True})) > 8 * 1024

    status, _h, payload = _post(base, "/api/discovery/mqtt-mapping/apply", {"document": document, "confirm": True})

    assert status == 200 and payload["ok"], payload
    assert json.loads(open(f"{root}/config/mqtt/mappings.json", encoding="utf-8").read()) == document


def test_the_broker_list_and_its_actions_need_a_session_and_writes_the_csrf_token(server):
    base, _compose, _root = server

    assert raw_request(f"{base}/api/discovery/mqtt-mapping/brokers")[0] == 401
    for action in ("search", "check", "account", "manual"):
        status = raw_request(f"{base}/api/discovery/mqtt-mapping/brokers/{action}", method="POST", body={})[0]
        assert status in (401, 403), action


def test_a_broker_added_by_hand_feeds_the_live_topics_and_saving_a_mapping_remembers_it(server):
    base, _compose, root = server

    status, _h, added = _post(base, "/api/discovery/mqtt-mapping/brokers/manual", {"host": "192.0.2.50", "port": 1883})
    listed = request(f"{base}/api/discovery/mqtt-mapping/brokers")[2]["brokers"]
    ref = added["ref"]
    topics = _post(base, "/api/discovery/mqtt-topics/sessions", {"broker_ref": ref})
    document = {
        "schema_version": 1,
        "devices": [{"id": "balkon", "broker_ref": ref, "values": {"inverterPower": {"sources": [{"topic": "plug/power", "format": "number"}]}}}],
    }
    applied = _post(base, "/api/discovery/mqtt-mapping/apply", {"document": document, "confirm": True})

    assert status == 200 and added == {"ok": True, "ref": "broker-192-0-2-50-1883"}
    assert [(item["kind"], item["access"], item["usable"]) for item in listed if item["ref"] == ref] == [("found", "anonymous", True)]
    assert topics[0] == 200 and FakeWatcher.instances[-1].endpoint["host"] == "192.0.2.50"
    assert applied[0] == 200 and applied[2]["ok"]
    written = json.loads(open(f"{root}/config/mqtt/mappings.json", encoding="utf-8").read())
    assert written["brokers"][ref]["host"] == "192.0.2.50"


def test_a_found_broker_that_needs_an_account_takes_one_and_reports_a_refusal(server):
    base, _compose, _root = server

    added = _post(base, "/api/discovery/mqtt-mapping/brokers/manual", {"host": "192.0.2.51", "port": 1883})[2]
    ref = added["ref"]
    before = [item for item in request(f"{base}/api/discovery/mqtt-mapping/brokers")[2]["brokers"] if item["ref"] == ref]
    refused = _post(base, "/api/discovery/mqtt-mapping/brokers/account", {"ref": ref, "username": "iob", "password": "wrong"})
    accepted = _post(base, "/api/discovery/mqtt-mapping/brokers/account", {"ref": ref, "username": "iob", "password": "iob-password"})
    after = [item for item in request(f"{base}/api/discovery/mqtt-mapping/brokers")[2]["brokers"] if item["ref"] == ref]
    topics = _post(base, "/api/discovery/mqtt-topics/sessions", {"broker_ref": ref})

    assert [item["access"] for item in before] == ["needs_account"]
    assert refused[0] == 422 and refused[2]["error"] == "mqtt_mapping_broker_refused"
    assert accepted[0] == 200 and [item["access"] for item in after] == ["account"]
    assert topics[0] == 200 and FakeWatcher.instances[-1].endpoint["username"] == "iob"
    assert "iob-password" not in json.dumps(after)


def test_an_ems_output_another_broker_carries_too_says_which_broker(server, monkeypatch):
    base, _compose, _root = server
    monkeypatch.setattr(
        MappingBrokers,
        "external_sources",
        lambda _self, _broker, address=None: {"balkon": {"builtin": {"configured"}, "192.0.2.99:1883": {"published"}}},
    )
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    session = _post(base, "/api/discovery/mqtt-topics/sessions", {"broker_ref": "ems-mqtt"})[2]["session"]
    FakeWatcher.instances[-1].tree.observe("ems-solarflow/balkon/state", b'{"inverterPower": 612.3}', True)
    FakeWatcher.instances[-1].tree.observe("ems-solarflow/terrasse/state", b'{"inverterPower": 80}', True)

    groups = {group["key"]: group for group in request(f"{base}/api/discovery/mqtt-topics/sessions/{session}")[2]["groups"]}

    assert groups["balkon"]["also_on"] == ["192.0.2.99:1883"]
    assert groups["terrasse"]["also_on"] == []


def test_a_new_mapping_may_not_take_an_id_another_broker_has(server, monkeypatch):
    base, _compose, _root = server
    monkeypatch.setattr(
        MappingBrokers, "external_sources", lambda _self, _broker, address=None: {"balkon": {"192.0.2.99:1883": {"published"}}}
    )
    document = {
        "schema_version": 1,
        "devices": [{"id": "balkon", "broker_ref": "ems-mqtt", "values": {"inverterPower": {"sources": [{"topic": "plug/power", "format": "number"}]}}}],
    }

    preview = _post(base, "/api/discovery/mqtt-mapping/preview", {"document": document})[2]
    applied = _post(base, "/api/discovery/mqtt-mapping/apply", {"document": document, "confirm": True})
    view = request(f"{base}/api/discovery/mqtt-mapping")[2]

    assert [(issue["code"], issue["params"]) for issue in preview["issues"]] == [
        ("mqtt_mapping_device_id_taken", {"id": "balkon", "where": "192.0.2.99:1883"})
    ]
    assert applied[0] == 422 and not applied[2]["ok"]
    assert view["taken_ids"]["ems-mqtt"] == ["balkon"]


# --- a saved mapping is an EMS device, without a search ------------------------

MAPPED_VALUES = {"inverterPower": {"sources": [{"topic": "plug/status", "format": "json", "path": "apower"}]}}


def _write_config(root, brokers=None):
    config = {
        "system": {"max_total_power": 1600},
        "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
    }
    if brokers:
        config["zendure_mqtt"] = {"brokers": brokers}
    with open(f"{root}/config/config.json", "w", encoding="utf-8") as handle:
        json.dump(config, handle)


def _map(base, broker_ref, *, device_id="balkon", enabled=True):
    device = {"id": device_id, "label": "Balkon", "broker_ref": broker_ref, "values": MAPPED_VALUES}
    if not enabled:
        device["enabled"] = False
    document = {"schema_version": 1, "devices": [device]}
    status, _h, payload = _post(base, "/api/discovery/mqtt-mapping/apply", {"document": document, "confirm": True})
    assert status == 200 and payload["ok"], payload


def _mapped_proposals(base):
    proposals = request(f"{base}/api/discovery/mqtt-proposals")[2]["proposals"]
    return [proposal for proposal in proposals if proposal.get("mapping_broker_ref")]


def _adopt(base, proposal, name="Balkon"):
    """What the mapping panel sends: the saved config and the device its proposal makes."""

    loaded = request(f"{base}/api/admin/maintenance/config")[2]
    assert loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    mqtt = proposal["config_fragment"]["mqtt"]
    draft["devices"].append(
        {
            "kind": "external_mqtt",
            "original_name": None,
            "proposal_id": proposal["id"],
            "proposal_broker_ref": proposal["broker_ref"],
            "name": name,
            "enabled": True,
            "mqtt": {key: mqtt[key] for key in ("broker_ref", "topic_family", "device_id")},
        }
    )
    return _post(base, "/api/admin/maintenance/config/apply", {"draft": draft, "revision": loaded["revision"], "confirm": True})


def _config(root):
    with open(f"{root}/config/config.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_a_device_mapped_on_the_built_in_broker_is_offered_without_a_search(server):
    base, _compose, root = server
    _write_config(root)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt")

    (proposal,) = _mapped_proposals(base)

    assert proposal["mapping_broker_ref"] == "ems-mqtt"
    assert proposal["config_fragment"]["mqtt"]["device_id"] == "balkon"
    assert proposal["config_fragment"]["mqtt"]["topic_family"] == "ems_solarflow"
    assert (proposal["broker_host"], proposal["broker_port"], proposal["credentials_ref"]) == ("192.0.2.10", 1883, "ems-mqtt")
    assert proposal["output_control_supported"] is False


def test_the_mapped_device_joins_the_config_in_one_apply(server):
    base, _compose, root = server
    _write_config(root)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt")

    status, _h, payload = _adopt(base, _mapped_proposals(base)[0])

    assert status == 200 and payload.get("ok") is not False, payload
    config = _config(root)
    (entry,) = [device for device in config["devices"] if device.get("type") == "external_mqtt"]
    assert entry["name"] == "Balkon"
    assert (entry["mqtt"]["topic_family"], entry["mqtt"]["device_id"]) == ("ems_solarflow", "balkon")
    profile = config["zendure_mqtt"]["brokers"][entry["mqtt"]["broker_ref"]]
    assert (profile["host"], profile["port"], profile["credentials_ref"]) == ("192.0.2.10", 1883, "ems-mqtt")


def test_a_device_mapped_on_a_remembered_broker_is_offered_on_that_broker(server):
    base, _compose, root = server
    _write_config(root)
    ref = _post(base, "/api/discovery/mqtt-mapping/brokers/manual", {"host": "192.0.2.50", "port": 1883})[2]["ref"]
    _map(base, ref)

    (proposal,) = _mapped_proposals(base)
    status, _h, payload = _adopt(base, proposal)

    assert proposal["mapping_broker_ref"] == ref
    assert (proposal["broker_host"], proposal["broker_port"]) == ("192.0.2.50", 1883)
    assert "credentials_ref" not in proposal
    assert status == 200 and payload.get("ok") is not False, payload
    config = _config(root)
    (entry,) = [device for device in config["devices"] if device.get("type") == "external_mqtt"]
    assert config["zendure_mqtt"]["brokers"][entry["mqtt"]["broker_ref"]]["host"] == "192.0.2.50"


def test_a_device_mapped_on_a_broker_the_ems_reads_keeps_that_profile(server):
    base, _compose, root = server
    CredentialStore(f"{root}/config").save_mqtt_broker_secret("garage-account", "garage", "garage-password")
    _write_config(root, {"garage": {"source": "local_mqtt", "host": "192.0.2.60", "port": 1883, "credentials_ref": "garage-account"}})
    _map(base, "garage")

    (proposal,) = _mapped_proposals(base)
    status, _h, payload = _adopt(base, proposal)

    assert proposal["mapping_broker_ref"] == "garage"
    assert "garage-password" not in json.dumps(proposal)
    assert status == 200 and payload.get("ok") is not False, payload
    config = _config(root)
    (entry,) = [device for device in config["devices"] if device.get("type") == "external_mqtt"]
    assert entry["mqtt"]["broker_ref"] == "garage"
    assert set(config["zendure_mqtt"]["brokers"]) == {"garage"}


def test_nothing_is_offered_for_a_broker_that_is_off_or_a_mapping_that_is_off(server):
    base, _compose, root = server
    _write_config(root)
    _map(base, "ems-mqtt")
    off = _mapped_proposals(base)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt", enabled=False)
    paused = _mapped_proposals(base)

    assert off == [] and paused == []


def test_a_mapping_removed_before_the_apply_is_refused_there(server):
    base, _compose, root = server
    _write_config(root)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt")
    proposal = _mapped_proposals(base)[0]
    _post(base, "/api/discovery/mqtt-mapping/apply", {"document": {"schema_version": 1, "devices": []}, "confirm": True})

    status, _h, payload = _adopt(base, proposal)

    assert status >= 400 or payload.get("ok") is False, payload
    assert [device for device in _config(root)["devices"] if device.get("type") == "external_mqtt"] == []


def test_the_built_in_broker_is_listened_on_again_after_the_admin_restarts(server):
    base, _compose, _root = server
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})

    restarted = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=lambda *a, **k: ([], [])))
    try:
        endpoints = [(item.get("host"), item.get("port")) for item in restarted.mqtt_discovery.candidates()]
    finally:
        restarted.server_close()

    assert ("192.0.2.10", 1883) in endpoints


def test_a_remembered_broker_whose_account_is_gone_offers_nothing(server):
    base, _compose, root = server
    _write_config(root)
    ref = _post(
        base, "/api/discovery/mqtt-mapping/brokers/manual", {"host": "192.0.2.51", "port": 1883, "username": "iob", "password": "iob-password"}
    )[2]["ref"]
    _map(base, ref)
    offered = _mapped_proposals(base)
    with open(f"{root}/config/mqtt/mappings.json", encoding="utf-8") as handle:
        credentials_ref = json.load(handle)["brokers"][ref]["credentials_ref"]
    CredentialStore(f"{root}/config").forget_mqtt_discovery_secret(credentials_ref)

    assert [proposal["mapping_broker_ref"] for proposal in offered] == [ref]
    assert _mapped_proposals(base) == []


def test_a_tls_profile_without_a_port_is_offered_on_the_tls_port_and_kept(server):
    base, _compose, root = server
    CredentialStore(f"{root}/config").save_mqtt_broker_secret("garage-account", "garage", "garage-password")
    _write_config(
        root, {"garage": {"source": "local_mqtt", "host": "192.0.2.60", "tls_mode": "system_ca", "credentials_ref": "garage-account"}}
    )
    _map(base, "garage")

    (proposal,) = _mapped_proposals(base)
    status, _h, payload = _adopt(base, proposal)

    assert (proposal["broker_port"], proposal["broker_tls"]) == (8883, True)
    assert status == 200 and payload.get("ok") is not False, payload
    config = _config(root)
    (entry,) = [device for device in config["devices"] if device.get("type") == "external_mqtt"]
    assert entry["mqtt"]["broker_ref"] == "garage"
    assert set(config["zendure_mqtt"]["brokers"]) == {"garage"}


def test_the_mapping_view_names_the_connection_its_device_is_read_on(server):
    base, _compose, root = server
    _write_config(root)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt")
    (proposal,) = _mapped_proposals(base)

    connections = request(f"{base}/api/discovery/mqtt-mapping")[2]["ems_connections"]
    _adopt(base, proposal)
    entry = next(
        device for device in request(f"{base}/api/admin/maintenance/config")[2]["draft"]["devices"] if device.get("kind") == "external_mqtt"
    )

    assert connections == {"balkon": proposal["catalog_connection_id"]}
    assert entry["catalog_connection_id"] == connections["balkon"]


def test_the_mapping_view_says_whether_the_running_ems_reads_each_mapped_device(server):
    base, _compose, root = server
    _write_config(root)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt")
    (proposal,) = _mapped_proposals(base)
    view = functools.partial(request, f"{base}/api/discovery/mqtt-mapping")

    assert view()[2]["ems_devices"] == {"known": False, "reason": "stopped", "started_at": None, "devices": {}}
    write_started_ems_snapshot()
    _adopt(base, proposal)
    waiting = view()[2]["ems_devices"]
    write_started_ems_snapshot()
    running = view()[2]["ems_devices"]

    assert waiting["known"] is True
    assert (waiting["devices"]["balkon"]["state"], waiting["devices"]["balkon"]["change"]) == ("waiting", "added")
    assert (running["devices"]["balkon"]["state"], running["devices"]["balkon"]["change"]) == ("running", None)


@pytest.mark.parametrize("name", ["localhost", "host.docker.internal", "ems-mqtt-broker"])
def test_an_ems_device_read_from_the_built_in_broker_by_another_name_is_not_read_elsewhere(server, name):
    base, _compose, root = server
    _write_config(root, {"this-host": {"source": "local_mqtt", "host": name, "port": 1883}})
    config = _config(root)
    config["devices"].append(
        {"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "this-host", "topic_family": "ems_solarflow", "device_id": "balkon"}}
    )
    with open(f"{root}/config/config.json", "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    _map(base, "ems-mqtt")

    connections = request(f"{base}/api/discovery/mqtt-mapping")[2]["ems_connections"]

    assert connections == {"balkon": None}


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [("localhost", 1884, None), ("ems-mqtt-broker", 1883, None), ("localhost", 1883, "elsewhere")],
    ids=["this-host-published-port", "container-name-inside-port", "this-host-old-port"],
)
def test_on_a_moved_port_the_container_name_still_reaches_the_built_in_broker(server, host, port, expected):
    base, _compose, root = server
    _write_config(root, {"this-host": {"source": "local_mqtt", "host": host, "port": port}})
    config = _config(root)
    config["devices"].append(
        {"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "this-host", "topic_family": "ems_solarflow", "device_id": "balkon"}}
    )
    with open(f"{root}/config/config.json", "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    _post(base, "/api/discovery/mqtt-broker/enable", {"confirm": True})
    assert _post(base, "/api/discovery/mqtt-broker/settings", {"port": 1884})[0] == 200
    _map(base, "ems-mqtt")

    connection = request(f"{base}/api/discovery/mqtt-mapping")[2]["ems_connections"]["balkon"]

    assert (connection is None) == (expected is None)
