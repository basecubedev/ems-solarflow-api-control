# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin network scan finds an OpenDTU and offers each inverter it reads.

An OpenDTU answers ``GET /api/livedata/status`` without a login while its
read-only access is on, and that answer lists every inverter by serial and name
beside the OpenDTU's own ``total``. Each listed inverter becomes a read-only
candidate the EMS can read as an ``opendtu_http`` device: never an inverter
role, which the Admin reserves for a Zendure the EMS controls over its local
API. Every device here is a loopback fake or a fake session; nothing reaches
the LAN.
"""

import json
import re
from pathlib import Path

import pytest

from admin import discovery
from admin.connection_capability import payload_output_control
from admin.discovery_unify import build_unified_devices
from admin.models import ROLE_READ_ONLY_DEVICE
from admin.observation_identity import observation_components, stamp_observations
from ems import opendtu_http
from ems.config import opendtu_http_device_settings
from tests.test_admin_discovery import ZENDURE_REPORT, _probe_single

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.integration,
]

TSUN = {
    "serial": "116180000001",
    "name": "TSUN TSOL-M800(DE)",
    "order": 0,
    "data_age": 22,
    "poll_enabled": True,
    "reachable": True,
    "producing": True,
    "limit_relative": 100,
    "limit_absolute": 600,
    "radio_stats": {"tx_request": 9965, "rx_success": 6770, "rssi": -80},
}
HOYMILES = {**TSUN, "serial": "114180000002", "name": "Garage", "order": 1}
TOTAL = {"Power": {"v": 0, "u": "W", "d": 0}, "YieldDay": {"v": 0, "u": "Wh", "d": 0}, "YieldTotal": {"v": 1.5, "u": "kWh", "d": 3}}
HINTS = {"time_sync": False, "radio_problem": False, "default_password": True, "pin_mapping_issue": False}


def _livedata(*inverters):
    return {"inverters": list(inverters), "total": TOTAL, "hints": HINTS}


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class _Session:
    """Answers the livedata path at one host; every other request is a 404."""

    def __init__(self, answer):
        self.answer = answer
        self.urls = []

    def get(self, url, timeout=None, headers=None, **_):
        self.urls.append(url)
        if url == f"http://127.0.0.1{opendtu_http.LIVEDATA_PATH}":
            return self.answer
        return _Response(404)


def test_the_livedata_answer_lists_each_inverter_by_serial_and_name():
    assert opendtu_http.listed_inverters(_livedata(TSUN, HOYMILES)) == (
        ("116180000001", "TSUN TSOL-M800(DE)"),
        ("114180000002", "Garage"),
    )


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {},
        {"inverters": [TSUN]},
        {"inverters": [TSUN], "total": {}},
        {"inverters": {"0": TSUN}, "total": TOTAL},
        ZENDURE_REPORT,
        {"em:0": {"total_act_power": 120.0}},
    ],
)
def test_an_answer_no_opendtu_gives_is_not_read_as_one(answer):
    assert opendtu_http.listed_inverters(answer) is None


def test_an_entry_without_a_serial_names_no_inverter():
    nameless = [{"name": "no serial"}, {**TSUN, "serial": ""}, {**TSUN, "serial": True}, {**TSUN, "serial": None}, "x"]

    assert opendtu_http.listed_inverters(_livedata(*nameless)) == ()
    assert opendtu_http.listed_inverters(_livedata({**TSUN, "name": None})) == (("116180000001", ""),)


def test_the_scan_offers_each_inverter_an_opendtu_reads_as_a_read_only_device():
    found = _probe_single({opendtu_http.LIVEDATA_PATH: _livedata(TSUN, HOYMILES)})

    assert isinstance(found, tuple)
    assert [(item.serial_number, item.display_name) for item in found] == [
        ("116180000001", "TSUN TSOL-M800(DE)"),
        ("114180000002", "Garage"),
    ]
    for item in found:
        assert (item.api_family, item.device_type, item.role_suggestion) == ("opendtu_http", "opendtu_http", ROLE_READ_ONLY_DEVICE)
        assert (item.port, item.config_ready, item.missing_config_fields) == (80, True, [])
    assert found[0].id != found[1].id


def test_a_found_inverter_is_never_offered_for_control():
    """The inverter role means a Zendure the EMS controls over its local API."""

    (found,) = _probe_single({opendtu_http.LIVEDATA_PATH: _livedata(TSUN)})

    assert found.role_suggestion != discovery.ROLE_INVERTER
    assert payload_output_control(found.to_dict()) is None


def test_every_offered_inverter_is_an_entry_the_ems_accepts():
    """What the scan offers is what the Devices page writes: name, address, serial."""

    for found in _probe_single({opendtu_http.LIVEDATA_PATH: _livedata(TSUN, HOYMILES)}):
        settings = opendtu_http_device_settings({"name": "OpenDTU", "ip": "192.0.2.80", "serial": found.serial_number})
        assert settings["serial"] == found.serial_number


@pytest.mark.parametrize("serial", ["116", "1161-8000", "1" * 21, "ünicode"])
def test_an_inverter_whose_serial_the_ems_refuses_is_left_out(serial):
    found = _probe_single({opendtu_http.LIVEDATA_PATH: _livedata({**TSUN, "serial": serial}, HOYMILES)})

    assert [item.serial_number for item in found] == ["114180000002"]


@pytest.mark.parametrize("answer", [_livedata(), _livedata({**TSUN, "serial": "x"})])
def test_an_opendtu_that_reads_no_usable_inverter_offers_nothing(answer):
    assert _probe_single({opendtu_http.LIVEDATA_PATH: answer}) is None


def test_an_opendtu_that_asks_for_a_login_offers_nothing():
    """Without read-only access the EMS could not read it either."""

    details = []

    assert discovery.probe_opendtu(_Session(_Response(401)), "127.0.0.1", 0.5, failure_details=details) is None
    assert details == [f"HTTP 401 from {opendtu_http.LIVEDATA_PATH}"]


def test_another_json_answer_on_the_path_is_named_as_no_opendtu():
    details = []

    assert discovery.probe_opendtu(_Session(_Response(200, {"status": "ok"})), "127.0.0.1", 0.5, failure_details=details) is None
    assert details == ["OpenDTU API response lists no inverters"]


def test_a_zendure_is_still_found_as_a_zendure():
    found = _probe_single({"/properties/report": ZENDURE_REPORT, opendtu_http.LIVEDATA_PATH: _livedata(TSUN)})

    assert not isinstance(found, tuple) and found.api_family != "opendtu_http"


def test_the_opendtu_probe_runs_with_the_http_probes_before_the_modbus_ones():
    assert discovery.probe_opendtu in discovery.PROBES
    assert discovery.PROBES.index(discovery.probe_opendtu) < discovery.PROBES.index(discovery.probe_e3dc_modbus)


def test_a_network_scan_lists_every_inverter_and_counts_them():
    progress = []

    devices, errors = discovery.scan_network(
        "127.0.0.1/32", timeout_ms=200, max_workers=1,
        session=_Session(_Response(200, _livedata(TSUN, HOYMILES))), progress_callback=progress.append,
    )

    assert errors == []
    assert [(item.api_family, item.ip, item.serial_number) for item in devices] == [
        ("opendtu_http", "127.0.0.1", "116180000001"),
        ("opendtu_http", "127.0.0.1", "114180000002"),
    ]
    assert progress[-1]["found_devices"] == 2


def test_the_scan_record_carries_each_inverter_once():
    found = _probe_single({opendtu_http.LIVEDATA_PATH: _livedata(TSUN, HOYMILES)})
    records = json.loads(json.dumps([item.to_dict() for item in found]))

    assert [record["id"] for record in records] == ["opendtu_http:116180000001", "opendtu_http:114180000002"]
    assert {record["role_suggestion"] for record in records} == {"read_only_device"}


KEY = b"k" * 32
ROOT = Path(__file__).resolve().parents[1]


def _records(*inverters):
    return [item.to_dict() for item in _probe_single({opendtu_http.LIVEDATA_PATH: _livedata(*inverters)})]


def test_each_inverter_of_one_opendtu_is_an_observation_of_its_own():
    """The page keys what it found by observation; one key per OpenDTU would keep one inverter."""

    found = _records(TSUN, HOYMILES)
    records = stamp_observations([dict(record) for record in found], key=KEY)

    assert len({record["observation_id"] for record in records}) == 2
    assert stamp_observations([dict(found[0])], key=KEY)[0]["observation_id"] == records[0]["observation_id"]


def test_a_device_one_endpoint_reads_alone_keeps_its_observation_by_address():
    zendure = {"ip": "192.0.2.10", "port": 80, "device_type": "zendure_solarflow_800_pro2", "serial_number": "SN1"}

    assert "serial" not in observation_components(zendure)
    assert observation_components({**zendure, "serial_number": "SN2"}) == observation_components(zendure)


def test_setup_lists_a_found_opendtu_inverter_as_read_only():
    devices = build_unified_devices({"local_api": _records(TSUN, HOYMILES)}, ["local_api"])

    assert sorted((device["serial_number"], device["read_only"]) for device in devices) == [
        ("114180000002", True),
        ("116180000001", True),
    ]


def test_the_page_names_the_read_only_role_as_the_server_does():
    source = (ROOT / "frontend/admin-modern/src/lib/discovery.js").read_text(encoding="utf-8")

    assert re.search(r'export const READ_ONLY_ROLE = "([^"]+)";', source).group(1) == ROLE_READ_ONLY_DEVICE
