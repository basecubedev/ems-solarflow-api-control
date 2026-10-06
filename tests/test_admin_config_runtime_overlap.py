# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only config/runtime overlap provenance for Admin maintenance (Tier 1)."""

import json
from types import SimpleNamespace

import pytest

from admin.config_runtime_overlap import (
    compute_overlap_provenance,
    read_runtime_state,
    resolve_runtime_state_path,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def _config():
    return {
        "system": {
            "enabled": True,
            "max_total_power": 1600,
            "loop_interval": 3,
            "min_output_limit": 35,
        },
        "winter": {"enabled": False},
        "devices": [
            {"name": "WR1", "max_power": 800, "pv_priority_factor": 1.0, "enabled": True},
        ],
    }


def test_no_runtime_data_is_all_config():
    prov = compute_overlap_provenance(_config(), {})
    assert prov["system.loop_interval"] == {
        "config_value": 3,
        "effective_value": 3,
        "source": "config",
    }
    assert prov["devices"]["WR1"]["max_power"]["source"] == "config"
    assert prov["winter.enabled"]["source"] == "config"


def test_runtime_override_is_detected():
    runtime = {"system": {"loop_interval": 5}, "devices": {"WR1": {"max_power": 600}}}
    prov = compute_overlap_provenance(_config(), runtime)
    loop = prov["system.loop_interval"]
    assert loop["config_value"] == 3
    assert loop["effective_value"] == 5
    assert loop["source"] == "dashboard_override"
    device = prov["devices"]["WR1"]["max_power"]
    assert device["effective_value"] == 600
    assert device["source"] == "dashboard_override"


def test_runtime_equal_to_config_is_not_override():
    prov = compute_overlap_provenance(_config(), {"system": {"loop_interval": 3}})
    assert prov["system.loop_interval"]["source"] == "config"
    assert prov["system.loop_interval"]["effective_value"] == 3


def test_winter_enabled_override():
    prov = compute_overlap_provenance(_config(), {"winter": {"enabled": True}})
    assert prov["winter.enabled"]["effective_value"] is True
    assert prov["winter.enabled"]["source"] == "dashboard_override"


def test_missing_device_in_runtime_is_config():
    prov = compute_overlap_provenance(_config(), {"devices": {}})
    assert prov["devices"]["WR1"]["enabled"]["source"] == "config"


def test_resolve_path_honors_configured_relative_path(tmp_path):
    context = SimpleNamespace(install_root=tmp_path)
    path = resolve_runtime_state_path(
        context, {"system": {"runtime_state_path": "data/rt.json"}}
    )
    assert path == str(tmp_path / "data" / "rt.json")


def test_resolve_path_defaults_when_config_silent(tmp_path):
    context = SimpleNamespace(install_root=tmp_path)
    path = resolve_runtime_state_path(context, {})
    assert path.endswith("runtime-state.json")


def test_resolve_path_absolute_is_used_verbatim(tmp_path):
    context = SimpleNamespace(install_root=tmp_path)
    absolute = str(tmp_path / "elsewhere" / "rt.json")
    path = resolve_runtime_state_path(context, {"system": {"runtime_state_path": absolute}})
    assert path == absolute


def test_read_missing_file_returns_empty(tmp_path):
    assert read_runtime_state(str(tmp_path / "nope.json")) == {}


def test_read_malformed_file_returns_empty(tmp_path):
    path = tmp_path / "rt.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_runtime_state(str(path)) == {}


def test_read_valid_file(tmp_path):
    path = tmp_path / "rt.json"
    path.write_text(json.dumps({"system": {"loop_interval": 9}}), encoding="utf-8")
    assert read_runtime_state(str(path)) == {"system": {"loop_interval": 9}}


def _config_silent_on_every_overlap_key(**system):
    """An installation configured before these keys existed: none of them set."""

    return {
        "config_upgrade": {"on_startup": "disabled"},
        "system": {"dry_run": True, **system},
        "grid_meter": {"type": "shelly", "ip": "10.0.0.50"},
        "zendure_mqtt": {
            "brokers": {
                "local": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.60",
                    "port": 1883,
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "WR1",
                "hardware_profile": "solarflow_800_pro_2",
                "mqtt": {
                    "broker_ref": "local",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV1",
                    "product_key": "PK1",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


def _runtime_the_ems_seeds(tmp_path, config):
    """The runtime-state the EMS itself creates for ``config``, built the EMS's way."""

    from ems import config as cfg
    from ems.runtime_state import build_runtime_defaults
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    args = SimpleNamespace(
        config=str(path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=True,
    )
    snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
    try:
        cfg.initialize(args, str(tmp_path))
        runtime = build_zendure_mqtt_control_runtime(
            cfg.CONFIG, service_factory=lambda _broker: SimpleNamespace()
        )
        assert not runtime.rejected, runtime.rejected
        return build_runtime_defaults(runtime.devices)
    finally:
        for name, value in snapshot.items():
            setattr(cfg, name, value)


@pytest.mark.parametrize("system", [{}, {"max_device_power": 1200}])
def test_every_overlap_key_defaults_to_what_the_ems_applies(tmp_path, system):
    """Walks every whitelisted key against the runtime the EMS seeds itself.

    The default stands in for the EMS when the config is silent. Written down a
    second time it drifts: AC charging was on in the EMS and off in this view, so
    every upgraded installation showed a Dashboard override nobody had made, and
    resetting it switched charging off.
    """

    from admin.config_runtime_overlap import (
        DEVICE_FIELDS,
        SECTION_FIELDS,
        SYSTEM_FIELDS,
        config_effective_value,
    )

    config = _config_silent_on_every_overlap_key(**system)
    seeded = _runtime_the_ems_seeds(tmp_path, config)

    differing = []
    for key in SYSTEM_FIELDS:
        admin = config_effective_value(config, "system", None, key)
        if admin != seeded["system"].get(key):
            differing.append(("system." + key, admin, seeded["system"].get(key)))
    for section, fields in SECTION_FIELDS.items():
        for key in fields:
            admin = config_effective_value(config, "section", section, key)
            ems = seeded.get(section, {}).get(key)
            if admin != ems:
                differing.append((section + "." + key, admin, ems))
    for key in DEVICE_FIELDS:
        admin = config_effective_value(config, "device", "WR1", key)
        ems = seeded["devices"]["WR1"].get(key)
        if admin != ems:
            differing.append(("devices[]." + key, admin, ems))

    assert differing == [], differing

    provenance = compute_overlap_provenance(config, seeded)
    overrides = sorted(
        path
        for path, entry in provenance.items()
        if path != "devices" and entry["source"] != "config"
    ) + sorted(
        "devices.WR1." + key
        for key, entry in provenance["devices"]["WR1"].items()
        if entry["source"] != "config"
    )
    assert overrides == []
