# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure cloud credential/deviceList tests (no real credentials or network)."""

import base64

import pytest
import requests

from admin.zendure_cloud_auth import (
    MAX_TIMEOUT_S,
    _SIGN_KEY,
    ZendureCloudError,
    fetch_device_list,
    normalize_app_key,
    parse_device_list_response,
    resolve_device_list_credential,
    sign_request,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]

API_URL = "https://app.zendure.tech/eu"
APP_KEY = "app-key-secret-123"


def _ha_token(api_url=API_URL, app_key=APP_KEY):
    return base64.b64encode(f"{api_url}.{app_key}".encode()).decode()


def _device_list_payload():
    return {
        "success": True,
        "code": 200,
        "data": {
            "deviceList": [
                {
                    "productKey": "PK-AAA",
                    "deviceKey": "DK-BBB",
                    "productModel": "SolarFlow 800",
                    "snNumber": "SN-EOD123",
                    "deviceName": "Balcony battery",
                }
            ],
            "mqtt": {
                "url": "mqtt.example.invalid:8883",
                "username": "mqtt-user",
                "password": "mqtt-secret",
                "clientId": "client-xyz",
            },
        },
    }


def test_normalize_app_key_strips_whitespace():
    assert normalize_app_key("  app-key-secret-123  ") == "app-key-secret-123"


def test_normalize_app_key_rejects_empty():
    for bad in ("", "   ", None, 123):
        with pytest.raises(ZendureCloudError) as exc:
            normalize_app_key(bad)
        assert "invalid" in str(exc.value).lower()


def test_resolve_raw_app_key_uses_eu_default():
    assert resolve_device_list_credential(APP_KEY) == (API_URL, APP_KEY)


def test_resolve_ha_token_uses_embedded_api_url_and_decoded_app_key():
    assert resolve_device_list_credential(_ha_token()) == (API_URL, APP_KEY)


def test_resolve_ha_token_accepts_global_zendure_base():
    api_url = "https://app.zendure.tech/v2"
    assert resolve_device_list_credential(_ha_token(api_url)) == (api_url, APP_KEY)


def test_resolve_ha_token_rejects_untrusted_embedded_url_without_leaking_it():
    token = _ha_token("https://attacker.example.invalid", "secret-in-token")
    with pytest.raises(ZendureCloudError) as exc:
        resolve_device_list_credential(token)
    message = str(exc.value)
    assert "invalid" in message.lower()
    assert token not in message
    assert "attacker" not in message
    assert "secret-in-token" not in message


def test_sign_request_is_stable_for_fixed_inputs():
    signature = sign_request(APP_KEY, 1700000000000, "abcd1234")
    assert signature == sign_request(APP_KEY, 1700000000000, "abcd1234")
    assert signature == signature.upper()
    assert len(signature) == 40  # uppercase SHA1 hex
    # Signature is not the bare content and does not embed the secret verbatim.
    assert APP_KEY not in signature
    assert _SIGN_KEY not in signature


def test_device_list_parses_mqtt_and_devices():
    parsed = parse_device_list_response(
        _device_list_payload(), api_url=API_URL, app_key=APP_KEY
    )
    assert parsed["mqtt"]["host"] == "mqtt.example.invalid"
    assert parsed["mqtt"]["port"] == 8883
    assert parsed["mqtt"]["username"] == "mqtt-user"
    assert parsed["mqtt"]["password"] == "mqtt-secret"
    assert parsed["mqtt"]["client_id"] == "client-xyz"
    assert parsed["devices"][0]["snNumber"] == "SN-EOD123"
    assert parsed["api_url"] == API_URL
    assert parsed["app_key"] == APP_KEY


def test_device_list_defaults_port_only_when_api_omits_it():
    payload = _device_list_payload()
    payload["data"]["mqtt"]["url"] = "ssl://mqtt.example.invalid"
    parsed = parse_device_list_response(payload)
    assert parsed["mqtt"]["host"] == "mqtt.example.invalid"
    assert parsed["mqtt"]["port"] == 1883
    assert parsed["mqtt"]["port_from_api"] is False


def test_device_list_rejects_missing_mqtt_block():
    payload = _device_list_payload()
    del payload["data"]["mqtt"]
    with pytest.raises(ZendureCloudError):
        parse_device_list_response(payload)


def test_device_list_rejects_empty_device_list():
    payload = _device_list_payload()
    payload["data"]["deviceList"] = []
    with pytest.raises(ZendureCloudError):
        parse_device_list_response(payload)


def test_device_list_rejects_unsuccessful_response():
    payload = _device_list_payload()
    payload["success"] = False
    with pytest.raises(ZendureCloudError):
        parse_device_list_response(payload)


def test_fetch_uses_signed_headers_and_parses(monkeypatch):
    captured = {}

    def fake_post(url, headers, json_body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json_body
        return _device_list_payload()

    parsed = fetch_device_list(API_URL, APP_KEY, 5.0, post=fake_post)
    assert captured["url"] == f"{API_URL}/api/ha/deviceList"
    assert captured["headers"]["clientid"] == "zenHa"
    assert captured["headers"]["sign"] == sign_request(
        APP_KEY, captured["headers"]["timestamp"], captured["headers"]["nonce"]
    )
    assert captured["body"] == {"appKey": APP_KEY}
    assert parsed["mqtt"]["host"] == "mqtt.example.invalid"


def test_fetch_sends_seconds_timestamp_and_five_digit_nonce():
    # Regression: the Zendure API rejects a milliseconds timestamp (code 1004) and any
    # nonce that is not a 5-digit integer (code 1007). The signed headers must use
    # a seconds epoch and a 10000-99999 nonce.
    captured = {}

    def fake_post(url, headers, json_body, timeout):
        captured["headers"] = headers
        return _device_list_payload()

    fetch_device_list(API_URL, APP_KEY, 5.0, post=fake_post)
    timestamp = captured["headers"]["timestamp"]
    nonce = captured["headers"]["nonce"]
    assert timestamp.isdigit() and len(timestamp) == 10  # seconds, not 13-digit ms
    assert nonce.isdigit() and len(nonce) == 5
    assert 10000 <= int(nonce) <= 99999


def test_generated_nonce_is_always_a_5_digit_integer():
    from admin.zendure_cloud_auth import _generate_nonce

    for _ in range(1000):
        nonce = _generate_nonce()
        assert nonce.isdigit() and len(nonce) == 5
        assert 10000 <= int(nonce) <= 99999


def test_fetch_normalizes_transport_error_to_safe_message():
    def boom(*_args, **_kwargs):
        raise RuntimeError(f"connection to {API_URL} failed with token {APP_KEY}")

    with pytest.raises(ZendureCloudError) as exc:
        fetch_device_list(API_URL, APP_KEY, 5.0, post=boom)
    message = str(exc.value)
    assert APP_KEY not in message
    assert API_URL not in message


def test_errors_never_leak_secrets():
    payload = _device_list_payload()
    payload["data"]["deviceList"] = []
    try:
        parse_device_list_response(payload, api_url=API_URL, app_key=APP_KEY)
    except ZendureCloudError as exc:
        message = str(exc)
        for secret in (APP_KEY, "mqtt-secret", "DK-BBB", "SN-EOD123", "PK-AAA"):
            assert secret not in message


def test_fetch_allows_25s_and_clamps_only_near_30s():
    captured = {}

    def fake_post(url, headers, json_body, timeout):
        captured["timeout"] = timeout
        return _device_list_payload()

    fetch_device_list(API_URL, APP_KEY, 25.0, post=fake_post)
    assert captured["timeout"] == pytest.approx(25.0)

    fetch_device_list(API_URL, APP_KEY, 999.0, post=fake_post)
    assert captured["timeout"] == pytest.approx(MAX_TIMEOUT_S)
    assert MAX_TIMEOUT_S == pytest.approx(30.0)


def test_fetch_timeout_raises_specific_redaction_safe_error():
    def slow(*_args, **_kwargs):
        raise requests.exceptions.ReadTimeout("read timed out")

    with pytest.raises(ZendureCloudError) as exc:
        fetch_device_list(API_URL, APP_KEY, 25.0, post=slow)
    message = str(exc.value)
    assert "timed out" in message.lower()
    assert "25" in message
    assert APP_KEY not in message
