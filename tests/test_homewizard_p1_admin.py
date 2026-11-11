# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin pairs a HomeWizard P1 Meter and carries its pairing through Maintenance.

The pairing route makes one attempt per request and stores nothing; the token
reaches config.json only through a saved preview, and every browser view shows
that a token exists, never the token.
"""

import json
import threading

import pytest

from admin.homewizard_pairing import PairingRequestError, pair_homewizard_p1, pairing_target
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
)
from admin.secret_policy import CLASS_PRESENCE_FLAG, SCOPE_BROWSER_VIEW, classify_config_key
from admin.server import ScanRegistry, create_server
from ems import homewizard
from tests.admin_auth_helpers import authenticate, raw_request, request

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.config,
    pytest.mark.integration,
]

SERIAL = "0a1b2c3d4e5f"
TOKEN = "0123456789ABCDEF0123456789ABCDEF"
NAME = "local/ems_solarflow#3f9a1c"
PAIRED = {"type": "homewizard_p1", "ip": "192.168.1.40", "serial": SERIAL, "token": TOKEN, "pairing_name": NAME}


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


# --- one pairing attempt -----------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"ip": "192.168.1.40", "pairing_name": NAME}, ("192.168.1.40", 443)),
        ({"ip": " 192.168.1.40 ", "port": 8443, "pairing_name": NAME}, ("192.168.1.40", 8443)),
        ({"ip": "fe80::1", "port": None, "pairing_name": NAME}, ("fe80::1", 443)),
    ],
)
def test_a_pairing_request_names_a_local_address(body, expected):
    assert pairing_target(body) == (*expected, NAME)


def test_the_stored_name_wins_over_whatever_the_browser_sends():
    assert pairing_target({"ip": "192.168.1.40", "pairing_name": "local/ems_solarflow#aaaaaa"}, NAME)[2] == NAME


@pytest.mark.parametrize("drafted", [None, "", "cloud/other", "local/bad!", "local/home-assistant#abc123", "local/ems_solarflow#ABCDEF"])
def test_without_a_stored_name_only_one_of_ours_is_reused_and_anything_else_gets_a_new_one(drafted):
    _ip, _port, name = pairing_target({"ip": "192.168.1.40", "pairing_name": drafted})

    assert name != drafted
    assert name.startswith("local/ems_solarflow#") and len(name) == len("local/ems_solarflow#") + 6
    assert pairing_target({"ip": "192.168.1.40", "pairing_name": name})[2] == name


def test_the_stored_name_is_read_from_config_json(tmp_path):
    from admin.homewizard_pairing import stored_pairing_name

    assert stored_pairing_name(base_dir=str(tmp_path)) is None
    _write_config(tmp_path, dict(PAIRED))
    assert stored_pairing_name(base_dir=str(tmp_path)) == NAME
    _write_config(tmp_path, {**PAIRED, "pairing_name": "no-prefix"})
    assert stored_pairing_name(base_dir=str(tmp_path)) is None


@pytest.mark.parametrize(
    "body, message",
    [
        ({}, "IP address"),
        ({"ip": "p1meter.local"}, "IP address"),
        ({"ip": "8.8.8.8"}, "local network"),
        ({"ip": "192.168.1.40", "port": "443"}, "port"),
        ({"ip": "192.168.1.40", "port": 0}, "port"),
        ({"ip": "192.168.1.40", "port": True}, "port"),
        (None, "IP address"),
    ],
)
def test_a_pairing_request_without_a_local_address_is_refused(body, message):
    with pytest.raises(PairingRequestError, match=message):
        pairing_target(body)


class Api:
    """One answer to ``pair``; records that it was closed."""

    instances = []

    def __init__(self, host, *, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.connected_serial = None
        self.closed = False
        Api.instances.append(self)

    def pair(self, name):
        self.name = name
        answer = Api.answer
        if isinstance(answer, BaseException):
            raise answer
        self.connected_serial = SERIAL
        return answer

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "answer, result",
    [
        (TOKEN, {"state": "paired", "serial": SERIAL, "token": TOKEN, "pairing_name": NAME}),
        (homewizard.HomeWizardError(homewizard.REASON_BUTTON_NOT_PRESSED, "press"), {"state": "waiting"}),
        (
            homewizard.HomeWizardError(homewizard.REASON_NOT_A_P1_METER, "not a P1 Meter"),
            {"state": "failed", "reason": "not_a_p1_meter", "message": "not a P1 Meter"},
        ),
        (
            homewizard.HomeWizardError(homewizard.REASON_NO_ROOM, "no room"),
            {"state": "failed", "reason": "no_room", "message": "no room"},
        ),
    ],
)
def test_one_attempt_answers_waiting_paired_or_failed_and_closes_the_connection(answer, result):
    Api.instances = []
    Api.answer = answer

    assert pair_homewizard_p1("192.168.1.40", 443, NAME, api_factory=Api) == result
    assert [(api.host, api.port, api.name, api.closed) for api in Api.instances] == [("192.168.1.40", 443, NAME, True)]


# --- the route ---------------------------------------------------------------------


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    srv = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=lambda *a, **k: ([], [])))
    asked = []

    def pairer(ip, port, name):
        asked.append((ip, port, name))
        return {"state": "paired", "serial": SERIAL, "token": TOKEN, "pairing_name": name}

    srv.homewizard_pairer = pairer
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base, asked
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_route_pairs_under_the_name_config_json_holds(server, monkeypatch):
    base, asked = server
    monkeypatch.setattr("admin.server.stored_pairing_name", lambda: NAME)

    status, _headers, payload = request(
        f"{base}/api/grid-meter/homewizard-p1/pair",
        method="POST",
        body={"ip": "192.168.1.40", "pairing_name": "local/home-assistant#abc123"},
    )

    assert status == 200
    assert payload == {"state": "paired", "serial": SERIAL, "token": TOKEN, "pairing_name": NAME}
    assert asked == [("192.168.1.40", 443, NAME)]


def test_the_route_refuses_a_public_address_without_contacting_it(server):
    base, asked = server

    status, _headers, payload = request(f"{base}/api/grid-meter/homewizard-p1/pair", method="POST", body={"ip": "8.8.8.8"})

    assert status == 400
    assert "local network" in payload["error"]
    assert asked == []


def test_the_route_needs_a_signed_in_session_with_its_csrf_token(server):
    base, asked = server

    status, _headers, _payload = raw_request(f"{base}/api/grid-meter/homewizard-p1/pair", method="POST", body={"ip": "192.168.1.40"})

    assert status in (401, 403)
    assert asked == []


# --- Maintenance -------------------------------------------------------------------


def _write_config(base_dir, grid_meter):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 800},
                "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}],
                "grid_meter": grid_meter,
            }
        ),
        encoding="utf-8",
    )


def _apply(tmp_path, change):
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    change(draft["grid_meter"])
    return prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))


def test_the_maintenance_view_says_a_token_exists_and_never_shows_it(tmp_path):
    _write_config(tmp_path, dict(PAIRED))

    loaded = load_maintenance_config(base_dir=str(tmp_path))

    grid = loaded["draft"]["grid_meter"]
    assert grid["serial"] == SERIAL
    assert grid["pairing_name"] == NAME
    assert grid["has_token"] is True
    assert TOKEN not in json.dumps(loaded)
    assert classify_config_key("has_token", scope=SCOPE_BROWSER_VIEW) == CLASS_PRESENCE_FLAG


def test_saving_without_retyping_the_token_keeps_it(tmp_path):
    _write_config(tmp_path, dict(PAIRED))

    prepared = _apply(tmp_path, lambda grid: grid.update(ip="192.168.1.41", token=""))

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["grid_meter"] == {**PAIRED, "ip": "192.168.1.41"}


def test_a_new_pairing_replaces_serial_and_token(tmp_path):
    _write_config(tmp_path, dict(PAIRED))

    prepared = _apply(tmp_path, lambda grid: grid.update(serial="ffeeddccbbaa", token="F" * 32))

    assert prepared["status"] == "ok", prepared
    grid = json.loads(prepared["payload"])["grid_meter"]
    assert (grid["serial"], grid["token"], grid["pairing_name"]) == ("ffeeddccbbaa", "F" * 32, NAME)


def test_switching_to_another_meter_drops_the_pairing(tmp_path):
    _write_config(tmp_path, dict(PAIRED))

    prepared = _apply(tmp_path, lambda grid: grid.update(type="shelly"))

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["grid_meter"] == {"type": "shelly", "ip": "192.168.1.40"}


def test_a_pairing_name_of_another_form_is_refused_as_the_ems_would_refuse_it(tmp_path):
    _write_config(tmp_path, dict(PAIRED))

    prepared = _apply(tmp_path, lambda grid: grid.update(pairing_name="no-prefix"))

    assert prepared["status"] != "ok"
    assert "grid_meter_homewizard_p1_unpaired" in json.dumps(prepared)
    assert "pairing_name" in json.dumps(prepared)


def test_an_unpaired_meter_is_refused_as_the_ems_would_refuse_it(tmp_path):
    _write_config(tmp_path, {"type": "shelly", "ip": "192.168.1.40"})

    prepared = _apply(tmp_path, lambda grid: grid.update(type="homewizard_p1"))

    assert prepared["status"] != "ok"
    assert "grid_meter_homewizard_p1_unpaired" in json.dumps(prepared)
