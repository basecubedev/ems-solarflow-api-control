# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure cloud auth and deviceList helpers (Admin only).

Resolves either a raw Zendure API key or Zendure's base64 HA/deviceList token,
signs and sends the ``deviceList`` request, and parses the returned device +
MQTT connection info. Discovery-only: this never publishes, never issues
properties/read|write, and never writes the EMS config.

Every raised error carries a short, redaction-safe message. Secrets (the raw
API key, MQTT password/username, product key, device key, serial number) are
never placed in error text or logs.
"""

import base64
import hashlib
import secrets
import time

import requests

# Fixed values the Zendure cloud API requires for the deviceList request. The
# signing key seeds the SHA1 signature; the endpoint path and client id are the
# server's own contract (it rejects other client ids with ``code 1002``).
_SIGN_KEY = "C*dafwArEOXK"
_CLIENT_ID = "zenHa"
_DEVICE_LIST_PATH = "/api/ha/deviceList"

# Zendure cloud API base for the EU region. ``/api/ha/deviceList`` is just the
# cloud route name and does not imply a Home Assistant install.
DEFAULT_ZENDURE_API_BASE_URL = "https://app.zendure.tech/eu"
GLOBAL_ZENDURE_API_BASE_URL = "https://app.zendure.tech/v2"

# HA/deviceList tokens embed the API base beside the appKey. Restrict decoded
# URLs to Zendure's known cloud bases: accepting an arbitrary token-provided URL
# would turn the authenticated Admin endpoint into an SSRF primitive.
_ALLOWED_ZENDURE_API_BASE_URLS = frozenset(
    {
        "https://app.zendure.tech",
        DEFAULT_ZENDURE_API_BASE_URL,
        GLOBAL_ZENDURE_API_BASE_URL,
    }
)

# The live deviceList endpoint can take ~14-15s to respond, so the effective
# timeout is generous and the clamp only guards against runaway values.
DEFAULT_TIMEOUT_S = 25.0
MAX_TIMEOUT_S = 30.0
DEFAULT_MQTT_PORT = 1883

_ERR_INVALID_KEY = "Zendure API key or HA token is invalid or expired."
_ERR_DEVICE_LIST_FAILED = "Zendure device list request failed."
_ERR_DEVICE_LIST_TIMEOUT = (
    "Zendure device list request timed out after {seconds}s. "
    "Zendure cloud may be slow; please retry."
)
_ERR_NO_DEVICES = "Zendure returned no devices for this API key or HA token."
_ERR_NO_MQTT = "Zendure did not return MQTT connection details."

# Back-compat alias: older call sites referenced the token wording.
_ERR_INVALID_TOKEN = _ERR_INVALID_KEY


class ZendureCloudError(Exception):
    """A cloud auth/deviceList failure whose message is always safe to show."""


def normalize_app_key(api_key):
    """Return a clean Zendure API key (the ``appKey``) or raise if empty."""

    if not isinstance(api_key, str) or not api_key.strip():
        raise ZendureCloudError(_ERR_INVALID_KEY)
    return api_key.strip()


def resolve_device_list_credential(credential):
    """Return ``(api_url, app_key)`` for a raw key or HA/deviceList token.

    Zendure's HA integration exposes a base64 value whose decoded form is
    ``<api_url>.<app_key>``. A raw appKey contains no endpoint, so it continues
    to use the EU default. The original credential is never returned to the UI
    or included in an exception.
    """

    value = normalize_app_key(credential)
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return DEFAULT_ZENDURE_API_BASE_URL, value

    if "." not in decoded:
        return DEFAULT_ZENDURE_API_BASE_URL, value
    api_url, app_key = decoded.rsplit(".", 1)
    api_url = api_url.strip().rstrip("/")
    app_key = app_key.strip()
    # A base64-looking raw key is still a raw key unless its decoded value has
    # the actual token shape. Once it claims to contain a URL, fail closed on
    # unknown hosts/paths rather than issuing a request to that location.
    if not api_url.lower().startswith(("http://", "https://")):
        return DEFAULT_ZENDURE_API_BASE_URL, value
    if api_url not in _ALLOWED_ZENDURE_API_BASE_URLS or not app_key:
        raise ZendureCloudError(_ERR_INVALID_KEY)
    return api_url, app_key


def sign_request(app_key, timestamp, nonce):
    """Return the uppercase SHA1 request signature.

    The signed content wraps ``appKey``/``nonce``/``timestamp`` (each label
    immediately followed by its value, in sorted key order) between the signing
    key on both sides.
    """

    fields = {"appKey": str(app_key), "nonce": str(nonce), "timestamp": str(timestamp)}
    joined = "".join(f"{key}{fields[key]}" for key in sorted(fields))
    content = f"{_SIGN_KEY}{joined}{_SIGN_KEY}"
    return hashlib.sha1(content.encode("utf-8")).hexdigest().upper()


def build_device_list_headers(app_key, timestamp, nonce):
    return {
        "Content-Type": "application/json",
        "timestamp": str(timestamp),
        "nonce": str(nonce),
        "clientid": _CLIENT_ID,
        "sign": sign_request(app_key, timestamp, nonce),
    }


def _current_timestamp():
    """Return the request timestamp in whole seconds.

    The Zendure API validates the ``timestamp`` header as a seconds-epoch value
    and rejects milliseconds with ``code 1004 timestamp format is incorrect``.
    """

    return int(time.time())


def _generate_nonce():
    """Return a fresh 5-digit numeric nonce.

    The Zendure API validates the ``nonce`` header as a 5-digit integer
    (``10000``–``99999``) and rejects any other length/format (including hex or
    leading-zero values) with ``code 1007 nonce format is incorrect``.
    """

    return str(secrets.randbelow(90000) + 10000)


def _split_host_port(url):
    text = str(url or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.strip("/")
    if not text:
        return None, None
    host, sep, port_text = text.rpartition(":")
    if not sep:
        return text, None
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        return text, None
    if not host or not (0 < port < 65536):
        return None, None
    return host, port


def parse_device_list_response(payload, *, api_url=None, app_key=None):
    """Parse a Zendure ``deviceList`` response into a safe internal shape.

    Rejects non-object payloads, unsuccessful responses, an empty device list,
    and a missing MQTT block. Preserves ``username``/``password``/``clientId``
    internally for the connection step but never echoes them in errors.
    """

    if not isinstance(payload, dict):
        raise ZendureCloudError(_ERR_DEVICE_LIST_FAILED)
    if "success" in payload and payload.get("success") is not True:
        raise ZendureCloudError(_ERR_INVALID_TOKEN)
    if "code" in payload and payload.get("code") not in (200, "200"):
        raise ZendureCloudError(_ERR_INVALID_TOKEN)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ZendureCloudError(_ERR_DEVICE_LIST_FAILED)
    device_list = data.get("deviceList")
    if not isinstance(device_list, list) or not device_list:
        raise ZendureCloudError(_ERR_NO_DEVICES)
    mqtt = data.get("mqtt")
    if not isinstance(mqtt, dict) or not mqtt.get("url"):
        raise ZendureCloudError(_ERR_NO_MQTT)
    host, port = _split_host_port(mqtt.get("url"))
    if not host:
        raise ZendureCloudError(_ERR_NO_MQTT)

    devices = []
    for entry in device_list:
        if not isinstance(entry, dict):
            continue
        devices.append(
            {
                "productKey": _clean_str(entry.get("productKey")),
                "deviceKey": _clean_str(entry.get("deviceKey")),
                "productModel": _clean_str(entry.get("productModel")),
                "snNumber": _clean_str(entry.get("snNumber")),
                "deviceName": _clean_str(entry.get("deviceName")),
            }
        )
    if not devices:
        raise ZendureCloudError(_ERR_NO_DEVICES)

    result = {
        "devices": devices,
        "mqtt": {
            "host": host,
            "port": port if port is not None else DEFAULT_MQTT_PORT,
            "port_from_api": port is not None,
            "username": _clean_str(mqtt.get("username")),
            "password": mqtt.get("password") if isinstance(mqtt.get("password"), str) else None,
            "client_id": _clean_str(mqtt.get("clientId")) or _clean_str(mqtt.get("client_id")),
        },
        "api_url": api_url,
        "app_key": app_key,
    }
    return result


def _clean_str(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _default_post(url, headers, json_body, timeout):
    response = requests.post(url, headers=headers, json=json_body, timeout=timeout)
    if response.status_code != 200:
        raise ZendureCloudError(_ERR_DEVICE_LIST_FAILED)
    try:
        return response.json()
    except ValueError as exc:
        raise ZendureCloudError(_ERR_DEVICE_LIST_FAILED) from exc


def fetch_device_list(api_url, app_key, timeout=DEFAULT_TIMEOUT_S, *, post=None):
    """Sign, POST the Zendure deviceList request, and return the parsed result.

    ``post`` is injectable for tests; the default uses ``requests``. Any
    transport error is normalized to a redaction-safe ``ZendureCloudError``.
    """

    if not api_url or not app_key:
        raise ZendureCloudError(_ERR_INVALID_TOKEN)
    timeout = max(0.5, min(float(timeout or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    headers = build_device_list_headers(app_key, _current_timestamp(), _generate_nonce())
    url = f"{api_url.rstrip('/')}{_DEVICE_LIST_PATH}"
    post = post or _default_post
    try:
        payload = post(url, headers, {"appKey": app_key}, timeout)
    except ZendureCloudError:
        raise
    except requests.Timeout as exc:  # slow Zendure cloud, not a bad key
        raise ZendureCloudError(
            _ERR_DEVICE_LIST_TIMEOUT.format(seconds=int(timeout))
        ) from exc
    except Exception as exc:  # never surface raw transport/URL/secret detail
        raise ZendureCloudError(_ERR_DEVICE_LIST_FAILED) from exc
    return parse_device_list_response(payload, api_url=api_url, app_key=app_key)
