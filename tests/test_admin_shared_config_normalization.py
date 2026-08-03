# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup and Maintenance share one config-field interpretation.

The two workflows differ in UI, confirmation and destructive policy. What they
must never differ in is what a field *means*: which keys a grid-meter variant
may carry, which catalog fields an editor may write, and how an answer to one
of them is read. That interpretation has one owner in ``ems.config_mutation``;
these tests pin the owner and the fact that both flows go through it rather
than around it.
"""

import inspect

import pytest

import admin.config_preview as config_preview
import admin.maintenance_config as maintenance_config
import admin.setup_config as setup_config
import ems.config_mutation as config_mutation
from ems.config_catalog import (
    GRID_METER_KNOWN_MQTT_KEYS,
    GRID_METER_KNOWN_TOP_KEYS,
    GRID_METER_VARIANTS,
    grid_meter_variant_field_spec,
)
from ems.config_mutation import (
    strip_incompatible_grid_meter_fields,
    strip_stale_grid_meter_keys,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def _grid_with_every_known_key(grid_type):
    grid = {key: f"value-{key}" for key in GRID_METER_KNOWN_TOP_KEYS}
    grid["type"] = grid_type
    grid["mqtt"] = {key: f"mqtt-{key}" for key in GRID_METER_KNOWN_MQTT_KEYS}
    return grid


@pytest.mark.parametrize("grid_type", sorted(GRID_METER_VARIANTS))
def test_both_cleanups_remove_the_same_incompatible_keys(grid_type):
    variant_grid = _grid_with_every_known_key(grid_type)
    declared_grid = _grid_with_every_known_key(grid_type)

    strip_incompatible_grid_meter_fields(variant_grid, grid_type)
    strip_stale_grid_meter_keys(declared_grid)

    assert set(variant_grid) == set(declared_grid)
    assert variant_grid.get("mqtt", {}).keys() == declared_grid.get("mqtt", {}).keys()


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
        strip_stale_grid_meter_keys,
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
        strip_stale_grid_meter_keys,
    ):
        grid = {"type": "shelly", "ip": "10.0.0.5", "site_note": "kept"}
        strip(grid)
        assert grid["site_note"] == "kept"


def _source(obj):
    return inspect.getsource(obj)


def test_core_owns_the_mutation_semantics():
    """The rules live in EMS Core, which knows nothing about Admin."""

    source = _source(config_mutation)
    assert "import admin" not in source
    assert "from admin" not in source
    for name in (
        "coerce_catalog_value",
        "resolve_change",
        "apply_config_changes",
        "apply_grid_meter_changes",
        "strip_incompatible_grid_meter_fields",
        "mutation_diff",
    ):
        assert callable(getattr(config_mutation, name))


def test_setup_delegates_field_interpretation():
    for func in (setup_config.apply_setup_features, setup_config.apply_device_config_values):
        source = _source(func)
        assert "apply_config_changes" in source or "apply_common_values" in source
    assert "apply_grid_meter_changes" in _source(setup_config.apply_setup_features)


def test_maintenance_delegates_field_interpretation():
    assert "apply_config_changes" in _source(maintenance_config._merge_features)
    assert "apply_grid_meter_changes" in _source(maintenance_config._merge_grid_meter)
    assert "apply_common_device_values" in _source(maintenance_config._apply_device_fields)
    assert "mutation_diff" in _source(maintenance_config.summarize_config_changes)


def test_no_flow_carries_a_second_grid_meter_cleanup():
    """Only the canonical module may define the variant cleanup."""

    for module in (setup_config, maintenance_config, config_preview):
        source = _source(module)
        assert "def strip_incompatible_grid_meter_fields" not in source
        assert "def strip_stale_grid_meter_keys" not in source
    assert (
        config_preview.strip_incompatible_grid_meter_fields
        is config_mutation.strip_incompatible_grid_meter_fields
    )


def test_coercion_has_one_owner():
    from admin.device_common_fields import coerce_field_value

    assert coerce_field_value is config_mutation.coerce_catalog_value
