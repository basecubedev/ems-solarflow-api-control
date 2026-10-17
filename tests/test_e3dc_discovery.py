# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin network scan finds an E3/DC by its Modbus magic word.

Only a device that answers ``0xE3DC`` at register 40001 becomes a candidate:
the hardware catalogue is a whitelist, so another Modbus device on port 502,
one in SunSpec mode, or a service that never answers is not offered. The probe
costs one TCP connect per host and gives up at the first transport error, so
a scan of a /24 stays as cheap as the HTTP probes beside it. Every device here
is a loopback fake; nothing reaches the LAN.
"""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest

from admin import discovery
from admin.models import ROLE_GRID_METER
from ems import config as cfg
from ems import e3dc_modbus as modbus

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.integration,
]

ROOT = Path(__file__).resolve().parents[1]
_PROBE_PATH = ROOT / "scripts" / "e3dc_modbus_probe.py"
_spec = importlib.util.spec_from_file_location("e3dc_modbus_probe_for_discovery", _PROBE_PATH)
probe_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_script)

TIMEOUT_S = 1.0


def _loopback(**options):
    words = probe_script.build_self_test_words(
        **{key: options.pop(key) for key in ("address_offset", "magic") if key in options}
    )
    return probe_script._LoopbackE3dcServer(words, **options)


class _SilentService:
    """Accepts one connection, keeps every byte it is sent, never answers."""

    def __init__(self):
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.received = bytearray()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        connection, _address = self._listener.accept()
        with connection:
            while True:
                try:
                    chunk = connection.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                self.received.extend(chunk)

    def finish(self):
        self._thread.join(timeout=5)
        self._listener.close()
        return not self._thread.is_alive()


def _closed_port():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()
    return port


def test_an_e3dc_is_a_grid_meter_candidate_on_its_modbus_endpoint():
    with _loopback() as device:
        found = discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=device.port)

    assert found is not None
    assert found.api_family == "e3dc_modbus"
    assert found.device_type == "e3dc_modbus"
    assert found.role_suggestion == ROLE_GRID_METER
    assert (found.ip, found.port, found.protocol) == ("127.0.0.1", device.port, "modbus_tcp")
    assert found.model == "S10 E AIO"
    assert found.display_name == "E3/DC S10 E AIO"
    assert found.firmware == "S10_2021_04"
    assert found.config_ready is True
    assert found.missing_config_fields == []


def test_the_candidate_names_the_system_by_its_serial():
    with _loopback() as device:
        found = discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=device.port)

    assert found.serial_number == "S10-12345678912"
    assert found.id == "e3dc_modbus:S10-12345678912"


@pytest.mark.parametrize("address_offset", modbus.ADDRESS_OFFSET_CANDIDATES)
def test_the_scan_accepts_every_offset_the_ems_accepts(address_offset):
    """The scan and the control loop share one detection, so a system the EMS
    reads is one the scan offers."""

    with _loopback(address_offset=address_offset) as device:
        found = discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=device.port)

    assert found is not None
    assert found.model == "S10 E AIO"


@pytest.mark.parametrize("magic", [modbus.SUNSPEC_FIRST_WORD, 0x1234, 0])
def test_a_modbus_device_without_the_magic_word_is_not_offered(magic):
    with _loopback(magic=magic) as device:
        assert discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=device.port) is None


def test_a_device_that_refuses_every_read_is_not_offered():
    with _loopback(unit_id=7) as device:
        assert discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=device.port) is None


def test_a_refused_identification_still_offers_the_e3dc_under_a_generic_name():
    """The magic word is the evidence; model and serial are only labels."""

    words = probe_script.build_self_test_words()
    model = next(field for field in modbus.IDENTIFICATION_FIELDS if field.key == "model")
    model_register = model.address - 1
    with probe_script._LoopbackE3dcServer(words, unsupported={model_register}) as device:
        found = discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=device.port)

    assert found is not None
    assert found.display_name == "E3/DC storage system"
    assert found.model is None
    assert found.serial_number is None
    assert found.config_ready is True


def test_a_closed_port_is_no_candidate():
    assert discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S, port=_closed_port()) is None


def test_a_service_that_never_answers_costs_one_request():
    """A port-502 service that is no Modbus device would otherwise hold a scan
    worker for one timeout per address offset."""

    service = _SilentService()
    found = discovery.probe_e3dc_modbus(None, "127.0.0.1", 0.3, port=service.port)

    assert service.finish(), "the probe left its connection open"
    assert found is None
    assert len(service.received) == 12


@pytest.mark.parametrize("ip", ["127.0.0.1:8080", "not a host", ""])
def test_an_unusable_address_is_no_candidate_and_no_error(ip):
    assert discovery.probe_e3dc_modbus(None, ip, TIMEOUT_S, port=_closed_port()) is None


def test_the_network_scan_runs_the_e3dc_probe_after_the_http_probes():
    assert discovery.PROBES[-1] is discovery.probe_e3dc_modbus
    assert discovery.E3DC_SCAN_PORT == 502


def test_the_scan_probes_the_endpoint_the_config_defaults_name(monkeypatch):
    """A candidate carries no unit ID, so the EMS reads it at the config
    default; the scan must have found it there."""

    opened = []

    class _Client:
        def __init__(self, host, port, unit_id, timeout):
            opened.append((port, unit_id))
            self.port = port

        def connect(self):
            raise modbus.ModbusError("refused")

        def close(self):
            pass

    monkeypatch.setattr(modbus, "ReadOnlyModbusTcpClient", _Client)
    assert discovery.probe_e3dc_modbus(None, "127.0.0.1", TIMEOUT_S) is None
    assert opened == [(cfg.E3DC_MODBUS_DEFAULT_PORT, cfg.E3DC_MODBUS_DEFAULT_UNIT_ID)]


def test_a_network_scan_finds_the_e3dc(monkeypatch):
    with _loopback() as device:
        monkeypatch.setattr(discovery, "E3DC_SCAN_PORT", device.port)
        devices, errors = discovery.scan_network("127.0.0.1/32", timeout_ms=1000, max_workers=1)

    assert errors == []
    assert [(item.api_family, item.ip, item.port) for item in devices] == [
        ("e3dc_modbus", "127.0.0.1", device.port)
    ]


def test_the_magic_word_is_searched_in_one_place():
    """The scan and the control loop ask one detection, so they cannot drift."""

    for path in ("admin/discovery.py", "ems/e3dc_runtime.py"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "MAGIC_ADDRESS" not in text, path
        assert "ADDRESS_OFFSET_CANDIDATES" not in text, path
