# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup and Maintenance share one config-field interpretation.

The two workflows differ in UI, confirmation and destructive policy. What they
must never differ in is what a field *means*: which keys a grid-meter variant
may carry, and which catalog fields an editor may write. Both are catalog-driven
here, so a new variant field reaches both flows without a second literal list.
"""

import pytest

from admin.maintenance_config import _strip_stale_grid_meter_keys
from admin.setup_config import strip_incompatible_grid_meter_fields
from ems.config_catalog import (
    GRID_METER_KNOWN_MQTT_KEYS,
    GRID_METER_KNOWN_TOP_KEYS,
    GRID_METER_VARIANTS,
    grid_meter_variant_field_spec,
)

pytestmark = pytest.mark.simulation


def _grid_with_every_known_key(grid_type):
    grid = {key: f"value-{key}" for key in GRID_METER_KNOWN_TOP_KEYS}
    grid["type"] = grid_type
    grid["mqtt"] = {key: f"mqtt-{key}" for key in GRID_METER_KNOWN_MQTT_KEYS}
    return grid


@pytest.mark.parametrize("grid_type", sorted(GRID_METER_VARIANTS))
def test_both_flows_remove_the_same_incompatible_keys(grid_type):
    setup_grid = _grid_with_every_known_key(grid_type)
    maintenance_grid = _grid_with_every_known_key(grid_type)

    strip_incompatible_grid_meter_fields(setup_grid, grid_type)
    _strip_stale_grid_meter_keys(maintenance_grid)

    assert set(setup_grid) == set(maintenance_grid)
    assert setup_grid.get("mqtt", {}).keys() == maintenance_grid.get("mqtt", {}).keys()


@pytest.mark.parametrize("grid_type", sorted(GRID_METER_VARIANTS))
def test_survivors_are_exactly_the_variant_spec(grid_type):
    grid = _grid_with_every_known_key(grid_type)
    strip_incompatible_grid_meter_fields(grid, grid_type)
    spec = grid_meter_variant_field_spec(grid_type)

    # Only keys some known variant claims are eligible for removal at all.
    eligible = {key for key in grid if key in GRID_METER_KNOWN_TOP_KEYS}
    assert eligible == set(spec["keys"]) & set(GRID_METER_KNOWN_TOP_KEYS)
    if spec["mqtt_keys"]:
        assert set(grid["mqtt"]) == set(spec["mqtt_keys"])
    else:
        assert "mqtt" not in grid


def test_switch_into_d0_drops_the_generic_mqtt_value_path():
    """The pinned regression: a D0 meter must not inherit ``mqtt.value_path``."""

    for strip in (
        lambda grid: strip_incompatible_grid_meter_fields(grid, grid["type"]),
        _strip_stale_grid_meter_keys,
    ):
        grid = {
            "type": "zendure_smartmeter_d0",
            "mqtt": {"host": "broker", "topic": "d0/1", "value_path": "power"},
        }
        strip(grid)
        assert "value_path" not in grid["mqtt"]
        assert grid["mqtt"]["topic"] == "d0/1"


def test_operator_defined_custom_keys_survive_both_flows():
    for strip in (
        lambda grid: strip_incompatible_grid_meter_fields(grid, grid["type"]),
        _strip_stale_grid_meter_keys,
    ):
        grid = {"type": "shelly", "ip": "10.0.0.5", "site_note": "kept"}
        strip(grid)
        assert grid["site_note"] == "kept"
