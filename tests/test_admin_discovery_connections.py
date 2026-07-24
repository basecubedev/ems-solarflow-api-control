# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent discovery connection metadata store + broker credential wiring."""

import json

import pytest

from admin.credential_store import CredentialStore
from admin.discovery_connections import (
    DiscoveryConnectionsStore,
    DiscoveryPreparationConfig,
    default_connections,
    normalize_connections,
    preparation_config,
)
from admin.discovery_preparation import DiscoveryPreparationStore
from admin.mqtt_discovery import MqttBrokerDiscovery

pytestmark = pytest.mark.simulation


def _store(tmp_path):
    return DiscoveryConnectionsStore(path=tmp_path / "discovery-connections.json")


def test_defaults_have_expected_priority(tmp_path):
    loaded = _store(tmp_path).load()
    assert loaded["discovery_priority"] == ["local_api", "local_mqtt", "zendure_mqtt"]
    assert all(loaded["sources"][s]["enabled"] for s in loaded["sources"])
    assert loaded["local_mqtt"]["credential_refs"] == []
    assert loaded["local_mqtt"]["brokers"] == []


def test_credential_refs_persist_and_delete_independently(tmp_path):
    store = _store(tmp_path)
    store.add_credential_ref("home-assistant")
    store.add_credential_ref("mosquitto")
    store.add_credential_ref("home-assistant")  # idempotent
    refs = _store(tmp_path).load()["local_mqtt"]["credential_refs"]
    assert refs == ["home-assistant", "mosquitto"]

    store.remove_credential_ref("home-assistant")
    remaining = _store(tmp_path).load()["local_mqtt"]["credential_refs"]
    assert remaining == ["mosquitto"]


def test_priority_and_enable_flags_persist(tmp_path):
    store = _store(tmp_path)
    store.save(
        {
            "discovery_priority": ["zendure_mqtt", "local_mqtt", "local_api"],
            "sources": {"local_mqtt": {"enabled": False}},
        }
    )
    reloaded = _store(tmp_path).load()
    assert reloaded["discovery_priority"] == ["zendure_mqtt", "local_mqtt", "local_api"]
    assert reloaded["local_mqtt"]["enabled"] is False
    assert reloaded["sources"]["local_mqtt"]["enabled"] is False


def test_multiple_brokers_with_refs_persist(tmp_path):
    store = _store(tmp_path)
    store.upsert_broker(
        {
            "id": "homeassistant",
            "label": "Home Assistant MQTT",
            "host": "192.168.1.20",
            "port": 1883,
            "tls": False,
            "credentials_ref": "homeassistant",
        }
    )
    store.upsert_broker(
        {
            "id": "mosquitto",
            "label": "Mosquitto",
            "host": "192.168.1.30",
            "port": 8883,
            "tls": True,
            "credentials_ref": "mosquitto",
        }
    )
    brokers = _store(tmp_path).load()["local_mqtt"]["brokers"]
    assert [b["id"] for b in brokers] == ["homeassistant", "mosquitto"]
    assert [b["credentials_ref"] for b in brokers] == ["homeassistant", "mosquitto"]
    assert brokers[1]["tls"] is True


def test_upsert_replaces_broker_by_id(tmp_path):
    store = _store(tmp_path)
    store.upsert_broker({"id": "hass", "host": "10.0.0.1", "label": "old"})
    store.upsert_broker({"id": "hass", "host": "10.0.0.2", "label": "new"})
    brokers = store.load()["local_mqtt"]["brokers"]
    assert len(brokers) == 1
    assert brokers[0]["host"] == "10.0.0.2"
    assert brokers[0]["label"] == "new"


def test_scan_ranges_persist(tmp_path):
    store = _store(tmp_path)
    store.save({"local_api": {"scan_ranges": ["192.168.1.0/24"], "manual_hosts": ["1.2.3.4"]}})
    local_api = _store(tmp_path).load()["local_api"]
    assert local_api["scan_ranges"] == ["192.168.1.0/24"]
    assert local_api["manual_hosts"] == ["1.2.3.4"]


def test_secrets_never_written_to_metadata_file(tmp_path):
    store = _store(tmp_path)
    store.upsert_broker(
        {"id": "hass", "host": "10.0.0.1", "credentials_ref": "hass"}
    )
    store.set_zendure_token_ref("zendure-cloud")
    raw = (tmp_path / "discovery-connections.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["zendure_mqtt"]["token_ref"] == "zendure-cloud"
    assert "password" not in raw
    assert "username" not in raw


def test_legacy_preparation_is_migrated(tmp_path):
    legacy = DiscoveryPreparationStore(tmp_path / "admin-data")
    legacy.save(
        {
            "discovery_priority": ["zendure_mqtt", "local_api", "local_mqtt"],
            "sources": {"local_api": {"enabled": False}},
        }
    )
    store = DiscoveryConnectionsStore(
        path=tmp_path / "config" / "discovery-connections.json",
        legacy_preparation_store=legacy,
    )
    loaded = store.load()
    assert loaded["discovery_priority"] == ["zendure_mqtt", "local_api", "local_mqtt"]
    assert loaded["local_api"]["enabled"] is False


def test_preparation_config_dataclass_view(tmp_path):
    store = _store(tmp_path)
    store.upsert_broker({"id": "hass", "host": "10.0.0.1", "credentials_ref": "hass"})
    config = preparation_config(store.load())
    assert isinstance(config, DiscoveryPreparationConfig)
    assert config.priority[0] == "local_api"
    assert config.local_mqtt_brokers[0].host == "10.0.0.1"
    assert config.local_mqtt_brokers[0].credentials_ref == "hass"


def test_normalize_drops_broker_without_host():
    normalized = normalize_connections(
        {"local_mqtt": {"brokers": [{"label": "no host"}, {"host": "1.1.1.1"}]}}
    )
    assert len(normalized["local_mqtt"]["brokers"]) == 1


def test_default_connections_path_honours_install_dir(tmp_path, monkeypatch):
    # In the Admin container ems.paths.BASE_DIR is the read-only /app; the
    # connections file must follow EMS_INSTALL_DIR into the writable mount so a
    # default-constructed store can persist credential refs (regression: setup
    # MQTT credential save failed with "Could not save the discovery connection
    # settings." because this path targeted /app while the secret store did not).
    from admin.discovery_connections import default_connections_path

    monkeypatch.delenv("EMS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    path = default_connections_path()
    assert path == tmp_path / "config" / "discovery-connections.json"

    store = DiscoveryConnectionsStore()
    store.add_credential_ref("setup-cred")
    assert path.exists()
    assert store.load()["local_mqtt"]["credential_refs"] == ["setup-cred"]


def test_default_connections_shape():
    assert set(default_connections()) == {
        "discovery_priority",
        "sources",
        "local_api",
        "local_mqtt",
        "zendure_mqtt",
    }


def _merge_candidate(discovery, host, port):
    discovery.store.merge(
        {"host": host, "port": port, "source": "network_probe", "status": "tcp_open"}
    )


def test_refresh_tries_anonymous_then_every_saved_credential(tmp_path):
    credentials = CredentialStore(config_dir=tmp_path)
    credentials.save_mqtt_discovery_secret("hass", "u-hass", "p-hass", label="Home Assistant")
    credentials.save_mqtt_discovery_secret("mosq", "u-mosq", "p-mosq", label="Mosquitto")

    calls = []

    def topic_discoverer(broker):
        calls.append({"tls": broker.get("tls"), "username": broker.get("username")})
        if broker.get("username") is None:
            raise RuntimeError("auth failed")  # anonymous rejected
        if broker.get("username") == "u-hass":
            return [{"id": "dev-1", "serial_number": "EOD1"}]
        return []  # mosquitto connects but sees no hardware topics

    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=topic_discoverer,
        credential_lookup=credentials.load_mqtt_discovery_secret,
        credential_refs_provider=lambda: ["hass", "mosq"],
    )
    # No stored broker entry: the candidate comes from a scan, credentials come
    # from the pool. Discovery must still try every credential.
    _merge_candidate(discovery, "192.168.1.20", 1883)
    result = discovery.refresh()

    candidate = result["candidates"][0]
    # anonymous first, then both saved credentials.
    assert [a["label"] for a in candidate["attempts"]] == [
        "anonymous",
        "Home Assistant",
        "Mosquitto",
    ]
    # Credential B is attempted even though anonymous failed.
    assert {"tls": False, "username": "u-mosq"} in calls
    # Port 1883 is treated as plain MQTT.
    assert candidate["transport"] == "plaintext"
    assert all(call["tls"] is False for call in calls)
    # Statuses reflect each attempt outcome and are redacted (no secrets).
    statuses = {a["label"]: a["status"] for a in candidate["attempts"]}
    assert statuses["anonymous"] == "connection_failed"
    assert statuses["Home Assistant"] == "topics_seen"
    assert statuses["Mosquitto"] == "mqtt_listened_no_topics"
    blob = json.dumps(result)
    for secret in ("u-hass", "p-hass", "u-mosq", "p-mosq"):
        assert secret not in blob


def test_refresh_infers_tls_transport_for_port_8883(tmp_path):
    credentials = CredentialStore(config_dir=tmp_path)
    credentials.save_mqtt_discovery_secret("hass", "u", "p", label="HA")
    calls = []

    def topic_discoverer(broker):
        calls.append(bool(broker.get("tls")))
        return []

    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=topic_discoverer,
        credential_lookup=credentials.load_mqtt_discovery_secret,
        credential_refs_provider=lambda: ["hass"],
    )
    _merge_candidate(discovery, "192.168.1.30", 8883)
    candidate = discovery.refresh()["candidates"][0]
    assert candidate["transport"] == "tls"
    assert calls and all(calls)  # every attempt used TLS


def test_refresh_dedupes_devices_across_credential_attempts(tmp_path):
    credentials = CredentialStore(config_dir=tmp_path)
    credentials.save_mqtt_discovery_secret("hass", "u", "p", label="HA")

    def topic_discoverer(broker):
        # Both anonymous and the credential see the same physical device.
        return [{"id": "mqtt-device:local_mqtt:mqtt:x:fam:EOD1", "serial_number": "EOD1"}]

    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=topic_discoverer,
        credential_lookup=credentials.load_mqtt_discovery_secret,
        credential_refs_provider=lambda: ["hass"],
    )
    _merge_candidate(discovery, "192.168.1.40", 1883)
    candidate = discovery.refresh()["candidates"][0]
    assert len(candidate["devices"]) == 1


def test_refresh_anonymous_only_when_no_credentials(tmp_path):
    seen = []

    def topic_discoverer(broker):
        seen.append(broker.get("username"))
        return []

    discovery = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: True,
        topic_discoverer=topic_discoverer,
        credential_refs_provider=lambda: [],
    )
    _merge_candidate(discovery, "1.1.1.1", 1883)
    candidate = discovery.refresh()["candidates"][0]
    assert seen == [None]  # anonymous only
    assert [a["label"] for a in candidate["attempts"]] == ["anonymous"]
