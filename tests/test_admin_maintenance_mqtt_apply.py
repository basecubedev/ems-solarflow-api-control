# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end Maintenance apply workflow for MQTT devices and their credentials.

A device added through Maintenance must behave exactly like one added through
Fresh Setup: the apply provisions the runtime credential records its broker
profiles reference (Zendure cloud MQTT credentials, promoted local discovery
credentials) before ``config.json`` changes, so a freshly restarted EMS — with
no Admin process memory — resolves every ``credentials_ref``. A provisioning
failure blocks the apply and leaves the config untouched.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin.credential_store import CredentialStore
from admin.inverter_names import next_compact_inverter_name
from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.server import ScanRegistry, create_server
from admin.zendure_cloud_auth import ZendureCloudError
from admin.zendure_cloud_mqtt import FakeCloudMqttListener, ZendureCloudDiscovery
from ems.mqtt_credentials import FileMqttCredentialResolver, MqttCredentialError
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_maintenance_config import _draft_item_from_proposal
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

pytestmark = pytest.mark.simulation

CLOUD_REF = "zendure-cloud"
API_KEY = "raw-account-api-key"
MQTT_USER = "cloud-mqtt-user"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _CloudFetch:
    """Mutable fake deviceList backend (supports outages and call counting)."""

    def __init__(self):
        self.password = "cloud-mqtt-secret-1"
        self.app_key = "cloud-app-key-1"
        self.client_id = "cloud-client-1"
        self.fail = False
        self.calls = 0

    def __call__(self, _token, _timeout):
        self.calls += 1
        if self.fail:
            raise ZendureCloudError("deviceList unavailable")
        return {
            "devices": [
                {
                    "productKey": "PK-AAA",
                    "deviceKey": "DK-BBB",
                    "productModel": "SolarFlow 800",
                    "snNumber": "SN-CLOUD1",
                    "deviceName": "Cloud inverter",
                }
            ],
            "mqtt": {
                "host": "mqtteu.zen-iot.com",
                "port": 8883,
                "username": MQTT_USER,
                "password": self.password,
                "client_id": self.client_id,
            },
            "app_key": self.app_key,
        }


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


def _existing_config():
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
    }


def _write_config(root, data):
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _local_observation(credentials_ref=None, serial="SN-LOCAL1"):
    observation = {
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "source_type": "local_mqtt",
        "topic_family": "zensdk_ha_scalar",
        "device_id": serial,
        "serial_number": serial,
        "metrics_seen": ["electricLevel", "outputHomePower"],
        "topics_seen": [f"Zendure/sensor/{serial}/electricLevel"],
    }
    if credentials_ref:
        observation["credentials_ref"] = credentials_ref
    return observation


def _local_discovery(observation):
    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    broker = {
        "id": "mqtt:10.0.0.10:1883",
        "host": "10.0.0.10",
        "port": 1883,
        "devices": [observation],
    }
    generation = store.begin_refresh()
    store.complete_refresh(generation, [broker], success=True)
    return MqttBrokerDiscovery(store=store, topic_discoverer=None)


def _serve(tmp_path, fetch, local_observation=None):
    messages = [
        (
            "iot/PK-AAA/DK-BBB/properties/report",
            json.dumps({"properties": {"electricLevel": 55, "outputHomePower": 120}}),
        )
    ]
    cloud = ZendureCloudDiscovery(
        CredentialStore().zendure,
        device_list_fetcher=fetch,
        listener_factory=lambda c: FakeCloudMqttListener(c, messages),
        timeout_s=0.0,
    )
    discovery = (
        _local_discovery(local_observation)
        if local_observation is not None
        else MqttBrokerDiscovery(
            store=MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900),
            topic_discoverer=None,
        )
    )
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=discovery,
        zendure_cloud_discovery=cloud,
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _cloud_proposal(base):
    status, payload = _request(
        f"{base}/api/discovery/zendure-cloud-mqtt/token", "POST", {"api_key": API_KEY}
    )
    assert status == 200 and payload["ok"] is True, payload
    status, payload = _request(
        f"{base}/api/discovery/zendure-cloud-mqtt/refresh", "POST", {}
    )
    assert status == 200 and payload["ok"] is True, payload
    status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200
    cloud = [p for p in payload["proposals"] if p["broker_ref"] == "zendure_cloud"]
    assert cloud, payload
    # The browser receives only an opaque proposal identity. The raw product key
    # remains server-side even though the trusted proposal can use it to enable
    # output control during preview/apply.
    assert "PK-AAA" not in json.dumps(cloud)
    return cloud[0]


def _local_proposal(base):
    status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200
    local = [p for p in payload["proposals"] if p["broker_ref"] != "zendure_cloud"]
    assert local, payload
    return local[0]


def _maintenance_apply_with_proposal(base, proposal):
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    names = [str(item.get("name") or "") for item in draft["devices"]]
    config_name = next_compact_inverter_name(names, len(draft["devices"]))
    draft["devices"].append(_draft_item_from_proposal(proposal, config_name))
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": loaded["revision"], "confirm": True},
    )


def _paths(root):
    return root / "config" / "config.json", root / "config" / "secrets"


def _cloud_broker_summary(config, resolver):
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=resolver,
    )
    try:
        for broker in runtime.status()["brokers"]:
            if broker["broker_ref"] == "zendure_cloud":
                return broker
        return None
    finally:
        runtime.stop()


def test_maintenance_apply_provisions_cloud_runtime_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
        broker = config["zendure_mqtt"]["brokers"]["zendure_cloud"]
        assert broker["credentials_ref"] == CLOUD_REF
        cloud_device = next(
            device
            for device in config["devices"]
            if device.get("type") == "zendure_mqtt"
        )
        assert cloud_device["mqtt"]["product_key"] == "PK-AAA"
        assert cloud_device["capabilities"]["write_output_limit"] is True
        # Only the reference lands in config; never a plaintext secret.
        for secret in (API_KEY, MQTT_USER, fetch.password, fetch.app_key, fetch.client_id):
            assert secret not in raw

        # The runtime record resolves through the Core resolver with no Admin
        # process memory: an EMS restart can reconnect on its own.
        assert (secrets_dir / f"mqtt-{CLOUD_REF}.json").is_file()
        resolver = FileMqttCredentialResolver(secrets_dir)
        credentials = resolver.resolve(CLOUD_REF)
        assert credentials.username == MQTT_USER
        assert credentials.password == fetch.password

        summary = _cloud_broker_summary(config, resolver)
        assert summary is not None
        assert summary.get("issue") != "broker_auth_missing"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_blocks_when_cloud_provisioning_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        proposal = _cloud_proposal(base)
        fetch.fail = True
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert payload.get("message"), payload

        # Nothing was committed: the config is byte-identical and no orphan
        # runtime secret exists.
        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_local_anonymous_broker_skips_cloud_provisioning(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    fetch.fail = True  # any cloud call would fail the apply
    srv, base = _serve(tmp_path, fetch, local_observation=_local_observation())
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        assert fetch.calls == 0

        config_path, secrets_dir = _paths(tmp_path)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        ref = proposal["broker_ref"]
        assert config["zendure_mqtt"]["brokers"][ref]["host"] == "10.0.0.10"
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_promotes_local_broker_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        assert fetch.calls == 0

        config_path, secrets_dir = _paths(tmp_path)
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
        ref = proposal["broker_ref"]
        assert config["zendure_mqtt"]["brokers"][ref]["credentials_ref"] == "home"
        assert "password" not in raw

        # The promoted runtime record resolves independently of Admin state.
        credentials = FileMqttCredentialResolver(secrets_dir).resolve("home")
        assert credentials.username == "user"
        assert credentials.password == "password"
    finally:
        srv.shutdown()
        srv.server_close()


def _manual_local_device(credentials_ref=None, ref="local_mqtt", host="10.0.0.11"):
    """A manually-typed local MQTT device draft item (no proposal_id).

    Mirrors the shape the Maintenance manual-add frontend produces: kind
    zendure_mqtt with an mqtt.broker_ref and a browser-owned broker block. No
    plaintext credential rides on the device — the secret is staged separately
    into the discovery pool and only referenced here by credentials_ref.
    """

    device = {
        "kind": "zendure_mqtt",
        "original_name": None,
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": "SN-MANUAL1",
        "device_id": "SN-MANUAL1",
        "product_key": "",
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "",
        "output_control": False,
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
        "mqtt": {"broker_ref": ref, "topic_family": "", "base_topic": None, "device_id": "SN-MANUAL1"},
        "broker": {
            "ref": ref,
            "host": host,
            "port": 1883,
            "tls": False,
            "tls_insecure": False,
            "tls_mode": "",
            "source": "local_mqtt",
        },
    }
    if credentials_ref:
        device["broker"]["credentials_ref"] = credentials_ref
    return device


def _maintenance_apply_with_manual_device(base, device):
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    names = [str(item.get("name") or "") for item in draft["devices"]]
    device = dict(device)
    device["name"] = next_compact_inverter_name(names, len(draft["devices"]))
    draft["devices"].append(device)
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": loaded["revision"], "confirm": True},
    )


def test_maintenance_apply_provisions_manual_local_broker_with_auth(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    fetch.fail = True  # no cloud device is involved in a manual local add
    srv, base = _serve(tmp_path, fetch)
    # The frontend mints the typed secret into the discovery pool before apply.
    srv.credential_store.save_mqtt_discovery_secret(
        "local_mqtt", "broker-user", "broker-pass"
    )
    try:
        device = _manual_local_device(credentials_ref="local_mqtt")
        status, payload = _maintenance_apply_with_manual_device(base, device)
        assert status == 200 and payload.get("ok") is True, payload
        assert fetch.calls == 0

        config_path, secrets_dir = _paths(tmp_path)
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
        broker = config["zendure_mqtt"]["brokers"]["local_mqtt"]
        assert broker["host"] == "10.0.0.11"
        assert broker["credentials_ref"] == "local_mqtt"
        # The password never lands in config.json.
        assert "broker-pass" not in raw

        # The promoted runtime record resolves without Admin process memory.
        credentials = FileMqttCredentialResolver(secrets_dir).resolve("local_mqtt")
        assert credentials.username == "broker-user"
        assert credentials.password == "broker-pass"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_provisions_manual_anonymous_local_broker(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    fetch.fail = True
    srv, base = _serve(tmp_path, fetch)
    try:
        device = _manual_local_device()  # no credentials_ref
        status, payload = _maintenance_apply_with_manual_device(base, device)
        assert status == 200 and payload.get("ok") is True, payload
        assert fetch.calls == 0

        config_path, secrets_dir = _paths(tmp_path)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        broker = config["zendure_mqtt"]["brokers"]["local_mqtt"]
        assert broker["host"] == "10.0.0.11"
        assert "credentials_ref" not in broker
        assert not (secrets_dir / "mqtt-local_mqtt.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_blocks_manual_local_broker_without_secret(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    fetch.fail = True
    srv, base = _serve(tmp_path, fetch)
    try:
        # credentials_ref names a secret that was never staged into the pool.
        device = _manual_local_device(credentials_ref="local_mqtt")
        status, payload = _maintenance_apply_with_manual_device(base, device)
        assert status >= 400, payload
        assert payload.get("ok") is False

        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        assert not (secrets_dir / "mqtt-local_mqtt.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_write_failure_rolls_back_promoted_credential(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    srv.config_apply.apply_maintenance = _boom
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 500, payload
        assert payload.get("ok") is False

        # The staged runtime record is rolled back and the config untouched.
        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        with pytest.raises(MqttCredentialError):
            FileMqttCredentialResolver(secrets_dir).resolve("home")
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_apply_blocks_when_local_credential_promotion_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    config_path = _write_config(tmp_path, _existing_config())
    original = config_path.read_bytes()
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    # Deliberately no discovery secret: the promotion source is missing.
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status >= 400, payload
        assert payload.get("ok") is False

        assert config_path.read_bytes() == original
        _, secrets_dir = _paths(tmp_path)
        with pytest.raises(MqttCredentialError):
            FileMqttCredentialResolver(secrets_dir).resolve("home")
    finally:
        srv.shutdown()
        srv.server_close()
