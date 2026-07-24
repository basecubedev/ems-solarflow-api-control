# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend orchestration for the unified discovery run.

The fresh-install "Run discovery" button executes exactly this flow: refresh
every *enabled* source once (concurrently, failures isolated per source), then
collect each source's already-collected candidates and unify them by identity
and configured priority. The UI never re-implements this fan-out; rescan and
maintenance callers keep the read-only variant (``refresh=False``). Selection
priority is applied after every refresh has finished, so completion order,
broker response time, or dict order can never override the configured
priority. Display-only: nothing here ever writes the EMS config.
"""

from concurrent.futures import ThreadPoolExecutor

from admin.discovery_preparation import (
    SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_MQTT,
    enabled_sources_in_priority,
)
from admin.discovery_unify import build_unified_devices

REFRESH_OK = "ok"
REFRESH_PARTIAL = "partial"
REFRESH_FAILED = "failed"

# Generic, redaction-safe fallback: raw exception text may carry hosts or
# broker detail, so it never reaches an operator-facing warning.
_GENERIC_REFRESH_ERROR = "refresh failed"


def gather_source_candidates(
    enabled_sources,
    *,
    registry,
    mdns_provider,
    mqtt_discovery,
    zendure_cloud_discovery,
):
    """Collect current per-source candidates for only the enabled sources.

    Read-only: it reuses each service's already-collected state and never
    starts a new network scan (that is what the per-source refresh is for).
    """

    candidates = {}
    if SOURCE_LOCAL_API in enabled_sources:
        merged = {}
        for device in registry.all_devices():
            merged[device.get("id") or id(device)] = device
        for device in mdns_provider.devices():
            merged[device.get("id") or id(device)] = device
        candidates[SOURCE_LOCAL_API] = list(merged.values())
    if SOURCE_LOCAL_MQTT in enabled_sources:
        devices = []
        for broker in mqtt_discovery.candidates():
            for device in broker.get("devices") or []:
                devices.append(device)
        candidates[SOURCE_LOCAL_MQTT] = devices
    if SOURCE_ZENDURE_MQTT in enabled_sources:
        # In-memory candidates are cleared on restart; lazily reseed from the
        # saved API key so priority can select Zendure without a prior manual
        # refresh. Best-effort: a failure just leaves the source empty.
        try:
            zendure_cloud_discovery.ensure_device_list_candidates()
        except Exception:
            pass
        candidates[SOURCE_ZENDURE_MQTT] = list(zendure_cloud_discovery.candidates())
    return candidates


def discovery_details(
    *, registry, mdns_provider, mqtt_discovery, zendure_cloud_discovery
):
    """Per-source detail view (redaction-safe) shown below the unified list."""

    api_devices = gather_source_candidates(
        [SOURCE_LOCAL_API],
        registry=registry,
        mdns_provider=mdns_provider,
        mqtt_discovery=mqtt_discovery,
        zendure_cloud_discovery=zendure_cloud_discovery,
    ).get(SOURCE_LOCAL_API, [])
    brokers = mqtt_discovery.candidates()
    mqtt_device_count = sum(len(b.get("devices") or []) for b in brokers)
    try:
        zendure_settings = zendure_cloud_discovery.settings()
    except Exception:
        zendure_settings = {"token_saved": False, "last_status": "error"}
    zendure_candidates = list(zendure_cloud_discovery.candidates())
    return {
        SOURCE_LOCAL_API: {
            "devices": api_devices,
            "device_count": len(api_devices),
        },
        SOURCE_LOCAL_MQTT: {
            "brokers": brokers,
            "broker_count": len(brokers),
            "device_count": mqtt_device_count,
        },
        SOURCE_ZENDURE_MQTT: {
            "settings": zendure_settings,
            "candidates": zendure_candidates,
            "device_count": len(zendure_candidates),
        },
    }


def _refresh_outcome(refresher):
    """Run one source refresh; classify the result without leaking detail.

    A raised exception, an explicit ``ok: False`` result (Zendure cloud), or an
    ``unavailable_*`` mDNS state count as a failed refresh; the source keeps
    whatever state it already had.
    """

    try:
        result = refresher()
    except Exception:
        return {
            "ok": False,
            "error": "refresh_failed",
            "message": _GENERIC_REFRESH_ERROR,
        }
    if isinstance(result, dict):
        if result.get("ok") is False:
            return {
                "ok": False,
                "error": str(result.get("error") or "refresh_failed"),
                "message": str(
                    result.get("message")
                    or result.get("error")
                    or _GENERIC_REFRESH_ERROR
                ),
            }
        state = str(result.get("state") or "")
        if state.startswith("unavailable_"):
            return {
                "ok": False,
                "error": state,
                "message": str(result.get("message") or _GENERIC_REFRESH_ERROR),
            }
    return {"ok": True, "error": None, "message": None}


def refresh_enabled_sources(
    enabled_sources,
    *,
    mdns_provider,
    mqtt_discovery,
    zendure_cloud_discovery,
):
    """Refresh each enabled source exactly once, concurrently and isolated.

    Returns ``(refresh_block, warnings)``: per-source ``{ok, error, message}``
    plus an overall status (``ok`` / ``partial`` / ``failed`` when every
    enabled source failed) and redaction-safe operator warnings.
    """

    refreshers = {
        SOURCE_LOCAL_API: mdns_provider.refresh,
        SOURCE_LOCAL_MQTT: mqtt_discovery.refresh,
        SOURCE_ZENDURE_MQTT: zendure_cloud_discovery.refresh,
    }
    sources = [source for source in enabled_sources if source in refreshers]
    statuses = {}
    if sources:
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            futures = {
                source: pool.submit(_refresh_outcome, refreshers[source])
                for source in sources
            }
            for source, future in futures.items():
                statuses[source] = future.result()
    warnings = [
        f"{source}: {status['message']}"
        for source, status in statuses.items()
        if not status["ok"]
    ]
    if statuses and all(not status["ok"] for status in statuses.values()):
        overall = REFRESH_FAILED
    elif warnings:
        overall = REFRESH_PARTIAL
    else:
        overall = REFRESH_OK
    return {"requested": True, "status": overall, "sources": statuses}, warnings


def run_discovery(
    preparation,
    *,
    refresh,
    registry,
    mdns_provider,
    mqtt_discovery,
    zendure_cloud_discovery,
):
    """One unified discovery run over the saved preparation settings.

    With ``refresh=True`` (the fresh-install "Run discovery" action) every
    enabled source is refreshed first and the payload carries the per-source
    ``refresh`` block plus ``warnings``; with ``refresh=False`` the payload is
    the unchanged read-only unify used by rescan/maintenance callers.
    """

    enabled = enabled_sources_in_priority(preparation)
    refresh_block = None
    warnings = []
    if refresh:
        refresh_block, warnings = refresh_enabled_sources(
            enabled,
            mdns_provider=mdns_provider,
            mqtt_discovery=mqtt_discovery,
            zendure_cloud_discovery=zendure_cloud_discovery,
        )
    candidates = gather_source_candidates(
        enabled,
        registry=registry,
        mdns_provider=mdns_provider,
        mqtt_discovery=mqtt_discovery,
        zendure_cloud_discovery=zendure_cloud_discovery,
    )
    payload = {
        "priority": preparation["discovery_priority"],
        "sources": preparation["sources"],
        "devices": build_unified_devices(
            candidates, preparation["discovery_priority"]
        ),
        "details": discovery_details(
            registry=registry,
            mdns_provider=mdns_provider,
            mqtt_discovery=mqtt_discovery,
            zendure_cloud_discovery=zendure_cloud_discovery,
        ),
    }
    if refresh_block is not None:
        payload["refresh"] = refresh_block
        payload["warnings"] = warnings
    return payload
