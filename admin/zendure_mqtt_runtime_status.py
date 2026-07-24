# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only Admin bridge to the EMS-owned Zendure MQTT telemetry status.

Admin is UI/orchestration only: it never parses MQTT topics, infers device
capabilities, computes freshness, or reinterprets telemetry. It prefers the live
status snapshot the running EMS persists (real online/stale/unseen device state)
and otherwise degrades to a config-derived offline view built by EMS/Core from
the installed config. Either way there is no publish, write, control, restart, or
config-write path here, and any failure degrades to a friendly unavailable view.
"""

import json
import time

from ems import paths

from admin.install_context import detect_install_context

# Defence in depth: EMS status is already credential-free, but this bridge drops
# any secret-looking key so a future EMS payload can never leak one to the UI.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "app_key",
    "appkey",
    "api_key",
    "apikey",
    "credential",
    "username",
)

_UNAVAILABLE_MESSAGE = "Zendure MQTT telemetry status is unavailable."

# A live snapshot older than this is treated as stale, so a stopped EMS falls
# back to the config-derived view instead of showing frozen "online" devices.
_LIVE_STATUS_MAX_AGE_SECONDS = 180


def _is_secret_key(key):
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _is_secret_key(k)}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _read_config(context):
    try:
        text = context.config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_live_status(context, *, now=None):
    """Scrubbed live EMS status, or ``(None, reason)`` when it can't be used."""

    now = time.time() if now is None else now
    try:
        path = context.data_dir / paths.ZENDURE_MQTT_STATUS_FILENAME
    except Exception:
        return None, "EMS data directory could not be resolved."
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "EMS live status snapshot is unavailable."
    try:
        payload = json.loads(text)
    except ValueError:
        return None, "EMS live status snapshot could not be read."
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), dict):
        return None, "EMS live status snapshot is malformed."
    written_at = payload.get("written_at")
    if not isinstance(written_at, (int, float)) or isinstance(written_at, bool):
        return None, "EMS live status snapshot is missing a timestamp."
    if now - written_at > _LIVE_STATUS_MAX_AGE_SECONDS:
        return None, "EMS live status snapshot is stale."
    return _scrub(payload["status"]), None


def _unavailable(message=_UNAVAILABLE_MESSAGE, *, fallback_reason=None):
    return {
        "available": False,
        "source": "offline_config",
        "live_available": False,
        "fallback_reason": fallback_reason,
        "runtime_state": "unavailable",
        "message": message,
        "enabled": False,
        "broker_configured": False,
        "endpoint": None,
        "configured_device_count": 0,
        "invalid_device_count": 0,
        "stale_after_seconds": None,
        "write_output_limit": False,
        "broker_count": 0,
        "brokers": [],
        "devices": [],
    }


def _runtime_state(status):
    # The feature is always on; a runtime without an active broker is inactive,
    # never "disabled in the installed config".
    return "configured" if status.get("enabled") else "inactive"


def _runtime_message(state, status):
    if state == "inactive":
        return (
            "Zendure MQTT telemetry is inactive: no MQTT broker is configured. "
            "It activates automatically once a broker or a Zendure MQTT device "
            "is configured — there is no feature toggle."
        )
    if not status.get("broker_configured"):
        return "Zendure MQTT telemetry is enabled but no broker endpoint is configured."
    count = status.get("configured_device_count") or 0
    return f"Zendure MQTT telemetry is configured for {count} device(s)."


def _view_from_status(status, *, source, live_available, fallback_reason):
    state = _runtime_state(status)
    return {
        "available": True,
        "source": source,
        "live_available": live_available,
        "fallback_reason": fallback_reason,
        "runtime_state": state,
        "message": _runtime_message(state, status),
        "enabled": bool(status.get("enabled")),
        "broker_configured": bool(status.get("broker_configured")),
        "endpoint": status.get("endpoint"),
        "configured_device_count": status.get("configured_device_count") or 0,
        "invalid_device_count": status.get("invalid_device_count") or 0,
        "stale_after_seconds": status.get("stale_after_seconds"),
        "write_output_limit": False,
        "broker_count": status.get("broker_count") or 0,
        "brokers": status.get("brokers") or [],
        "devices": status.get("devices") or [],
    }


def _build_status_via_ems(config):
    """EMS-built offline status. Own seam so failures are testable."""

    from ems.zendure_mqtt import build_zendure_mqtt_runtime

    return build_zendure_mqtt_runtime(config).status()


def _config_uses_zendure_mqtt(config):
    """Presence check only (no EMS imports): does this install use MQTT at all?"""

    zmqtt = config.get("zendure_mqtt")
    if isinstance(zmqtt, dict):
        host = zmqtt.get("host")
        if isinstance(host, str) and host.strip():
            return True
        brokers = zmqtt.get("brokers")
        if isinstance(brokers, dict) and brokers:
            return True
    devices = config.get("devices")
    if isinstance(devices, list):
        for item in devices:
            if (
                isinstance(item, dict)
                and str(item.get("type") or "").strip().lower() == "zendure_mqtt"
            ):
                return True
    return False


def _offline_status(context):
    """Config-derived EMS status, or ``None`` when it can't be built.

    Read-only: EMS/Core builds the status from the installed config; this never
    starts the telemetry service, opens the broker, or reinterprets device state.
    When the EMS builder cannot run at all (e.g. an older Admin image without
    the runtime modules), an install that plainly does not use MQTT still gets
    a quiet "inactive" view instead of a red "unavailable" warning; an install
    that does use MQTT keeps the warning, because its status truly is unknown.
    """

    if not context.config_exists:
        return None
    config = _read_config(context)
    if config is None:
        return None
    try:
        return _scrub(_build_status_via_ems(config))
    except Exception:
        if _config_uses_zendure_mqtt(config):
            return None
        return {"enabled": False, "broker_configured": False}


def build_runtime_status_view(base_dir=None):
    """EMS-owned, credential-free Zendure MQTT runtime status for the Admin UI.

    Prefers the live status snapshot persisted by a running EMS; falls back to a
    config-derived offline view when that snapshot is missing, stale or malformed.
    """

    try:
        context = detect_install_context(base_dir=base_dir)
    except Exception:
        return _unavailable(fallback_reason="EMS install context is unavailable.")

    live_status, fallback_reason = _read_live_status(context)
    if live_status is not None:
        return _view_from_status(
            live_status,
            source="live_runtime",
            live_available=True,
            fallback_reason=None,
        )

    if not context.config_exists:
        return _unavailable(
            "No installed config was found, so telemetry status is unavailable.",
            fallback_reason=fallback_reason,
        )
    offline_status = _offline_status(context)
    if offline_status is None:
        return _unavailable(fallback_reason=fallback_reason)
    return _view_from_status(
        offline_status,
        source="offline_config",
        live_available=False,
        fallback_reason=fallback_reason,
    )
