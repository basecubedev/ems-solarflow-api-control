# SPDX-License-Identifier: AGPL-3.0-or-later
"""The canonical config-mutation contract.

Every rule an Admin workflow used to carry its own copy of lives here: what a
catalog type does to a raw value, what an empty answer means, what a credential
box means when it is blank, which representation an edited grid meter keeps,
and what a mutation is allowed to report as applied. The workflow adapters are
tested where they live; this is the domain matrix behind both of them.
"""

import pytest

from ems.config_mutation import (
    CLEAR,
    KEEP,
    MAINTENANCE_POLICY,
    SET,
    SETUP_POLICY,
    ConfigChange,
    CredentialIntent,
    MutationPolicy,
    apply_common_values,
    apply_config_changes,
    apply_grid_meter_changes,
    coerce_catalog_value,
    editable_grid_meter_mqtt_keys,
    mutation_diff,
    resolve_change,
)

pytestmark = pytest.mark.simulation


# --- catalog coercion ------------------------------------------------------


@pytest.mark.parametrize(
    "field_type,raw,expected",
    [
        ("boolean", "true", True),
        ("boolean", "off", False),
        ("boolean", 1, True),
        ("boolean", False, False),
        ("integer", "42", 42),
        ("integer", "not-a-number", "not-a-number"),
        ("number", "1883", 1883),
        ("number", "1.5", 1.5),
        ("number", "2.0", 2),
        ("text", "  padded  ", "padded"),
        ("select", " mqtt ", "mqtt"),
        ("url", " http://a/b ", "http://a/b"),
        ("string_list", "x, y", ["x", "y"]),
        ("string_list", [" x ", "", "y"], ["x", "y"]),
        ("month_list", "10, 11, 12", [10, 11, 12]),
        ("integer_list", [1, "2"], [1, 2]),
    ],
)
def test_catalog_types_coerce_one_way(field_type, raw, expected):
    assert coerce_catalog_value({"type": field_type}, raw) == expected


def test_unknown_type_still_strips_text():
    assert coerce_catalog_value({}, "  a  ") == "a"


# --- set / clear / keep ----------------------------------------------------


def test_empty_answer_clears_a_normal_field():
    assert resolve_change({"type": "text"}, ConfigChange("a", "")) == (CLEAR, None)
    assert resolve_change({"type": "text"}, ConfigChange("a", "   ")) == (CLEAR, None)
    assert resolve_change({"type": "number"}, ConfigChange("a", None)) == (CLEAR, None)


def test_empty_answer_keeps_a_secret():
    """A blank credential box is "not retyped", never "delete the secret"."""

    assert resolve_change({"type": "password"}, ConfigChange("a", "")) == (KEEP, None)
    assert resolve_change({"risk": "secret"}, ConfigChange("a", "")) == (KEEP, None)


def test_explicit_operations_win():
    assert resolve_change({"type": "text"}, ConfigChange("a", "x", CLEAR)) == (CLEAR, None)
    assert resolve_change({"type": "text"}, ConfigChange("a", "x", KEEP)) == (KEEP, None)


def test_falsy_but_present_values_are_set_not_cleared():
    assert resolve_change({"type": "boolean"}, ConfigChange("a", False)) == (SET, False)
    assert resolve_change({"type": "integer"}, ConfigChange("a", 0)) == (SET, 0)


# --- whole-config application ----------------------------------------------


def test_only_writable_catalog_paths_are_applied():
    config = {"winter": {"enabled": False}}
    result = apply_config_changes(
        config,
        [
            ConfigChange("winter.enabled", "true"),
            ConfigChange("invented.path", "evil"),
            ConfigChange("devices[].max_power", 9000),
        ],
        SETUP_POLICY,
    )

    assert config == {"winter": {"enabled": True}}
    assert result.applied_paths == ("winter.enabled",)
    assert [issue.code for issue in result.issues] == ["config_field_not_writable"]


def test_clearing_an_absent_path_is_not_an_applied_change():
    config = {}
    result = apply_config_changes(config, [ConfigChange("winter.months", "")], SETUP_POLICY)

    assert config == {}
    assert result.applied_paths == ()


def test_a_policy_that_may_not_remove_leaves_the_value_alone():
    policy = MutationPolicy(workflow="setup", scope="setup", allow_remove=False)
    config = {"winter": {"months": [1]}}
    apply_config_changes(config, [ConfigChange("winter.months", "")], policy)

    assert config == {"winter": {"months": [1]}}


def test_unknown_existing_keys_survive_a_mutation():
    config = {"winter": {"enabled": False, "operator_note": "kept"}, "custom": {"a": 1}}
    apply_config_changes(config, [ConfigChange("winter.enabled", True)], MAINTENANCE_POLICY)

    assert config["winter"]["operator_note"] == "kept"
    assert config["custom"] == {"a": 1}


# --- repeated entries ------------------------------------------------------


def test_common_values_apply_and_clear_by_the_same_rules():
    fields = {"max_power": {"type": "integer"}, "pv_kwp": {"type": "number"}}
    device = {"name": "WR1", "max_power": 800}
    applied = apply_common_values(device, {"max_power": "", "pv_kwp": "1.5"}, fields)

    assert device == {"name": "WR1", "pv_kwp": 1.5}
    assert {change.path: change.operation for change in applied} == {
        "max_power": CLEAR,
        "pv_kwp": SET,
    }


# --- grid meter ------------------------------------------------------------


def _mqtt_meter():
    return {
        "type": "mqtt",
        "mqtt": {"host": "192.0.2.20", "port": 1883, "topic": "meter/power"},
    }


def _flat_mqtt_meter():
    return {"type": "mqtt", "host": "192.0.2.20", "port": 1883, "topic": "meter/power"}


@pytest.mark.parametrize("policy", [SETUP_POLICY, MAINTENANCE_POLICY])
def test_type_switch_drops_keys_the_target_variant_cannot_carry(policy):
    grid = {"type": "shelly", "ip": "192.0.2.10", "channels": ["a"]}
    apply_grid_meter_changes(
        grid,
        [ConfigChange("type", "mqtt"), ConfigChange("ip", "192.0.2.10"),
         ConfigChange("mqtt.host", "192.0.2.20")],
        policy,
    )

    assert "ip" not in grid
    assert "channels" not in grid
    assert grid["mqtt"]["host"] == "192.0.2.20"


@pytest.mark.parametrize("policy", [SETUP_POLICY, MAINTENANCE_POLICY])
def test_switching_into_d0_drops_the_generic_value_path(policy):
    grid = {"type": "mqtt", "mqtt": {"host": "b", "topic": "t", "value_path": "power"}}
    apply_grid_meter_changes(grid, [ConfigChange("type", "zendure_smartmeter_d0")], policy)

    assert "value_path" not in grid["mqtt"]
    assert grid["mqtt"]["topic"] == "t"


def test_maintenance_edits_a_legacy_flat_meter_where_it_lives():
    grid = _flat_mqtt_meter()
    apply_grid_meter_changes(
        grid, [ConfigChange("mqtt.topic", "new/topic")], MAINTENANCE_POLICY
    )

    assert grid["topic"] == "new/topic"
    assert "mqtt" not in grid


def test_setup_writes_the_canonical_nested_representation():
    grid = _flat_mqtt_meter()
    apply_grid_meter_changes(
        grid, [ConfigChange("mqtt.topic", "new/topic")], SETUP_POLICY
    )

    assert grid["mqtt"]["topic"] == "new/topic"


def test_an_unchanged_value_is_not_rewritten():
    grid = _mqtt_meter()
    result = apply_grid_meter_changes(
        grid,
        [ConfigChange("mqtt.topic", "meter/power"), ConfigChange("mqtt.port", 1883)],
        MAINTENANCE_POLICY,
    )

    assert result.applied_paths == ()
    assert grid == _mqtt_meter()


def test_a_key_the_variant_cannot_hold_is_never_written():
    grid = {"type": "zendure_smartmeter_d0", "mqtt": {"host": "b", "topic": "t"}}
    apply_grid_meter_changes(
        grid, [ConfigChange("mqtt.value_path", "power")], MAINTENANCE_POLICY
    )

    assert "value_path" not in grid["mqtt"]


def test_operator_defined_grid_meter_keys_survive():
    grid = {"type": "shelly", "ip": "192.0.2.10", "site_note": "kept"}
    apply_grid_meter_changes(grid, [ConfigChange("type", "ecotracker")], MAINTENANCE_POLICY)

    assert grid["site_note"] == "kept"


# --- credential intent -----------------------------------------------------


def test_blank_password_keeps_the_stored_secret():
    grid = {"type": "mqtt", "mqtt": {"host": "b", "password": "stored"}}
    apply_grid_meter_changes(
        grid, [], MAINTENANCE_POLICY, credential=CredentialIntent.from_draft({"password": ""})
    )

    assert grid["mqtt"]["password"] == "stored"


def test_new_password_replaces_the_stored_secret():
    grid = {"type": "mqtt", "mqtt": {"host": "b", "password": "stored"}}
    apply_grid_meter_changes(
        grid,
        [],
        MAINTENANCE_POLICY,
        credential=CredentialIntent.from_draft({"password": "fresh"}),
    )

    assert grid["mqtt"]["password"] == "fresh"


def test_explicit_clear_removes_the_stored_secret():
    grid = {"type": "mqtt", "mqtt": {"host": "b", "password": "stored"}}
    apply_grid_meter_changes(
        grid,
        [],
        MAINTENANCE_POLICY,
        credential=CredentialIntent.from_draft({"clear_password": True}),
    )

    assert "password" not in grid["mqtt"]


def test_a_missing_credential_fragment_is_a_keep():
    assert CredentialIntent.from_draft(None).operation == KEEP
    assert CredentialIntent.from_draft({}).operation == KEEP


def test_password_is_never_an_editable_mqtt_key():
    for meter_type in (None, "mqtt", "zendure_smartmeter_d0"):
        assert "password" not in editable_grid_meter_mqtt_keys(meter_type)


# --- diff ------------------------------------------------------------------


def _is_secret(path):
    return path.endswith("password") or path.endswith("token")


def test_diff_is_sorted_and_reports_each_kind_of_change():
    before = {"b": 1, "a": {"x": 1}, "gone": True}
    after = {"b": 2, "a": {"x": 1, "added": 3}}
    diff = mutation_diff(before, after, is_secret_leaf=_is_secret)

    assert diff["changed"] is True
    assert [entry["path"] for entry in diff["changes"]] == ["b"]
    assert [entry["path"] for entry in diff["added"]] == ["a.added"]
    assert [entry["path"] for entry in diff["removed"]] == ["gone"]


def test_diff_never_surfaces_a_secret_on_either_side():
    diff = mutation_diff(
        {"mqtt": {"password": "old"}},
        {"mqtt": {"password": "new"}},
        is_secret_leaf=_is_secret,
    )

    entry = diff["changes"][0]
    assert entry["path"] == "mqtt.password"
    assert "old" not in str(entry.values())
    assert "new" not in str(entry.values())


def test_diff_of_identical_configs_is_empty():
    config = {"a": {"b": [1, 2]}, "c": "x"}
    assert mutation_diff(config, dict(config), is_secret_leaf=_is_secret)["changed"] is False


def test_diff_is_deterministic_for_differently_ordered_dicts():
    left = mutation_diff({"a": 1, "b": 2}, {"b": 3, "a": 1}, is_secret_leaf=_is_secret)
    right = mutation_diff({"b": 2, "a": 1}, {"a": 1, "b": 3}, is_secret_leaf=_is_secret)

    assert left == right


def test_device_entries_read_as_indexed_paths():
    diff = mutation_diff(
        {"devices": [{"name": "WR1", "max_power": 800}]},
        {"devices": [{"name": "WR1", "max_power": 900}]},
        is_secret_leaf=_is_secret,
    )

    assert diff["changes"][0]["path"] == "devices[0].max_power"
