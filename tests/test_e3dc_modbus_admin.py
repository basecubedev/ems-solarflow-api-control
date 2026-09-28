# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Setup and Maintenance carry the E3/DC through the Core paths.

As a grid meter the E3/DC adds one key no other meter has (``unit_id``) and a
port that belongs to its protocol; as a device it is an entry the EMS only
reads. Both have to survive the Setup preview and a Maintenance round trip, and
be refused by Admin exactly when the EMS would refuse them.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.setup_config import build_setup_catalog
from ems import config as cfg
from ems.config_catalog import grid_meter_types

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.config,
    pytest.mark.contract,
]

_ADMIN_JS = Path(__file__).resolve().parents[1] / "admin" / "static" / "admin.js"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _ReleaseManager:
    def config_template(self):
        return {
            "tag": "v0.8.11",
            "template": {
                "system": {"max_total_power": 800},
                "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN"}],
                "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
            },
        }


def _inverter():
    return {
        "config_name": "inverter_1",
        "display_name": "SolarFlow 1",
        "role": "inverter",
        "enabled": True,
        "ip": "192.168.1.1",
        "serial_number": "SN1",
        "device_type": "zendure_solarflow_800_pro",
        "api_family": "zendure_local_http",
    }


def _e3dc_meter(**values):
    item = {
        "config_name": "grid_meter",
        "display_name": "E3/DC S10",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.168.1.40",
        "port": 502,
        "grid_meter_type": "e3dc_modbus",
        "api_family": "",
        "device_type": "",
    }
    item.update(values)
    return item


def _preview(meter, features=None):
    return ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_inverter(), meter], 1, features=features
    )


def _error_codes(result):
    return {issue["code"] for issue in result["validation"]["errors"]}


def test_setup_preview_writes_a_meter_the_ems_factory_accepts():
    result = _preview(_e3dc_meter(), features={"grid_meter.unit_id": 3})

    assert result["ready"] is True, result["validation"]
    grid = result["config"]["grid_meter"]
    assert grid == {"type": "e3dc_modbus", "ip": "192.168.1.40", "port": 502, "unit_id": 3}
    assert cfg.e3dc_modbus_grid_meter_settings(grid)["unit_id"] == 3
    assert result["summary"]["grid_meter"] == {
        "type": "e3dc_modbus",
        "transport": "modbus_tcp",
    }


def test_setup_preview_refuses_what_the_ems_factory_would_refuse():
    result = _preview(
        _e3dc_meter(),
        features={"grid_meter.unit_id": 300},
    )

    assert result["ready"] is False
    assert "grid_meter_e3dc_modbus_invalid" in _error_codes(result)


def test_setup_preview_requires_a_host():
    result = _preview(_e3dc_meter(ip=""))

    assert result["ready"] is False
    assert "grid_meter_host_invalid" in _error_codes(result)


def test_setup_offers_the_e3dc_as_a_manual_meter_on_port_502():
    catalog = build_setup_catalog()
    variant = catalog["grid_meter_variants"]["e3dc_modbus"]
    assert variant["port_description"] == "Modbus TCP port. The E3/DC default is 502."
    assert "grid_meter.unit_id" in variant["fields"]
    card = next(item for item in catalog["hardware_variants"]["grid_meter"] if item["id"] == "e3dc_modbus")
    assert card["default_port"] == 502


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


_STORED = {
    "type": "e3dc_modbus",
    "ip": "192.168.1.40",
    "port": 502,
    "unit_id": 1,
}


def test_maintenance_round_trips_every_modbus_key(tmp_path):
    _write_config(tmp_path, dict(_STORED))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    for key in ("unit_id", "port"):
        assert draft["grid_meter"][key] == _STORED[key]
    draft["grid_meter"]["unit_id"] = 4

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["grid_meter"] == {**_STORED, "unit_id": 4}


def test_maintenance_refuses_what_the_ems_factory_would_refuse(tmp_path):
    _write_config(tmp_path, dict(_STORED))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["unit_id"] = 300

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] != "ok"
    assert "grid_meter_e3dc_modbus_invalid" in json.dumps(prepared)


def test_maintenance_switch_to_another_meter_drops_the_modbus_keys(tmp_path):
    _write_config(tmp_path, dict(_STORED))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["type"] = "ecotracker"

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    grid = json.loads(prepared["payload"])["grid_meter"]
    assert grid["type"] == "ecotracker"
    assert "unit_id" not in grid


def test_admin_js_type_mirror_matches_the_catalog():
    source = _ADMIN_JS.read_text(encoding="utf-8")
    block = re.search(r"const GRID_METER_TYPE_CHOICES = new Set\(\[(.*?)\]\);", source, re.S)
    assert block, "GRID_METER_TYPE_CHOICES not found in admin.js"
    mirrored = set(re.findall(r'"([a-z0-9_]+)"', block.group(1)))
    assert mirrored == set(grid_meter_types())


@pytest.mark.parametrize(
    "stored, new_type",
    [
        ({"type": "zendure_grid_meter_http", "ip": "192.168.1.40", "port": 80}, "e3dc_modbus"),
        (dict(_STORED), "zendure_grid_meter_http"),
    ],
)
def test_maintenance_type_switch_without_a_port_drops_the_old_protocols_port(
    tmp_path, stored, new_type
):
    _write_config(tmp_path, stored)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["type"] = new_type
    draft["grid_meter"].pop("port")

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    assert "port" not in json.loads(prepared["payload"])["grid_meter"]


def test_maintenance_type_switch_within_a_protocol_keeps_the_port(tmp_path):
    _write_config(tmp_path, {"type": "shelly", "ip": "192.168.1.40", "port": 8080})
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["type"] = "ecotracker"
    draft["grid_meter"].pop("port")

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["grid_meter"]["port"] == 8080


@pytest.mark.parametrize("port", [1502, 8080])
def test_maintenance_type_switch_keeps_a_port_the_draft_sends(tmp_path, port):
    """Including a typed port that happens to equal the old protocol's one."""

    _write_config(tmp_path, {"type": "shelly", "ip": "192.168.1.40", "port": 8080})
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["type"] = "e3dc_modbus"
    draft["grid_meter"]["port"] = port

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["grid_meter"]["port"] == port


def test_setup_preview_does_not_carry_a_discovered_http_port_to_the_e3dc():
    meter = _e3dc_meter(grid_meter_type="", api_family="shelly_gen2", port=80)

    result = _preview(meter, features={"grid_meter.type": "e3dc_modbus"})

    assert result["ready"] is True, result["validation"]
    assert result["config"]["grid_meter"] == {"type": "e3dc_modbus", "ip": "192.168.1.40"}


@pytest.mark.parametrize("port", [502, 5020])
def test_setup_preview_keeps_the_port_the_card_carries_after_a_variant_switch(port):
    """The card resets its port when the variant changes protocol, so what it
    carries afterwards, reset or typed, is the port of the card's own type."""

    meter = _e3dc_meter(grid_meter_type="e3dc_modbus", api_family="shelly_gen2", port=port)

    result = _preview(meter, features={"grid_meter.type": "e3dc_modbus"})

    assert result["ready"] is True, result["validation"]
    assert result["config"]["grid_meter"]["port"] == port


class _ExistingConfigReleaseManager(_ReleaseManager):
    def __init__(self, grid_meter):
        self.grid_meter = grid_meter

    def config_template(self):
        resource = super().config_template()
        resource["template"]["grid_meter"] = dict(self.grid_meter)
        return resource


@pytest.mark.parametrize(
    "carried, meter, feature_type, expected_port",
    [
        ({"type": "shelly", "ip": "192.168.1.3", "port": 8080}, _e3dc_meter(), "e3dc_modbus", 502),
        (
            dict(_STORED),
            _e3dc_meter(grid_meter_type="shelly", port=None),
            "shelly",
            None,
        ),
    ],
)
def test_setup_preview_does_not_carry_the_base_config_port_across_protocols(
    carried, meter, feature_type, expected_port
):
    result = ConfigPreviewGenerator(_ExistingConfigReleaseManager(carried)).generate(
        [_inverter(), meter], 1, features={"grid_meter.type": feature_type}
    )

    grid = result["config"]["grid_meter"]
    assert grid["type"] == feature_type
    assert grid.get("port") == expected_port


def test_setup_variants_carry_the_default_port_the_frontend_resets_to():
    variants = build_setup_catalog()["grid_meter_variants"]
    assert variants["e3dc_modbus"]["default_port"] == cfg.E3DC_MODBUS_DEFAULT_PORT
    assert variants["shelly"]["default_port"] == 80
    assert "default_port" not in variants["mqtt"]


def test_admin_js_port_switch_agrees_with_the_core_rule_for_every_variant_pair():
    from ems.config_catalog import GRID_METER_VARIANTS, grid_meter_port_carries_over

    source = _ADMIN_JS.read_text(encoding="utf-8")
    body = re.search(
        r"function gridMeterPortAfterSwitch\(port, previous, next\) \{.*?\n\}", source, re.S
    )
    assert body, "gridMeterPortAfterSwitch not found in admin.js"
    variants = build_setup_catalog()["grid_meter_variants"]
    types = sorted(GRID_METER_VARIANTS)
    script = (
        body.group(0)
        + "\nconst variants = "
        + json.dumps(variants)
        + ";\nconst types = "
        + json.dumps(types)
        + ";\nconst out = {};\n"
        + "for (const a of types) for (const b of types) {\n"
        + "  out[a + '>' + b] = gridMeterPortAfterSwitch(4711, variants[a], variants[b]) ?? null;\n"
        + "}\nconsole.log(JSON.stringify(out));\n"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    js = json.loads(result.stdout)
    for a in types:
        for b in types:
            expected = (
                4711
                if grid_meter_port_carries_over(a, b)
                else GRID_METER_VARIANTS[b].get("default_port")
            )
            if b not in variants:
                continue
            assert js[f"{a}>{b}"] == expected, (a, b)


def test_maintenance_loads_and_sends_every_variant_top_level_key():
    """The draft keys are a hand list; a catalog field missing from it would be
    neither shown nor written by Maintenance, silently."""

    from admin.maintenance_config import _GRID_METER_DRAFT_KEYS
    from ems.config_catalog import GRID_METER_KNOWN_TOP_KEYS

    assert GRID_METER_KNOWN_TOP_KEYS - {"mqtt"} <= set(_GRID_METER_DRAFT_KEYS)



# --- the E3/DC as a read-only device ---------------------------------------------


def _write_devices(base_dir, devices, grid_meter=None):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 800},
                "devices": devices,
                "grid_meter": grid_meter or {"type": "shelly", "ip": "192.168.1.3"},
            }
        ),
        encoding="utf-8",
    )


_ZENDURE = {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
_DEVICE = {
    "name": "E3DC",
    "type": "e3dc_modbus",
    "ip": "192.168.1.40",
    "port": 502,
    "unit_id": 1,
    "note": "kept",
}


def test_maintenance_shows_an_e3dc_device_as_its_own_kind(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_DEVICE)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]["devices"][1]
    assert draft == {
        "kind": "e3dc_modbus",
        "original_name": "E3DC",
        "name": "E3DC",
        "ip": "192.168.1.40",
        "enabled": True,
        "has_enabled_key": False,
        "port": 502,
        "unit_id": 1,
    }
    assert loaded["summary"]["device_count"] == 1
    assert loaded["summary"]["read_only_device_count"] == 1


def test_maintenance_round_trips_an_e3dc_device_without_zendure_values(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_DEVICE)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][1]["ip"] = "192.168.1.41"
    draft["devices"][1]["unit_id"] = "3"

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    written = json.loads(prepared["payload"])["devices"]
    assert written[1] == {**_DEVICE, "ip": "192.168.1.41", "unit_id": 3}
    assert written[0]["sn"] == "AAA"


def test_maintenance_adds_an_e3dc_device_from_its_draft(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(
        {
            "kind": "e3dc_modbus",
            "original_name": None,
            "name": "E3DC",
            "ip": "192.168.1.40",
            "port": 502,
            "unit_id": 1,
            "enabled": True,
            "has_enabled_key": True,
        }
    )

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"])["devices"][1] == {
        "name": "E3DC",
        "type": "e3dc_modbus",
        "ip": "192.168.1.40",
        "port": 502,
        "unit_id": 1,
        "enabled": True,
    }


@pytest.mark.parametrize(
    "change, code",
    [
        ({"ip": "not a host!"}, "device_host_invalid"),
        ({"unit_id": 300}, "e3dc_modbus_device_invalid"),
    ],
)
def test_maintenance_refuses_an_e3dc_device_the_ems_would_refuse(tmp_path, change, code):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_DEVICE)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][1].update(change)

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] != "ok"
    assert code in json.dumps(prepared)


def test_maintenance_refuses_a_config_with_only_an_e3dc_device(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), dict(_DEVICE)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][0]["removed"] = True

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] != "ok"
    assert "no_control_devices" in json.dumps(prepared)


def test_setup_preview_shows_the_e3dc_meter_as_a_read_only_device_once():
    result = _preview(_e3dc_meter(), features={"grid_meter.unit_id": 2})

    assert result["ready"] is True, result["validation"]
    devices = result["config"]["devices"]
    assert devices[-1] == {
        "name": "E3DC",
        "type": "e3dc_modbus",
        "ip": "192.168.1.40",
        "port": 502,
        "unit_id": 2,
    }
    assert sum(1 for device in devices if device.get("type") == "e3dc_modbus") == 1
    assert result["summary"]["read_only_devices"] == 1
    assert "e3dc_read_only_device_added" in {
        issue["code"] for issue in result["validation"]["info"]
    }


def test_setup_preview_adds_no_device_for_another_meter():
    meter = _e3dc_meter(grid_meter_type="shelly", port=80)
    result = _preview(meter, features={"grid_meter.type": "shelly"})

    assert not any(device.get("type") == "e3dc_modbus" for device in result["config"]["devices"])
    assert result["summary"]["read_only_devices"] == 0


@pytest.mark.parametrize(
    "existing, added",
    [
        ({"name": "Keller", "type": "e3dc_modbus", "ip": "192.168.1.40"}, False),
        ({"name": "Keller", "type": "e3dc_modbus", "ip": "192.168.1.40", "unit_id": 2}, True),
        ({"name": "E3DC", "type": "e3dc_modbus", "ip": "192.168.1.99"}, True),
    ],
)
def test_the_meter_is_added_as_a_device_only_when_nothing_reads_that_endpoint(existing, added):
    from admin.config_preview import _add_e3dc_meter_as_read_only_device

    preview = {
        "devices": [dict(existing)],
        "grid_meter": {"type": "e3dc_modbus", "ip": "192.168.1.40"},
    }
    names = []
    _add_e3dc_meter_as_read_only_device(preview, names, {"info": []})

    new = preview["devices"][1:]
    assert bool(new) is added
    if added:
        assert new[0]["name"] != existing["name"]
        assert new[0]["name"] in names


def test_admin_js_offers_the_e3dc_device_card_and_button():
    source = _ADMIN_JS.read_text(encoding="utf-8")
    html = (_ADMIN_JS.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="maintenance-config-add-e3dc-device"' in html
    assert "function renderMaintenanceE3dcDevice(" in source
    assert 'kind: "e3dc_modbus"' in source
    assert "if (mconfigIsE3dcDevice(configured)) return;" in source


def test_nothing_about_an_e3dc_device_is_mirrored_into_runtime_state(tmp_path):
    from admin.runtime_convergence import _classify

    config = {"devices": [dict(_ZENDURE), dict(_DEVICE)]}
    assert _classify("devices[0].enabled", config) == ("device", "WR1", "enabled")
    assert _classify("devices[1].enabled", config) is None

    _write_devices(tmp_path, [dict(_ZENDURE), dict(_DEVICE)])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][1]["enabled"] = False
    draft["devices"][0]["enabled"] = False
    diff = preview_maintenance_config(draft, base_dir=str(tmp_path))["diff"]
    live = {entry["path"]: entry["applies_live"] for entry in diff["added"] + diff["changes"]}
    assert live["devices[1].enabled"] is False
    assert live["devices[0].enabled"] is True


def test_removing_an_e3dc_device_is_not_reported_as_live(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE), {**_DEVICE, "enabled": True}])
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"][1]["removed"] = True

    diff = preview_maintenance_config(draft, base_dir=str(tmp_path))["diff"]

    removed = {entry["path"]: entry["applies_live"] for entry in diff["removed"]}
    assert removed["devices[1].enabled"] is False
