# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local network discovery for EMS-related devices.

Discovery is manual, bounded, and probe-based: for each host in a validated
local CIDR it issues a few short-timeout HTTP GETs to known local device APIs
and classifies the JSON response. No daemon, no shell-out, no ping/nmap/arp, no
public ranges. Concurrency and timeouts are clamped to keep a scan cheap enough
for a small Raspberry Pi.
"""

import ipaddress
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from admin.models import (
    ROLE_GRID_METER,
    ROLE_INVERTER,
    DiscoveredDevice,
)

logger = logging.getLogger("admin.discovery")

TIMEOUT_MS_MIN = 200
TIMEOUT_MS_MAX = 3000
ENDPOINT_TIMEOUT_MS_MAX = 5000
TIMEOUT_MS_DEFAULT = 600
MAX_WORKERS_MIN = 1
MAX_WORKERS_MAX = 64
MAX_WORKERS_DEFAULT = 32
# Broader than /24 would scan 512+ hosts; reject by default to stay cheap and to
# avoid sweeping ranges the operator did not clearly intend.
MAX_HOSTS = 256
DEFAULT_PORTS = (80,)


class CidrValidationError(ValueError):
    """Raised when a requested scan CIDR is missing, malformed, or not allowed."""


def clamp_timeout_ms(value, maximum=TIMEOUT_MS_MAX):
    return _clamp_int(value, TIMEOUT_MS_DEFAULT, TIMEOUT_MS_MIN, maximum)


def clamp_max_workers(value):
    return _clamp_int(value, MAX_WORKERS_DEFAULT, MAX_WORKERS_MIN, MAX_WORKERS_MAX)


def _clamp_int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def validate_cidr(raw_cidr):
    """Validate a scan CIDR and return the parsed network.

    Only private, link-local, or loopback ranges are allowed, and the host count
    is capped (``MAX_HOSTS``). Public internet ranges are rejected; there is no
    unsafe override.
    """

    if not isinstance(raw_cidr, str) or not raw_cidr.strip():
        raise CidrValidationError("CIDR is required")

    try:
        network = ipaddress.ip_network(raw_cidr.strip(), strict=False)
    except ValueError as exc:
        raise CidrValidationError(f"invalid CIDR: {exc}") from exc

    if not (network.is_private or network.is_link_local or network.is_loopback):
        raise CidrValidationError(
            "only private, link-local, or loopback ranges may be scanned"
        )

    if network.num_addresses > MAX_HOSTS:
        raise CidrValidationError(
            f"range too broad: {network.num_addresses} addresses "
            f"(max {MAX_HOSTS}, i.e. no broader than /24)"
        )

    return network


def iter_scan_hosts(network):
    """Yield the host addresses to probe for a validated network.

    A single-host (/32) network has no ``hosts()`` entries, so fall back to the
    network address itself.
    """

    hosts = list(network.hosts())
    if not hosts:
        hosts = [network.network_address]
    return [str(ip) for ip in hosts]


# --- probe families ------------------------------------------------------

def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


ZENDURE_PROPERTY_KEYS = (
    "electricLevel",
    "solarInputPower",
    "outputHomePower",
    "packInputPower",
    "outputPackPower",
    "solarPower1",
    "solarPower2",
    "outputLimit",
    "smartMode",
    "acMode",
    "socLimit",
)


def probe_zendure(session, ip, timeout_s, port=80, failure_details=None):
    """Probe the Zendure local HTTP device at ``/properties/report``."""

    data = _get_json(
        session, ip, "/properties/report", timeout_s, port=port,
        failure_details=failure_details,
    )
    if not isinstance(data, dict):
        return None
    device = _zendure_inverter_candidate(data, ip, port)
    if device is None:
        _record_failure(failure_details, "Zendure API response missing expected properties")
    return device


def _zendure_inverter_candidate(data, ip, port):
    """Build an inverter candidate from a nested ``properties`` payload, or None."""

    properties = data.get("properties")
    if not isinstance(properties, dict):
        return None
    known = sum(1 for key in ZENDURE_PROPERTY_KEYS if key in properties)
    if known == 0:
        return None

    serial = _first_str(
        data.get("sn"),
        data.get("serialNumber"),
        data.get("deviceSn"),
        properties.get("sn"),
        properties.get("serialNumber"),
    )
    product = _first_str(
        data.get("product"),
        properties.get("product"),
        data.get("productName"),
        properties.get("productName"),
        data.get("productKey"),
    )
    missing = [] if serial else ["serial_number"]
    return DiscoveredDevice(
        ip=ip,
        api_family="zendure_local_http",
        device_type=_zendure_device_type(product),
        role_suggestion=ROLE_INVERTER,
        port=port,
        display_name=f"Zendure {product}" if product else "Zendure SolarFlow device",
        model=product,
        serial_number=serial,
        confidence=0.95 if serial else 0.85,
        config_ready=bool(serial),
        missing_config_fields=missing,
    )


ZENDURE_MODEL_KEYS = (
    "product",
    "productName",
    "model",
    "modelName",
    "deviceType",
    "type",
    "productKey",
)
# Real D0 and Smart Meter 3CT samples both carry the three per-clamp apparent
# powers (all zero on a D0), so the clamp triplet is NOT reliable model evidence
# and must never be used to identify a 3CT.
ZENDURE_3CT_CLAMP_KEYS = ("a_aprt_power", "b_aprt_power", "c_aprt_power")
# Known SmartMeter 3CT model identifiers, normalized (lowercased, separators
# stripped). Matching is an explicit allowlist, never an arbitrary "3ct"
# substring, so values like "not3ct" or "device3ctcompatible" stay neutral. This
# only enriches the display label; the config always uses the generic HTTP type.
ZENDURE_3CT_MODEL_IDS = frozenset(
    {
        "smartmeter3ct",
        "zenduresmartmeter3ct",
    }
)


def _normalize_model_token(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _zendure_model_tokens(data):
    tokens = []
    sources = [data]
    for key in ("metadata", "device", "info"):
        nested = data.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for key in ZENDURE_MODEL_KEYS:
            token = _normalize_model_token(source.get(key))
            if token:
                tokens.append(token)
    return tokens


def _has_3ct_model_evidence(data, model_hint=None):
    tokens = list(_zendure_model_tokens(data))
    # An mDNS model hint is untrusted free text; it only counts as evidence when
    # it normalizes to an exact allowlist identifier, never via a bare substring.
    hint_token = _normalize_model_token(model_hint)
    if hint_token:
        tokens.append(hint_token)
    return any(token in ZENDURE_3CT_MODEL_IDS for token in tokens)


def _looks_like_zendure_3ct(data, model_hint=None):
    # Only an explicit model identifier identifies a 3CT. The per-clamp
    # apparent-power triplet is deliberately NOT used: a D0 exposes the same
    # fields, so it would misclassify a D0 as a 3CT.
    return _has_3ct_model_evidence(data, model_hint)


def probe_zendure_grid_meter(session, ip, timeout_s, port=80, failure_details=None):
    """Probe a Zendure grid meter serving a flat ``total_power`` at ``/properties/report``.

    The inverter probe owns payloads with a nested ``properties`` object; a grid
    meter reports a flat top-level ``total_power`` and must never be classified as
    an inverter.

    A flat numeric ``total_power`` is the functional read criterion. Both a
    Zendure D0 and a Smart Meter 3CT serve the same shape, so the device is a
    config-ready generic ``zendure_grid_meter_http`` candidate regardless of
    model. An explicit trusted model field only enriches the display label; it
    never changes the generic config type and phase fields are never used.
    """

    data = _get_json(
        session, ip, "/properties/report", timeout_s, port=port,
        failure_details=failure_details,
    )
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("properties"), dict):
        return None
    device = _zendure_grid_meter_candidate(data, ip, port)
    if device is None:
        _record_failure(
            failure_details,
            "Zendure grid meter response missing numeric total_power",
        )
    return device


def _zendure_grid_meter_candidate(data, ip, port, model_hint=None):
    """Build a grid-meter candidate from a flat ``total_power`` payload, or None.

    Numeric ``total_power`` alone makes the device config-ready via the generic
    local-HTTP grid-meter client. Explicit 3CT model evidence only adds a model
    detail to the display name; the config type stays generic either way.
    """

    if not _is_number(data.get("total_power")):
        return None

    serial = _first_str(
        data.get("deviceId"), data.get("sn"), data.get("serialNumber")
    )
    display_name = "Zendure Grid Meter via local HTTP"
    device_type = "zendure_grid_meter_http"
    if _looks_like_zendure_3ct(data, model_hint):
        display_name = "Zendure Grid Meter via local HTTP (Smart Meter 3CT)"
        device_type = "zendure_smartmeter_3ct"
    return DiscoveredDevice(
        ip=ip,
        api_family="zendure_grid_meter_http",
        device_type=device_type,
        role_suggestion=ROLE_GRID_METER,
        port=port,
        display_name=display_name,
        serial_number=serial,
        confidence=0.9,
        config_ready=True,
        missing_config_fields=[],
    )


def classify_zendure_report(data, *, ip, port, model_hint=None):
    """Classify a single ``/properties/report`` payload into a discovery candidate.

    Shared by the network scan probes and the mDNS verification path so both use
    identical inverter / grid-meter rules. A nested ``properties`` object is an
    inverter; a flat numeric ``total_power`` is a config-ready generic local-HTTP
    grid meter (an explicit model field only enriches the label). Returns
    ``None`` for anything else.
    """

    if not isinstance(data, dict):
        return None
    inverter = _zendure_inverter_candidate(data, ip, port)
    if inverter is not None:
        return inverter
    if isinstance(data.get("properties"), dict):
        return None
    return _zendure_grid_meter_candidate(data, ip, port, model_hint=model_hint)


def _zendure_device_type(product):
    """Derive a stable device_type slug from the Zendure ``product`` model.

    Falls back to the generic ``_unknown`` type when the firmware does not report
    a product (older firmware / partial payloads).
    """

    if not product:
        return "zendure_solarflow_unknown"
    slug = re.sub(r"[^a-z0-9]+", "_", product.lower()).strip("_")
    return f"zendure_{slug}" if slug else "zendure_solarflow_unknown"


def probe_shelly_gen2(session, ip, timeout_s, port=80, failure_details=None):
    """Probe the Shelly Pro / Gen2 RPC meter at ``/rpc/Shelly.GetStatus``."""

    data = _get_json(
        session, ip, "/rpc/Shelly.GetStatus", timeout_s, port=port,
        failure_details=failure_details,
    )
    if not isinstance(data, dict):
        return None

    em = data.get("em:0")
    has_em = isinstance(em, dict) and _is_number(em.get("total_act_power"))
    has_em1 = any(
        isinstance(data.get(key), dict) and _is_number(data[key].get("act_power"))
        for key in data
        if isinstance(key, str) and key.startswith("em1:")
    )
    if not (has_em or has_em1):
        _record_failure(failure_details, "Shelly API response missing expected meter properties")
        return None

    serial = _shelly_serial(data)
    return DiscoveredDevice(
        ip=ip,
        api_family="shelly_gen2",
        device_type="shelly_pro_em",
        role_suggestion=ROLE_GRID_METER,
        port=port,
        display_name="Shelly Pro / Gen2 meter",
        serial_number=serial,
        confidence=0.9,
        config_ready=True,
        missing_config_fields=[],
    )


def probe_shelly_3em_gen1(session, ip, timeout_s, port=80, failure_details=None):
    """Probe the Shelly 3EM Gen1 meter at ``/status``."""

    data = _get_json(
        session, ip, "/status", timeout_s, port=port,
        failure_details=failure_details,
    )
    if not isinstance(data, dict):
        return None

    emeters = data.get("emeters")
    has_emeters = isinstance(emeters, list) and any(
        isinstance(meter, dict) and _is_number(meter.get("power"))
        for meter in emeters
    )
    if not (has_emeters or _is_number(data.get("total_power"))):
        _record_failure(failure_details, "Shelly API response missing expected meter properties")
        return None

    serial = _first_str(data.get("mac"), _nested(data, ("device", "hostname")))
    return DiscoveredDevice(
        ip=ip,
        api_family="shelly_3em_gen1",
        device_type="shelly_3em_gen1",
        role_suggestion=ROLE_GRID_METER,
        port=port,
        display_name="Shelly 3EM (Gen1) meter",
        serial_number=serial,
        confidence=0.9,
        config_ready=True,
        missing_config_fields=[],
    )


def probe_ecotracker(session, ip, timeout_s):
    """Probe the everHome EcoTracker local REST API at ``/v1/json``."""

    data = _get_json(session, ip, "/v1/json", timeout_s)
    if not isinstance(data, dict) or not _is_number(data.get("power")):
        return None

    return DiscoveredDevice(
        ip=ip,
        api_family="ecotracker",
        device_type="ecotracker",
        role_suggestion=ROLE_GRID_METER,
        display_name="everHome EcoTracker",
        serial_number=_first_str(data.get("id"), data.get("serial")),
        confidence=0.85,
        config_ready=True,
        missing_config_fields=[],
    )


PROBES = (
    probe_zendure,
    probe_zendure_grid_meter,
    probe_shelly_gen2,
    probe_shelly_3em_gen1,
    probe_ecotracker,
)


def verify_zendure_endpoint(ip, port=80, timeout_ms=TIMEOUT_MS_DEFAULT, session=None,
                            failure_details=None, model_hint=None):
    """HTTP-verify a single Zendure host:port (used by mDNS discovery).

    Requests ``/properties/report`` once and runs it through the shared
    classifier, so an mDNS-advertised inverter, Smart Meter 3CT, or neutral grid
    meter is promoted through the same rules the network scan uses. Returns a
    ``DiscoveredDevice`` on success or ``None`` on any failure. ``model_hint`` is
    untrusted mDNS metadata; it can only confirm a 3CT via the strict allowlist.
    """

    timeout_s = clamp_timeout_ms(timeout_ms, ENDPOINT_TIMEOUT_MS_MAX) / 1000.0
    owns_session = session is None
    session = session or requests.Session()
    try:
        data = _get_json(
            session, ip, "/properties/report", timeout_s, port=port,
            failure_details=failure_details,
        )
        if not isinstance(data, dict):
            return None
        device = classify_zendure_report(
            data, ip=ip, port=port, model_hint=model_hint
        )
        if device is None:
            _record_failure(
                failure_details,
                "Zendure API response is not a supported inverter or grid meter",
            )
        return device
    except requests.RequestException as exc:
        _record_failure(failure_details, _request_failure_reason(exc, timeout_s))
        return None
    except ValueError:
        _record_failure(failure_details, "Invalid JSON from /properties/report")
        return None
    finally:
        if owns_session:
            session.close()


def verify_shelly_meter_endpoint(ip, port=80, timeout_ms=TIMEOUT_MS_DEFAULT,
                                 session=None, failure_details=None):
    """HTTP-verify a supported Shelly grid meter advertised through mDNS."""

    timeout_s = clamp_timeout_ms(timeout_ms, ENDPOINT_TIMEOUT_MS_MAX) / 1000.0
    owns_session = session is None
    session = session or requests.Session()
    try:
        for probe in (probe_shelly_gen2, probe_shelly_3em_gen1):
            try:
                device = probe(
                    session, ip, timeout_s, port=port,
                    failure_details=failure_details,
                )
            except requests.RequestException as exc:
                _record_failure(
                    failure_details, _request_failure_reason(exc, timeout_s)
                )
                continue
            except ValueError:
                _record_failure(failure_details, "Invalid JSON from Shelly API")
                continue
            if device is not None:
                return device
        return None
    finally:
        if owns_session:
            session.close()


def probe_host(session, ip, timeout_s):
    """Run every probe family against one host; return the first match or None."""

    for probe in PROBES:
        try:
            device = probe(session, ip, timeout_s)
        except requests.RequestException:
            # Unreachable/timeout/refused hosts are the common case in a sweep;
            # never let one host fail the whole scan.
            continue
        except ValueError:
            continue
        if device is not None:
            return device
    return None


def scan_network(cidr, timeout_ms=TIMEOUT_MS_DEFAULT, max_workers=MAX_WORKERS_DEFAULT,
                 session=None, progress_callback=None):
    """Scan a validated CIDR and return ``(devices, errors)``.

    Runs bounded-parallel HTTP probes with a shared session. Partial results are
    returned even when most hosts are unreachable. Raises ``CidrValidationError``
    on an invalid or disallowed CIDR before any network traffic.

    When ``progress_callback`` is given it is invoked after each host result with
    the running counts, so a caller can surface granular per-host progress.
    """

    network = validate_cidr(cidr)
    timeout_s = clamp_timeout_ms(timeout_ms) / 1000.0
    workers = clamp_max_workers(max_workers)
    hosts = iter_scan_hosts(network)

    owns_session = session is None
    session = session or requests.Session()
    devices = []
    errors = []
    checked = 0
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
            futures = {
                pool.submit(probe_host, session, ip, timeout_s): ip for ip in hosts
            }
            for future in as_completed(futures):
                ip = futures[future]
                try:
                    device = future.result()
                except Exception as exc:  # defensive: keep the scan alive
                    errors.append({"ip": ip, "error": str(exc)})
                else:
                    if device is not None:
                        devices.append(device)
                checked += 1
                if progress_callback is not None:
                    progress_callback({
                        "total_hosts": len(hosts),
                        "checked_hosts": checked,
                        "found_devices": len(devices),
                        "failed_hosts": len(errors),
                        "current_ip": ip,
                    })
    finally:
        if owns_session:
            session.close()

    devices.sort(key=lambda d: tuple(int(part) for part in d.ip.split(".")) if
                 d.ip.count(".") == 3 else (0,))
    return devices, errors


# --- low level HTTP helpers ---------------------------------------------

def _get_json(session, ip, path, timeout_s, port=80, failure_details=None):
    host = ip if port == 80 else f"{ip}:{port}"
    response = session.get(
        f"http://{host}{path}",
        timeout=timeout_s,
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        _record_failure(
            failure_details, f"HTTP {response.status_code} from {path}"
        )
        return None
    try:
        return response.json()
    except ValueError:
        _record_failure(failure_details, f"Invalid JSON from {path}")
        return None


def _record_failure(failure_details, reason):
    if failure_details is not None:
        failure_details.append(reason)


def _request_failure_reason(exc, timeout_s):
    if isinstance(exc, requests.Timeout):
        return f"HTTP verification timeout after {timeout_s:g}s"
    text = str(exc)
    if isinstance(exc, requests.ConnectionError) and "refused" in text.lower():
        return "HTTP connection refused"
    if isinstance(exc, requests.ConnectionError):
        return f"HTTP connection failed: {text}"
    return f"HTTP verification failed: {text}"


def _first_str(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested(data, path):
    current = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _shelly_serial(data):
    sys_info = data.get("sys")
    if isinstance(sys_info, dict):
        return _first_str(sys_info.get("mac"), sys_info.get("id"))
    return None
