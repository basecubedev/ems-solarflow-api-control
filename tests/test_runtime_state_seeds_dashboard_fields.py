# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every runtime field the Dashboard can write is seeded from config by the EMS.

The Dashboard's runtime controls render what runtime-state holds, and the EMS
reads runtime-state first and config second. A key the Dashboard may write but
the EMS never seeds therefore shows as *off* in the Dashboard while the EMS
applies the config default -- which is how ``ac_charge_control.enabled`` and
the per-device ``ac_charge_enabled`` shipped: the Admin said *Enabled* (the
config), the Dashboard said *disabled* (an absent key), and the EMS charged.

Seeding is the single source of truth: the value the Dashboard shows is the
value the controller resolves.
"""

import json
from types import SimpleNamespace

import pytest

import ems.config as cfg
from dashboard.runtime_write import DEVICE_FIELDS, SECTION_FIELDS, SYSTEM_FIELDS
from ems.runtime_state import RuntimeState, build_runtime_defaults, merge_runtime_defaults

pytestmark = [
    pytest.mark.contract,
]


@pytest.fixture(autouse=True)
def _loaded_config(monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG", {"ha": {}})


def _device(name="WR1", **overrides):
    values = {
        "name": name,
        "sn": f"SN-{name}",
        "max_power": 800,
        "pv_priority_factor": 1.0,
        "ac_charge_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_every_dashboard_runtime_field_is_seeded_from_config():
    defaults = build_runtime_defaults([_device("WR1"), _device("WR2")])

    assert set(SYSTEM_FIELDS) <= set(defaults["system"])
    for section, fields in SECTION_FIELDS.items():
        assert set(fields) <= set(defaults[section]), section
    for name, entry in defaults["devices"].items():
        assert set(DEVICE_FIELDS) <= set(entry), name


def test_ac_charge_control_is_seeded_with_the_configured_value(monkeypatch):
    monkeypatch.setattr(cfg, "AC_CHARGE_CONTROL_CONFIG", {"enabled": False})
    assert build_runtime_defaults([_device()])["ac_charge_control"] == {"enabled": False}

    monkeypatch.setattr(cfg, "AC_CHARGE_CONTROL_CONFIG", {"enabled": True})
    assert build_runtime_defaults([_device()])["ac_charge_control"] == {"enabled": True}


def test_a_device_seeds_its_ac_charge_flag_from_its_config_entry():
    defaults = build_runtime_defaults(
        [_device("WR1", ac_charge_enabled=False), _device("WR2", ac_charge_enabled=True)]
    )

    assert defaults["devices"]["WR1"]["ac_charge_enabled"] is False
    assert defaults["devices"]["WR2"]["ac_charge_enabled"] is True


def test_an_existing_runtime_file_gains_the_section_and_keeps_a_stored_override(
    tmp_path, monkeypatch
):
    """A file written before the feature existed reads the config value; a
    file that already carries an operator's choice keeps it."""

    monkeypatch.setattr(cfg, "AC_CHARGE_CONTROL_CONFIG", {"enabled": True})
    defaults = build_runtime_defaults([_device("WR1")])
    path = tmp_path / "runtime-state.json"
    path.write_text(
        json.dumps(
            {
                "system": {"enabled": True},
                "devices": {"WR1": {"enabled": True, "max_power": 600}},
            }
        )
    )

    state = RuntimeState(str(path), defaults)
    state.load_or_create()

    assert state.data["ac_charge_control"] == {"enabled": True}
    assert state.data["devices"]["WR1"]["ac_charge_enabled"] is True
    assert state.data["devices"]["WR1"]["max_power"] == 600
    assert cfg.ac_charge_control_enabled(state) is True

    path.write_text(
        json.dumps(
            {
                "system": {"enabled": True},
                "ac_charge_control": {"enabled": False},
                "devices": {"WR1": {"enabled": True, "ac_charge_enabled": False}},
            }
        )
    )
    reloaded = RuntimeState(str(path), defaults)
    reloaded.load_or_create()

    assert reloaded.data["ac_charge_control"] == {"enabled": False}
    assert reloaded.data["devices"]["WR1"]["ac_charge_enabled"] is False
    assert cfg.ac_charge_control_enabled(reloaded) is False


def test_the_value_the_dashboard_shows_is_the_value_the_controller_resolves(monkeypatch):
    for configured in (True, False):
        monkeypatch.setattr(cfg, "AC_CHARGE_CONTROL_CONFIG", {"enabled": configured})
        defaults = build_runtime_defaults([_device("WR1")])
        state = SimpleNamespace(data=merge_runtime_defaults({}, defaults))

        shown = state.data["ac_charge_control"]["enabled"]
        assert cfg.ac_charge_control_enabled(state) is shown is configured


def test_emsctl_seeds_the_same_runtime_keys_and_ac_charge_values_as_the_ems(monkeypatch):
    """``emsctl`` builds runtime defaults from ``config.json`` on its own. Its
    shape and its AC charge answers must be the EMS's, or a state file written
    by the CLI reads differently from one written by the controller."""

    import emsctl

    device = {"name": "WR1", "ip": "192.168.1.100", "sn": "SN1", "max_power": 800}
    config = {
        "system": {},
        "devices": [dict(device, ac_charge_enabled=False)],
        "ac_charge_control": {"enabled": False},
    }
    monkeypatch.setattr(cfg, "AC_CHARGE_CONTROL_CONFIG", {"enabled": False})

    cli = emsctl.runtime_defaults(config)
    ems = build_runtime_defaults([_device("WR1", sn="SN1", ac_charge_enabled=False)])

    assert set(cli) == set(ems)
    for section in ("system", "ha", "winter", "ac_charge_control"):
        assert set(cli[section]) == set(ems[section]), section
    assert set(cli["devices"]["WR1"]) == set(ems["devices"]["WR1"])
    assert cli["ac_charge_control"] == ems["ac_charge_control"] == {"enabled": False}
    assert cli["devices"]["WR1"]["ac_charge_enabled"] is ems["devices"]["WR1"]["ac_charge_enabled"] is False


def test_emsctl_treats_a_missing_ac_charge_block_as_on_like_the_ems():
    import emsctl

    defaults = emsctl.runtime_defaults(
        {"system": {}, "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "SN1"}]}
    )

    assert defaults["ac_charge_control"] == {"enabled": True}
    assert defaults["devices"]["WR1"]["ac_charge_enabled"] is True
