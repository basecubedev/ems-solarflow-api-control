# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure cloud MQTT discovery tests (fake deviceList + fake listeners)."""

import base64
import json
import ssl

import pytest

from admin.secret_store import ZendureTokenStore
from admin.zendure_cloud_auth import DEFAULT_ZENDURE_API_BASE_URL, ZendureCloudError
from admin.zendure_cloud_mqtt import (
    CREDENTIAL_MODE_API_KEY,
    CREDENTIAL_MODE_HA_TOKEN,
    DEFAULT_TLS_MQTT_PORT,
    FakeCloudMqttListener,
    PahoTlsMqttListener,
    TLS_ENCRYPTED_NO_VERIFY,
    TLS_SYSTEM_CA,
    ZendureCloudDiscovery,
    _effective_tls_port,
    credential_mode_is_supported,
)

pytestmark = pytest.mark.simulation

APP_KEY = "app-key-secret"


def _device_list_result():
    return {
        "devices": [
            {
                "productKey": "PK-AAA",
                "deviceKey": "DK-BBB",
                "productModel": "SolarFlow 800",
                "snNumber": "SN-EOD123",
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
        "app_key": APP_KEY,
    }


def _fetcher(result=None, error=None):
    def _fetch(_token, _timeout):
        if error is not None:
            raise error
        return result if result is not None else _device_list_result()

    return _fetch


def _discovery(tmp_path, *, messages=(), fail=False, fetcher=None, tls_mode=TLS_SYSTEM_CA):
    store = ZendureTokenStore(data_dir=tmp_path)
    store.save_token("saved-token")
    listeners = []

    def factory(connection):
        listener = FakeCloudMqttListener(connection, messages, fail=fail)
        listeners.append(listener)
        return listener

    discovery = ZendureCloudDiscovery(
        store,
        device_list_fetcher=fetcher or _fetcher(),
        listener_factory=factory,
        timeout_s=0.0,
        tls_mode=tls_mode,
    )
    return discovery, listeners


def test_refresh_without_token_returns_not_configured(tmp_path):
    discovery = ZendureCloudDiscovery(
        ZendureTokenStore(data_dir=tmp_path),
        device_list_fetcher=_fetcher(),
        listener_factory=lambda c: FakeCloudMqttListener(c),
    )
    result = discovery.refresh()
    assert result["ok"] is False
    assert result["error"] == "not_configured"


def test_device_list_only_candidates_are_created(tmp_path):
    discovery, _ = _discovery(tmp_path)
    result = discovery.refresh()
    assert result["ok"] is True
    assert result["device_list_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["source_type"] == "zendure_cloud_mqtt"
    assert candidate["discovery_status"] == "device_list_only"
    assert candidate["serial_number"] == "SN-EOD123"
    assert candidate["model_hint"] == "SolarFlow 800"
    assert candidate["confidence"] == pytest.approx(0.5)


def test_device_list_only_trusted_candidate_uses_device_key(tmp_path):
    discovery, _ = _discovery(tmp_path)
    result = discovery.refresh()

    assert result["mqtt_observed_count"] == 0
    public = discovery.candidates()[0]
    trusted = discovery.trusted_candidates()[0]
    assert public["device_id"] == "SN-EOD123"
    assert "DK-BBB" not in json.dumps(public)
    assert trusted["serial_number"] == "SN-EOD123"
    assert trusted["device_id"] == "DK-BBB"


def test_mqtt_observed_topics_enrich_matching_candidate(tmp_path):
    messages = [
        ("iot/PK-AAA/DK-BBB/properties/report", json.dumps({"properties": {"electricLevel": 55}})),
        ("SN-EOD123/sensor/SN-EOD123/outputHomePower", None),
    ]
    discovery, _ = _discovery(tmp_path, messages=messages)
    result = discovery.refresh()
    assert result["mqtt_observed_count"] >= 1
    observed = [c for c in result["candidates"] if c["discovery_status"] == "mqtt_observed"]
    assert observed
    enriched = observed[0]
    assert "electricLevel" in enriched["metrics_seen"] or (
        "outputHomePower" in enriched["metrics_seen"]
    )
    assert enriched["confidence"] > 0.5


def test_cloud_listener_receives_credentials(tmp_path):
    discovery, listeners = _discovery(tmp_path)
    discovery.refresh()
    assert listeners
    connection = listeners[0].connection
    assert connection.username == "mqtt-user"
    assert connection.password == "mqtt-secret"
    assert connection.client_id == "client-xyz"
    assert connection.host == "mqtt.example.invalid"
    assert connection.port == 8883


def test_cloud_listener_applies_tls_and_never_publishes(tmp_path):
    discovery, listeners = _discovery(tmp_path)
    discovery.refresh()
    listener = listeners[0]
    assert listener.connection.tls_enabled is True
    assert listener.tls_configured is True
    assert listener.published == []


def test_subscriptions_are_bounded_and_per_device(tmp_path):
    discovery, listeners = _discovery(tmp_path)
    discovery.refresh()
    subs = listeners[0].connection.subscriptions
    assert "#" not in subs
    assert "/PK-AAA/DK-BBB/#" in subs
    assert "iot/PK-AAA/DK-BBB/#" in subs
    assert f"{APP_KEY}/#" in subs


def test_tls_failure_returns_safe_status_without_crashing(tmp_path):
    discovery, _ = _discovery(tmp_path, fail=True)
    result = discovery.refresh()
    # deviceList candidates are still returned; the MQTT failure is a status.
    assert result["ok"] is True
    assert result["mqtt_status"] == "error"
    assert "TLS" in result["mqtt_message"]
    assert result["device_list_count"] == 1


def test_no_plaintext_fallback_for_cloud(tmp_path):
    discovery, listeners = _discovery(tmp_path, fail=True)
    discovery.refresh()
    # Exactly one listener is created and it was TLS-enabled; there is no retry
    # with a plaintext connection.
    assert len(listeners) == 1
    assert listeners[0].connection.tls_enabled is True


def test_device_list_failure_returns_error_status(tmp_path):
    from admin.zendure_cloud_auth import ZendureCloudError

    discovery, _ = _discovery(
        tmp_path, fetcher=_fetcher(error=ZendureCloudError("Zendure token is invalid or expired."))
    )
    result = discovery.refresh()
    assert result["ok"] is False
    assert result["error"] == "device_list_failed"


def test_test_token_reports_device_count_without_mqtt(tmp_path):
    discovery, listeners = _discovery(tmp_path)
    result = discovery.test()
    assert result["ok"] is True
    assert result["devices_found"] == 1
    assert result["tls_required"] is True
    assert result["broker"] == "mqtt.example.invalid:8883"
    # test() never connects to MQTT.
    assert listeners == []


def test_tls_connects_on_8883_when_api_omits_port(tmp_path):
    # The real Zendure deviceList returns the broker without a port; the raw parse
    # then defaults to plaintext 1883. TLS discovery must connect on 8883 instead.
    result = _device_list_result()
    result["mqtt"]["port"] = 1883
    result["mqtt"]["port_from_api"] = False
    discovery, listeners = _discovery(tmp_path, fetcher=_fetcher(result=result))
    payload = discovery.refresh()
    assert listeners[0].connection.port == 8883
    assert payload["broker"].endswith(":8883")
    candidate = payload["candidates"][0]
    assert candidate["broker_port"] == 8883
    assert candidate["broker_label"].endswith(":8883")


def test_api_supplied_port_is_honoured(tmp_path):
    result = _device_list_result()
    result["mqtt"]["port"] = 9001
    result["mqtt"]["port_from_api"] = True
    discovery, listeners = _discovery(tmp_path, fetcher=_fetcher(result=result))
    payload = discovery.refresh()
    assert listeners[0].connection.port == 9001
    candidate = payload["candidates"][0]
    assert candidate["broker_port"] == 9001
    assert candidate["broker_label"].endswith(":9001")


def test_mqtt_failure_does_not_persist_error_status_when_devices_found(tmp_path):
    # A failed live-MQTT listen must not mark the whole discovery as errored: the
    # deviceList devices were found, so the persisted status stays "ok" and only
    # the transient payload sub-status reflects the MQTT trouble.
    discovery, _ = _discovery(tmp_path, fail=True)
    payload = discovery.refresh()
    assert payload["mqtt_status"] == "error"
    settings = discovery.settings()
    assert settings["last_status"] == "ok"
    assert settings["last_error"] is None


def test_candidates_never_expose_raw_product_or_device_keys(tmp_path):
    messages = [("iot/PK-AAA/DK-BBB/properties/report", json.dumps({"sn": "SN-EOD123"}))]
    discovery, _ = _discovery(tmp_path, messages=messages)
    result = discovery.refresh()
    blob = json.dumps(result["candidates"])
    assert "PK-AAA" not in blob
    assert "DK-BBB" not in blob
    # Serial is allowed in the local Admin browser.
    assert "SN-EOD123" in blob


def test_trusted_candidate_uses_observed_route_id_while_public_stays_redacted(tmp_path):
    messages = [
        (
            "/PK-AAA/DK-BBB/properties/report",
            json.dumps({"sn": "SN-EOD123", "properties": {"electricLevel": 55}}),
        )
    ]
    discovery, _ = _discovery(tmp_path, messages=messages)
    discovery.refresh()

    public = discovery.candidates()[0]
    trusted = discovery.trusted_candidates()[0]
    assert public["serial_number"] == "SN-EOD123"
    assert public["device_id"] == "SN-EOD123"
    assert "DK-BBB" not in json.dumps(public)
    assert trusted["serial_number"] == "SN-EOD123"
    assert trusted["device_id"] == "DK-BBB"


def test_paho_listener_configures_tls_before_connect():
    calls = []

    class FakeClient:
        def tls_set(self, *args, **kwargs):
            calls.append("tls_set")

        def tls_insecure_set(self, *args, **kwargs):
            calls.append("tls_insecure_set")

        def username_pw_set(self, *args, **kwargs):
            calls.append(("username_pw_set", args))

        def connect(self, *args, **kwargs):
            calls.append("connect")

        def loop_start(self):
            calls.append("loop_start")

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

        def subscribe(self, *args, **kwargs):
            pass

    from admin.zendure_cloud_mqtt import CloudMqttConnection

    connection = CloudMqttConnection(
        host="mqtt.example.invalid",
        port=8883,
        username="u",
        password="p",
        client_id="c",
        subscriptions=("iot/PK/DK/#",),
        tls_enabled=True,
        tls_mode=TLS_SYSTEM_CA,
    )
    listener = PahoTlsMqttListener(connection, client_factory=lambda _c: FakeClient())
    listener.listen(0.0, lambda *_: None)
    assert "tls_set" in calls
    assert calls.index("tls_set") < calls.index("connect")
    assert any(
        isinstance(c, tuple) and c[0] == "username_pw_set" for c in calls
    )


def test_preflight_unreachable_broker_fails_fast(monkeypatch):
    import socket
    import time as _time

    from admin.zendure_cloud_mqtt import (
        CONNECT_TIMEOUT_S,
        CloudMqttConnection,
        CloudMqttError,
    )

    # Real (non-injected) factory path so the preflight runs; a socket that
    # always times out must raise quickly, within the total connect budget.
    class _SlowSocket:
        def __init__(self, *a, **k):
            pass

        def settimeout(self, _t):
            pass

        def connect(self, _addr):
            raise TimeoutError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _SlowSocket)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.1", 8883)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.2", 8883)),
        ],
    )
    connection = CloudMqttConnection(
        host="broker.invalid", port=8883, tls_enabled=True, tls_mode=TLS_SYSTEM_CA
    )
    listener = PahoTlsMqttListener(connection)  # no client_factory -> preflight runs
    started = _time.monotonic()
    with pytest.raises(CloudMqttError):
        listener.listen(8.0, lambda *_: None)
    assert _time.monotonic() - started < CONNECT_TIMEOUT_S + 1.0


def test_paho_listener_encrypted_no_verify_disables_verification():
    modes = []

    class FakeClient:
        def tls_set(self, *args, **kwargs):
            modes.append(("tls_set", kwargs.get("cert_reqs")))

        def tls_insecure_set(self, insecure):
            modes.append(("tls_insecure_set", insecure))

        def username_pw_set(self, *a, **k):
            pass

        def connect(self, *a, **k):
            pass

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

        def subscribe(self, *a, **k):
            pass

    from admin.zendure_cloud_mqtt import CloudMqttConnection

    connection = CloudMqttConnection(
        host="h", port=8883, tls_enabled=True, tls_mode=TLS_ENCRYPTED_NO_VERIFY
    )
    listener = PahoTlsMqttListener(connection, client_factory=lambda _c: FakeClient())
    listener.listen(0.0, lambda *_: None)
    assert ("tls_insecure_set", True) in modes
    assert ("tls_set", ssl.CERT_NONE) in modes


# --- API key / HA-token credential flow ----------------------------------


def _fresh_discovery(tmp_path, *, saved_key=None, fetcher=None):
    store = ZendureTokenStore(data_dir=tmp_path)
    if saved_key:
        store.save_token(saved_key)
    return ZendureCloudDiscovery(
        store,
        device_list_fetcher=fetcher or _fetcher(),
        listener_factory=lambda c: FakeCloudMqttListener(c),
    )


def test_credential_mode_is_supported_accepts_api_key_ha_token_or_empty():
    assert credential_mode_is_supported(None) is True
    assert credential_mode_is_supported("") is True
    assert credential_mode_is_supported(CREDENTIAL_MODE_API_KEY) is True
    assert credential_mode_is_supported(CREDENTIAL_MODE_HA_TOKEN) is True
    assert credential_mode_is_supported("manual_mqtt_credentials") is False


def test_save_raw_api_key_persists_it_verbatim(tmp_path):
    discovery = _fresh_discovery(tmp_path)
    result = discovery.save_token("  raw-api-key  ")
    assert result["ok"] is True
    assert result["token_saved"] is True
    # Stored trimmed and unchanged (never base64-decoded).
    assert discovery.store.load_token() == "raw-api-key"


def test_save_ha_token_preserves_embedded_region_for_later_resolution(tmp_path):
    token = base64.b64encode(
        b"https://app.zendure.tech/v2.embedded-app-key"
    ).decode()
    discovery = _fresh_discovery(tmp_path)
    result = discovery.save_token(f"  {token}  ")
    assert result["ok"] is True
    assert discovery.store.load_token() == token


def test_save_rejects_empty_api_key(tmp_path):
    discovery = _fresh_discovery(tmp_path)
    with pytest.raises(ZendureCloudError):
        discovery.save_token("   ")
    assert discovery.store.load_token() is None


def test_test_uses_fixed_eu_base_and_raw_key(tmp_path):
    captured = {}

    def fetcher(api_key, timeout):
        captured["api_key"] = api_key
        captured["timeout"] = timeout
        return _device_list_result()

    discovery = _fresh_discovery(tmp_path, saved_key="raw-api-key", fetcher=fetcher)
    result = discovery.test()
    assert result["ok"] is True
    # The resolver receives the raw key; the fixed EU base is applied downstream.
    assert captured["api_key"] == "raw-api-key"
    assert DEFAULT_ZENDURE_API_BASE_URL == "https://app.zendure.tech/eu"


def test_refresh_uses_raw_key_resolver(tmp_path):
    captured = {}

    def fetcher(api_key, timeout):
        captured["api_key"] = api_key
        return _device_list_result()

    discovery = _fresh_discovery(tmp_path, saved_key="raw-api-key", fetcher=fetcher)
    result = discovery.refresh()
    assert result["ok"] is True
    assert captured["api_key"] == "raw-api-key"


def test_ensure_seeds_device_list_candidates_when_empty(tmp_path):
    discovery = _fresh_discovery(tmp_path, saved_key="raw-api-key")
    assert discovery.candidates() == []
    count = discovery.ensure_device_list_candidates()
    assert count == 1
    assert discovery.candidates()[0]["serial_number"] == "SN-EOD123"
    assert discovery.candidates()[0]["discovery_status"] == "device_list_only"


def test_ensure_is_noop_when_candidates_present(tmp_path):
    calls = []

    def fetcher(api_key, _timeout):
        calls.append(api_key)
        return _device_list_result()

    discovery = _fresh_discovery(tmp_path, saved_key="raw-api-key", fetcher=fetcher)
    discovery.refresh()
    calls.clear()
    assert discovery.ensure_device_list_candidates() == len(discovery.candidates())
    assert calls == []  # already cached -> no extra deviceList call


def test_ensure_cooldown_prevents_repeated_fetch_on_failure(tmp_path):
    calls = []

    def failing(api_key, _timeout):
        calls.append(api_key)
        raise ZendureCloudError("boom")

    discovery = _fresh_discovery(tmp_path, saved_key="raw-api-key", fetcher=failing)
    assert discovery.ensure_device_list_candidates() == 0
    assert discovery.ensure_device_list_candidates() == 0
    assert len(calls) == 1  # second call is within the cooldown window


def test_ensure_without_saved_key_is_noop(tmp_path):
    discovery = _fresh_discovery(tmp_path)
    assert discovery.ensure_device_list_candidates() == 0
    assert discovery.candidates() == []


def test_default_fetcher_targets_fixed_eu_device_list(monkeypatch):
    from admin import zendure_cloud_mqtt as mod

    captured = {}

    def fake_fetch(api_url, app_key, timeout):
        captured["api_url"] = api_url
        captured["app_key"] = app_key
        return _device_list_result()

    monkeypatch.setattr(mod, "fetch_device_list", fake_fetch)
    mod._default_device_list_fetcher("raw-api-key", 25.0)
    assert captured["api_url"] == "https://app.zendure.tech/eu"
    assert captured["app_key"] == "raw-api-key"


def test_default_fetcher_decodes_ha_token_and_uses_embedded_region(monkeypatch):
    from admin import zendure_cloud_mqtt as mod

    captured = {}

    def fake_fetch(api_url, app_key, timeout):
        captured.update(api_url=api_url, app_key=app_key, timeout=timeout)
        return _device_list_result()

    token = base64.b64encode(
        b"https://app.zendure.tech/eu.embedded-app-key"
    ).decode()
    monkeypatch.setattr(mod, "fetch_device_list", fake_fetch)
    mod._default_device_list_fetcher(token, 25.0)
    assert captured == {
        "api_url": "https://app.zendure.tech/eu",
        "app_key": "embedded-app-key",
        "timeout": 25.0,
    }


# --- TLS transport policy (Issue 3) --------------------------------------


def test_effective_tls_port_defaults_to_8883_when_port_omitted():
    assert _effective_tls_port({"port": 1883, "port_from_api": False}) == DEFAULT_TLS_MQTT_PORT
    assert _effective_tls_port({}) == DEFAULT_TLS_MQTT_PORT


def test_effective_tls_port_upgrades_plaintext_1883_to_tls():
    # No silent downgrade: an API-supplied plaintext 1883 resolves to the TLS
    # listener (8883), never a TLS handshake against the plaintext port.
    assert _effective_tls_port({"port": 1883, "port_from_api": True}) == DEFAULT_TLS_MQTT_PORT


def test_effective_tls_port_honours_explicit_non_plaintext_port():
    assert _effective_tls_port({"port": 9001, "port_from_api": True}) == 9001


def test_refresh_never_connects_plaintext_when_api_reports_1883(tmp_path):
    result = _device_list_result()
    result["mqtt"]["port"] = 1883
    result["mqtt"]["port_from_api"] = True
    discovery, listeners = _discovery(tmp_path, fetcher=_fetcher(result=result))
    payload = discovery.refresh()
    assert listeners[0].connection.port == DEFAULT_TLS_MQTT_PORT
    assert listeners[0].connection.tls_enabled is True
    assert payload["broker"].endswith(":8883")
    candidate = payload["candidates"][0]
    assert candidate["broker_port"] == DEFAULT_TLS_MQTT_PORT
    assert candidate["broker_label"].endswith(":8883")
