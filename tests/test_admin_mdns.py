# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live mDNS discovery provider tests (fake services, no real multicast)."""

import pytest
import requests

from admin.discovery import verify_zendure_endpoint
from admin.mdns import (
    DeviceStore,
    HTTP_MDNS_SERVICE_TYPE,
    MDNS_SERVICE_TYPES,
    MdnsProvider,
    SHELLY_MDNS_SERVICE_TYPE,
    ZENDURE_MDNS_SERVICE_TYPE,
    VERIFY_MAX_WORKERS,
    VERIFY_TIMEOUT_MS,
    build_candidate,
    classify_zendure_service,
    decode_txt,
    merge_entries,
    verify_candidate,
)
from admin.models import DiscoveredDevice

pytestmark = pytest.mark.simulation

OBSERVED_ZENDURE_HTTP_NAME = (
    "Zendure-solarFlow800Pro2-EOD1NLN9P010611._http._tcp.local."
)


def _zendure_device(ip="192.168.178.42", serial="SN123456789", port=80):
    return DiscoveredDevice(
        ip=ip,
        api_family="zendure_local_http",
        device_type="zendure_solarflow_800_pro2",
        role_suggestion="inverter",
        port=port,
        display_name="SolarFlow 800 Pro 2",
        model="SolarFlow 800 Pro 2",
        serial_number=serial,
        confidence=0.95,
        config_ready=True,
    )


def _shelly_device(ip="192.168.178.50", serial="78421c591928", port=80):
    return DiscoveredDevice(
        ip=ip,
        api_family="shelly_gen2",
        device_type="shelly_pro_em",
        role_suggestion="grid_meter",
        port=port,
        display_name="Shelly Pro / Gen2 meter",
        serial_number=serial,
        confidence=0.9,
        config_ready=True,
    )


def _shelly_candidate(service_type=SHELLY_MDNS_SERVICE_TYPE):
    return build_candidate(
        "shellypro3em-78421c591928." + service_type,
        "shellypro3em-78421c591928.local.",
        ["192.168.178.50"],
        80,
        {},
        service_type=service_type,
    )


def _candidate(ip="192.168.178.42", port=80, name="Zendure-800Pro2-AABBCCDDEEFF"):
    return build_candidate(
        service_name=name,
        hostname="zendure-800.local.",
        addresses=[ip] if ip else [],
        port=port,
        properties={b"sn": b"SN123456789", b"model": b"800Pro2"},
    )


# --- candidate parsing / TXT --------------------------------------------

def test_build_candidate_parses_service_fields():
    cand = _candidate(port=8080)
    assert cand["ip"] == "192.168.178.42"
    assert cand["port"] == 8080
    assert cand["service_type"] == "_zendure._tcp.local."
    assert cand["source"] == "mdns"
    assert cand["vendor"] == "Zendure"
    assert cand["txt"] == {"sn": "SN123456789", "model": "800Pro2"}


def test_mdns_service_types_are_hardcoded():
    assert HTTP_MDNS_SERVICE_TYPE == "_http._tcp.local."
    assert ZENDURE_MDNS_SERVICE_TYPE == "_zendure._tcp.local."
    assert SHELLY_MDNS_SERVICE_TYPE == "_shelly._tcp.local."
    assert MDNS_SERVICE_TYPES == (
        "_http._tcp.local.",
        "_zendure._tcp.local.",
        "_shelly._tcp.local.",
    )
    assert "service_type" not in MdnsProvider.__init__.__code__.co_varnames


def test_http_service_name_is_classified_with_model_and_serial_hints():
    candidate = build_candidate(
        OBSERVED_ZENDURE_HTTP_NAME,
        "zendure.local.",
        ["192.168.178.42"],
        80,
        {},
        service_type=HTTP_MDNS_SERVICE_TYPE,
    )
    assert candidate["vendor"] == "Zendure"
    assert candidate["model_hint"] == "solarFlow800Pro2"
    assert candidate["serial_number_hint"] == "EOD1NLN9P010611"
    assert candidate["service_type"] == "_http._tcp.local."
    assert candidate["source"] == "mdns"


def test_http_service_classifies_shelly_and_keeps_unknown_for_diagnostics():
    assert classify_zendure_service(
        "shellypro3em-78421c591928._http._tcp.local.",
        HTTP_MDNS_SERVICE_TYPE,
    ) is None
    assert _shelly_candidate(HTTP_MDNS_SERVICE_TYPE)["vendor"] == "Shelly"
    unknown = build_candidate(
        "other-device._http._tcp.local.",
        "other.local.",
        ["192.168.178.50"],
        80,
        {},
        service_type=HTTP_MDNS_SERVICE_TYPE,
    )
    assert "vendor" not in unknown


def test_decode_txt_handles_bytes_and_none():
    assert decode_txt({b"a": b"1", b"flag": None}) == {"a": "1", "flag": ""}
    assert decode_txt(None) == {}


def test_build_candidate_without_address_has_no_ip():
    cand = build_candidate("n", "h.local.", [], 80, {})
    assert cand["ip"] is None


# --- verification --------------------------------------------------------

def test_verify_candidate_promotes_verified_device():
    device = _zendure_device()
    entry = verify_candidate(_candidate(port=80), verifier=lambda ip, port: device)
    assert entry["verified"] is True
    assert entry["usable_for_config"] is True
    assert entry["source"] == "mdns"
    assert entry["source_detail"] == "_zendure._tcp.local."
    assert entry["id"] == "zendure_local_http:SN123456789"
    assert entry["serial_number"] == "SN123456789"
    assert entry["sources"] == ["mdns"]


def test_verify_candidate_prefers_mdns_port():
    device = _zendure_device(port=80)
    entry = verify_candidate(_candidate(port=8080), verifier=lambda ip, port: device)
    assert entry["port"] == 8080


def test_verify_candidate_marks_unverified_on_http_failure():
    entry = verify_candidate(_candidate(), verifier=lambda ip, port: None)
    assert entry["verified"] is False
    assert entry["usable_for_config"] is False
    assert entry["confidence"] == 0.45
    assert "verification" in entry["reason"].lower()
    assert entry["last_verify_attempt"]


def test_mdns_verification_is_bounded_and_uses_five_second_timeout():
    assert VERIFY_MAX_WORKERS == 4
    assert VERIFY_TIMEOUT_MS == 5000


@pytest.mark.parametrize(
    ("failure_reason", "expected"),
    [
        ("HTTP verification timeout after 5s", "timeout after 5s"),
        ("HTTP 404 from /properties/report", "HTTP 404"),
    ],
)
def test_default_verifier_stores_specific_http_failure(
    monkeypatch, failure_reason, expected
):
    import admin.mdns as mdns

    def fail(ip, port, timeout_ms, failure_details, model_hint=None):
        failure_details.append(failure_reason)
        return None

    monkeypatch.setattr(mdns, "verify_zendure_endpoint", fail)
    entry = verify_candidate(_candidate())
    assert expected in entry["reason"]


def test_zendure_verifier_reports_timeout_details():
    class Session:
        def get(self, *args, **kwargs):
            raise requests.Timeout()

    failures = []
    assert verify_zendure_endpoint(
        "192.168.178.42", timeout_ms=5000, session=Session(),
        failure_details=failures,
    ) is None
    assert failures == ["HTTP verification timeout after 5s"]


def test_zendure_verifier_reports_http_status_details():
    class Session:
        def get(self, *args, **kwargs):
            return type("Response", (), {"status_code": 404})()

    failures = []
    assert verify_zendure_endpoint(
        "192.168.178.42", timeout_ms=5000, session=Session(),
        failure_details=failures,
    ) is None
    assert failures == ["HTTP 404 from /properties/report"]


def test_verify_candidate_without_ip_is_unverified():
    entry = verify_candidate(_candidate(ip=None), verifier=lambda ip, port: 1 / 0)
    assert entry["verified"] is False


def test_verifier_receives_candidate_ip_and_port():
    seen = {}

    def verifier(ip, port):
        seen["ip"] = ip
        seen["port"] = port
        return _zendure_device()

    verify_candidate(_candidate(ip="10.0.0.5", port=443), verifier=verifier)
    assert seen == {"ip": "10.0.0.5", "port": 443}


@pytest.mark.parametrize(
    "service_type", [SHELLY_MDNS_SERVICE_TYPE, HTTP_MDNS_SERVICE_TYPE]
)
def test_shelly_grid_meter_is_promoted(service_type):
    entry = verify_candidate(
        _shelly_candidate(service_type), verifier=lambda ip, port: _shelly_device()
    )
    assert entry["verified"] is True
    assert entry["api_family"] == "shelly_gen2"
    assert entry["role_suggestion"] == "grid_meter"


def test_non_grid_shelly_and_unknown_http_are_ignored():
    non_grid = _zendure_device()
    shelly = verify_candidate(
        _shelly_candidate(), verifier=lambda ip, port: non_grid
    )
    unknown = verify_candidate(build_candidate(
        "printer._http._tcp.local.", "printer.local.", ["192.168.178.60"],
        80, {}, service_type=HTTP_MDNS_SERVICE_TYPE,
    ))
    assert shelly["verified"] is False
    assert "not a supported EMS grid meter" in shelly["reason"]
    assert unknown["verified"] is False
    assert "not a supported EMS device" in unknown["reason"]


# --- merge / dedup / stale ----------------------------------------------

def test_merge_dedupes_by_serial_and_unions_sources():
    mdns = verify_candidate(_candidate(), verifier=lambda ip, port: _zendure_device())
    scan = _zendure_device().to_dict()  # same serial, source http_probe
    merged = merge_entries(mdns, scan)
    assert merged["id"] == "zendure_local_http:SN123456789"
    assert set(merged["sources"]) == {"mdns", "http_probe"}
    assert merged["verified"] is True


def test_merge_updates_ip_for_same_serial():
    old = verify_candidate(
        _candidate(ip="192.168.178.42"), verifier=lambda ip, port: _zendure_device(ip="192.168.178.42")
    )
    new = verify_candidate(
        _candidate(ip="192.168.178.99"), verifier=lambda ip, port: _zendure_device(ip="192.168.178.99")
    )
    merged = merge_entries(old, new)
    assert merged["ip"] == "192.168.178.99"


def test_merge_keeps_verified_when_new_event_is_unverified():
    verified = verify_candidate(_candidate(), verifier=lambda ip, port: _zendure_device())
    unverified = verify_candidate(_candidate(), verifier=lambda ip, port: None)
    merged = merge_entries(verified, unverified)
    assert merged["verified"] is True
    assert "reason" not in merged


def test_store_merges_same_device_into_one_entry():
    store = DeviceStore()
    store.merge(verify_candidate(_candidate(), verifier=lambda ip, port: _zendure_device()))
    store.merge(verify_candidate(_candidate(), verifier=lambda ip, port: _zendure_device()))
    devices = store.to_list()
    assert len(devices) == 1
    assert devices[0]["stale"] is False
    assert devices[0]["stale_level"] == "recent"


def test_store_marks_old_last_seen_as_stale():
    store = DeviceStore()
    entry = verify_candidate(_candidate(), verifier=lambda ip, port: _zendure_device())
    entry["last_seen"] = "2000-01-01T00:00:00Z"
    store.merge(entry)
    device = store.to_list()[0]
    assert device["stale"] is True
    assert device["stale_level"] == "old"


# --- provider lifecycle / status ----------------------------------------

def test_provider_unavailable_without_library(monkeypatch):
    import admin.mdns as mdns

    monkeypatch.setattr(mdns.importlib.util, "find_spec", lambda name: None)
    provider = MdnsProvider()
    status = provider.status()
    assert status["available"] is False
    assert status["state"] == "unavailable_dependency"
    assert status["message"] == (
        "Automatic mDNS discovery is unavailable because zeroconf is not installed."
    )
    assert "service" not in status
    # Starting when unavailable must not raise.
    start = provider.start()
    assert start["state"] == "unavailable_dependency"
    assert "zeroconf" in start["last_error"]


def test_provider_handles_candidate_and_reports_status():
    def factory(service_type, handler):
        factory.service_types.append(service_type)
        factory.handler = handler
        return object()

    factory.service_types = []
    provider = MdnsProvider(
        verifier=lambda ip, port: _zendure_device(), browser_factory=factory
    )
    status = provider.start()
    assert status["available"] is True
    assert status["enabled"] is True
    assert status["running"] is True
    assert status["state"] == "running_no_devices"
    assert status["message"].endswith("No supported devices found yet.")
    assert factory.service_types == list(MDNS_SERVICE_TYPES)

    # Simulate a resolved mDNS service; verification runs inline (no executor race
    # in the test) via the handler the provider handed the browser factory.
    provider._executor = None  # force synchronous verify+merge for the assertion
    factory.handler(_candidate())

    status = provider.status()
    assert status["state"] == "running_with_devices"
    assert status["message"] == "Automatic mDNS discovery found 1 supported device."
    assert status["verified_count"] == 1
    assert status["last_event"]
    assert provider.devices()[0]["serial_number"] == "SN123456789"
    provider.stop()


def test_provider_http_zendure_candidate_is_verified_and_merged():
    calls = []

    def factory(service_type, handler):
        factory.handler = handler
        return object()

    def verifier(ip, port):
        calls.append((ip, port))
        return _zendure_device(ip=ip, serial="EOD1NLN9P010611", port=port)

    provider = MdnsProvider(verifier=verifier, browser_factory=factory)
    provider.start()
    provider._executor = None
    factory.handler(build_candidate(
        OBSERVED_ZENDURE_HTTP_NAME,
        "zendure.local.",
        ["192.168.178.42"],
        8080,
        {},
        service_type=HTTP_MDNS_SERVICE_TYPE,
    ))

    assert calls == [("192.168.178.42", 8080)]
    devices = provider.devices()
    assert len(devices) == 1
    assert devices[0]["serial_number"] == "EOD1NLN9P010611"
    assert devices[0]["model_hint"] == "solarFlow800Pro2"
    assert devices[0]["serial_number_hint"] == "EOD1NLN9P010611"
    assert devices[0]["source_detail"] == "_http._tcp.local."
    provider.stop()


def test_provider_disable_stops_but_keeps_devices():
    def factory(service_type, handler):
        factory.handler = handler
        return object()

    provider = MdnsProvider(
        verifier=lambda ip, port: _zendure_device(), browser_factory=factory
    )
    provider.start()
    provider._executor = None
    factory.handler(_candidate())
    provider.disable()
    status = provider.status()
    assert status["enabled"] is False
    assert status["state"] == "disabled"
    assert status["message"] == "Automatic mDNS discovery is disabled."
    assert status["verified_count"] == 1  # devices remain visible after disable


def test_provider_does_not_reverify_unchanged_service():
    calls = []

    def factory(service_type, handler):
        factory.handler = handler
        return object()

    def verifier(ip, port):
        calls.append((ip, port))
        return _zendure_device(ip=ip, port=port)

    provider = MdnsProvider(verifier=verifier, browser_factory=factory)
    provider.start()
    provider._executor = None
    factory.handler(_candidate())
    first_seen = provider.devices()[0]["last_seen"]
    factory.handler(_candidate())

    assert calls == [("192.168.178.42", 80)]
    assert provider.devices()[0]["last_seen"] >= first_seen


def test_refresh_reverifies_failed_unchanged_candidate_and_promotes_it():
    calls = []

    def verifier(ip, port):
        calls.append((ip, port))
        if len(calls) == 1:
            return None
        return _zendure_device(ip=ip, serial="EOD1NLN9P010611", port=port)

    provider = MdnsProvider(
        verifier=verifier,
        browser_factory=lambda service_type, handler: object(),
    )
    provider.start()
    provider._executor = None
    candidate = _candidate(name="Zendure-solarFlow800Pro2-EOD1NLN9P010611")
    provider.handle_candidate(candidate)
    assert len(provider.ignored_devices()) == 1

    provider.refresh()

    assert calls == [
        ("192.168.178.42", 80),
        ("192.168.178.42", 80),
    ]
    assert provider.ignored_devices() == []
    assert provider.devices()[0]["serial_number"] == "EOD1NLN9P010611"
    provider.stop()


def test_provider_keeps_ignored_candidates_out_of_devices():
    provider = MdnsProvider(browser_factory=lambda service_type, handler: object())
    provider.start()
    provider._executor = None
    provider.handle_candidate(build_candidate(
        "printer._http._tcp.local.", "printer.local.", ["192.168.178.60"],
        80, {}, service_type=HTTP_MDNS_SERVICE_TYPE,
    ))
    assert provider.devices() == []
    assert len(provider.ignored_devices()) == 1
    assert provider.status()["ignored_count"] == 1
    provider.stop()


def test_refresh_recreates_bounded_browsers_without_clearing_store():
    class Browser:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    browsers = []

    def factory(service_type, handler):
        browser = Browser()
        browsers.append(browser)
        return browser

    provider = MdnsProvider(
        verifier=lambda ip, port: _zendure_device(), browser_factory=factory
    )
    provider.start()
    provider._executor = None
    provider.handle_candidate(_candidate())
    first_generation = list(browsers)
    status = provider.refresh()
    assert all(browser.cancelled for browser in first_generation)
    assert len(browsers) == len(MDNS_SERVICE_TYPES) * 2
    assert status["last_refresh"]
    assert len(provider.devices()) == 1
    provider.stop()
