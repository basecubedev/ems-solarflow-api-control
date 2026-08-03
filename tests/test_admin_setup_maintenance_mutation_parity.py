# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup and Maintenance apply the same accepted field the same way.

The two workflows keep distinct policies — what may be created, what may be
removed, what needs confirmation. What a *field* means is not one of those
policies: a catalog-declared path given the same raw value has to end up as the
same stored value, or the config an operator reviews in one flow is not the
config the other flow would have written from the same answer.

These are the domain cases where the two paths historically disagreed because
each carried its own interpretation of a catalog field.
"""

import copy
import json

import pytest

import admin.maintenance_config as maintenance_config
from admin.device_common_fields import apply_common_device_values
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.setup_config import apply_device_config_values, apply_setup_features

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture
def _isolate(isolated_install_root):
    return isolated_install_root


def _setup_grid_meter(current, features):
    """Apply grid-meter edits the way the Setup config preview does."""

    config = {"grid_meter": copy.deepcopy(current)}
    apply_setup_features(config, features)
    return config.get("grid_meter")


def _maintenance_grid_meter(current, draft):
    """Apply grid-meter edits the way the Maintenance merge does."""

    merged = {"grid_meter": copy.deepcopy(current)}
    maintenance_config._merge_grid_meter(merged, draft, [])
    return merged.get("grid_meter")


_HTTP_METER = {"type": "shelly", "ip": "192.0.2.10", "port": 80, "channels": ["a"]}
_MQTT_METER = {
    "type": "mqtt",
    "mqtt": {
        "host": "192.0.2.20",
        "port": 1883,
        "topic": "meter/power",
        "value_path": "power",
        "max_age_seconds": 30,
    },
}


def test_padded_text_field_is_stored_identically():
    """``grid_meter.mqtt.value_path`` is one catalog text field, not two."""

    setup = _setup_grid_meter(_MQTT_METER, {"grid_meter.mqtt.value_path": "  a/b  "})
    maintenance = _maintenance_grid_meter(
        _MQTT_METER, {"type": "mqtt", "mqtt": {"value_path": "  a/b  "}}
    )

    assert setup["mqtt"]["value_path"] == maintenance["mqtt"]["value_path"]


def test_padded_host_field_is_stored_identically():
    setup = _setup_grid_meter(_HTTP_METER, {"grid_meter.ip": " 192.0.2.77 "})
    maintenance = _maintenance_grid_meter(
        _HTTP_METER, {"type": "shelly", "ip": " 192.0.2.77 "}
    )

    assert setup["ip"] == maintenance["ip"] == "192.0.2.77"


def test_emptied_number_field_is_cleared_in_both_flows():
    """An emptied number field must never be stored as ``""``.

    EMS Core reads ``max_age_seconds`` as a number; an empty string is not a
    smaller value, it is a config the controller cannot parse.
    """

    setup = _setup_grid_meter(_MQTT_METER, {"grid_meter.mqtt.max_age_seconds": ""})
    maintenance = _maintenance_grid_meter(
        _MQTT_METER, {"type": "mqtt", "mqtt": {"max_age_seconds": ""}}
    )

    assert "max_age_seconds" not in setup["mqtt"]
    assert "max_age_seconds" not in maintenance["mqtt"]


def test_emptied_text_field_is_cleared_in_both_flows():
    meter = {"type": "tasmota", "ip": "192.0.2.30", "power_path": "StatusSNS.Power"}

    setup = _setup_grid_meter(meter, {"grid_meter.power_path": ""})
    maintenance = _maintenance_grid_meter(
        meter, {"type": "tasmota", "power_path": ""}
    )

    assert "power_path" not in setup
    assert "power_path" not in maintenance


def test_list_field_accepts_the_same_raw_shapes():
    """``channels`` is a catalog ``string_list``; a comma string is one input."""

    setup = _setup_grid_meter(_HTTP_METER, {"grid_meter.channels": "x, y"})
    maintenance = _maintenance_grid_meter(
        _HTTP_METER, {"type": "shelly", "channels": "x, y"}
    )

    assert setup["channels"] == maintenance["channels"] == ["x", "y"]


def test_number_field_coerces_identically():
    setup = _setup_grid_meter(_MQTT_METER, {"grid_meter.mqtt.port": "1884"})
    maintenance = _maintenance_grid_meter(
        _MQTT_METER, {"type": "mqtt", "mqtt": {"port": "1884"}}
    )

    assert setup["mqtt"]["port"] == maintenance["mqtt"]["port"] == 1884


def test_device_value_fields_coerce_identically():
    """The one device-value set both flows write reads the same raw values."""

    setup_device = {"name": "WR1"}
    maintenance_device = {"name": "WR1"}
    values = {"max_power": "800", "min_soc": "15", "pv_kwp": "1.5"}

    apply_device_config_values(setup_device, values)
    apply_common_device_values(maintenance_device, values)

    assert setup_device["max_power"] == maintenance_device["max_power"] == 800
    assert setup_device["min_soc"] == maintenance_device["min_soc"] == 15
    assert setup_device["pv_kwp"] == maintenance_device["pv_kwp"] == 1.5


def test_maintenance_preview_and_apply_produce_the_same_config(tmp_path, _isolate):
    """What an operator reviewed is what gets written, byte for byte."""

    config = {
        "system": {"max_total_power": 1600},
        "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}],
        "grid_meter": {"type": "mqtt", "mqtt": {"host": "192.0.2.20", "topic": "m/p"}},
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["mqtt"]["topic"] = "  changed/topic  "

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )

    assert prepared["status"] == "ok", prepared
    assert preview["diff"] == prepared["diff"]
    applied = json.loads(prepared["payload"])
    assert applied["grid_meter"]["mqtt"]["topic"] == "changed/topic"


def test_scalar_feature_paths_coerce_identically():
    setup_config = {"winter": {}, "dashboard": {}}
    maintenance_config_dict = {"winter": {}, "dashboard": {}}
    features = {
        "winter.enabled": "true",
        "winter.months": "10, 11, 12",
        "dashboard.port": "9090",
    }

    apply_setup_features(setup_config, features)
    maintenance_config._merge_features(maintenance_config_dict, features)

    assert setup_config["winter"] == maintenance_config_dict["winter"]
    assert setup_config["dashboard"] == maintenance_config_dict["dashboard"]
