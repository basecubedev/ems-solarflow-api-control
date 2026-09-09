# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write-gate projection from a saved config, without loading it into EMS.

The Admin Console must be able to state which transports a stored config would
permit without becoming a second write-gate authority. These contracts pin the
projection to the same policy the running controller applies.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ems.config as cfg

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

GATES = ("api", "mqtt_local", "mqtt_zendure")


def _config(system=None, devices=None, zendure_mqtt=None):
    config = {"system": dict(system or {})}
    if devices is not None:
        config["devices"] = devices
    if zendure_mqtt is not None:
        config["zendure_mqtt"] = zendure_mqtt
    return config


def _system(**overrides):
    system = {
        "enabled": True,
        "dry_run": False,
        "simulation_mode": False,
        "allow_hardware_writes": True,
        "allow_mqtt_local_control_writes": True,
        "allow_mqtt_zendure_control_writes": True,
    }
    system.update(overrides)
    return system


def _globals_matching(system):
    return patch.multiple(
        cfg,
        DRY_RUN=system["dry_run"],
        SIMULATION_MODE=system["simulation_mode"],
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=system["allow_hardware_writes"],
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=system["allow_mqtt_local_control_writes"],
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=system["allow_mqtt_zendure_control_writes"],
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"dry_run": True},
        {"simulation_mode": True},
        {"allow_hardware_writes": False},
        {"allow_mqtt_local_control_writes": False},
        {"allow_mqtt_zendure_control_writes": False},
        {"dry_run": True, "allow_hardware_writes": False},
        {
            "allow_hardware_writes": False,
            "allow_mqtt_local_control_writes": False,
            "allow_mqtt_zendure_control_writes": False,
        },
    ],
)
def test_projection_matches_the_loaded_resolver(overrides):
    """The projection is a view of one policy, never a second implementation."""

    system = _system(**overrides)
    config = _config(system)
    with _globals_matching(system):
        for gate in GATES:
            assert cfg.resolve_config_write_gate(config, gate) == cfg.resolve_write_gate(
                gate
            )


def test_missing_gate_keys_resolve_to_release_defaults():
    config = _config({"enabled": True, "dry_run": False, "simulation_mode": False})
    for gate in GATES:
        decision = cfg.resolve_config_write_gate(config, gate)
        assert decision.gate_enabled is True
        assert decision.allowed is True


def test_dry_run_blocks_every_transport():
    config = _config(_system(dry_run=True))
    for gate in GATES:
        decision = cfg.resolve_config_write_gate(config, gate)
        assert decision.allowed is False
        assert "dry_run" in decision.blocked_by


def test_simulation_mode_blocks_every_transport():
    config = _config(_system(simulation_mode=True))
    for gate in GATES:
        decision = cfg.resolve_config_write_gate(config, gate)
        assert decision.allowed is False
        assert "simulation_mode" in decision.blocked_by


def test_each_transport_names_its_own_gate():
    config = _config(
        _system(
            allow_hardware_writes=True,
            allow_mqtt_local_control_writes=False,
            allow_mqtt_zendure_control_writes=False,
        )
    )
    assert cfg.resolve_config_write_gate(config, "api").gate_name == (
        "allow_hardware_writes"
    )
    assert cfg.resolve_config_write_gate(config, "api").allowed is True
    assert cfg.resolve_config_write_gate(config, "mqtt_local").gate_name == (
        "allow_mqtt_local_control_writes"
    )
    assert cfg.resolve_config_write_gate(config, "mqtt_local").allowed is False
    assert cfg.resolve_config_write_gate(config, "mqtt_zendure").gate_name == (
        "allow_mqtt_zendure_control_writes"
    )
    assert cfg.resolve_config_write_gate(config, "mqtt_zendure").allowed is False


def test_unknown_transport_falls_back_to_the_api_gate():
    config = _config(_system())
    decision = cfg.resolve_config_write_gate(config, "nonsense")
    assert decision.gate_name == "allow_hardware_writes"
    assert decision.transport == "http"


@pytest.mark.parametrize("config", [None, [], "config", {"system": "broken"}])
def test_unreadable_config_fails_closed(config):
    for gate in GATES:
        decision = cfg.resolve_config_write_gate(config, gate)
        assert decision.allowed is False
        assert decision.gate_enabled is False
        assert decision.gate_name in decision.blocked_by


def test_non_boolean_gate_value_is_never_trusted_as_armed():
    config = _config(_system(allow_hardware_writes="true"))
    decision = cfg.resolve_config_write_gate(config, "api")
    assert decision.gate_enabled is False
    assert decision.allowed is False


def _api_device(name="api-1", enabled=True):
    return {
        "name": name,
        "ip": "192.168.50.20",
        "sn": "API-SERIAL",
        "enabled": enabled,
    }


def _mqtt_device(name, broker_ref, enabled=True, control=True):
    return {
        "name": name,
        "type": "zendure_mqtt",
        "enabled": enabled,
        "capabilities": {"write_output_limit": control},
        "mqtt": {"broker_ref": broker_ref, "device_id": name.upper()},
    }


def _brokers(**profiles):
    return {"brokers": {ref: dict(profile) for ref, profile in profiles.items()}}


def test_control_gate_counts_group_devices_by_their_transport():
    config = _config(
        _system(),
        devices=[
            _api_device("api-1"),
            _api_device("api-2"),
            _mqtt_device("local-1", "home"),
            _mqtt_device("cloud-1", "account"),
        ],
        zendure_mqtt=_brokers(
            home={"host": "192.168.50.10", "source": "local_mqtt"},
            account={"host": "mq.zen-iot.com", "source": "zendure_cloud_mqtt"},
        ),
    )
    assert cfg.config_control_gate_counts(config) == {
        "api": 2,
        "mqtt_local": 1,
        "mqtt_zendure": 1,
    }


def test_disabled_and_telemetry_only_devices_arm_no_gate():
    config = _config(
        _system(),
        devices=[
            _api_device("api-off", enabled=False),
            _mqtt_device("mqtt-off", "home", enabled=False),
            _mqtt_device("telemetry", "home", control=False),
        ],
        zendure_mqtt=_brokers(home={"host": "192.168.50.10", "source": "local_mqtt"}),
    )
    assert cfg.config_control_gate_counts(config) == {
        "api": 0,
        "mqtt_local": 0,
        "mqtt_zendure": 0,
    }


def test_broker_profile_decides_the_gate_not_the_device_entry():
    """The runtime takes the transport from the broker profile; so does this."""

    device = _mqtt_device("claims-local", "account")
    device["mqtt"]["source"] = "local_mqtt"
    config = _config(
        _system(),
        devices=[device],
        zendure_mqtt=_brokers(
            account={"host": "mq.zen-iot.com", "source": "zendure_cloud_mqtt"}
        ),
    )
    assert cfg.config_control_gate_counts(config)["mqtt_zendure"] == 1
    assert cfg.config_control_gate_counts(config)["mqtt_local"] == 0


def test_legacy_default_broker_without_source_counts_as_local():
    config = _config(
        _system(),
        devices=[_mqtt_device("legacy", "default")],
        zendure_mqtt={"host": "192.168.50.10"},
    )
    assert cfg.config_control_gate_counts(config)["mqtt_local"] == 1


def test_unresolvable_broker_ref_never_counts_as_an_api_device():
    config = _config(
        _system(),
        devices=[_mqtt_device("orphan", "missing")],
        zendure_mqtt=_brokers(home={"host": "192.168.50.10", "source": "local_mqtt"}),
    )
    counts = cfg.config_control_gate_counts(config)
    assert counts["api"] == 0
    assert counts["mqtt_local"] == 1


@pytest.mark.parametrize("config", [None, [], "config", {}, {"devices": "broken"}])
def test_gate_counts_stay_zero_for_an_unreadable_config(config):
    assert cfg.config_control_gate_counts(config) == {
        "api": 0,
        "mqtt_local": 0,
        "mqtt_zendure": 0,
    }


def test_control_flags_read_a_normal_config():
    flags = cfg.config_control_flags(_config(_system()))
    assert flags == {
        "enabled": True,
        "dry_run": False,
        "simulation_mode": False,
        "allow_state_reconciliation_writes": True,
    }


def test_missing_control_flags_resolve_to_the_template_defaults():
    flags = cfg.config_control_flags({"system": {}})
    assert flags["enabled"] is True
    assert flags["dry_run"] is False
    assert flags["simulation_mode"] is False
    assert flags["allow_state_reconciliation_writes"] is True


@pytest.mark.parametrize(
    "flag, blocking",
    [
        ("enabled", False),
        ("dry_run", True),
        ("simulation_mode", True),
        ("allow_state_reconciliation_writes", False),
    ],
)
def test_a_non_boolean_control_flag_resolves_to_its_blocking_side(flag, blocking):
    flags = cfg.config_control_flags(_config(_system(**{flag: "yes"})))
    assert flags[flag] is blocking


@pytest.mark.parametrize("config", [None, [], "config", {}, {"system": "broken"}])
def test_control_flags_stay_safe_for_an_unreadable_config(config):
    flags = cfg.config_control_flags(config)
    assert flags["dry_run"] is False
    assert flags["simulation_mode"] is False


def test_grouped_control_devices_back_the_counts():
    config = _config(
        _system(),
        devices=[_api_device("api-1"), _mqtt_device("local-1", "home")],
        zendure_mqtt=_brokers(home={"host": "192.168.50.10", "source": "local_mqtt"}),
    )
    grouped = cfg.config_control_devices_by_gate(config)
    assert [item["name"] for item in grouped["api"]] == ["api-1"]
    assert [item["name"] for item in grouped["mqtt_local"]] == ["local-1"]
    assert grouped["mqtt_zendure"] == []
    assert cfg.config_control_gate_counts(config) == {
        gate: len(items) for gate, items in grouped.items()
    }
