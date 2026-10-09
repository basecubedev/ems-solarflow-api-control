# SPDX-License-Identifier: AGPL-3.0-or-later
"""A catalog device found by discovery is adopted by Setup and by Maintenance.

The reported case: an inverter whose readings FHEM publishes on a local broker
into the project namespace, ``ems-solarflow/<id>/inverterPower``. Discovery
recognises it from the
catalog, offers it as a read-only device, and both workflows write the same
entry -- its catalog family, its device id and its broker, nothing else -- which
the EMS runtime then reads. A topic the catalog does not describe is never
offered, so there is nothing to adopt.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.mqtt_topic_discovery import MqttTopicAggregator
from admin.server import ScanRegistry, create_server
from ems.zendure_mqtt.config_entries import (
    find_zendure_mqtt_broker_profile_issues,
    validate_external_mqtt_device_config,
)
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.setup_config import authorize_setup_mutation
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]

BROKER = {"id": "mqtt:10.0.0.71:1883", "host": "10.0.0.71", "port": 1883}
SERIAL = "EXAMPLE0000001"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _observed(messages):
    """What the real discovery listener makes of what the broker carries."""

    aggregator = MqttTopicAggregator(BROKER)
    for topic, payload in messages:
        aggregator.observe(topic, payload)
    return aggregator.results()


def _discovery(messages):
    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    generation = store.begin_refresh()
    store.complete_refresh(
        generation, [{**BROKER, "devices": _observed(messages)}], success=True
    )
    return MqttBrokerDiscovery(store=store, topic_discoverer=None)


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


def _serve(tmp_path, messages):
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=_discovery(messages),
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _proposals(base):
    status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200, payload
    return payload["proposals"]


def _external_proposal(base):
    proposals = [p for p in _proposals(base) if p["topic_family"] == "ems_solarflow"]
    assert len(proposals) == 1, proposals
    return proposals[0]


INVERTER = [(f"ems-solarflow/{SERIAL}/inverterPower", b"765")]
LOCAL_INVERTER = {
    "source_id": "local:wr1",
    "config_name": "WR1",
    "display_name": "SolarFlow 800",
    "role": "inverter",
    "enabled": True,
    "ip": "192.168.1.100",
    "serial_number": "AAA",
}


def _run_setup(root, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    srv, base = _serve(root, INVERTER)
    try:
        proposal = _external_proposal(base)
        body = authorize_setup_mutation(
            base,
            _workflow_request,
            {
                "devices": [LOCAL_INVERTER],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [
                    {"id": proposal["id"], "broker_ref": proposal["broker_ref"]}
                ],
            },
        )
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()
    return json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))


def _workflow_request(url, method="GET", body=None):
    status, payload = _request(url, method, body)
    return status, {}, payload


def _external_draft_item(proposal, name):
    """Mirror the browser's ``mconfigAddExternalProposal`` draft projection."""

    mqtt = proposal["config_fragment"]["mqtt"]
    return {
        "kind": "external_mqtt",
        "original_name": None,
        "proposal_id": proposal["id"],
        "proposal_broker_ref": proposal["broker_ref"],
        "name": name,
        "enabled": True,
        "mqtt": {key: mqtt[key] for key in ("broker_ref", "topic_family", "device_id")},
    }


def _write_base_config(root):
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 1600},
                "devices": [
                    {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
                ],
                "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
            }
        ),
        encoding="utf-8",
    )
    return config_dir


def _run_maintenance(root, monkeypatch, *, item=_external_draft_item):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    config_dir = _write_base_config(root)
    srv, base = _serve(root, INVERTER)
    try:
        proposal = _external_proposal(base)
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        draft = loaded["draft"]
        draft["devices"].append(item(proposal, "Garage"))
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": draft, "revision": loaded["revision"], "confirm": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()
    return status, payload, json.loads((config_dir / "config.json").read_text(encoding="utf-8"))


def _external_entries(config):
    return [d for d in config["devices"] if d.get("type") == "external_mqtt"]


def _assert_the_runtime_reads_it(config):
    (entry,) = _external_entries(config)
    brokers = config["zendure_mqtt"]["brokers"]
    ref = entry["mqtt"]["broker_ref"]
    assert brokers[ref]["host"] == BROKER["host"]
    assert not find_zendure_mqtt_broker_profile_issues(config)
    assert validate_external_mqtt_device_config(
        entry, known_broker_refs=set(brokers), brokers_defined=True
    ) == []

    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config, service_factory=network.telemetry_service_factory()
    )
    runtime.start()
    try:
        broker = network.broker(ref)
        assert broker.inject(f"ems-solarflow/{SERIAL}/inverterPower", b"765")
        assert runtime.snapshots()[f"ems-solarflow/{SERIAL}"].metrics["outputHomePower"] == 765
    finally:
        runtime.stop()


def test_discovery_offers_the_catalog_device_as_a_read_only_device(tmp_path):
    srv, base = _serve(tmp_path, INVERTER)
    try:
        proposal = _external_proposal(base)
    finally:
        srv.shutdown()
        srv.server_close()

    assert proposal["target"] == "device"
    assert proposal["output_control_supported"] is False
    assert proposal["config_fragment"] == {
        "type": "external_mqtt",
        "enabled": True,
        "name": f"External device {SERIAL}",
        "mqtt": {
            "broker_ref": proposal["broker_ref"],
            "source": "local_mqtt",
            "topic_family": "ems_solarflow",
            "device_id": SERIAL,
        },
    }


def test_a_topic_the_catalog_does_not_describe_is_never_offered(tmp_path):
    srv, base = _serve(
        tmp_path,
        [
            ("hallo/EXAMPLE0000002/inverterPower", b"765"),
            ("ems-solarflow/EXAMPLE0000003/dailyYield", b"12"),
            ("EMS-SolarFlow/EXAMPLE0000004/inverterPower", b"765"),
        ],
    )
    try:
        proposals = _proposals(base)
    finally:
        srv.shutdown()
        srv.server_close()

    assert proposals == []


def test_a_payload_the_runtime_would_not_read_is_never_offered(tmp_path):
    srv, base = _serve(
        tmp_path,
        [
            (f"ems-solarflow/{SERIAL}/inverterPower", b"765 W"),
            ("ems-solarflow/EXAMPLE0000005/inverterPower", b'{"value": 765}'),
        ],
    )
    try:
        proposals = _proposals(base)
    finally:
        srv.shutdown()
        srv.server_close()

    assert proposals == []


def test_a_device_without_its_required_key_is_never_offered(tmp_path):
    """Everything but ``inverterPower``, on single topics and in the bundle."""

    srv, base = _serve(
        tmp_path,
        [
            ("ems-solarflow/EXAMPLE0000006/batterySoc", b"50"),
            ("ems-solarflow/EXAMPLE0000006/batteryPower", b"-120"),
            ("ems-solarflow/EXAMPLE0000006/solarPower", b"300"),
            (
                "ems-solarflow/EXAMPLE0000007/state",
                b'{"solarPower": 300, "batteryPower": 40, "batterySoc": 50}',
            ),
            ("ems-solarflow/EXAMPLE0000008/state", b'{"inverterPower": -20}'),
        ],
    )
    try:
        proposals = _proposals(base)
    finally:
        srv.shutdown()
        srv.server_close()

    assert proposals == []


def test_a_device_that_reports_only_its_bundle_is_offered(tmp_path):
    srv, base = _serve(
        tmp_path,
        [
            (
                f"ems-solarflow/{SERIAL}/state",
                b'{"inverterPower": 765, "batteryPower": -55, "batterySoc": 42}',
            )
        ],
    )
    try:
        proposal = _external_proposal(base)
    finally:
        srv.shutdown()
        srv.server_close()

    assert proposal["device_id"] == SERIAL
    assert proposal["config_fragment"]["mqtt"]["topic_family"] == "ems_solarflow"
    assert proposal["output_control_supported"] is False


@pytest.mark.parametrize(
    "zendure",
    [
        ("Zendure/sensor/ABC123/electricLevel", b"80"),
        ("iot/PK1/ABC123/properties/report", b'{"properties": {"electricLevel": 80}}'),
    ],
    ids=["zensdk-scalar", "legacy-json"],
)
def test_a_zendure_device_with_the_same_id_stays_a_device_of_its_own(
    tmp_path, monkeypatch, zendure
):
    """One id, one broker, two devices: the Admin keeps them two all the way.

    The selection id, the connection and the identity tokens are what Setup,
    Maintenance and the browser use to decide that two offers are one inverter;
    shared, choosing the Zendure device wrote the external one and the other way
    round.
    """

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve(tmp_path, [zendure, ("ems-solarflow/ABC123/inverterPower", b"765")])
    try:
        offers = {p["config_fragment"]["type"]: p for p in _proposals(base)}
        assert set(offers) == {"zendure_mqtt", "external_mqtt"}, offers
        zendure_offer, external_offer = offers["zendure_mqtt"], offers["external_mqtt"]

        def tokens(offer):
            return {
                offer.get("physical_identity_token"),
                *(offer.get("physical_identity_alias_tokens") or []),
            } - {None}

        assert zendure_offer["id"] != external_offer["id"]
        assert zendure_offer["connection_id"] != external_offer["connection_id"]
        assert tokens(zendure_offer) and tokens(external_offer)
        assert not tokens(zendure_offer) & tokens(external_offer)

        body = authorize_setup_mutation(
            base,
            _workflow_request,
            {
                "devices": [LOCAL_INVERTER],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [
                    {"id": offer["id"], "broker_ref": offer["broker_ref"]}
                    for offer in (zendure_offer, external_offer)
                ],
            },
        )
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()

    config = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    written = sorted(
        (device["type"], device["mqtt"]["device_id"])
        for device in config["devices"]
        if device.get("type")
    )
    assert written == [("external_mqtt", "ABC123"), ("zendure_mqtt", "ABC123")]


def test_maintenance_refuses_to_adopt_onto_a_disabled_broker_profile(
    tmp_path, monkeypatch
):
    """The discovered endpoint matches a profile the operator switched off.

    Reusing it is right, writing the device onto it unremarked is not: the
    device would never be read.
    """

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_dir = _write_base_config(tmp_path)
    config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    config["zendure_mqtt"] = {
        "brokers": {
            "fhem": {
                "enabled": False,
                "source": "local_mqtt",
                "host": BROKER["host"],
                "port": BROKER["port"],
            }
        }
    }
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    srv, base = _serve(tmp_path, INVERTER)
    try:
        proposal = _external_proposal(base)
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        draft = loaded["draft"]
        draft["devices"].append(_external_draft_item(proposal, "Garage"))
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": draft, "revision": loaded["revision"], "confirm": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert not (status == 200 and payload.get("ok") is True), payload
    after = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert _external_entries(after) == []


def test_setup_writes_the_entry_and_the_runtime_reads_it(tmp_path, monkeypatch):
    config = _run_setup(tmp_path, monkeypatch)

    (entry,) = _external_entries(config)
    assert set(entry) == {"name", "type", "enabled", "mqtt"}
    assert set(entry["mqtt"]) == {"broker_ref", "source", "topic_family", "device_id"}
    _assert_the_runtime_reads_it(config)


def test_maintenance_writes_the_entry_and_the_runtime_reads_it(tmp_path, monkeypatch):
    status, payload, config = _run_maintenance(tmp_path, monkeypatch)

    assert status == 200 and payload.get("ok") is True, payload
    (entry,) = _external_entries(config)
    assert entry["name"] == "Garage"
    assert set(entry) == {"name", "type", "enabled", "mqtt"}
    _assert_the_runtime_reads_it(config)


def test_setup_and_maintenance_write_the_same_device(tmp_path_factory, monkeypatch):
    setup = _run_setup(tmp_path_factory.mktemp("setup"), monkeypatch)
    _status, _payload, maintenance = _run_maintenance(
        tmp_path_factory.mktemp("maintenance"), monkeypatch
    )

    def comparable(config):
        (entry,) = _external_entries(config)
        return {**entry, "name": None}

    assert comparable(setup) == comparable(maintenance)
    assert setup["zendure_mqtt"] == maintenance["zendure_mqtt"]


def test_maintenance_takes_nothing_but_the_selection_from_the_browser(
    tmp_path, monkeypatch
):
    """Whatever else the draft row carries, the entry comes from the proposal."""

    def padded(proposal, name):
        item = _external_draft_item(proposal, name)
        item["mqtt"].update(topic_family="hallo", product_key="PK")
        item["capabilities"] = {"write_output_limit": True}
        item["topics"] = {"outputHomePower": "hallo/x/y"}
        item["max_power"] = 800
        item["broker"] = {"ref": "elsewhere", "host": "10.9.9.9", "port": 1883}
        return item

    status, payload, config = _run_maintenance(tmp_path, monkeypatch, item=padded)

    assert status == 200 and payload.get("ok") is True, payload
    (entry,) = _external_entries(config)
    assert set(entry) == {"name", "type", "enabled", "mqtt"}
    assert entry["mqtt"]["topic_family"] == "ems_solarflow"
    assert set(entry["mqtt"]) == {"broker_ref", "source", "topic_family", "device_id"}
    _assert_the_runtime_reads_it(config)


def test_maintenance_refuses_a_row_naming_another_device(tmp_path, monkeypatch):
    def other_device(proposal, name):
        item = _external_draft_item(proposal, name)
        item["mqtt"]["device_id"] = "SOMEONE-ELSE"
        return item

    status, payload, config = _run_maintenance(tmp_path, monkeypatch, item=other_device)

    assert not (status == 200 and payload.get("ok") is True), payload
    assert _external_entries(config) == []


def test_maintenance_refuses_a_row_no_current_proposal_backs(tmp_path, monkeypatch):
    def unbacked(proposal, name):
        item = _external_draft_item(proposal, name)
        item.pop("proposal_id")
        return item

    status, payload, config = _run_maintenance(tmp_path, monkeypatch, item=unbacked)

    assert not (status == 200 and payload.get("ok") is True), payload
    assert _external_entries(config) == []


def test_maintenance_refuses_a_zendure_row_naming_the_catalog_proposal(
    tmp_path, monkeypatch
):
    """The row's kind must be the proposal's kind.

    Otherwise a Zendure row could take the catalog device's proposal and write
    a Zendure entry -- with battery defaults -- for a device that is neither.
    """

    def as_zendure(proposal, name):
        item = _external_draft_item(proposal, name)
        item["kind"] = "zendure_mqtt"
        return item

    status, payload, config = _run_maintenance(tmp_path, monkeypatch, item=as_zendure)

    assert status == 400, payload
    assert [error["code"] for error in payload["validation"]["errors"]] == [
        "mqtt_proposal_untrusted"
    ]
    assert "not the kind of device this row adds" in payload["message"]
    assert [device for device in config["devices"] if "mqtt" in device] == []


def test_an_installed_device_is_recognised_whatever_its_profile_is_called(
    tmp_path, monkeypatch
):
    """Discovery mints its own broker ref; the installed profile has a name.

    Both name one device on one broker endpoint, so the server issues both the
    same catalog device id, and the page shows the offer as in config rather
    than offering to add the device a second time.
    """

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_dir = _write_base_config(tmp_path)
    config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    config["zendure_mqtt"] = {
        "brokers": {
            "fhem": {
                "enabled": True,
                "source": "local_mqtt",
                "host": BROKER["host"].upper(),
                "port": BROKER["port"],
            }
        }
    }
    config["devices"].append(
        {
            "name": "Garage",
            "type": "external_mqtt",
            "mqtt": {"broker_ref": "fhem", "topic_family": "ems_solarflow", "device_id": SERIAL},
        }
    )
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    srv, base = _serve(tmp_path, INVERTER)
    try:
        proposal = _external_proposal(base)
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200, loaded
    (installed,) = [d for d in loaded["draft"]["devices"] if d.get("kind") == "external_mqtt"]
    assert proposal["broker_ref"] != "fhem"
    assert proposal["catalog_device_id"].startswith("catalog:v1:")
    assert installed["catalog_device_id"] == proposal["catalog_device_id"]



def test_setup_reports_an_external_devices_broker_like_any_devices(tmp_path):
    """Setup starting from a config whose external device's profile is off.

    No difference between a Zendure device and any other: the same message.
    """

    from tests.test_admin_config_preview import _device, _existing_generator, _write_config

    path = _write_config(
        tmp_path,
        {
            "devices": [
                {"name": "WR1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
                {
                    "name": "Garage",
                    "type": "external_mqtt",
                    "mqtt": {"broker_ref": "fhem", "topic_family": "ems_solarflow", "device_id": SERIAL},
                },
                {
                    "name": "Zendure",
                    "type": "zendure_mqtt",
                    "mqtt": {"broker_ref": "fhem", "topic_family": "zensdk_ha_scalar", "device_id": "ZEN1"},
                },
            ],
            "zendure_mqtt": {
                "brokers": {
                    "fhem": {"enabled": False, "source": "local_mqtt", "host": "10.0.0.71", "port": 1883}
                }
            },
            "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        },
    )

    result = _existing_generator(path).generate([_device(1, config_name="WR1")])

    messages = [
        issue["message"]
        for issue in result["validation"]["errors"]
        if issue["code"] == "zendure_mqtt_broker_ref_disabled"
    ]
    assert [m.replace("devices.1", "devices.N").replace("devices.2", "devices.N") for m in messages] == [
        "devices.N references broker profile 'fhem', which is disabled. Configure the broker before applying.",
    ] * 2


BROKER_B = {"id": "mqtt:10.0.0.72:1883", "host": "10.0.0.72", "port": 1883}


def _serve_brokers(tmp_path, observed):
    """Discovery holding what each broker carries, as the real listener saw it."""

    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    generation = store.begin_refresh()
    candidates = []
    for broker, messages in observed:
        aggregator = MqttTopicAggregator(broker)
        for topic, payload in messages:
            aggregator.observe(topic, payload)
        candidates.append({**broker, "devices": aggregator.results()})
    store.complete_refresh(generation, candidates, success=True)
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=MqttBrokerDiscovery(store=store, topic_discoverer=None),
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _installed_on_broker_a(tmp_path, monkeypatch, device_id=SERIAL):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_dir = _write_base_config(tmp_path)
    config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    config["zendure_mqtt"] = {
        "brokers": {
            "fhem": {"enabled": True, "source": "local_mqtt", "host": BROKER["host"], "port": 1883}
        }
    }
    config["devices"].append(
        {
            "name": "Garage roof",
            "type": "external_mqtt",
            "enabled": False,
            "mqtt": {"broker_ref": "fhem", "topic_family": "ems_solarflow", "device_id": device_id},
        }
    )
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return config_dir


def _use_connection(base, proposal, **row_overrides):
    """Mirror the browser's ``mconfigUseExternalConnection`` on the installed row."""

    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200, loaded
    draft = loaded["draft"]
    (row,) = [d for d in draft["devices"] if d.get("kind") == "external_mqtt"]
    mqtt = proposal["config_fragment"]["mqtt"]
    row.update(row_overrides)
    row.update(
        proposal_id=proposal["id"],
        proposal_broker_ref=proposal["broker_ref"],
        catalog_connection_id=proposal["catalog_connection_id"],
        mqtt={key: mqtt[key] for key in ("broker_ref", "topic_family", "device_id")},
    )
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": loaded["revision"], "confirm": True},
    )


def test_maintenance_moves_an_installed_device_to_another_broker(tmp_path, monkeypatch):
    """Use connection, as for a Zendure device: the name and on/off stay."""

    config_dir = _installed_on_broker_a(tmp_path, monkeypatch)
    srv, base = _serve_brokers(tmp_path, [(BROKER, INVERTER), (BROKER_B, INVERTER)])
    try:
        offers = [p for p in _proposals(base) if p["topic_family"] == "ems_solarflow"]
        (on_b,) = [p for p in offers if p["broker_host"] == BROKER_B["host"]]
        (on_a,) = [p for p in offers if p["broker_host"] == BROKER["host"]]
        assert on_a["catalog_device_id"] == on_b["catalog_device_id"]
        assert on_a["catalog_connection_id"] != on_b["catalog_connection_id"]
        status, payload = _use_connection(base, on_b)
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200 and payload.get("ok") is True, payload
    config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    entry = _external_entries(config)[0]
    profile = config["zendure_mqtt"]["brokers"][entry["mqtt"]["broker_ref"]]
    assert profile["host"] == BROKER_B["host"]
    assert (entry["name"], entry["enabled"]) == ("Garage roof", False)
    assert entry["mqtt"]["device_id"] == SERIAL
    assert entry["mqtt"]["topic_family"] == "ems_solarflow"


def test_moving_back_onto_the_broker_it_is_on_changes_nothing(tmp_path, monkeypatch):
    """Discovery names the installed profile's broker its own way; it is one broker."""

    config_dir = _installed_on_broker_a(tmp_path, monkeypatch)
    before = (config_dir / "config.json").read_bytes()
    srv, base = _serve_brokers(tmp_path, [(BROKER, INVERTER), (BROKER_B, INVERTER)])
    try:
        (on_a,) = [
            p for p in _proposals(base)
            if p["topic_family"] == "ems_solarflow" and p["broker_host"] == BROKER["host"]
        ]
        assert on_a["broker_ref"] != "fhem"
        status, payload = _use_connection(base, on_a)
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200 and payload.get("ok") is True, payload
    assert json.loads((config_dir / "config.json").read_bytes()) == json.loads(before)


@pytest.mark.parametrize(
    "row_overrides",
    [
        pytest.param({}, id="as-the-page-sends-it"),
        # Without its original name the server's own early check has nothing to
        # look the stored device up by; the merge must refuse it on its own.
        pytest.param({"original_name": ""}, id="without-the-original-name"),
    ],
)
def test_maintenance_refuses_to_move_a_device_onto_another_devices_offer(
    tmp_path, monkeypatch, row_overrides
):
    config_dir = _installed_on_broker_a(tmp_path, monkeypatch)
    before = (config_dir / "config.json").read_bytes()
    other = [("ems-solarflow/EXAMPLE0000009/inverterPower", b"765")]
    srv, base = _serve_brokers(tmp_path, [(BROKER, INVERTER), (BROKER_B, other)])
    try:
        (foreign,) = [p for p in _proposals(base) if p["device_id"] == "EXAMPLE0000009"]
        status, payload = _use_connection(base, foreign, **row_overrides)
    finally:
        srv.shutdown()
        srv.server_close()

    assert not (status == 200 and payload.get("ok") is True), payload
    assert (config_dir / "config.json").read_bytes() == before
