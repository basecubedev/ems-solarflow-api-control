# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one catalog query API behind every Admin config field index.

Setup and Maintenance ask different questions of the catalog, but neither may
re-derive which fields exist or when one is editable. The filter matrix lives
here; the Admin modules only pass their filters.
"""

import pytest

from ems.config_catalog import (
    DEVICE_IDENTITY_FIELD_KEYS,
    config_field_index,
    get_config_feature_field_index,
    is_editable_catalog_field,
    is_secret_catalog_field,
)

pytestmark = pytest.mark.simulation


# --- editability predicate ---------------------------------------------------
@pytest.mark.parametrize(
    "field,scope,expected",
    [
        ({"scope": "setup"}, "setup", True),
        ({"scope": "both"}, "setup", True),
        ({"scope": "both"}, "maintenance", True),
        ({"scope": "maintenance"}, "setup", False),
        ({"scope": "setup"}, "maintenance", False),
        ({}, "setup", False),
        ({"scope": "both", "level": "deprecated"}, "setup", False),
        ({"scope": "both", "editable": False}, "setup", False),
        ({"scope": "both", "editable": True}, "setup", True),
    ],
)
def test_scope_and_visibility_filter(field, scope, expected):
    assert is_editable_catalog_field(field, scope=scope) is expected


@pytest.mark.parametrize("field", [{"risk": "secret"}, {"type": "password"}])
def test_secret_fields_are_optional_per_consumer(field):
    declared = {"scope": "both", **field}
    assert is_secret_catalog_field(declared) is True
    assert is_editable_catalog_field(declared, scope="maintenance") is True
    assert (
        is_editable_catalog_field(declared, scope="maintenance", allow_secret=False)
        is False
    )


def test_plain_field_is_never_secret():
    assert is_secret_catalog_field({"type": "string", "key": "password_policy"}) is False


# --- query filters against the real catalog ----------------------------------
def test_index_is_a_subset_of_the_catalog_with_matching_scope():
    catalog = get_config_feature_field_index()
    index = config_field_index(scope="setup")
    assert index
    assert set(index) <= set(catalog)
    for path, field in index.items():
        assert is_editable_catalog_field(field, scope="setup")
        assert catalog[path]["path"] == path


def test_allow_secret_false_drops_every_secret_field():
    with_secrets = config_field_index(scope="maintenance")
    without = config_field_index(scope="maintenance", allow_secret=False)
    assert set(without) <= set(with_secrets)
    assert not [f for f in without.values() if is_secret_catalog_field(f)]
    dropped = set(with_secrets) - set(without)
    assert all(is_secret_catalog_field(with_secrets[p]) for p in dropped)


def test_exclude_repeated_and_prefixes():
    index = config_field_index(
        scope="maintenance",
        allow_secret=False,
        exclude_repeated=True,
        exclude_prefixes=("devices", "grid_meter"),
    )
    assert index
    assert not [p for p in index if "[]" in p]
    assert not [p for p in index if p.startswith(("devices", "grid_meter"))]


def test_prefix_keys_by_the_remainder():
    index = config_field_index(scope="maintenance", prefix="devices[].")
    assert index
    assert not [key for key in index if key.startswith("devices[].")]
    assert "max_power" in index


def test_flat_keys_reject_nested_and_repeated_remainders():
    flat = config_field_index(scope="setup", prefix="devices[].", flat_keys=True)
    nested = config_field_index(scope="setup", prefix="devices[].")
    assert set(flat) <= set(nested)
    assert not [key for key in flat if "." in key or "[" in key]


def test_exclude_keys_removes_exactly_those_keys():
    full = config_field_index(scope="maintenance", prefix="devices[].")
    trimmed = config_field_index(
        scope="maintenance", prefix="devices[].", exclude_keys=DEVICE_IDENTITY_FIELD_KEYS
    )
    assert set(full) - set(trimmed) == set(DEVICE_IDENTITY_FIELD_KEYS) & set(full)


def test_index_returns_copies_not_catalog_state():
    index = config_field_index(scope="setup")
    path = next(iter(index))
    index[path]["label"] = "mutated"
    assert config_field_index(scope="setup")[path].get("label") != "mutated"


# --- the Admin consumers ask this API, and nothing else ----------------------
def test_setup_indexes_delegate_to_the_catalog_query():
    from admin import setup_config

    assert setup_config._setup_field_index() == config_field_index(scope="setup")
    assert setup_config._device_field_index() == config_field_index(
        scope="setup",
        prefix="devices[].",
        flat_keys=True,
        exclude_keys=setup_config._DEVICE_MAPPED_KEYS,
    )
    # Identity fields keep their dedicated draft mapping and are never writable
    # through the generic per-device value path.
    for key in setup_config._DEVICE_MAPPED_KEYS:
        assert key not in setup_config._device_field_index()


def test_maintenance_indexes_delegate_to_the_catalog_query():
    from admin import device_common_fields, maintenance_config

    assert maintenance_config._maintenance_field_index() == config_field_index(
        scope="maintenance",
        allow_secret=False,
        exclude_repeated=True,
        exclude_prefixes=("devices", "grid_meter"),
    )
    assert device_common_fields.common_device_value_fields() == config_field_index(
        scope="maintenance",
        allow_secret=False,
        prefix="devices[].",
        exclude_keys=DEVICE_IDENTITY_FIELD_KEYS,
    )


def test_the_two_maintenance_editability_predicates_are_one_rule():
    from admin import device_common_fields, maintenance_config

    for field in get_config_feature_field_index().values():
        assert device_common_fields.is_editable_maintenance_field(field) == (
            maintenance_config._is_maintenance_field(field)
        )


def test_no_editor_index_can_surface_a_secret_value():
    from admin import device_common_fields, maintenance_config

    for index in (
        maintenance_config._maintenance_field_index(),
        device_common_fields.common_device_value_fields(),
    ):
        assert not [f for f in index.values() if is_secret_catalog_field(f)]
