# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Maintenance carries an inverter read by an OpenDTU through the Core paths.

An ``opendtu_http`` entry is a device the EMS only reads. It has to survive a
Maintenance round trip with every key it carries, count as a read-only device
rather than an inverter, and be refused by Admin exactly when the EMS would
refuse it.
"""

import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from ems import config as cfg
from ems.read_only_devices import READ_ONLY_DEVICE_TYPES

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.config,
    pytest.mark.contract,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


_ZENDURE = {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
_E3DC = {"name": "E3DC", "type": "e3dc_modbus", "ip": "192.168.1.40"}
_OPENDTU = {
    "name": "TSUN OpenDTU",
    "type": "opendtu_http",
    "ip": "192.168.1.70",
    "serial": "116491234567",
    "port": 80,
    "max_age_seconds": 60,
    "note": "kept",
}


def _write_devices(base_dir, devices):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 800},
                "devices": devices,
                "grid_meter": {"type": "shelly", "ip": "192.168.1.3"},
            }
        ),
        encoding="utf-8",
    )


def _apply(tmp_path, change):
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    change(draft)
    return prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))


def test_maintenance_shows_an_opendtu_inverter_as_its_own_read_only_kind(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_E3DC), dict(_OPENDTU)])

    loaded = load_maintenance_config(base_dir=str(tmp_path))

    assert loaded["draft"]["devices"][2] == {
        "kind": "opendtu_http",
        "original_name": "TSUN OpenDTU",
        "name": "TSUN OpenDTU",
        "ip": "192.168.1.70",
        "serial": "116491234567",
        "port": 80,
        "max_age_seconds": 60,
        "enabled": True,
        "has_enabled_key": False,
    }
    assert loaded["summary"]["device_count"] == 1
    assert loaded["summary"]["enabled_device_count"] == 1
    assert loaded["summary"]["read_only_device_count"] == 2


def test_maintenance_round_trips_an_opendtu_inverter_and_keeps_its_custom_keys(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_OPENDTU)])

    def change(draft):
        draft["devices"][1].update(
            {"name": "Balcony", "ip": " 192.168.1.71 ", "serial": "116491234568", "port": "8080", "max_age_seconds": "120", "enabled": False}
        )

    prepared = _apply(tmp_path, change)

    assert prepared["status"] == "ok", prepared
    written = json.loads(prepared["payload"])["devices"]
    assert written[1] == {
        **_OPENDTU,
        "name": "Balcony",
        "ip": "192.168.1.71",
        "serial": "116491234568",
        "port": 8080,
        "max_age_seconds": 120,
        "enabled": False,
    }
    assert written[0]["sn"] == "AAA"
    assert cfg.opendtu_http_device_settings(written[1])["max_age_seconds"] == 120


def test_maintenance_leaves_an_untouched_opendtu_entry_as_it_was(tmp_path):
    stored = {"name": "OpenDTU", "type": "opendtu_http", "ip": "192.168.1.70", "serial": 116491234567}
    _write_devices(tmp_path, [dict(_ZENDURE), dict(stored)])

    prepared = _apply(tmp_path, lambda draft: None)

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["devices"][1] == stored


def test_maintenance_drops_the_optional_keys_an_operator_empties(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_OPENDTU)])

    def change(draft):
        draft["devices"][1].update({"serial": " ", "port": "", "max_age_seconds": None})

    prepared = _apply(tmp_path, change)

    assert prepared["status"] == "ok", prepared
    written = json.loads(prepared["payload"])["devices"][1]
    assert written == {"name": "TSUN OpenDTU", "type": "opendtu_http", "ip": "192.168.1.70", "note": "kept"}


def test_maintenance_adds_an_opendtu_inverter_from_its_draft(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE)])

    def change(draft):
        draft["devices"].append(
            {
                "kind": "opendtu_http",
                "original_name": None,
                "name": "OpenDTU",
                "ip": "192.168.1.70",
                "serial": "116491234567",
                "enabled": True,
                "has_enabled_key": True,
            }
        )

    prepared = _apply(tmp_path, change)

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["devices"][1] == {
        "name": "OpenDTU",
        "type": "opendtu_http",
        "ip": "192.168.1.70",
        "serial": "116491234567",
        "enabled": True,
    }


def test_maintenance_removes_an_opendtu_inverter(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_OPENDTU)])

    def change(draft):
        draft["devices"][1]["removed"] = True

    prepared = _apply(tmp_path, change)

    assert prepared["status"] == "ok", prepared
    assert [device["name"] for device in json.loads(prepared["payload"])["devices"]] == ["WR1"]


@pytest.mark.parametrize(
    "change, code",
    [
        ({"ip": "not a host!"}, "device_host_invalid"),
        ({"serial": "11-64"}, "opendtu_http_device_invalid"),
        ({"max_age_seconds": 2}, "opendtu_http_device_invalid"),
        ({"port": 70000}, "opendtu_http_device_invalid"),
    ],
)
def test_maintenance_refuses_an_opendtu_inverter_the_ems_would_refuse(tmp_path, change, code):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_OPENDTU)])

    prepared = _apply(tmp_path, lambda draft: draft["devices"][1].update(change))

    assert prepared["status"] != "ok"
    assert code in json.dumps(prepared)
    if code == "opendtu_http_device_invalid":
        with pytest.raises(ValueError):
            cfg.opendtu_http_device_settings({**_OPENDTU, **change})


def test_the_maintenance_preview_names_an_invalid_opendtu_inverter(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_OPENDTU)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][1]["serial"] = "x"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    codes = {issue["code"] for issue in preview["validation"]["errors"]}
    assert "opendtu_http_device_invalid" in codes


def test_maintenance_refuses_a_config_whose_only_device_is_an_opendtu_inverter(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_OPENDTU)])

    prepared = _apply(tmp_path, lambda draft: draft["devices"][0].update({"removed": True}))

    assert prepared["status"] != "ok"
    assert "no_control_devices" in json.dumps(prepared)


def test_nothing_about_an_opendtu_inverter_is_mirrored_into_runtime_state():
    from admin.runtime_convergence import _classify

    config = {"devices": [dict(_ZENDURE), dict(_OPENDTU)]}
    assert _classify("devices[0].enabled", config) == ("device", "WR1", "enabled")
    assert _classify("devices[1].enabled", config) is None


def test_every_read_only_kind_has_a_draft_and_a_validator():
    """A read-only type without an entry here would be drawn as nothing and fail the merge."""

    from admin.maintenance_config import _READ_ONLY_KINDS

    assert set(_READ_ONLY_KINDS) == READ_ONLY_DEVICE_TYPES
