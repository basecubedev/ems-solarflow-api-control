# SPDX-License-Identifier: AGPL-3.0-or-later
"""A real broker, the real Admin, the written config.json, the real runtime.

A Mosquitto broker carries what a FHEM installation publishes -- the readings
the catalog describes and the things it does not: an unreadable payload, a key
the catalog does not list, a topic of another shape, a prefix in the wrong case
-- beside a Zendure device. The Admin finds the broker by its network probe,
listens to it with its own discovery client, offers exactly the catalog device
and the Zendure device, and Setup or Maintenance writes the catalog device into
config.json. The EMS telemetry runtime is then built from that file alone, reads
the device from the same broker, and publishes nothing to it.

Input paths and output gates are where this kind of feature tends to break. The
input end runs for real: broker, network probe, discovery client, Admin HTTP,
config.json, runtime subscription. The output end is the control gates, checked
on the written config -- no control list, no control runtime device, refused for
AC charging -- with a spy on the broker as a net under the running telemetry
path, which has no publish method to begin with.
"""

import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from admin.mdns import MdnsProvider
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.mqtt_topic_discovery import default_topic_discoverer
from admin.server import ScanRegistry, create_server
from dashboard.telemetry import build_dashboard_snapshot
from ems.config import http_control_device_configs, mqtt_control_device_configs
from ems.diagnostics import diagnose_ac_charge_model_refusal
from ems.zendure_mqtt.config_entries import has_runtime_control_device
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.mosquitto import (
    _new_paho_client,
    mosquitto_broker,
    publish_once,
    publish_until,
    require_real_broker_environment,
    wait_until,
)
from tests.helpers.setup_config import authorize_setup_mutation
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_server import _FakeReleaseManager, _fake_gateway_prober, _fake_scan

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.admin,
    pytest.mark.e2e,
    pytest.mark.docker,
]

require_real_broker_environment()

SERIAL = "EXAMPLE0000001"
KOSTAL_TOPIC = f"KostalPiko/{SERIAL}/solarPower"

# Everything the broker carries before discovery listens, retained so the
# listener sees it the moment it subscribes.
RETAINED = (
    (KOSTAL_TOPIC, b"765"),
    ("KostalPiko/EXAMPLE0000002/solarPower", b"765 W"),
    ("KostalPiko/EXAMPLE0000003/dailyYield", b"12"),
    ("hallo/EXAMPLE0000004/solarPower", b"765"),
    ("kostalpiko/EXAMPLE0000005/solarPower", b"765"),
    ("Zendure/sensor/ZENDUREDEV1/electricLevel", b"55"),
    ("Zendure/sensor/ZENDUREDEV1/outputHomePower", b"120"),
)

LOCAL_INVERTER = {
    "source_id": "local:wr1",
    "config_name": "WR1",
    "display_name": "SolarFlow 800",
    "role": "inverter",
    "enabled": True,
    "ip": "192.168.1.100",
    "serial_number": "AAA",
}


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@contextlib.contextmanager
def _running_broker(tmp_path, retained):
    """A broker as the network sees it: its container address on port 1883."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    with mosquitto_broker(tmp_path, include_container_name=True) as (_host, _port, name):
        address = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                name,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert address, "the broker container has no network address"
        for topic, payload in retained:
            publish_once(address, 1883, topic, payload, retain=True)
        yield SimpleNamespace(host=address, port=1883, retained=tuple(retained))


@pytest.fixture
def broker(tmp_path):
    with _running_broker(tmp_path / "broker-a", RETAINED) as running:
        yield running


# The same Kostal, republished on a second broker with a reading of its own,
# so where a value came from is never in doubt.
RETAINED_B = ((KOSTAL_TOPIC, b"4321"),)


@pytest.fixture
def broker_b(tmp_path):
    with _running_broker(tmp_path / "broker-b", RETAINED_B) as running:
        yield running


class _Spy:
    """Everything that crosses the broker, whoever sent it."""

    def __init__(self, broker):
        self.messages = []
        self._client = _new_paho_client()
        subscribed = threading.Event()
        self._client.on_connect = lambda client, *_args: client.subscribe("#", qos=1)
        self._client.on_subscribe = lambda *_args: subscribed.set()
        self._client.on_message = lambda _client, _userdata, message: self.messages.append(
            (message.topic, bytes(message.payload))
        )
        self._client.connect(broker.host, broker.port, keepalive=10)
        self._client.loop_start()
        assert subscribed.wait(10), "the spy never subscribed"

    def close(self):
        self._client.loop_stop()
        self._client.disconnect()


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


def _workflow_request(url, method="GET", body=None):
    status, payload = _request(url, method, body)
    return status, {}, payload


def _serve(tmp_path):
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mdns_provider=MdnsProvider(
            verifier=lambda ip, port: None,
            browser_factory=lambda service_type, handler: object(),
        ),
        mqtt_discovery=MqttBrokerDiscovery(topic_discoverer=default_topic_discoverer),
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _discover(base, *brokers):
    """The Admin's own path: probe the network, then listen to what it found."""

    for broker in brokers:
        status, probe = _request(
            f"{base}/api/discovery/mqtt-brokers/probe", "POST", {"cidr": f"{broker.host}/32"}
        )
        assert status == 200 and probe["found"] == 1, probe
    status, refresh = _request(f"{base}/api/discovery/mqtt-brokers/refresh", "POST", {})
    assert status == 200 and refresh["reachable"] == len(brokers), refresh
    status, brokers = _request(f"{base}/api/discovery/mqtt-brokers")
    assert status == 200, brokers
    status, proposals = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200, proposals
    return brokers, proposals["proposals"]


def _kostal(proposals):
    found = [p for p in proposals if p["topic_family"] == "kostal_piko"]
    assert len(found) == 1, proposals
    return found[0]


def _external_entry(config):
    (entry,) = [d for d in config["devices"] if d.get("type") == "external_mqtt"]
    return entry


def _write_base_config(root):
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 1600},
                "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}],
                "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
            }
        ),
        encoding="utf-8",
    )


def _assert_the_written_config_is_read_and_never_written(config, broker):
    """From config.json on: input over the broker, nothing out of any gate."""

    entry = _external_entry(config)
    assert set(entry) == {"name", "type", "enabled", "mqtt"}
    profile = config["zendure_mqtt"]["brokers"][entry["mqtt"]["broker_ref"]]
    assert (profile["host"], profile["port"]) == (broker.host, broker.port)

    assert http_control_device_configs(config["devices"]) == [config["devices"][0]]
    assert mqtt_control_device_configs(config["devices"]) == []
    assert build_zendure_mqtt_control_runtime(config).devices == []
    assert diagnose_ac_charge_model_refusal(entry) == "telemetry_only"
    assert has_runtime_control_device({"devices": [entry]}) is False

    spy = _Spy(broker)
    runtime = build_zendure_mqtt_runtime(config)
    runtime.start()
    try:
        wait_until(
            lambda: runtime.snapshots().get(SERIAL)
            and runtime.snapshots()[SERIAL].metrics.get("outputHomePower") == 765,
            message="the retained reading never arrived",
        )
        publish_until(
            lambda: publish_once(broker.host, broker.port, KOSTAL_TOPIC, b"1234"),
            lambda: runtime.snapshots()[SERIAL].metrics.get("outputHomePower") == 1234,
            message="a new reading never arrived",
        )
        publish_once(broker.host, broker.port, KOSTAL_TOPIC, b"OFF")
        publish_until(
            lambda: publish_once(broker.host, broker.port, KOSTAL_TOPIC, b"1500"),
            lambda: runtime.snapshots()[SERIAL].metrics.get("outputHomePower") == 1500,
            message="a reading after an unreadable one never arrived",
        )
        assert set(runtime.snapshots()) == {SERIAL}
        (summary,) = runtime.device_summaries()
        assert (summary["name"], summary["status"]) == (entry["name"], "online")
        assert summary["write_output_limit"] is False

        controller = SimpleNamespace(
            devices=[],
            runtime_state=None,
            device_online={},
            commanded_total_w=0,
            filtered_load_w=0,
            _dashboard_capabilities=[],
            zendure_mqtt_runtime=runtime,
        )
        snapshot = build_dashboard_snapshot(
            controller,
            load_w=0,
            states=[],
            targets=[],
            effective_targets=[],
            allocated_total_w=0,
            effective_total_w=0,
            enabled=True,
            max_total_power=1600,
            min_output_limit=35,
        )
        tile = snapshot["devices"][entry["name"]]
        assert tile["read_only"] is True
        assert tile["output_w"] == 1500
        assert snapshot["inverter_output_w"] == 1500
    finally:
        runtime.stop()
        spy.close()

    ours = set(RETAINED) | {(KOSTAL_TOPIC, value) for value in (b"1234", b"OFF", b"1500")}
    assert [message for message in spy.messages if message not in ours] == [], (
        "the EMS published to the broker"
    )


def test_discovery_offers_exactly_the_devices_the_catalog_and_zendure_describe(
    tmp_path, broker
):
    srv, base = _serve(tmp_path)
    try:
        brokers, proposals = _discover(base, broker)
    finally:
        srv.shutdown()
        srv.server_close()

    (found,) = brokers["candidates"]
    families = sorted(device["topic_family"] for device in found["devices"])
    assert families == ["kostal_piko", "zensdk_ha_scalar"], found["devices"]

    kostal = _kostal(proposals)
    assert kostal["device_id"] == SERIAL
    assert kostal["output_control_supported"] is False
    assert kostal["config_fragment"]["type"] == "external_mqtt"
    assert {p["topic_family"] for p in proposals} == {"kostal_piko", "zensdk_ha_scalar"}
    offered = json.dumps(proposals)
    for absent in ("EXAMPLE0000002", "EXAMPLE0000003", "EXAMPLE0000004", "EXAMPLE0000005"):
        assert absent not in offered, absent


def test_setup_writes_config_json_and_the_runtime_reads_it_from_the_broker(
    tmp_path, monkeypatch, broker
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve(tmp_path)
    try:
        _brokers, proposals = _discover(base, broker)
        kostal = _kostal(proposals)
        body = authorize_setup_mutation(
            base,
            _workflow_request,
            {
                "devices": [LOCAL_INVERTER],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [
                    {"id": kostal["id"], "broker_ref": kostal["broker_ref"]}
                ],
            },
        )
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()

    config = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    _assert_the_written_config_is_read_and_never_written(config, broker)


def test_maintenance_writes_config_json_and_the_runtime_reads_it_from_the_broker(
    tmp_path, monkeypatch, broker
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_base_config(tmp_path)
    srv, base = _serve(tmp_path)
    try:
        _brokers, proposals = _discover(base, broker)
        kostal = _kostal(proposals)
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        draft = loaded["draft"]
        mqtt = kostal["config_fragment"]["mqtt"]
        draft["devices"].append(
            {
                "kind": "external_mqtt",
                "original_name": None,
                "proposal_id": kostal["id"],
                "proposal_broker_ref": kostal["broker_ref"],
                "catalog_device_id": kostal["catalog_device_id"],
                "name": "Kostal",
                "enabled": True,
                "mqtt": {key: mqtt[key] for key in ("broker_ref", "topic_family", "device_id")},
            }
        )
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": draft, "revision": loaded["revision"], "confirm": True},
        )
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()

    config = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    _assert_the_written_config_is_read_and_never_written(config, broker)


def _zendure(proposals):
    found = [p for p in proposals if p["topic_family"] == "zensdk_ha_scalar"]
    assert len(found) == 1, proposals
    return found[0]


def _apply_maintenance(base, edit):
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    edit(draft)
    status, payload = _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": loaded["revision"], "confirm": True},
    )
    assert status == 200 and payload.get("ok") is True, payload


def _readings(config, *publish):
    """What the runtime built from ``config`` takes off its brokers.

    ``publish`` are ``(broker, topic, payload, predicate)`` steps: each is
    published until its predicate holds, so every reading asked for is in
    before the answer is read.
    """

    runtime = build_zendure_mqtt_runtime(config)
    runtime.start()
    try:
        for broker, topic, payload, arrived in publish:
            publish_until(
                lambda broker=broker, topic=topic, payload=payload: publish_once(
                    broker.host, broker.port, topic, payload
                ),
                lambda arrived=arrived: arrived(runtime.snapshots()),
                message=f"{topic} = {payload!r} never arrived",
            )
        return {key: dict(snap.metrics) for key, snap in runtime.snapshots().items()}
    finally:
        runtime.stop()


def _zendure_read(broker, watts):
    return (
        broker,
        "Zendure/sensor/ZENDUREDEV1/outputHomePower",
        str(watts).encode(),
        lambda snapshots: snapshots.get("ZENDUREDEV1") is not None
        and snapshots["ZENDUREDEV1"].metrics.get("outputHomePower") == watts,
    )


def _kostal_read(broker, watts):
    return (
        broker,
        KOSTAL_TOPIC,
        str(watts).encode(),
        lambda snapshots: snapshots.get(SERIAL) is not None
        and snapshots[SERIAL].metrics.get("outputHomePower") == watts,
    )


def _kostal_row(draft):
    (row,) = [d for d in draft["devices"] if d.get("kind") == "external_mqtt"]
    return row


def _profile_of(config, name):
    (device,) = [d for d in config["devices"] if d["name"] == name]
    return config["zendure_mqtt"]["brokers"][device["mqtt"]["broker_ref"]]


def test_maintenance_edits_devices_across_two_brokers_and_the_runtime_follows(
    tmp_path, monkeypatch, broker, broker_b
):
    """Add, rename, switch off and on, move to another broker, remove.

    The basic edits every device has, made the way the page makes them, on two
    real brokers; after each, config.json says exactly that and the runtime
    reads exactly that.
    """

    from tests.test_admin_maintenance_config import _draft_item_from_proposal

    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_base_config(tmp_path)
    config_path = tmp_path / "config" / "config.json"

    def written():
        return json.loads(config_path.read_text(encoding="utf-8"))

    srv, base = _serve(tmp_path)
    spies = [_Spy(broker), _Spy(broker_b)]
    try:
        _brokers, proposals = _discover(base, broker, broker_b)
        kostal_offers = [p for p in proposals if p["topic_family"] == "kostal_piko"]
        (on_a,) = [p for p in kostal_offers if p["broker_host"] == broker.host]
        (on_b,) = [p for p in kostal_offers if p["broker_host"] == broker_b.host]
        zendure = _zendure(proposals)

        # 1. Add the Kostal and the Zendure device found on broker A.
        def add_both(draft):
            mqtt = on_a["config_fragment"]["mqtt"]
            draft["devices"].append(
                {
                    "kind": "external_mqtt",
                    "original_name": None,
                    "proposal_id": on_a["id"],
                    "proposal_broker_ref": on_a["broker_ref"],
                    "name": "Kostal",
                    "enabled": True,
                    "mqtt": {key: mqtt[key] for key in ("broker_ref", "topic_family", "device_id")},
                }
            )
            draft["devices"].append(_draft_item_from_proposal(zendure, "Zendure"))

        _apply_maintenance(base, add_both)
        config = written()
        assert {d["name"]: d.get("type") for d in config["devices"]} == {
            "WR1": None,
            "Kostal": "external_mqtt",
            "Zendure": "zendure_mqtt",
        }
        assert _profile_of(config, "Kostal")["host"] == broker.host
        readings = _readings(config, _zendure_read(broker, 120), _kostal_read(broker, 765))
        assert readings[SERIAL]["outputHomePower"] == 765

        # 2. Rename it and switch it off: kept in config.json, no longer read.
        def rename_and_switch_off(draft):
            _kostal_row(draft).update(name="Kostal Dach", enabled=False)

        _apply_maintenance(base, rename_and_switch_off)
        config = written()
        (kostal,) = _external_entries_of(config)
        assert (kostal["name"], kostal["enabled"]) == ("Kostal Dach", False)
        assert set(_readings(config, _zendure_read(broker, 121))) == {"ZENDUREDEV1"}

        # 3. Switch it on and move it to broker B with Use connection.
        def switch_on_and_move(draft):
            row = _kostal_row(draft)
            mqtt = on_b["config_fragment"]["mqtt"]
            row.update(
                enabled=True,
                proposal_id=on_b["id"],
                proposal_broker_ref=on_b["broker_ref"],
                catalog_connection_id=on_b["catalog_connection_id"],
                mqtt={key: mqtt[key] for key in ("broker_ref", "topic_family", "device_id")},
            )

        _apply_maintenance(base, switch_on_and_move)
        config = written()
        (kostal,) = _external_entries_of(config)
        assert (kostal["name"], kostal["enabled"]) == ("Kostal Dach", True)
        assert _profile_of(config, "Kostal Dach")["host"] == broker_b.host
        assert _profile_of(config, "Zendure")["host"] == broker.host
        readings = _readings(
            config, _zendure_read(broker, 122), _kostal_read(broker_b, 4322)
        )
        assert readings[SERIAL]["outputHomePower"] == 4322

        # 4. Remove it with its Remove: gone from config.json and from the runtime.
        def remove_it(draft):
            row = _kostal_row(draft)
            draft["devices"].remove(row)
            draft.setdefault("removed_external", []).append(row["entry_ref"])

        _apply_maintenance(base, remove_it)
        config = written()
        assert [d["name"] for d in config["devices"]] == ["WR1", "Zendure"]
        assert set(_readings(config, _zendure_read(broker, 123))) == {"ZENDUREDEV1"}
    finally:
        srv.shutdown()
        srv.server_close()
        for spy in spies:
            spy.close()

    for spy, running in zip(spies, (broker, broker_b)):
        ours = set(running.retained) | {
            (KOSTAL_TOPIC, b"765"),
            (KOSTAL_TOPIC, b"4322"),
        } | {
            ("Zendure/sensor/ZENDUREDEV1/outputHomePower", str(watts).encode())
            for watts in (120, 121, 122, 123)
        }
        assert [m for m in spy.messages if m not in ours] == [], "the EMS published to a broker"


def _external_entries_of(config):
    return [d for d in config["devices"] if d.get("type") == "external_mqtt"]
