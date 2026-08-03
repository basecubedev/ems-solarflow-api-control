# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fresh-install discovery contract: settings -> refresh -> gather -> unify.

Models the flow behind the setup "Run discovery" button end to end against the
real Admin HTTP server: save discovery preparation, start the run, refresh every
enabled source exactly once, collect all per-source candidates, merge identical
devices case-insensitively by serial, select the source by configured priority,
and return one unified result. Every network-touching dependency (LAN scan,
mDNS, local MQTT brokers, Zendure cloud) is a controlled fake or spy — these
tests validate orchestration, priority selection, merging, failure isolation,
and broker configuration, never real MQTT hardware behavior.

The backend entry point under contract is ``POST /api/discovery/run`` with a
``{"refresh": true}`` body: the one small testable orchestration function the
fresh-install UI runs (the UI keeps only the LAN scan, which by design finishes
before the source refreshes).
"""

import base64
import json
import os
import threading
import time
from dataclasses import dataclass

import pytest

from ems import paths

from admin.credential_store import CredentialStore
from admin.discovery_connections import DiscoveryConnectionsStore
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.server import ScanRegistry, create_admin_runtime, create_server
from admin.zendure_cloud_auth import ZendureCloudError
from admin.zendure_cloud_mqtt import FakeCloudMqttListener, ZendureCloudDiscovery
from tests.admin_auth_helpers import authenticate, request

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

SERIAL = "SN-PRIORITY-001"
LOWER_SERIAL = "sn-priority-001"
ALL_SOURCES = ["local_api", "local_mqtt", "zendure_mqtt"]

API_DISPLAY_NAME = "API SolarFlow 800 Pro 2"
LOCAL_MQTT_DISPLAY_NAME = "Local MQTT SolarFlow"
CLOUD_DISPLAY_NAME = "Balcony battery"

POOL_PASSWORD = "pool-secret-pw"
CLOUD_MQTT_PASSWORD = "CLOUD-MQTT-SECRET"
# base64 of "<api_url>.<app_key>"; the deviceList fetcher is faked so only
# decodability matters (same shape as the real saved Zendure API key).
ZENDURE_API_KEY = base64.b64encode(
    b"https://app.zendure.tech.APP-KEY-SECRET"
).decode("ascii")

SECRET_MARKERS = (
    "APP-KEY-SECRET",
    "PK-SECRET-AAA",
    "DK-SECRET-BBB",
    CLOUD_MQTT_PASSWORD,
    POOL_PASSWORD,
    ZENDURE_API_KEY,
)


def local_api_device(serial=SERIAL, ip="192.168.1.50"):
    return {
        "id": f"zendure_local_http:{serial or ip}",
        "ip": ip,
        "port": 80,
        "api_family": "zendure_local_http",
        "device_type": "zendure_solarflow_800_pro_2",
        "role_suggestion": "inverter",
        "display_name": API_DISPLAY_NAME,
        "model": "SolarFlow 800 Pro 2",
        "serial_number": serial,
        "confidence": 0.95,
        "verified": True,
    }


def local_mqtt_device(
    serial=LOWER_SERIAL,
    broker="local-broker-a",
    device_id="local-mqtt-device-1",
    display_name=LOCAL_MQTT_DISPLAY_NAME,
):
    return {
        "id": f"mqtt-device:local_mqtt:{broker}:{serial or device_id}",
        "serial_number": serial,
        "device_id": device_id,
        "model_hint": "SolarFlow 800 Pro 2",
        "display_name": display_name,
        "source_type": "local_mqtt",
    }


def zendure_device_list(_api_key, _timeout):
    return {
        "devices": [
            {
                "productKey": "PK-SECRET-AAA",
                "deviceKey": "DK-SECRET-BBB",
                "productModel": "SolarFlow 800 Pro 2",
                "snNumber": SERIAL,
                "deviceName": CLOUD_DISPLAY_NAME,
            }
        ],
        "mqtt": {
            "host": "mqtt.example.invalid",
            "port": 8883,
            "username": "cloud-user",
            "password": CLOUD_MQTT_PASSWORD,
            "client_id": "client-xyz",
        },
        "api_url": "https://app.zendure.tech",
        "app_key": "APP-KEY-SECRET",
    }


def failing_device_list(_api_key, _timeout):
    raise ZendureCloudError("Zendure cloud request failed.")


class CountingFetcher:
    def __init__(self, impl=zendure_device_list):
        self.impl = impl
        self.calls = 0

    def __call__(self, api_key, timeout):
        self.calls += 1
        return self.impl(api_key, timeout)


class SpyMdnsProvider:
    """Local-API mDNS fake: devices become visible only after ``refresh()``.

    Gating the devices on the refresh makes a missing backend refresh call show
    up as a missing candidate, not just as a spy-count mismatch.
    """

    def __init__(self, devices=(), *, fail=False, delay_s=0.0):
        self._pending = [dict(device) for device in devices]
        self._devices = []
        self.refresh_calls = 0
        self.fail = fail
        self.delay_s = delay_s

    def refresh(self):
        self.refresh_calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("mdns refresh failed")
        self._devices = [dict(device) for device in self._pending]
        return self.status()

    def devices(self):
        return [dict(device) for device in self._devices]

    def ignored_devices(self):
        return []

    def status(self):
        return {
            "available": True,
            "enabled": True,
            "running": True,
            "state": "running_with_devices" if self._devices else "running_no_devices",
            "message": "fake mdns",
            "verified_count": len(self._devices),
            "ignored_count": 0,
            "mdns_device_count": len(self._devices),
        }


class SpyMqttDiscovery(MqttBrokerDiscovery):
    def __init__(self, *args, fail=False, delay_s=0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.refresh_calls = 0
        self.fail = fail
        self.delay_s = delay_s

    def refresh(self):
        self.refresh_calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("local mqtt refresh failed")
        return super().refresh()


class SpyZendureDiscovery(ZendureCloudDiscovery):
    def __init__(self, *args, delay_s=0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.refresh_calls = 0
        self.delay_s = delay_s

    def refresh(self):
        self.refresh_calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        return super().refresh()


class FakeBrokerNetwork:
    """Fake local MQTT world: (host, port) -> required transport/auth + devices.

    ``connector`` and ``topic_discoverer`` plug into the real
    ``MqttBrokerDiscovery`` machinery, so the contract runs the genuine
    per-broker refresh (generation window, anonymous + credential-pool attempt
    matrix, TLS transport inference) with no real broker.
    """

    def __init__(self):
        self.brokers = {}
        self.attempts = []

    def add(self, host, port, devices, *, username=None, password=None, tls=None):
        self.brokers[(host, port)] = {
            "devices": [dict(device) for device in devices],
            "username": username,
            "password": password,
            "tls": bool(tls) if tls is not None else int(port) == 8883,
        }

    def connector(self, host, port, _timeout_s):
        return (host, int(port)) in self.brokers

    def topic_discoverer(self, attempt_broker):
        key = (attempt_broker.get("host"), int(attempt_broker.get("port") or 0))
        self.attempts.append(
            {
                "host": key[0],
                "port": key[1],
                "tls": bool(attempt_broker.get("tls")),
                "username": attempt_broker.get("username"),
                "password": attempt_broker.get("password"),
                "credentials_ref": attempt_broker.get("credentials_ref"),
            }
        )
        spec = self.brokers.get(key)
        if spec is None:
            return {"status": "connection_failed", "devices": []}
        if bool(attempt_broker.get("tls")) != spec["tls"]:
            return {"status": "connection_failed", "devices": []}
        if spec["username"] is not None and (
            attempt_broker.get("username") != spec["username"]
            or attempt_broker.get("password") != spec["password"]
        ):
            return {"status": "connection_failed", "devices": []}
        return {
            "status": "topics_seen",
            "devices": [dict(device) for device in spec["devices"]],
        }


@dataclass
class Harness:
    base: str
    mdns: SpyMdnsProvider
    mqtt: SpyMqttDiscovery
    zendure: SpyZendureDiscovery
    network: FakeBrokerNetwork
    fetcher: CountingFetcher
    prep_store: DiscoveryConnectionsStore


@pytest.fixture()
def harness_factory(tmp_path, monkeypatch, isolated_install_root):
    """Build fully faked Admin servers; every server is shut down on teardown."""

    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path))
    servers = []

    def build(
        *,
        api_devices=(),
        network=None,
        brokers=(),
        fetcher=None,
        zendure_api_key=ZENDURE_API_KEY,
        mdns_fail=False,
        mqtt_fail=False,
        mdns_delay=0.0,
        mqtt_delay=0.0,
        zendure_delay=0.0,
    ):
        prep_store = DiscoveryConnectionsStore()
        cred_store = CredentialStore()
        network = network or FakeBrokerNetwork()
        fetcher = fetcher or CountingFetcher()
        mdns = SpyMdnsProvider(api_devices, fail=mdns_fail, delay_s=mdns_delay)
        mqtt = SpyMqttDiscovery(
            connector=network.connector,
            topic_discoverer=network.topic_discoverer,
            credential_lookup=cred_store.load_mqtt_discovery_secret,
            credential_refs_provider=(
                lambda: prep_store.load()["local_mqtt"]["credential_refs"]
            ),
            fail=mqtt_fail,
            delay_s=mqtt_delay,
        )
        for broker in brokers:
            mqtt.add_mdns_candidate(dict(broker))
        zendure = SpyZendureDiscovery(
            store=cred_store.zendure,
            device_list_fetcher=fetcher,
            listener_factory=lambda conn: FakeCloudMqttListener(conn),
            delay_s=zendure_delay,
        )
        if zendure_api_key:
            zendure.save_token(zendure_api_key)
        runtime = create_admin_runtime(
            registry=ScanRegistry(),
            mdns_provider=mdns,
            mqtt_discovery=mqtt,
            zendure_cloud_discovery=zendure,
            discovery_preparation=prep_store,
        )
        srv = create_server("127.0.0.1", 0, runtime=runtime)
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        authenticate(base)
        return Harness(
            base=base,
            mdns=mdns,
            mqtt=mqtt,
            zendure=zendure,
            network=network,
            fetcher=fetcher,
            prep_store=prep_store,
        )

    yield build
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def plain_broker(host="192.168.1.60", port=1883):
    return {
        "host": host,
        "port": port,
        "source": "network_probe",
        "status": "tcp_open",
        "confidence": 0.6,
    }


def three_source_harness(harness_factory, **overrides):
    """One physical device visible from all three sources at once."""

    network = overrides.pop("network", None) or FakeBrokerNetwork()
    if ("192.168.1.60", 1883) not in network.brokers:
        network.add("192.168.1.60", 1883, [local_mqtt_device()])
    defaults = {
        "api_devices": [local_api_device()],
        "network": network,
        "brokers": [plain_broker()],
    }
    defaults.update(overrides)
    return harness_factory(**defaults)


def save_preparation(base, payload):
    status, _, saved = request(
        f"{base}/api/discovery/preparation", method="POST", body=payload
    )
    assert status == 200
    return saved


def run_discovery(base, *, refresh=True):
    body = {"refresh": True} if refresh else None
    return request(f"{base}/api/discovery/run", method="POST", body=body)


def matching_devices(payload, serial=SERIAL):
    return [
        d
        for d in payload["devices"]
        if (d.get("serial_number") or "").lower() == serial.lower()
    ]


# --- refresh orchestration -------------------------------------------------


def test_fresh_install_discovery_refreshes_every_enabled_source(harness_factory):
    harness = three_source_harness(harness_factory)
    status, _, payload = run_discovery(harness.base)
    assert status == 200
    assert harness.mdns.refresh_calls == 1
    assert harness.mqtt.refresh_calls == 1
    assert harness.zendure.refresh_calls == 1
    refresh = payload["refresh"]
    assert set(refresh["sources"]) == set(ALL_SOURCES)
    assert all(refresh["sources"][source]["ok"] for source in ALL_SOURCES)
    assert refresh["status"] == "ok"


def test_fresh_install_discovery_skips_disabled_sources(harness_factory):
    harness = three_source_harness(harness_factory)
    save_preparation(
        harness.base,
        {
            "sources": {
                "local_mqtt": {"enabled": False},
                "zendure_mqtt": {"enabled": False},
            }
        },
    )
    status, _, payload = run_discovery(harness.base)
    assert status == 200
    assert harness.mdns.refresh_calls == 1
    assert harness.mqtt.refresh_calls == 0
    assert harness.zendure.refresh_calls == 0
    assert harness.fetcher.calls == 0
    assert set(payload["refresh"]["sources"]) == {"local_api"}

    devices = matching_devices(payload)
    assert len(devices) == 1
    device = devices[0]
    assert device["selected_source"] == "local_api"
    assert device["sources"] == ["local_api"]
    assert {c["source"] for c in device["candidates"]} == {"local_api"}


def test_run_without_refresh_request_stays_read_only(harness_factory):
    # Rescan/maintenance callers keep using the read-only unify: a plain POST
    # without a refresh request must not start any source refresh.
    harness = three_source_harness(harness_factory)
    status, _, _payload = run_discovery(harness.base, refresh=False)
    assert status == 200
    assert harness.mdns.refresh_calls == 0
    assert harness.mqtt.refresh_calls == 0
    assert harness.zendure.refresh_calls == 0


# --- priority matrix ---------------------------------------------------------


PRIORITY_PERMUTATIONS = [
    (["local_api", "local_mqtt", "zendure_mqtt"], "local_api"),
    (["local_api", "zendure_mqtt", "local_mqtt"], "local_api"),
    (["local_mqtt", "local_api", "zendure_mqtt"], "local_mqtt"),
    (["local_mqtt", "zendure_mqtt", "local_api"], "local_mqtt"),
    (["zendure_mqtt", "local_api", "local_mqtt"], "zendure_mqtt"),
    (["zendure_mqtt", "local_mqtt", "local_api"], "zendure_mqtt"),
]

WINNER_DISPLAY_NAME = {
    "local_api": API_DISPLAY_NAME,
    "local_mqtt": LOCAL_MQTT_DISPLAY_NAME,
    "zendure_mqtt": CLOUD_DISPLAY_NAME,
}


@pytest.mark.parametrize(("priority", "expected_source"), PRIORITY_PERMUTATIONS)
def test_fresh_install_discovery_honors_all_priority_permutations(
    harness_factory, priority, expected_source
):
    harness = three_source_harness(harness_factory)
    save_preparation(harness.base, {"discovery_priority": priority})

    status, _, payload = run_discovery(harness.base)
    assert status == 200
    assert payload["priority"] == priority

    devices = matching_devices(payload)
    assert len(devices) == 1
    device = devices[0]

    assert device["selected_source"] == expected_source
    assert device["sources"] == priority
    assert device["selected_reason"] == "Selected by discovery priority"
    assert device["confidence"] == "high"
    assert {c["source"] for c in device["candidates"]} == set(ALL_SOURCES)

    # The winner's own identity must be selected, not just its source label.
    assert device["display_name"] == WINNER_DISPLAY_NAME[expected_source]
    if expected_source == "local_api":
        assert device["ip"] == "192.168.1.50"
        assert device["api_family"] == "zendure_local_http"
    elif expected_source == "local_mqtt":
        assert device["device_id"] == "local-mqtt-device-1"
    else:
        assert device["device_id"] == SERIAL

    # Connection facts classify by the winning transport: only a local-API
    # winner carries the local-API family/IP. An MQTT winner must not inherit
    # them from the also-present local-API candidate, or it would read as API.
    if expected_source == "local_api":
        assert device["api_family"] == "zendure_local_http"
    else:
        assert device["api_family"] is None
        assert device["ip"] is None


@pytest.mark.parametrize(
    ("priority", "expected_source", "delays"),
    [
        (
            ["local_api", "local_mqtt", "zendure_mqtt"],
            "local_api",
            {"mdns_delay": 0.3, "mqtt_delay": 0.0, "zendure_delay": 0.15},
        ),
        (
            ["zendure_mqtt", "local_mqtt", "local_api"],
            "zendure_mqtt",
            {"mdns_delay": 0.15, "mqtt_delay": 0.0, "zendure_delay": 0.3},
        ),
    ],
)
def test_fresh_install_discovery_ignores_completion_order(
    harness_factory, priority, expected_source, delays
):
    # The highest-priority source finishes last; completion order, broker
    # response time, and dict order must never override the configured priority.
    harness = three_source_harness(harness_factory, **delays)
    save_preparation(harness.base, {"discovery_priority": priority})

    status, _, payload = run_discovery(harness.base)
    assert status == 200
    assert payload["refresh"]["status"] == "ok"

    devices = matching_devices(payload)
    assert len(devices) == 1
    assert devices[0]["selected_source"] == expected_source
    assert devices[0]["sources"] == priority


# --- enabled-source combinations ---------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "priority", "expected_source"),
    [
        (["local_api"], ALL_SOURCES, "local_api"),
        (["local_mqtt"], ALL_SOURCES, "local_mqtt"),
        (["zendure_mqtt"], ALL_SOURCES, "zendure_mqtt"),
        (["local_api", "local_mqtt"], ALL_SOURCES, "local_api"),
        (
            ["local_api", "local_mqtt"],
            ["local_mqtt", "local_api", "zendure_mqtt"],
            "local_mqtt",
        ),
        (["local_api", "zendure_mqtt"], ALL_SOURCES, "local_api"),
        (
            ["local_api", "zendure_mqtt"],
            ["zendure_mqtt", "local_api", "local_mqtt"],
            "zendure_mqtt",
        ),
        (["local_mqtt", "zendure_mqtt"], ALL_SOURCES, "local_mqtt"),
        (
            ["local_mqtt", "zendure_mqtt"],
            ["zendure_mqtt", "local_mqtt", "local_api"],
            "zendure_mqtt",
        ),
    ],
)
def test_fresh_install_discovery_covers_all_enabled_source_combinations(
    harness_factory, enabled, priority, expected_source
):
    harness = three_source_harness(harness_factory)
    save_preparation(
        harness.base,
        {
            "discovery_priority": priority,
            "sources": {source: {"enabled": source in enabled} for source in ALL_SOURCES},
        },
    )

    status, _, payload = run_discovery(harness.base)
    assert status == 200

    devices = matching_devices(payload)
    assert len(devices) == 1
    device = devices[0]
    assert device["selected_source"] == expected_source
    # Disabled sources never leak into sources/candidates/selected_source; the
    # order of the surviving sources still follows the configured priority.
    assert device["sources"] == [s for s in priority if s in enabled]
    assert {c["source"] for c in device["candidates"]} == set(enabled)

    assert harness.mdns.refresh_calls == (1 if "local_api" in enabled else 0)
    assert harness.mqtt.refresh_calls == (1 if "local_mqtt" in enabled else 0)
    assert harness.zendure.refresh_calls == (1 if "zendure_mqtt" in enabled else 0)
    if "zendure_mqtt" not in enabled:
        assert harness.fetcher.calls == 0
    assert set(payload["refresh"]["sources"]) == set(enabled)


# --- identity merging --------------------------------------------------------


def test_fresh_install_discovery_merges_same_serial_case_insensitively(
    harness_factory,
):
    harness = three_source_harness(harness_factory)
    status, _, payload = run_discovery(harness.base)
    assert status == 200

    devices = matching_devices(payload)
    assert len(devices) == 1, "case-differing serials must collapse into one device"
    device = devices[0]
    assert device["confidence"] == "high"
    assert (device["serial_number"] or "").lower() == LOWER_SERIAL
    serials = {(c["serial_number"] or "").lower() for c in device["candidates"]}
    assert serials == {LOWER_SERIAL}


def test_fresh_install_discovery_preserves_multiple_local_broker_candidates(
    harness_factory,
):
    network = FakeBrokerNetwork()
    network.add(
        "192.168.1.60",
        1883,
        [local_mqtt_device(broker="local-broker-a", device_id="dev-broker-a")],
    )
    network.add(
        "192.168.1.61",
        1883,
        [
            local_mqtt_device(broker="local-broker-b", device_id="dev-broker-b"),
            local_mqtt_device(
                serial="SN-OTHER-002",
                broker="local-broker-b",
                device_id="dev-other",
                display_name="Second device",
            ),
        ],
    )
    harness = harness_factory(
        network=network,
        brokers=[plain_broker(), plain_broker(host="192.168.1.61")],
    )
    save_preparation(
        harness.base,
        {
            "sources": {
                "local_api": {"enabled": False},
                "zendure_mqtt": {"enabled": False},
            }
        },
    )

    status, _, payload = run_discovery(harness.base)
    assert status == 200

    # Same serial on two brokers: one unified device, both candidates kept.
    shared = matching_devices(payload)
    assert len(shared) == 1
    candidates = shared[0]["candidates"]
    assert len(candidates) == 2
    assert {c["source"] for c in candidates} == {"local_mqtt"}
    assert {c["device_id"] for c in candidates} == {"dev-broker-a", "dev-broker-b"}

    # A different device on the second broker stays its own unified device.
    assert len(matching_devices(payload, serial="SN-OTHER-002")) == 1


def test_fresh_install_discovery_keeps_devices_without_serial_separate(
    harness_factory,
):
    network = FakeBrokerNetwork()
    network.add(
        "192.168.1.60",
        1883,
        [local_mqtt_device(serial=None, device_id="anon-mqtt-device")],
    )
    harness = harness_factory(
        api_devices=[local_api_device(serial=None, ip="192.168.1.50")],
        network=network,
        brokers=[plain_broker()],
        zendure_api_key=None,
    )
    save_preparation(
        harness.base, {"sources": {"zendure_mqtt": {"enabled": False}}}
    )

    status, _, payload = run_discovery(harness.base)
    assert status == 200
    assert len(payload["devices"]) == 2, "serial-less devices must never merge"
    for device in payload["devices"]:
        assert device["confidence"] == "low"
        assert len(device["candidates"]) == 1


# --- broker variants ---------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "tls", "username", "password"),
    [
        (1883, False, None, None),
        (1883, False, "pool-user", POOL_PASSWORD),
        (8883, True, None, None),
        (8883, True, "pool-user", POOL_PASSWORD),
    ],
)
def test_fresh_install_discovery_supports_local_broker_variants(
    harness_factory, port, tls, username, password
):
    network = FakeBrokerNetwork()
    network.add(
        "192.168.1.60",
        port,
        [local_mqtt_device()],
        username=username,
        password=password,
        tls=tls,
    )
    harness = harness_factory(
        network=network,
        brokers=[plain_broker(port=port)],
        zendure_api_key=None,
    )
    save_preparation(
        harness.base,
        {
            "sources": {
                "local_api": {"enabled": False},
                "zendure_mqtt": {"enabled": False},
            }
        },
    )
    if username is not None:
        status, _, _ = request(
            f"{harness.base}/api/discovery/connections/mqtt-credentials",
            method="POST",
            body={"label": "pool", "username": username, "password": password},
        )
        assert status == 200

    status, _, payload = run_discovery(harness.base)
    assert status == 200

    devices = matching_devices(payload)
    assert len(devices) == 1
    assert devices[0]["selected_source"] == "local_mqtt"

    attempts = [
        a for a in harness.network.attempts if (a["host"], a["port"]) == ("192.168.1.60", port)
    ]
    assert attempts, "the configured broker endpoint must actually be probed"
    assert all(a["tls"] == tls for a in attempts)
    if username is not None:
        assert any(
            a["username"] == username and a["password"] == password for a in attempts
        ), "saved discovery credentials must be attempted against the broker"
    assert POOL_PASSWORD not in json.dumps(payload)


# --- failure isolation -------------------------------------------------------


@pytest.mark.parametrize(
    "failing_source", ["local_api", "local_mqtt", "zendure_mqtt"]
)
def test_fresh_install_discovery_survives_single_source_failures(
    harness_factory, failing_source
):
    overrides = {}
    if failing_source == "local_api":
        overrides["mdns_fail"] = True
    elif failing_source == "local_mqtt":
        overrides["mqtt_fail"] = True
    else:
        overrides["fetcher"] = CountingFetcher(failing_device_list)
    harness = three_source_harness(harness_factory, **overrides)

    status, _, payload = run_discovery(harness.base)
    assert status == 200, "a single failing source must never break the run"

    healthy = [s for s in ALL_SOURCES if s != failing_source]
    devices = matching_devices(payload)
    assert len(devices) == 1, "healthy sources' results must survive the failure"
    for source in healthy:
        assert source in devices[0]["sources"]
        assert payload["refresh"]["sources"][source]["ok"] is True

    assert payload["refresh"]["sources"][failing_source]["ok"] is False
    assert payload["refresh"]["status"] == "partial"
    assert any(failing_source in warning for warning in payload["warnings"])
    blob = json.dumps(payload)
    for marker in SECRET_MARKERS:
        assert marker not in blob


def test_fresh_install_discovery_all_sources_failing_is_a_controlled_error(
    harness_factory,
):
    harness = three_source_harness(
        harness_factory,
        mdns_fail=True,
        mqtt_fail=True,
        fetcher=CountingFetcher(failing_device_list),
    )

    status, _, payload = run_discovery(harness.base)
    assert status == 200, "even a fully failing run must not surface as a 500"
    assert payload["refresh"]["status"] == "failed"
    assert all(
        payload["refresh"]["sources"][source]["ok"] is False for source in ALL_SOURCES
    )
    assert len(payload["warnings"]) >= 3
    # Nothing was ever collected, so no stale results may be presented as new.
    assert payload["devices"] == []
    assert not paths.standard_config_path().exists()
    assert not paths.legacy_config_path().exists()


# --- fresh restart -----------------------------------------------------------


def test_fresh_install_discovery_after_admin_restart(harness_factory):
    network = FakeBrokerNetwork()
    network.add(
        "192.168.1.70",
        1883,
        [local_mqtt_device(broker="cellar")],
        username="pool-user",
        password=POOL_PASSWORD,
    )

    first = harness_factory(api_devices=[local_api_device()], network=network)
    save_preparation(
        first.base,
        {"discovery_priority": ["zendure_mqtt", "local_mqtt", "local_api"]},
    )
    status, _, _ = request(
        f"{first.base}/api/discovery/zendure-cloud-mqtt/token",
        method="POST",
        body={"api_key": ZENDURE_API_KEY},
    )
    assert status == 200
    status, _, _ = request(
        f"{first.base}/api/discovery/connections/mqtt-credentials",
        method="POST",
        body={"label": "pool", "username": "pool-user", "password": POOL_PASSWORD},
    )
    assert status == 200
    status, _, _ = request(
        f"{first.base}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={"label": "cellar", "host": "192.168.1.70", "port": 1883},
    )
    assert status == 200

    # Restart: a new Admin server with empty in-memory candidates; only the
    # persisted settings/credentials survive. No token is saved in-process.
    second = harness_factory(
        api_devices=[local_api_device()], network=network, zendure_api_key=None
    )
    assert second.zendure.candidates() == []

    status, _, payload = run_discovery(second.base)
    assert status == 200
    assert payload["priority"] == ["zendure_mqtt", "local_mqtt", "local_api"]
    assert second.zendure.refresh_calls == 1
    assert second.mqtt.refresh_calls == 1

    devices = matching_devices(payload)
    assert len(devices) == 1
    device = devices[0]
    # deviceList counted without a prior manual refresh; the stored broker and
    # credential pool were re-used; priority still selects the cloud source.
    assert device["selected_source"] == "zendure_mqtt"
    assert set(device["sources"]) == set(ALL_SOURCES)
    assert {c["source"] for c in device["candidates"]} == set(ALL_SOURCES)


# --- safety ------------------------------------------------------------------


def test_fresh_install_discovery_never_writes_config(harness_factory):
    harness = three_source_harness(harness_factory)
    save_preparation(
        harness.base,
        {"discovery_priority": ["zendure_mqtt", "local_api", "local_mqtt"]},
    )
    request(
        f"{harness.base}/api/discovery/connections/mqtt-credentials",
        method="POST",
        body={"label": "pool", "username": "pool-user", "password": POOL_PASSWORD},
    )
    status, _, _ = run_discovery(harness.base)
    assert status == 200
    run_discovery(harness.base)

    assert not paths.standard_config_path().exists()
    assert not paths.legacy_config_path().exists()


def test_fresh_install_discovery_never_exposes_secrets(harness_factory):
    network = FakeBrokerNetwork()
    network.add(
        "192.168.1.60",
        1883,
        [local_mqtt_device()],
        username="pool-user",
        password=POOL_PASSWORD,
    )
    harness = three_source_harness(harness_factory, network=network)
    status, _, _ = request(
        f"{harness.base}/api/discovery/connections/mqtt-credentials",
        method="POST",
        body={"label": "pool", "username": "pool-user", "password": POOL_PASSWORD},
    )
    assert status == 200

    status, _, payload = run_discovery(harness.base)
    assert status == 200
    blob = json.dumps(payload)
    for marker in SECRET_MARKERS:
        assert marker not in blob

    status, _, connections = request(f"{harness.base}/api/discovery/connections")
    assert status == 200
    blob = json.dumps(connections)
    for marker in SECRET_MARKERS:
        assert marker not in blob


# --- UI orchestration --------------------------------------------------------


STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def test_fresh_install_run_button_delegates_refresh_to_the_backend():
    """The UI runs only the backend orchestration; no parallel discovery logic.

    ``runUnifiedDiscovery`` keeps the LAN scan (which finishes first by design)
    and then triggers exactly the orchestrated run — it must not re-implement
    the per-source refresh fan-out that the backend already owns.
    """

    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    assert "async function runUnifiedDiscovery" in js
    body = js.split("async function runUnifiedDiscovery", 1)[1].split("\n}", 1)[0]
    assert "refresh: true" in body or "refreshUnifiedDevices(true)" in body, (
        "the fresh-install run must request the backend source refresh"
    )
    for forbidden in (
        "refreshMdns()",
        "refreshMqttBrokers()",
        "refreshZendureCloudDiscovery()",
    ):
        assert forbidden not in body, (
            "per-source refresh orchestration must live in the backend, "
            f"not in runUnifiedDiscovery ({forbidden})"
        )
