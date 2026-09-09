# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only control-and-safety state of the installed config.

The Maintenance overview must be able to answer "is EMS allowed to change my
inverter" without becoming a second write-gate authority and without ever
claiming more than the config proves.
"""

import json

import pytest

from admin.maintenance_config import load_maintenance_config

pytestmark = [
    pytest.mark.admin,
    pytest.mark.config,
    pytest.mark.integration,
    pytest.mark.simulation,
]

GATE_ORDER = (
    "allow_hardware_writes",
    "allow_mqtt_local_control_writes",
    "allow_mqtt_zendure_control_writes",
)


def _system(**overrides):
    system = {
        "enabled": True,
        "dry_run": False,
        "simulation_mode": False,
        "allow_hardware_writes": True,
        "allow_mqtt_local_control_writes": True,
        "allow_mqtt_zendure_control_writes": True,
        "allow_state_reconciliation_writes": True,
        "max_total_power": 1600,
        "max_device_power": 800,
    }
    system.update(overrides)
    return system


def _api_device(name="WR1", **overrides):
    device = {
        "name": name,
        "ip": "192.168.50.20",
        "sn": "AAA",
        "max_power": 800,
        "min_soc": 10,
        "max_soc": 100,
    }
    device.update(overrides)
    return device


def _mqtt_device(name="WR2", broker_ref="home", control=True, **overrides):
    device = {
        "name": name,
        "type": "zendure_mqtt",
        "capabilities": {"write_output_limit": control},
        "mqtt": {"broker_ref": broker_ref, "device_id": "DEV-2"},
        "min_soc": 10,
        "max_soc": 100,
    }
    device.update(overrides)
    return device


def _config(system=None, devices=None, zendure_mqtt=None):
    config = {
        "system": _system(**(system or {})),
        "devices": devices if devices is not None else [_api_device()],
        "grid_meter": {"type": "shelly", "ip": "192.168.50.2"},
    }
    if zendure_mqtt is not None:
        config["zendure_mqtt"] = zendure_mqtt
    return config


def _control(tmp_path, config):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    return loaded["summary"]["control"]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def test_a_writing_installation_reports_that_control_is_permitted(tmp_path):
    control = _control(tmp_path, _config())
    assert control["status"] == "may_control"
    assert control["enabled"] is True
    assert control["dry_run"] is False
    assert control["simulation_mode"] is False


def test_transports_are_listed_with_their_gate_and_device_count(tmp_path):
    config = _config(
        devices=[_api_device(), _api_device("WR2", sn="BBB"), _mqtt_device()],
        zendure_mqtt={
            "brokers": {"home": {"host": "192.168.50.10", "source": "local_mqtt"}}
        },
    )
    control = _control(tmp_path, config)
    transports = {entry["gate"]: entry for entry in control["transports"]}
    assert tuple(entry["gate"] for entry in control["transports"]) == GATE_ORDER
    assert transports["allow_hardware_writes"]["device_count"] == 2
    assert transports["allow_hardware_writes"]["armed"] is True
    assert transports["allow_mqtt_local_control_writes"]["device_count"] == 1
    assert transports["allow_mqtt_zendure_control_writes"]["device_count"] == 0


def test_a_closed_gate_is_reported_with_the_reason_that_blocks_it(tmp_path):
    control = _control(tmp_path, _config({"allow_hardware_writes": False}))
    transports = {entry["gate"]: entry for entry in control["transports"]}
    api = transports["allow_hardware_writes"]
    assert api["armed"] is False
    assert api["blocked_by"] == ["allow_hardware_writes"]
    assert control["status"] == "not_writing"


def test_dry_run_is_named_as_the_cause_and_never_reads_as_controlling(tmp_path):
    control = _control(tmp_path, _config({"dry_run": True}))
    assert control["status"] == "calculating_only"
    assert control["dry_run"] is True
    for entry in control["transports"]:
        assert entry["armed"] is False
        assert "dry_run" in entry["blocked_by"]


def test_simulation_mode_outranks_dry_run_in_the_reported_status(tmp_path):
    control = _control(tmp_path, _config({"simulation_mode": True, "dry_run": True}))
    assert control["status"] == "simulated"


def test_a_disabled_ems_outranks_every_other_cause(tmp_path):
    control = _control(
        tmp_path, _config({"enabled": False, "simulation_mode": True, "dry_run": True})
    )
    assert control["status"] == "disabled"


def test_an_armed_gate_without_a_device_does_not_read_as_permitted(tmp_path):
    control = _control(tmp_path, _config(devices=[]))
    assert control["status"] == "not_writing"
    for entry in control["transports"]:
        assert entry["device_count"] == 0


def test_a_disabled_device_arms_no_transport(tmp_path):
    control = _control(tmp_path, _config(devices=[_api_device(enabled=False)]))
    assert control["status"] == "not_writing"


def test_missing_gate_keys_report_the_release_defaults(tmp_path):
    config = _config()
    for gate in GATE_ORDER:
        config["system"].pop(gate)
    control = _control(tmp_path, config)
    assert control["status"] == "may_control"
    assert all(entry["armed"] for entry in control["transports"])


def test_a_non_boolean_flag_never_reads_as_permitted(tmp_path):
    control = _control(tmp_path, _config({"dry_run": "false"}))
    assert control["status"] != "may_control"


def test_state_reconciliation_is_reported_separately(tmp_path):
    assert _control(tmp_path, _config())["state_reconciliation"] is True
    assert (
        _control(tmp_path, _config({"allow_state_reconciliation_writes": False}))[
            "state_reconciliation"
        ]
        is False
    )


def test_the_physical_envelope_is_reported(tmp_path):
    control = _control(tmp_path, _config())
    assert control["envelope"] == {
        "total_output_w": 1600,
        "device_output_w": 800,
        "soc_min": 10,
        "soc_max": 100,
        "soc_uniform": True,
    }


def test_a_mixed_soc_window_is_reported_as_not_uniform(tmp_path):
    config = _config(
        devices=[_api_device(), _api_device("WR2", sn="BBB", min_soc=20, max_soc=90)]
    )
    envelope = _control(tmp_path, config)["envelope"]
    assert envelope["soc_min"] == 10
    assert envelope["soc_max"] == 100
    assert envelope["soc_uniform"] is False


def test_an_unreadable_envelope_value_stays_null(tmp_path):
    control = _control(tmp_path, _config({"max_total_power": "lots"}))
    assert control["envelope"]["total_output_w"] is None


def test_a_missing_config_reports_no_control_claim():
    """A degraded load must not carry a summary that implies a control state."""

    from admin.maintenance_config import load_maintenance_config as load

    result = load(base_dir="/nonexistent-install-root")
    assert result["status"] == "missing"
    assert "summary" not in result
