# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery model and scanner tests (local fake HTTP servers, no real network)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admin import discovery
from admin.discovery import (
    CidrValidationError,
    clamp_max_workers,
    clamp_timeout_ms,
    probe_host,
    scan_network,
    validate_cidr,
)

pytestmark = pytest.mark.simulation


ZENDURE_REPORT = {
    "sn": "SN123456",
    "properties": {
        "electricLevel": 62,
        "solarInputPower": 210,
        "outputHomePower": 180,
        "outputLimit": 800,
        "acMode": 2,
        "socLimit": 0,
    },
}
ZENDURE_REPORT_WITH_PRODUCT = {
    "sn": "SN999",
    "product": "solarFlow800Pro2",
    "properties": {"electricLevel": 70, "solarInputPower": 300, "outputLimit": 800},
}
ZENDURE_REPORT_NO_SERIAL = {
    "properties": {"electricLevel": 50, "solarInputPower": 100, "outputLimit": 600}
}
SHELLY_GEN2_STATUS = {"em:0": {"total_act_power": -320.5}, "sys": {"mac": "AABBCCDDEEFF"}}
SHELLY_3EM_GEN1_STATUS = {
    "total_power": -145.2,
    "emeters": [{"power": -60.0}, {"power": -40.0}, {"power": -45.2}],
    "mac": "112233445566",
}
ECOTRACKER_JSON = {"power": 133.7, "id": "eco-1"}
ZENDURE_3CT_REPORT = {
    "timestamp": 1783163312,
    "messageId": 12,
    "deviceId": "rhRkw909",
    "a_aprt_power": 0,
    "b_aprt_power": 0,
    "c_aprt_power": -798,
    "total_power": -798,
}


class _FakeDeviceHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        payload = self.routes.get(self.path)
        if payload is None:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return


def _make_fake_device(routes):
    handler = type("Handler", (_FakeDeviceHandler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _probe_single(routes):
    server = _make_fake_device(routes)
    try:
        import requests

        ip = f"127.0.0.1:{server.server_address[1]}"
        session = requests.Session()
        # probe_host builds http://<ip><path>; embedding the port in ``ip`` lets
        # the ephemeral fake server stand in for a real device on port 80.
        return probe_host(session, ip, timeout_s=1.0)
    finally:
        server.shutdown()
        server.server_close()


# --- CIDR validation -----------------------------------------------------

def test_validate_cidr_accepts_private_24():
    network = validate_cidr("192.168.178.0/24")
    assert str(network) == "192.168.178.0/24"


def test_validate_cidr_accepts_loopback_and_link_local():
    assert validate_cidr("127.0.0.0/30")
    assert validate_cidr("169.254.0.0/24")


def test_validate_cidr_rejects_public_range():
    with pytest.raises(CidrValidationError):
        validate_cidr("8.8.8.0/24")


def test_validate_cidr_rejects_too_broad():
    with pytest.raises(CidrValidationError):
        validate_cidr("10.0.0.0/16")


def test_validate_cidr_rejects_empty_or_garbage():
    with pytest.raises(CidrValidationError):
        validate_cidr("")
    with pytest.raises(CidrValidationError):
        validate_cidr("not-a-cidr")


# --- clamping ------------------------------------------------------------

def test_clamp_timeout_ms_bounds():
    assert clamp_timeout_ms(50) == discovery.TIMEOUT_MS_MIN
    assert clamp_timeout_ms(999999) == discovery.TIMEOUT_MS_MAX
    assert clamp_timeout_ms("nonsense") == discovery.TIMEOUT_MS_DEFAULT
    assert clamp_timeout_ms(600) == 600


def test_clamp_max_workers_bounds():
    assert clamp_max_workers(0) == discovery.MAX_WORKERS_MIN
    assert clamp_max_workers(9999) == discovery.MAX_WORKERS_MAX
    assert clamp_max_workers(None) == discovery.MAX_WORKERS_DEFAULT


# --- probe recognition ---------------------------------------------------

def test_zendure_report_detected():
    device = _probe_single({"/properties/report": ZENDURE_REPORT})
    assert device is not None
    assert device.api_family == "zendure_local_http"
    assert device.role_suggestion == "inverter"
    assert device.serial_number == "SN123456"
    assert device.config_ready is True
    assert device.id == "zendure_local_http:SN123456"


def test_zendure_product_sets_type_and_model():
    device = _probe_single({"/properties/report": ZENDURE_REPORT_WITH_PRODUCT})
    assert device is not None
    assert device.model == "solarFlow800Pro2"
    assert device.device_type == "zendure_solarflow800pro2"
    assert device.display_name == "Zendure solarFlow800Pro2"


def test_zendure_without_product_falls_back_to_unknown_type():
    device = _probe_single({"/properties/report": ZENDURE_REPORT})
    assert device is not None
    assert device.device_type == "zendure_solarflow_unknown"
    assert device.model is None


def test_zendure_without_serial_not_config_ready():
    device = _probe_single({"/properties/report": ZENDURE_REPORT_NO_SERIAL})
    assert device is not None
    assert device.serial_number is None
    assert device.config_ready is False
    assert "serial_number" in device.missing_config_fields
    assert device.id.startswith("zendure_local_http:127.0.0.1")


def test_shelly_gen2_detected_as_grid_meter():
    device = _probe_single({"/rpc/Shelly.GetStatus": SHELLY_GEN2_STATUS})
    assert device is not None
    assert device.api_family == "shelly_gen2"
    assert device.role_suggestion == "grid_meter"


def test_shelly_3em_gen1_detected_as_grid_meter():
    device = _probe_single({"/status": SHELLY_3EM_GEN1_STATUS})
    assert device is not None
    assert device.api_family == "shelly_3em_gen1"
    assert device.role_suggestion == "grid_meter"


def test_ecotracker_detected_as_grid_meter():
    device = _probe_single({"/v1/json": ECOTRACKER_JSON})
    assert device is not None
    assert device.api_family == "ecotracker"
    assert device.role_suggestion == "grid_meter"


def test_zendure_3ct_report_detected_as_generic_http_grid_meter():
    # The real 3CT sample carries the per-clamp apparent powers, but a D0 exposes
    # the same fields, so they are NOT model proof. Numeric total_power alone
    # makes it a config-ready generic local-HTTP grid meter.
    device = _probe_single({"/properties/report": ZENDURE_3CT_REPORT})
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.role_suggestion == "grid_meter"
    assert device.serial_number == "rhRkw909"
    assert device.config_ready is True
    assert device.missing_config_fields == []


def test_zendure_explicit_3ct_model_enriches_label_but_keeps_generic_family():
    device = _probe_single(
        {"/properties/report": {"total_power": 321, "sn": "3CTSN", "product": "SmartMeter3CT"}}
    )
    assert device is not None
    # The config type stays generic; the model only enriches the display detail.
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_smartmeter_3ct"
    assert device.role_suggestion == "grid_meter"
    assert device.serial_number == "3CTSN"
    assert device.config_ready is True
    assert "3CT" in device.display_name


@pytest.mark.parametrize(
    "product",
    ["smart_meter_3ct", "smart-meter-3ct", "Smart Meter 3CT"],
)
def test_zendure_3ct_model_evidence_is_case_and_separator_insensitive(product):
    device = _probe_single(
        {"/properties/report": {"total_power": 5, "sn": "S", "productName": product}}
    )
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_smartmeter_3ct"


@pytest.mark.parametrize(
    "product",
    ["Zendure SmartMeter 3CT", "ZENDURE  smartmeter  3CT"],
)
def test_zendure_prefixed_3ct_model_is_recognized(product):
    device = _probe_single(
        {"/properties/report": {"total_power": 5, "sn": "S", "model": product}}
    )
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_smartmeter_3ct"


@pytest.mark.parametrize(
    "product",
    ["not3ct", "device3ctcompatible", "abc3ctxyz", "3ct-emulator"],
)
def test_zendure_3ct_substring_false_positives_stay_generic(product):
    # A model string that merely contains "3ct" is not a supported identifier;
    # the device is still a config-ready generic HTTP grid meter, not a 3CT.
    device = _probe_single(
        {"/properties/report": {"total_power": 321, "sn": "SN", "product": product}}
    )
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_grid_meter_http"
    assert device.config_ready is True
    assert "3CT" not in device.display_name


def test_zendure_flat_total_power_without_model_is_config_ready_generic_meter():
    # A flat total_power with no model evidence (e.g. a D0 reader) is a
    # config-ready generic local-HTTP grid meter; it is never claimed as a 3CT.
    device = _probe_single({"/properties/report": {"total_power": 321, "sn": "D0SN"}})
    assert device is not None
    assert device.role_suggestion == "grid_meter"
    assert device.serial_number == "D0SN"
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_grid_meter_http"
    assert device.config_ready is True
    assert device.missing_config_fields == []
    assert "3CT" not in device.display_name
    assert "Smart Meter" not in device.display_name


def test_zendure_complete_clamp_triplet_is_not_3ct_evidence():
    # The D0 sample also carries an all-zero clamp triplet, so the triplet must
    # never be treated as 3CT proof.
    device = _probe_single(
        {
            "/properties/report": {
                "total_power": 300,
                "a_aprt_power": 100,
                "b_aprt_power": 100,
                "c_aprt_power": 100,
            }
        }
    )
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_grid_meter_http"


@pytest.mark.parametrize(
    "payload",
    [{"total_power": "321"}, {"total_power": True}, {}, {"total_power": None}],
)
def test_zendure_grid_meter_rejects_non_numeric_total_power(payload):
    assert _probe_single({"/properties/report": payload}) is None


def test_zendure_inverter_report_not_classified_as_3ct_meter():
    # A nested ``properties`` payload is the inverter; it must keep its inverter
    # role even though the 3CT meter shares the /properties/report path.
    device = _probe_single({"/properties/report": ZENDURE_REPORT})
    assert device.api_family == "zendure_local_http"
    assert device.role_suggestion == "inverter"


def test_unknown_http_device_is_ignored():
    assert _probe_single({"/properties/report": {"hello": "world"}}) is None


# --- mDNS endpoint verification ------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _RecordingSession:
    """Minimal requests.Session stand-in that records requested URLs."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code
        self.requests = []

    def get(self, url, timeout=None, headers=None):
        self.requests.append(url)
        return _FakeResponse(self._payload, self._status_code)

    def close(self):
        pass


def _verify_mdns(payload, ip="192.168.1.80", port=8080, model_hint=None):
    session = _RecordingSession(payload)
    device = discovery.verify_zendure_endpoint(
        ip, port, session=session, model_hint=model_hint
    )
    return device, session


def test_verify_mdns_zendure_inverter_candidate():
    device, _ = _verify_mdns(
        {
            "product": "solarFlow800Pro",
            "sn": "INV123",
            "properties": {"electricLevel": 50, "outputHomePower": 100},
        }
    )
    assert device is not None
    assert device.api_family == "zendure_local_http"
    assert device.role_suggestion == "inverter"
    assert device.serial_number == "INV123"
    assert device.ip == "192.168.1.80"
    assert device.port == 8080


def test_verify_mdns_explicit_3ct_candidate():
    device, _ = _verify_mdns(
        {"product": "SmartMeter3CT", "sn": "3CT123", "total_power": 321}
    )
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_smartmeter_3ct"
    assert "3CT" in device.display_name
    assert device.config_ready is True
    assert device.ip == "192.168.1.80"
    assert device.port == 8080


def test_verify_mdns_clamp_triplet_is_generic_not_3ct():
    device, _ = _verify_mdns(
        {
            "sn": "3CT123",
            "total_power": 300,
            "a_aprt_power": 100,
            "b_aprt_power": 90,
            "c_aprt_power": 110,
        }
    )
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_grid_meter_http"


def test_verify_mdns_flat_grid_meter_is_config_ready_generic():
    device, _ = _verify_mdns({"sn": "D0SN", "total_power": 321})
    assert device is not None
    assert device.device_type == "zendure_grid_meter_http"
    assert device.display_name == "Zendure Grid Meter via local HTTP"
    assert device.config_ready is True
    assert device.role_suggestion == "grid_meter"
    assert device.serial_number == "D0SN"
    assert device.ip == "192.168.1.80"
    assert device.port == 8080
    # A flat D0-like reading must never be pre-claimed as another type.
    assert "3CT" not in device.display_name
    assert device.api_family == "zendure_grid_meter_http"


@pytest.mark.parametrize(
    "hint",
    ["SmartMeter3CT", "Smart Meter 3CT", "smart_meter_3ct", "smart-meter-3ct",
     "Zendure SmartMeter 3CT"],
)
def test_verify_mdns_accepted_model_hint_enriches_label(hint):
    device, _ = _verify_mdns({"sn": "S", "total_power": 5}, model_hint=hint)
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_smartmeter_3ct"


@pytest.mark.parametrize(
    "hint",
    ["not3ct", "abc3ctxyz", "device3ctcompatible", "3ct-emulator"],
)
def test_verify_mdns_rejected_model_hint_stays_generic(hint):
    device, _ = _verify_mdns({"sn": "S", "total_power": 5}, model_hint=hint)
    assert device is not None
    assert device.api_family == "zendure_grid_meter_http"
    assert device.device_type == "zendure_grid_meter_http"


@pytest.mark.parametrize(
    "payload",
    [{}, {"total_power": True}, {"total_power": None}, {"total_power": {}}],
)
def test_verify_mdns_invalid_grid_meter_payload_is_rejected(payload):
    device, _ = _verify_mdns(payload)
    assert device is None


def test_verify_mdns_uses_advertised_port():
    device, session = _verify_mdns({"sn": "D0SN", "total_power": 321})
    assert session.requests == ["http://192.168.1.80:8080/properties/report"]
    assert device.port == 8080


def test_verify_mdns_requests_report_only_once():
    _, session = _verify_mdns({"sn": "D0SN", "total_power": 321})
    assert len(session.requests) == 1


# --- scan resilience -----------------------------------------------------

def test_scan_rejects_invalid_cidr_before_network():
    with pytest.raises(CidrValidationError):
        scan_network("1.1.1.0/24")


def test_unreachable_hosts_do_not_fail_scan():
    # A tiny loopback /30 with nothing listening: every probe fails, but the scan
    # must finish cleanly with an empty device list instead of raising.
    devices, errors = scan_network("127.0.0.0/30", timeout_ms=200, max_workers=4)
    assert devices == []
    assert isinstance(errors, list)


def test_scan_network_reports_progress_callback_per_host():
    # A /30 has two probeable hosts; the callback must fire once per finished
    # host with a monotonically rising checked count and a stable total.
    updates = []
    scan_network(
        "127.0.0.0/30", timeout_ms=200, max_workers=4,
        progress_callback=updates.append,
    )
    assert len(updates) == 2
    assert [u["checked_hosts"] for u in updates] == [1, 2]
    assert all(u["total_hosts"] == 2 for u in updates)
    assert all("current_ip" in u for u in updates)
