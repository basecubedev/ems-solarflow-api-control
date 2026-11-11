# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The network scan and mDNS find a SunSpec meter, a HomeWizard P1 Meter and a poweropti.

A SunSpec meter is found over Modbus TCP at the unit IDs meters are commonly
served at, and the candidate carries the unit it answered at into the setup.
A HomeWizard P1 Meter's TLS handshake gets more time than the scan's connect,
and its own mDNS service names it. A poweropti is known only by its mDNS name,
since its local API answers nothing without the key, so it stays unverified
and needs that key. Every device here is a loopback fake or a stub; nothing
reaches the LAN.
"""

import json
import socket
import struct
import threading

import pytest

from admin import discovery, mdns
from admin.config_preview import ConfigPreviewGenerator
from admin.models import ROLE_GRID_METER, DiscoveredDevice
from tests.test_admin_config_preview import _device, _ReleaseManager
from tests.test_sunspec_grid_meter import registers

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.integration,
]

TIMEOUT_S = 1.0


def _text(value, count):
    raw = value.encode("latin-1").ljust(2 * count, b"\0")
    return list(struct.unpack(f">{count}H", raw))


def fronius_meter():
    regs = registers(213, -678.5)
    for offset, value in ((4, _text("Fronius", 16)), (20, _text("Smart Meter 63A", 16)), (52, _text("12345678", 16))):
        regs.update({40000 + offset + index: word for index, word in enumerate(value)})
    return regs


class LoopbackModbus:
    """Serves holding registers per unit ID over Modbus TCP; an unknown unit answers exception 0x0B."""

    def __init__(self, units):
        self.units = units
        self.requests = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _address = self._listener.accept()
            except OSError:
                return
            with conn:
                while (head := self._read(conn, 12)) is not None:
                    transaction, _protocol, _length, unit, function, address, count = struct.unpack(">HHHBBHH", head)
                    self.requests.append((unit, function, address, count))
                    regs = self.units.get(unit)
                    if regs is None:
                        pdu = struct.pack(">BB", function | 0x80, 0x0B)
                    else:
                        values = [regs.get(item, 0) for item in range(address, address + count)]
                        pdu = struct.pack(f">BB{count}H", function, 2 * count, *values)
                    conn.sendall(struct.pack(">HHHB", transaction, 0, len(pdu) + 1, unit) + pdu)

    @staticmethod
    def _read(conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def close(self):
        self._listener.close()


@pytest.fixture()
def modbus():
    servers = []

    def start(units):
        server = LoopbackModbus(units)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


# --- SunSpec over Modbus TCP ---------------------------------------------------------


def test_a_fronius_meter_is_found_at_unit_200_and_named_by_its_common_model(modbus):
    inverter = {40000: 0x5375, 40001: 0x6E53, 40002: 1, 40003: 66, 40070: 0xFFFF}
    server = modbus({1: inverter, 200: fronius_meter()})

    device = discovery.probe_sunspec_modbus(None, "127.0.0.1", TIMEOUT_S, port=server.port)

    assert (device.api_family, device.role_suggestion, device.unit_id, device.port) == ("sunspec_modbus", ROLE_GRID_METER, 200, server.port)
    assert (device.display_name, device.model, device.serial_number) == ("Fronius Smart Meter 63A", "Smart Meter 63A", "12345678")
    assert device.config_ready and device.protocol == "modbus_tcp"
    assert {function for _unit, function, _address, _count in server.requests} == {3}


def test_a_meter_at_unit_1_is_found_without_trying_the_others(modbus):
    server = modbus({1: registers(203, 450, 0)})

    device = discovery.probe_sunspec_modbus(None, "127.0.0.1", TIMEOUT_S, port=server.port)

    assert (device.unit_id, device.display_name, device.model) == (1, "SunSpec meter", "SunSpec model 203")
    assert {unit for unit, *_rest in server.requests} == {1}


@pytest.mark.parametrize("units", [{}, {1: {40000: 0x5375, 40001: 0x6E53, 40002: 1, 40003: 66, 40070: 0xFFFF}}, {1: {40000: 0xE3DC}}])
def test_a_device_without_a_sunspec_meter_is_no_candidate(modbus, units):
    server = modbus(units)

    assert discovery.probe_sunspec_modbus(None, "127.0.0.1", TIMEOUT_S, port=server.port) is None


def test_a_closed_port_is_no_candidate():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    assert discovery.probe_sunspec_modbus(None, "127.0.0.1", TIMEOUT_S, port=port) is None


def test_the_sunspec_probe_runs_last_after_the_e3dc_one():
    assert discovery.PROBES[-1] is discovery.probe_sunspec_modbus
    assert discovery.PROBES[-2] is discovery.probe_e3dc_modbus


def test_a_found_sunspec_meter_reaches_the_config_with_its_unit():
    found = DiscoveredDevice(
        ip="192.168.1.64", api_family="sunspec_modbus", device_type="sunspec_modbus",
        role_suggestion=ROLE_GRID_METER, port=502, protocol="modbus_tcp",
        display_name="Fronius Smart Meter 63A", config_ready=True, unit_id=200,
    ).to_dict()
    meter = {**found, "config_name": "grid_meter", "role": "grid_meter", "enabled": True}

    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)

    assert result["ready"] is True, result["validation"]
    assert result["config"]["grid_meter"] == {"type": "sunspec_modbus", "ip": "192.168.1.64", "port": 502, "unit_id": 200}


# --- HomeWizard P1 Meter --------------------------------------------------------------


def test_the_homewizard_handshake_gets_more_time_than_the_scan_connect(monkeypatch):
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    built = []

    class Api:
        def __init__(self, host, *, port, timeout, context):
            built.append((host, port, timeout))

        def identify(self):
            return "0a1b2c3d4e5f"

        def close(self):
            pass

    monkeypatch.setattr(discovery, "HomeWizardP1Api", Api)
    try:
        device = discovery.probe_homewizard_p1(None, "127.0.0.1", 0.6, port=port)
    finally:
        listener.close()

    assert built == [("127.0.0.1", port, discovery.HOMEWIZARD_HANDSHAKE_TIMEOUT_S)]
    assert device.serial_number == "0a1b2c3d4e5f"


def test_a_host_without_the_https_port_never_starts_a_handshake(monkeypatch):
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    monkeypatch.setattr(discovery, "HomeWizardP1Api", lambda *args, **kwargs: pytest.fail("no handshake without a port"))

    assert discovery.probe_homewizard_p1(None, "127.0.0.1", 0.6, port=port) is None


@pytest.mark.parametrize("service_type, advertised_port", [("_homewizard._tcp.local.", 443), ("_hwenergy._tcp.local.", 80)])
def test_a_homewizard_service_is_verified_by_the_certificate_on_port_443(monkeypatch, service_type, advertised_port):
    asked = []

    def probe(session, ip, timeout_s, port):
        asked.append((ip, port))
        return DiscoveredDevice(
            ip=ip, api_family="homewizard_p1", device_type="homewizard_p1", role_suggestion=ROLE_GRID_METER,
            port=port, protocol="https", display_name="HomeWizard P1 Meter", serial_number="0a1b2c3d4e5f", config_ready=True,
        )

    monkeypatch.setattr(mdns, "probe_homewizard_p1", probe)
    candidate = mdns.build_candidate(f"p1meter-ABCDEF.{service_type}", "p1meter.local.", ["192.168.1.40"], advertised_port, {}, service_type=service_type)

    entry = mdns.verify_candidate(candidate)

    assert asked == [("192.168.1.40", 443)]
    assert (entry["api_family"], entry["port"], entry["protocol"], entry["verified"]) == ("homewizard_p1", 443, "https", True)


# --- powerfox poweropti ---------------------------------------------------------------


def test_a_poweropti_is_offered_by_its_mdns_name_and_still_needs_its_key():
    candidate = mdns.build_candidate("powerfox-a1b2c3._http._tcp.local.", "poweropti.local.", ["192.168.1.50"], 80, {}, service_type=mdns.HTTP_MDNS_SERVICE_TYPE)

    entry = mdns.verify_candidate(candidate)

    assert (entry["api_family"], entry["role_suggestion"], entry["verified"], entry["usable_for_config"]) == ("powerfox_poweropti", ROLE_GRID_METER, False, False)
    assert entry["missing_config_fields"] == ["grid_meter.token"]


def test_another_http_service_is_no_poweropti():
    candidate = mdns.build_candidate("printer._http._tcp.local.", "printer.local.", ["192.168.1.9"], 80, {}, service_type=mdns.HTTP_MDNS_SERVICE_TYPE)

    assert mdns.verify_candidate(candidate)["reason"] == mdns.UNKNOWN_SERVICE_REASON


def test_a_found_poweropti_is_ready_once_its_key_is_typed():
    found = mdns._powerfox_candidate({"ip": "192.168.1.50"}).to_dict()
    meter = {**found, "config_name": "grid_meter", "role": "grid_meter", "enabled": True}
    generator = ConfigPreviewGenerator(_ReleaseManager())

    assert generator.generate([_device(), meter], 1)["ready"] is False
    result = generator.generate([_device(), meter], 1, {"grid_meter.token": "1a2b3c4d5e6f"})
    assert result["ready"] is True, result["validation"]
    assert result["config"]["grid_meter"] == {"type": "powerfox_poweropti", "ip": "192.168.1.50", "port": 80, "token": "1a2b3c4d5e6f"}
    assert "1a2b3c4d5e6f" not in json.dumps(found)


def test_every_console_turns_a_found_meter_into_the_same_type():
    import re
    from pathlib import Path

    from admin.config_preview import _GRID_TYPES

    source = (Path(__file__).parents[1] / "admin" / "static" / "admin.js").read_text(encoding="utf-8")
    classic = re.search(r"const GRID_METER_FAMILY_TYPES = \{(.*?)\};", source, re.S).group(1)
    assert dict(re.findall(r'([a-z0-9_]+): "([a-z0-9_]+)"', classic)) == _GRID_TYPES


def test_a_scanned_unit_id_does_not_follow_the_meter_to_another_type():
    found = DiscoveredDevice(
        ip="192.168.1.64", api_family="sunspec_modbus", device_type="sunspec_modbus",
        role_suggestion=ROLE_GRID_METER, port=502, protocol="modbus_tcp", config_ready=True, unit_id=200,
    ).to_dict()
    meter = {**found, "config_name": "grid_meter", "role": "grid_meter", "enabled": True, "grid_meter_type": "victron_gx_modbus"}

    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)

    assert result["config"]["grid_meter"] == {"type": "victron_gx_modbus", "ip": "192.168.1.64", "port": 502}
