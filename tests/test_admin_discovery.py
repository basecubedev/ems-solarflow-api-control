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


def test_unknown_http_device_is_ignored():
    assert _probe_single({"/properties/report": {"hello": "world"}}) is None


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
