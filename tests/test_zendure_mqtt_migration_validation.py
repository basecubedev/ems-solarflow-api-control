# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration must validate and canonicalize its result, never write an invalid config.

Before applying, migration builds the complete result in memory, strips obsolete
write metadata, derives canonical registry metadata, and runs the normal control
validation — refusing to write an invalid result. A pin needs complete write
addressing; exact model evidence with incomplete addressing disables control and
preserves telemetry rather than reporting success while leaving
``write_target_missing``. Every planned change carries a stable device identity
and exact before/after paths, so duplicate device names are never ambiguous.
"""

import copy

import pytest

from ems.zendure_mqtt.config_entries import zendure_mqtt_hardware_profile
from ems.zendure_mqtt.migration import (
    ACTION_DISABLE_CONTROL,
    ACTION_PIN_PROFILE,
    migrate_zendure_mqtt_control_configs,
    plan_zendure_mqtt_migration,
    validate_migrated_zendure_mqtt_config,
    validate_zendure_mqtt_control_configs,
    zendure_mqtt_control_migration_startup_error,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _control_device(**over):
    device = {
        "type": "zendure_mqtt",
        "name": "Legacy",
        "mqtt": {
            "broker_ref": "local_a",
            # The broker profile is authoritative for the transport; a stored
            # entry commonly mirrors it, and it is the write carrier the
            # capability layer reads when no profile map is supplied.
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
        },
        "capabilities": {"write_output_limit": True},
    }
    device.update(over)
    return device


def _config(*devices):
    return {
        "zendure_mqtt": {
            "brokers": {
                "local_a": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                }
            }
        },
        "devices": list(devices),
    }


# --- canonicalization --------------------------------------------------------


def test_pin_removes_stale_write_protocol():
    device = _control_device(product="Hyper 2000")
    device["mqtt"]["write_protocol"] = "legacy_properties_write"
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in device["mqtt"]


def test_pin_overwrites_inconsistent_power_write_profile():
    device = _control_device(
        product="Hyper 2000", power_write_profile="zensdk_properties_write"
    )
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["power_write_profile"] == "legacy_object_device_automation"


def test_pinned_device_with_mismatched_power_write_profile_is_canonicalized():
    # Already pinned + addressable, but a stale mismatched power_write_profile —
    # migration canonicalizes it rather than reporting the config already safe.
    device = _control_device(
        hardware_profile="hyper_2000",
        power_write_profile="zensdk_properties_write",
    )
    changes = plan_zendure_mqtt_migration(_config(device))
    assert len(changes) == 1
    assert changes[0].action == ACTION_PIN_PROFILE
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["power_write_profile"] == "legacy_object_device_automation"


# --- final validation --------------------------------------------------------


def test_migrated_result_passes_normal_validation():
    device = _control_device(product="Hyper 2000")
    config = _config(device)
    assert validate_migrated_zendure_mqtt_config(config) == []


def test_raw_validator_detects_write_target_missing():
    # An already-pinned control device with no product_key/write_topic is invalid.
    device = _control_device(hardware_profile="hyper_2000")
    device["mqtt"].pop("product_key")
    errors = validate_zendure_mqtt_control_configs(_config(device))
    assert any(e["code"] == "write_target_missing" for e in errors)


def test_migrated_config_is_never_write_target_missing():
    device = _control_device(product="Hyper 2000")
    device["mqtt"].pop("product_key")  # exact model but no addressing
    config = _config(device)
    assert validate_migrated_zendure_mqtt_config(config) == []


# --- incomplete addressing ---------------------------------------------------


def test_incomplete_addressing_disables_control_instead_of_pinning():
    device = _control_device(product="Hyper 2000")
    device["mqtt"].pop("product_key")  # no product_key and no write_topic
    changes = plan_zendure_mqtt_migration(_config(device))
    assert [c.action for c in changes] == [ACTION_DISABLE_CONTROL]
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert "hardware_profile" not in device
    # Telemetry is preserved (the device entry itself remains).
    assert device["mqtt"]["device_id"] == "DEV"


def test_incomplete_addressing_warning_is_actionable():
    device = _control_device(product="Hyper 2000")
    device["mqtt"].pop("product_key")
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert warnings
    assert any("address" in w["message"].lower() for w in warnings)


# --- stable identity + concrete diff -----------------------------------------


def test_plan_exposes_concrete_before_after_changes():
    device = _control_device(product="Hyper 2000")
    changes = plan_zendure_mqtt_migration(_config(device))
    change = changes[0]
    entries = {c["path"]: c for c in change.changes}
    assert entries["devices[0].hardware_profile"]["before"] is None
    assert entries["devices[0].hardware_profile"]["after"] == "hyper_2000"
    assert entries["devices[0].power_write_profile"]["after"] == (
        "legacy_object_device_automation"
    )


def test_plan_carries_stable_device_identity():
    device = _control_device(product="Hyper 2000")
    change = plan_zendure_mqtt_migration(_config(device))[0]
    assert change.index == 0
    assert change.device_id == "DEV"


def test_duplicate_device_names_are_not_ambiguous():
    a = _control_device(name="WR", product="Hyper 2000")
    a["mqtt"]["device_id"] = "DEV_A"
    b = _control_device(name="WR")  # same name, no evidence → disable
    b["mqtt"]["device_id"] = "DEV_B"
    config = _config(a, b)
    changes = plan_zendure_mqtt_migration(config)
    assert {c.index for c in changes} == {0, 1}
    assert {c.device_id for c in changes} == {"DEV_A", "DEV_B"}
    # Startup identifies both blocking control devices distinctly.
    error = zendure_mqtt_control_migration_startup_error(config)
    assert error["count"] == 2


# --- invariant: applied result always validates clean ------------------------


@pytest.mark.parametrize(
    "device",
    [
        _control_device(product="Hyper 2000"),
        _control_device(),  # unknown model → disable
        _control_device(product="Hyper", ),  # ambiguous → disable
        _control_device(product="ACE 1500"),  # telemetry-only → disable
        _control_device(product="Hyper 2000", power_write_profile="zensdk_properties_write"),
    ],
)
def test_applied_migration_result_is_always_valid(device):
    config = _config(copy.deepcopy(device))
    migrate_zendure_mqtt_control_configs(config)
    # A second full validation of the mutated config finds nothing to fix/flag.
    assert validate_zendure_mqtt_control_configs(config) == []
    assert plan_zendure_mqtt_migration(config) == []


# --- obsolete profile write_topic: warned, never blocked ---------------------


def _pinned_pro2_with_write_topic():
    device = _control_device(
        hardware_profile="solarflow_800_pro_2",
        power_write_profile="zensdk_properties_write",
    )
    device["mqtt"]["write_topic"] = "/PK/DEV/properties/report"
    return device


def test_profile_write_topic_is_a_warning_not_an_error():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    device = _pinned_pro2_with_write_topic()
    issues = validate_zendure_mqtt_control_device_config(device)
    obsolete = [i for i in issues if i["code"] == "profile_write_topic_obsolete"]
    assert len(obsolete) == 1
    assert obsolete[0]["severity"] == "warning"
    # A warning must not make the device unwritable: no error, no startup block.
    assert [i for i in issues if i["severity"] == "error"] == []
    assert zendure_mqtt_control_migration_startup_error(_config(device)) is None


def test_normalization_clears_the_obsolete_write_topic_warning():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    device = _pinned_pro2_with_write_topic()
    config = _config(device)
    migrate_zendure_mqtt_control_configs(config)
    issues = validate_zendure_mqtt_control_device_config(device)
    assert [i for i in issues if i["code"] == "profile_write_topic_obsolete"] == []


# --- nested mqtt.power_write_profile -----------------------------------------


def _pinned_hyper_with_nested_write_profile(value):
    device = _control_device(hardware_profile="hyper_2000")
    device["mqtt"]["power_write_profile"] = value
    return device


def test_nested_power_write_profile_mismatch_is_migrated_like_the_flat_one():
    device = _pinned_hyper_with_nested_write_profile("zensdk_properties_write")
    config = _config(device)
    assert any(
        e["code"] == "power_write_profile_mismatch"
        for e in validate_zendure_mqtt_control_configs(config)
    )

    changes = plan_zendure_mqtt_migration(config)

    assert [c.action for c in changes] == [ACTION_PIN_PROFILE]
    entries = {c["path"]: c for c in changes[0].changes}
    assert entries["devices[0].mqtt.power_write_profile"] == {
        "path": "devices[0].mqtt.power_write_profile",
        "before": "zensdk_properties_write",
        "after": None,
    }
    assert entries["devices[0].power_write_profile"]["after"] == (
        "legacy_object_device_automation"
    )
    assert zendure_mqtt_control_migration_startup_error(config)["count"] == 1

    migrate_zendure_mqtt_control_configs(config)

    assert device["power_write_profile"] == "legacy_object_device_automation"
    assert "power_write_profile" not in device["mqtt"]
    assert validate_zendure_mqtt_control_configs(config) == []
    assert plan_zendure_mqtt_migration(config) == []


@pytest.mark.parametrize(
    "device",
    [
        _pinned_hyper_with_nested_write_profile("legacy_object_device_automation"),
        {
            **_pinned_hyper_with_nested_write_profile("zensdk_properties_write"),
            "power_write_profile": "legacy_object_device_automation",
        },
    ],
    ids=["nested_matches", "flat_matches_and_wins"],
)
def test_a_power_write_profile_the_validator_accepts_stays_byte_identical(device):
    import json

    config = _config(copy.deepcopy(device))
    before = json.dumps(config, sort_keys=False)
    assert validate_zendure_mqtt_control_configs(config) == []

    assert plan_zendure_mqtt_migration(config) == []
    _cfg, warnings = migrate_zendure_mqtt_control_configs(config)

    assert warnings == []
    assert json.dumps(config, sort_keys=False) == before


# --- a disable that keeps the model -----------------------------------------


def _place(device, level, key, value):
    target = device if level == "flat" else device["mqtt"]
    target[key] = value


def _model_at(device, level):
    return (device if level == "flat" else device["mqtt"]).get("hardware_profile")


def _model_kept_on_disable(hardware_profile, write_profiles, model_at="flat"):
    """A control device the migration disables while it keeps its model.

    ``solarflow_800_pro_2`` on a local scalar route is blocked by the broker
    source; ``ace_1500`` is a read-only model. ``write_profiles`` maps
    ``"flat"`` / ``"nested"`` to the stored ``power_write_profile`` values;
    ``model_at`` says whether the model is ``hardware_profile`` or
    ``mqtt.hardware_profile``.
    """

    device = _control_device()
    _place(device, model_at, "hardware_profile", hardware_profile)
    if hardware_profile == "solarflow_800_pro_2":
        device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
    if "flat" in write_profiles:
        device["power_write_profile"] = write_profiles["flat"]
    if "nested" in write_profiles:
        device["mqtt"]["power_write_profile"] = write_profiles["nested"]
    return device


def _telemetry_errors(config, device):
    from ems.zendure_mqtt.config_entries import (
        broker_sources_from_config,
        validate_zendure_mqtt_device_config,
    )

    return [
        issue["code"]
        for issue in validate_zendure_mqtt_device_config(
            device, broker_sources=broker_sources_from_config(config)
        )
        if issue["severity"] == "error"
    ]


_CONTRADICTING = "legacy_object_device_automation"


@pytest.mark.parametrize(
    ("hardware_profile", "code"),
    [
        ("solarflow_800_pro_2", "zendure_mqtt_control_disabled_broker_source"),
        ("ace_1500", "zendure_mqtt_control_disabled_read_only_model"),
    ],
    ids=["broker_source", "read_only_model"],
)
@pytest.mark.parametrize(
    "write_profiles",
    [
        {"flat": _CONTRADICTING},
        {"nested": _CONTRADICTING},
        {"flat": _CONTRADICTING, "nested": _CONTRADICTING},
    ],
    ids=["flat", "nested", "flat_and_nested"],
)
@pytest.mark.parametrize("model_at", ["flat", "nested"], ids=["model", "mqtt_model"])
def test_a_disable_that_keeps_the_model_removes_a_contradicting_write_profile(
    hardware_profile, code, write_profiles, model_at
):
    from ems.zendure_mqtt.runtime import classify_zendure_mqtt_devices

    device = _model_kept_on_disable(hardware_profile, write_profiles, model_at)
    config = _config(device)

    changes = plan_zendure_mqtt_migration(config)

    assert [(c.action, c.code) for c in changes] == [(ACTION_DISABLE_CONTROL, code)]
    removed = {
        "flat": "devices[0].power_write_profile",
        "nested": "devices[0].mqtt.power_write_profile",
    }
    entries = {c["path"]: c for c in changes[0].changes}
    for location in write_profiles:
        assert entries[removed[location]] == {
            "path": removed[location],
            "before": _CONTRADICTING,
            "after": None,
        }

    _cfg, warnings = migrate_zendure_mqtt_control_configs(config)

    assert [w["code"] for w in warnings] == [code]
    assert changes[0].hardware_profile == hardware_profile
    assert _model_at(device, model_at) == hardware_profile
    assert device["capabilities"]["write_output_limit"] is False
    assert "power_write_profile" not in device
    assert "power_write_profile" not in device["mqtt"]
    assert _telemetry_errors(config, device) == []
    valid, invalid = classify_zendure_mqtt_devices(
        config["devices"], known_broker_refs=["local_a"], brokers_defined=True
    )
    assert [d.name for d in valid] == ["Legacy"] and invalid == []
    assert plan_zendure_mqtt_migration(config) == []


@pytest.mark.parametrize(
    ("hardware_profile", "write_profiles"),
    [
        ("solarflow_800_pro_2", {}),
        ("solarflow_800_pro_2", {"flat": "zensdk_properties_write"}),
        ("solarflow_800_pro_2", {"nested": "zensdk_properties_write"}),
        (
            "solarflow_800_pro_2",
            {"flat": "zensdk_properties_write", "nested": _CONTRADICTING},
        ),
        ("ace_1500", {"flat": "telemetry_only"}),
        ("ace_1500", {"nested": "telemetry_only"}),
    ],
    ids=[
        "none",
        "flat_matches",
        "nested_matches",
        "flat_matches_and_wins",
        "read_only_flat_matches",
        "read_only_nested_matches",
    ],
)
@pytest.mark.parametrize("model_at", ["flat", "nested"], ids=["model", "mqtt_model"])
def test_a_disable_that_keeps_the_model_leaves_an_accepted_write_profile_alone(
    hardware_profile, write_profiles, model_at
):
    device = _model_kept_on_disable(hardware_profile, write_profiles, model_at)
    config = _config(device)
    expected = copy.deepcopy(device)
    expected["capabilities"]["write_output_limit"] = False

    changes = plan_zendure_mqtt_migration(config)

    assert [c.action for c in changes] == [ACTION_DISABLE_CONTROL]
    assert [c["path"] for c in changes[0].changes] == [
        "devices[0].capabilities.write_output_limit"
    ]
    migrate_zendure_mqtt_control_configs(config)
    assert device == expected
    assert _telemetry_errors(config, device) == []


# --- a disable that drops the model -----------------------------------------


_MODEL_KEYS = (
    ("devices[0].hardware_profile", lambda d: d.get("hardware_profile")),
    ("devices[0].power_write_profile", lambda d: d.get("power_write_profile")),
    ("devices[0].mqtt.hardware_profile", lambda d: d["mqtt"].get("hardware_profile")),
    (
        "devices[0].mqtt.power_write_profile",
        lambda d: d["mqtt"].get("power_write_profile"),
    ),
)


@pytest.mark.parametrize(
    "write_profiles",
    [
        {},
        {"flat": "legacy_object_device_automation"},
        {"flat": "zensdk_properties_write"},
        {"nested": "legacy_object_device_automation"},
        {"nested": "zensdk_properties_write"},
        {"flat": "legacy_object_device_automation", "nested": "zensdk_properties_write"},
    ],
    ids=[
        "none",
        "flat_matches",
        "flat_contradicts",
        "nested_matches",
        "nested_contradicts",
        "flat_matches_nested_contradicts",
    ],
)
@pytest.mark.parametrize("model_at", ["flat", "nested"], ids=["model", "mqtt_model"])
def test_a_disable_that_drops_the_model_removes_it_at_both_levels_and_lists_each_key(
    write_profiles, model_at
):
    """An unaddressable ``hyper_2000`` (no product_key) is disabled and its model dropped."""

    device = _control_device()
    device["mqtt"].pop("product_key")
    _place(device, model_at, "hardware_profile", "hyper_2000")
    for level, value in write_profiles.items():
        _place(device, level, "power_write_profile", value)
    config = _config(device)
    stored = {path: read(device) for path, read in _MODEL_KEYS if read(device) is not None}

    changes = plan_zendure_mqtt_migration(config)

    assert [(c.action, c.code, c.hardware_profile) for c in changes] == [
        (ACTION_DISABLE_CONTROL, "zendure_mqtt_control_disabled_unaddressable", None)
    ]
    entries = {c["path"]: c for c in changes[0].changes}
    for path, before in stored.items():
        assert entries[path] == {"path": path, "before": before, "after": None}

    migrate_zendure_mqtt_control_configs(config)

    assert [path for path, read in _MODEL_KEYS if read(device) is not None] == []
    assert device["capabilities"]["write_output_limit"] is False
    assert device["mqtt"]["device_id"] == "DEV"
    assert _telemetry_errors(config, device) == []
    assert plan_zendure_mqtt_migration(config) == []


def test_a_broker_source_disable_without_a_pinned_model_lists_the_write_profile_it_removes():
    """The model comes only from ``product``: nothing is pinned, so nothing is kept."""

    device = _control_device(
        product="SolarFlow 800 Pro 2", power_write_profile="zensdk_properties_write"
    )
    device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
    config = _config(device)

    changes = plan_zendure_mqtt_migration(config)

    assert [(c.code, c.hardware_profile) for c in changes] == [
        ("zendure_mqtt_control_disabled_broker_source", None)
    ]
    assert {c["path"]: c for c in changes[0].changes}[
        "devices[0].power_write_profile"
    ] == {
        "path": "devices[0].power_write_profile",
        "before": "zensdk_properties_write",
        "after": None,
    }
    migrate_zendure_mqtt_control_configs(config)
    assert "power_write_profile" not in device


# --- every disable writes exactly what its plan lists -----------------------


_ABSENT = object()


def _leaves(mapping, prefix):
    out = {}
    for key, value in mapping.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(_leaves(value, path))
        else:
            out[path] = value
    return out


def _written_paths(before, after):
    old, new = _leaves(before, "devices[0]"), _leaves(after, "devices[0]")
    return {
        path
        for path in old.keys() | new.keys()
        if old.get(path, _ABSENT) != new.get(path, _ABSENT)
    }


def _disable_shape(shape, model_at):
    device = _control_device()
    if shape == "broker_source":
        device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
        _place(device, model_at, "hardware_profile", "solarflow_800_pro_2")
    elif shape == "broker_source_from_product":
        device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
        device["product"] = "SolarFlow 800 Pro 2"
    elif shape == "read_only_model":
        _place(device, model_at, "hardware_profile", "ace_1500")
    elif shape == "unaddressable":
        device["mqtt"].pop("product_key")
        _place(device, model_at, "hardware_profile", "hyper_2000")
    elif shape == "unaddressable_from_product":
        device["mqtt"].pop("product_key")
        device["product"] = "Hyper 2000"
    elif shape == "custom_unaddressable":
        device["mqtt"].pop("product_key")
        device["mqtt"]["write_protocol"] = "custom_properties_write"
        device["mqtt"]["write_topic"] = "iot/PK/+/properties/write"
    elif shape == "unknown_model":
        device["mqtt"]["write_protocol"] = "legacy_properties_write"
    return device


_DISABLE_SHAPES = [
    *(
        pytest.param(shape, model_at, id=f"{shape}-{model_at}")
        for shape in ("broker_source", "read_only_model", "unaddressable")
        for model_at in ("flat", "nested")
    ),
    *(
        pytest.param(shape, None, id=shape)
        for shape in (
            "broker_source_from_product",
            "unaddressable_from_product",
            "custom_unaddressable",
            "unknown_model",
        )
    ),
]

_WRITE_PROFILE_SETS = {
    "none": {},
    "flat_a": {"flat": "legacy_object_device_automation"},
    "flat_b": {"flat": "zensdk_properties_write"},
    "flat_c": {"flat": "telemetry_only"},
    "nested_a": {"nested": "legacy_object_device_automation"},
    "nested_b": {"nested": "zensdk_properties_write"},
    "nested_c": {"nested": "telemetry_only"},
    "flat_a_nested_b": {
        "flat": "legacy_object_device_automation",
        "nested": "zensdk_properties_write",
    },
    "flat_b_nested_a": {
        "flat": "zensdk_properties_write",
        "nested": "legacy_object_device_automation",
    },
    "flat_c_nested_a": {
        "flat": "telemetry_only",
        "nested": "legacy_object_device_automation",
    },
}


@pytest.mark.parametrize("write_profiles", list(_WRITE_PROFILE_SETS), ids=str)
@pytest.mark.parametrize(("shape", "model_at"), _DISABLE_SHAPES)
def test_every_disable_writes_exactly_the_paths_its_plan_lists(
    shape, model_at, write_profiles
):
    """Walks every disable shape: apply writes the plan's paths and no others."""

    device = _disable_shape(shape, model_at)
    for level, value in _WRITE_PROFILE_SETS[write_profiles].items():
        _place(device, level, "power_write_profile", value)
    config = _config(device)
    before = copy.deepcopy(device)

    changes = plan_zendure_mqtt_migration(config)

    assert [c.action for c in changes] == [ACTION_DISABLE_CONTROL]
    planned = {entry["path"] for entry in changes[0].changes}
    migrate_zendure_mqtt_control_configs(config)
    assert _written_paths(before, device) == planned
    assert _telemetry_errors(config, device) == []
    assert plan_zendure_mqtt_migration(config) == []


@pytest.mark.parametrize("nested", ["hyper_2000", "solarflow_800_pro_2"])
def test_a_dropping_disable_removes_the_model_at_both_levels_when_both_are_stored(nested):
    """A model stored at the top level and under ``mqtt`` alike leaves neither
    behind, so the nested one cannot become the effective model."""

    device = _control_device(hardware_profile="hyper_2000")
    device["mqtt"].pop("product_key")
    device["mqtt"]["hardware_profile"] = nested
    config = _config(device)

    paths = {change["path"] for change in plan_zendure_mqtt_migration(config)[0].changes}
    migrate_zendure_mqtt_control_configs(config)

    assert "devices[0].mqtt.hardware_profile" in paths
    assert zendure_mqtt_hardware_profile(device) is None, device
