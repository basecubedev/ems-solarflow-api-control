# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin connections API: broker CRUD, redaction, migrated Zendure token."""

import base64
import json
import threading

import pytest

from admin.secret_store import ZendureTokenStore
from admin.server import ScanRegistry, create_server
from admin.zendure_cloud_mqtt import ZendureCloudDiscovery
from tests.admin_auth_helpers import authenticate, request

pytestmark = pytest.mark.simulation

VALID_TOKEN = base64.b64encode(b"https://app.zendure.tech.APP-KEY-SECRET").decode("ascii")


@pytest.fixture()
def server(tmp_path, monkeypatch, isolated_install_root):
    # config/secrets and discovery-connections.json land under the isolated root.
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def test_connections_defaults(server):
    status, _, payload = request(f"{server}/api/discovery/connections")
    assert status == 200
    assert payload["discovery_priority"] == ["local_api", "local_mqtt", "zendure_mqtt"]
    assert payload["local_mqtt"]["credential_refs"] == []
    assert payload["local_mqtt"]["credentials"] == []
    assert payload["zendure_mqtt"]["token_saved"] is False


def test_mqtt_credential_pool_crud_stores_and_redacts(server):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-credentials",
        method="POST",
        body={
            "label": "Home Assistant MQTT",
            "username": "mqtt-user",
            "password": "mqtt-secret",
        },
    )
    assert status == 200
    credentials = payload["local_mqtt"]["credentials"]
    assert len(credentials) == 1
    entry = credentials[0]
    assert entry["id"] == "home-assistant-mqtt"
    assert entry["label"] == "Home Assistant MQTT"
    assert entry["username_configured"] is True
    assert entry["password_configured"] is True
    # No raw secret anywhere in the API response.
    blob = json.dumps(payload)
    assert "mqtt-secret" not in blob
    assert "mqtt-user" not in blob

    # A second credential keeps the first; the pool holds multiple entries.
    request(
        f"{server}/api/discovery/connections/mqtt-credentials",
        method="POST",
        body={"label": "Mosquitto", "username": "m-user", "password": "m-pass"},
    )
    status, _, listing = request(
        f"{server}/api/discovery/connections/mqtt-credentials"
    )
    assert status == 200
    assert {c["id"] for c in listing["credentials"]} == {
        "home-assistant-mqtt",
        "mosquitto",
    }
    # The listing never renders password values.
    assert "m-pass" not in json.dumps(listing)

    # Deleting one credential keeps the others.
    status, _, deleted = request(
        f"{server}/api/discovery/connections/mqtt-credentials/home-assistant-mqtt",
        method="DELETE",
    )
    assert status == 200
    assert deleted["removed"] is True
    _, _, after = request(f"{server}/api/discovery/connections")
    assert [c["id"] for c in after["local_mqtt"]["credentials"]] == ["mosquitto"]


def test_mqtt_credential_requires_label(server):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-credentials",
        method="POST",
        body={"username": "u", "password": "p"},
    )
    assert status == 400
    assert "label" in payload["error"]


def test_broker_crud_stores_and_redacts_credentials(server):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={
            "id": "homeassistant",
            "label": "Home Assistant MQTT",
            "host": "192.168.1.20",
            "port": 1883,
            "tls": False,
            "username": "mqtt-user",
            "password": "mqtt-secret",
        },
    )
    assert status == 200
    broker = payload["local_mqtt"]["brokers"][0]
    assert broker["id"] == "homeassistant"
    assert broker["credentials_ref"] == "homeassistant"
    assert broker["username_configured"] is True
    assert broker["password_configured"] is True
    # No raw secret anywhere in the API response.
    blob = json.dumps(payload)
    assert "mqtt-secret" not in blob
    assert "mqtt-user" not in blob

    # A second broker keeps the first; multiple brokers are supported.
    request(
        f"{server}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={"id": "mosquitto", "host": "192.168.1.30", "port": 8883, "tls": True},
    )
    _, _, reloaded = request(f"{server}/api/discovery/connections")
    assert {b["id"] for b in reloaded["local_mqtt"]["brokers"]} == {
        "homeassistant",
        "mosquitto",
    }

    # Deleting a broker detaches its stored secret.
    status, _, deleted = request(
        f"{server}/api/discovery/connections/mqtt-brokers/homeassistant",
        method="DELETE",
    )
    assert status == 200
    assert deleted["removed"] is True
    _, _, after = request(f"{server}/api/discovery/connections")
    assert [b["id"] for b in after["local_mqtt"]["brokers"]] == ["mosquitto"]


def test_broker_saved_tls_anonymous_reports_transport_metadata(server):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={
            "id": "mosq-tls",
            "host": "192.168.1.40",
            "port": 8883,
            "tls": True,
            "tls_mode": "insecure_no_verify",
        },
    )
    assert status == 200
    broker = payload["local_mqtt"]["brokers"][0]
    assert broker["transport"] == "tls"
    assert broker["tls_mode"] == "insecure_no_verify"
    assert broker["auth_mode"] == "anonymous"
    # Anonymous brokers never create a credential secret.
    assert broker["username_configured"] is False
    assert broker["password_configured"] is False
    assert broker["credentials_ref"] is None


def test_broker_saved_with_credentials_reports_auth_mode_without_secret(server):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={
            "id": "mosq-auth",
            "host": "192.168.1.41",
            "port": 8883,
            "tls": True,
            "tls_mode": "system_ca",
            "username": "mqtt-user",
            "password": "mqtt-secret",
        },
    )
    assert status == 200
    broker = payload["local_mqtt"]["brokers"][0]
    assert broker["transport"] == "tls"
    assert broker["auth_mode"] == "username_password"
    assert broker["username_configured"] is True
    blob = json.dumps(payload)
    assert "mqtt-secret" not in blob
    assert "mqtt-user" not in blob


def test_zendure_token_saved_via_connections_is_redacted(server):
    status, _, payload = request(
        f"{server}/api/discovery/zendure-cloud-mqtt/token",
        method="POST",
        body={"token": VALID_TOKEN},
    )
    assert status == 200
    _, _, connections = request(f"{server}/api/discovery/connections")
    assert connections["zendure_mqtt"]["token_saved"] is True
    assert connections["zendure_mqtt"]["token_ref"] == "zendure-cloud"
    assert VALID_TOKEN not in json.dumps(connections)

    # Forgetting the token clears the reference.
    request(f"{server}/api/discovery/zendure-cloud-mqtt/token", method="DELETE")
    _, _, cleared = request(f"{server}/api/discovery/connections")
    assert cleared["zendure_mqtt"]["token_saved"] is False
    assert cleared["zendure_mqtt"]["token_ref"] is None


def test_raw_api_key_saves_without_leaking_secret(server):
    raw_api_key = "PLAIN-ZENDURE-API-KEY-SECRET"
    status, _, payload = request(
        f"{server}/api/discovery/zendure-cloud-mqtt/token",
        method="POST",
        body={"api_key": raw_api_key, "credential_mode": "zendure_api_key"},
    )
    assert status == 200
    assert payload["token_saved"] is True
    assert raw_api_key not in json.dumps(payload)
    _, _, connections = request(f"{server}/api/discovery/connections")
    assert connections["zendure_mqtt"]["token_saved"] is True
    assert raw_api_key not in json.dumps(connections)


def test_ha_token_mode_is_accepted_but_manual_mqtt_mode_is_rejected(server):
    status, _, payload = request(
        f"{server}/api/discovery/zendure-cloud-mqtt/token",
        method="POST",
        body={"api_key": "x", "credential_mode": "ha_device_list_token"},
    )
    assert status == 200
    assert payload["token_saved"] is True

    status, _, payload = request(
        f"{server}/api/discovery/zendure-cloud-mqtt/test",
        method="POST",
        body={"credential_mode": "manual_mqtt_credentials"},
    )
    assert status == 400
    assert payload["error"] == "unsupported_credential_mode"


def test_zendure_test_uses_migrated_legacy_token(tmp_path, monkeypatch, isolated_install_root):
    # A legacy Admin-local token must remain usable after the store moves to
    # config/secrets: the deviceList test resolves the migrated token.
    admin_dir = tmp_path / "admin-data"
    ZendureTokenStore(admin_dir).save_token(VALID_TOKEN)

    from admin.credential_store import CredentialStore

    credential_store = CredentialStore(
        config_dir=tmp_path / "config", legacy_admin_data_dir=admin_dir
    )
    seen = {}

    def _fetch(token, _timeout):
        seen["token"] = token
        return {"devices": [], "mqtt": {"host": "mqtt.invalid", "port": 8883}}

    discovery = ZendureCloudDiscovery(
        credential_store.zendure, device_list_fetcher=_fetch
    )
    result = discovery.test()
    assert result["ok"] is True
    assert seen["token"] == VALID_TOKEN


# --- strict legacy broker-save validation (HTTP 400, no side effects) -------


def _broker_secret_file(install_root, ref):
    return install_root / "config" / "secrets" / f"mqtt-{ref}.json"


def _assert_no_broker_persisted(server):
    _, _, connections = request(f"{server}/api/discovery/connections")
    assert connections["local_mqtt"]["brokers"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"host": "h", "port": "broken"},
        {"host": "h", "port": 0},
        {"host": "h", "port": 70000},
        {"host": "h", "port": True},
        {"host": "h", "tls": "false"},
        {"host": "h", "tls": 0},
        {"host": "h", "tls_mode": "does_not_exist"},
        {"host": "h", "tls": False, "tls_mode": "system_ca"},
        {"host": "h", "username": "only-user"},
        {"host": "h", "password": "only-pass"},
    ],
)
def test_broker_save_rejects_invalid_input_with_400(server, body):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-brokers", method="POST", body=body
    )
    assert status == 400
    assert payload.get("error") == "invalid_broker"
    assert payload.get("message")
    _assert_no_broker_persisted(server)


def test_broker_save_invalid_input_creates_no_credential_secret(
    isolated_install_root, server
):
    # A username/password pair alongside an invalid port must not write the secret
    # before the broker body is validated: no orphan credential file may survive.
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={
            "id": "orphan",
            "host": "h",
            "port": "broken",
            "username": "mqtt-user",
            "password": "mqtt-secret",
        },
    )
    assert status == 400
    assert not _broker_secret_file(isolated_install_root, "orphan").exists()
    _assert_no_broker_persisted(server)


def test_broker_save_string_false_tls_is_rejected_not_coerced(server):
    status, _, payload = request(
        f"{server}/api/discovery/connections/mqtt-brokers",
        method="POST",
        body={"id": "coerce", "host": "h", "port": 1883, "tls": "false"},
    )
    assert status == 400
    _assert_no_broker_persisted(server)
