# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard shows each device setting as the inverter reports it, and
offers one only for a device that has it."""

from types import SimpleNamespace

import pytest

from dashboard.server import _offered_device_settings
from dashboard.telemetry import _device_settings_card
from ems import device_settings, runtime_commands
from ems.device_settings import SETTINGS
from tests.test_device_settings import Inverter, _ems, _set, make_install

pytestmark = [
    pytest.mark.contract,
    pytest.mark.power_control,
    pytest.mark.simulation,
]


def test_the_device_card_shows_each_setting_by_label_and_the_value_set_where_it_differs(tmp_path):
    install = make_install(tmp_path)
    _set(install, "light", "off")
    _set(install, "fan", "auto")
    controller, dev, _ = _ems(install, Inverter())

    assert _device_settings_card(controller, dev, dev.fetch()) == {
        "light": {"label": "Light", "reported": "On", "desired": "Off", "reported_value": "on", "desired_value": "off"},
        "fan": {"label": "Fan", "reported": "Auto", "desired": None, "reported_value": "auto", "desired_value": None},
    }


def test_the_runtime_answer_lists_the_settings_and_which_device_has_them(tmp_path):
    install = make_install(tmp_path)
    _, dev, _ = _ems(install, Inverter())
    dev.fetch()
    server = SimpleNamespace(device_settings=lambda: device_settings.offered_by_device([dev]))

    limits = runtime_commands.attach_limits({}, None, _offered_device_settings(server))["_limits"]

    assert [setting["key"] for setting in limits["device_settings"]] == [s.key for s in SETTINGS]
    assert limits["device_settings"][1]["values"][1] == {"value": "1", "label": "Speed 1"}
    assert limits["device_settings_offered"] == {"WR1": ["light", "fan"]}
    assert runtime_commands.attach_limits({}, None, _offered_device_settings(SimpleNamespace()))[
        "_limits"
    ]["device_settings_offered"] == {}
