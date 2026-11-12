# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The setup preview and Maintenance check a reader meter as the EMS does.

A meter with settings of its own (an API key, a Modbus unit, a TCP port) is
refused by the Admin Console for exactly the reason the EMS would refuse to
start with it, and a stored API key is never shown back.
"""

import copy
import json

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.maintenance_config import load_maintenance_config, prepare_maintenance_config_apply, redact_config_for_browser
from tests.test_admin_config_preview import _device, _meter, _ReleaseManager

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.config,
    pytest.mark.integration,
]

KEY = "1a2b3c4d5e6f"
POWERFOX = {"type": "powerfox_poweropti", "ip": "192.168.1.50", "token": KEY}
HOMEWIZARD = {
    "type": "homewizard_p1",
    "ip": "192.168.1.40",
    "serial": "0a1b2c3d4e5f",
    "token": "0123456789ABCDEF0123456789ABCDEF",
    "pairing_name": "local/ems_solarflow#3f9a1c",
}


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


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


def _saved_meter(prepared):
    assert prepared["status"] == "ok", prepared
    return json.loads(prepared["payload"])["grid_meter"]


# --- powerfox poweropti --------------------------------------------------------------


def test_maintenance_says_an_api_key_exists_and_never_shows_it(tmp_path):
    _write_config(tmp_path, dict(POWERFOX))

    loaded = load_maintenance_config(base_dir=str(tmp_path))

    assert loaded["draft"]["grid_meter"]["has_token"] is True
    assert KEY not in json.dumps(loaded)


def test_saving_without_retyping_the_api_key_keeps_it(tmp_path):
    _write_config(tmp_path, dict(POWERFOX))

    saved = _saved_meter(_apply(tmp_path, lambda grid: grid.update(ip="192.168.1.51", token="")))

    assert saved == {**POWERFOX, "ip": "192.168.1.51"}


def test_a_homewizard_token_does_not_become_the_api_key_of_another_meter(tmp_path):
    _write_config(tmp_path, dict(HOMEWIZARD))

    prepared = _apply(tmp_path, lambda grid: grid.update(type="powerfox_poweropti", ip="192.168.1.50", token=""))

    assert prepared["status"] != "ok"
    assert "grid_meter_settings_invalid" in json.dumps(prepared)
    assert HOMEWIZARD["token"] not in json.dumps(prepared)


def test_a_blank_api_key_does_not_carry_the_homewizard_token_over(tmp_path):
    _write_config(tmp_path, dict(HOMEWIZARD))

    prepared = _apply(tmp_path, lambda grid: grid.update(type="powerfox_poweropti", ip="192.168.1.50", token="   "))

    assert prepared["status"] != "ok"
    assert HOMEWIZARD["token"] not in json.dumps(prepared)


def test_a_typed_api_key_goes_with_the_new_meter(tmp_path):
    _write_config(tmp_path, dict(HOMEWIZARD))

    saved = _saved_meter(_apply(tmp_path, lambda grid: grid.update(type="powerfox_poweropti", ip="192.168.1.50", token=KEY)))

    assert saved == POWERFOX


def test_the_setup_preview_needs_the_api_key_and_hides_it():
    meter = _meter(api_family="", device_type="", grid_meter_type="powerfox_poweropti", ip="192.168.1.50")
    generator = ConfigPreviewGenerator(_ReleaseManager())

    missing = generator.generate([_device(), meter], 1)
    assert missing["ready"] is False
    assert "grid_meter_settings_invalid" in json.dumps(missing["validation"])

    result = generator.generate([_device(), meter], 1, {"grid_meter.token": KEY})
    assert result["ready"] is True
    assert result["config"]["grid_meter"] == POWERFOX
    assert KEY not in json.dumps(redact_config_for_browser(copy.deepcopy(result)))


# --- DSMR P1 over TCP ----------------------------------------------------------------


def test_maintenance_refuses_a_p1_bridge_without_its_port_as_the_ems_would(tmp_path):
    _write_config(tmp_path, {"type": "shelly", "ip": "192.168.1.40"})

    refused = _apply(tmp_path, lambda grid: grid.update(type="dsmr_p1_tcp", ip="192.168.1.80", port=None))
    assert refused["status"] != "ok"
    assert "grid_meter.port" in json.dumps(refused)

    saved = _saved_meter(_apply(tmp_path, lambda grid: grid.update(type="dsmr_p1_tcp", ip="192.168.1.80", port=23)))
    assert saved == {"type": "dsmr_p1_tcp", "ip": "192.168.1.80", "port": 23}


@pytest.mark.parametrize("port", [23, "23"])
def test_the_setup_preview_keeps_the_port_typed_for_a_p1_bridge(port):
    meter = _meter(api_family="", device_type="", grid_meter_type="dsmr_p1_tcp", ip="192.168.1.80", port=port)

    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)

    assert result["ready"] is True, result["validation"]
    assert result["config"]["grid_meter"] == {"type": "dsmr_p1_tcp", "ip": "192.168.1.80", "port": 23}


# --- SunSpec over Modbus TCP ---------------------------------------------------------


def test_maintenance_saves_a_sunspec_meter_and_refuses_a_unit_id_modbus_cannot_address(tmp_path):
    _write_config(tmp_path, {"type": "e3dc_modbus", "ip": "192.168.1.90", "unit_id": 1})

    saved = _saved_meter(_apply(tmp_path, lambda grid: grid.update(type="sunspec_modbus", unit_id=200)))
    assert saved == {"type": "sunspec_modbus", "ip": "192.168.1.90", "unit_id": 200}

    refused = _apply(tmp_path, lambda grid: grid.update(type="sunspec_modbus", unit_id=300))
    assert refused["status"] != "ok"


# --- Modbus profiles -----------------------------------------------------------------


def test_maintenance_saves_a_profile_meter_and_refuses_a_unit_id_modbus_cannot_address(tmp_path):
    _write_config(tmp_path, {"type": "shelly", "ip": "192.168.1.95"})

    saved = _saved_meter(_apply(tmp_path, lambda grid: grid.update(type="victron_gx_modbus", unit_id=100)))
    assert saved == {"type": "victron_gx_modbus", "ip": "192.168.1.95", "unit_id": 100}

    refused = _apply(tmp_path, lambda grid: grid.update(type="victron_gx_modbus", unit_id=300))
    assert refused["status"] != "ok"


def test_a_unit_id_belongs_to_its_device_and_does_not_follow_a_type_change(tmp_path):
    _write_config(tmp_path, {"type": "sunspec_modbus", "ip": "192.168.1.64", "unit_id": 200})

    def switch(grid):
        grid.update(type="victron_gx_modbus")
        grid.pop("unit_id")

    saved = _saved_meter(_apply(tmp_path, switch))

    assert saved == {"type": "victron_gx_modbus", "ip": "192.168.1.64"}
