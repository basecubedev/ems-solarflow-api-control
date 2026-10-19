# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light, fan, buzzer and battery heating, kept where the operator set them.

The owner's decisions of 2026-10-17: a setting nobody set is never written;
one that is set is written by one reconciler behind the state gates, over the
local API only, with off is off; and a setting is offered only on a catalogue
model that lists it and whose report carries it.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import emsctl
from ems import device_settings, runtime_commands
from ems.clients import ZendureClient, parse_device
from ems.controller import EMSController
from ems.device_settings import (
    HOLD_SECONDS,
    MAX_ATTEMPTS,
    RESEND_SECONDS,
    SETTING_PROPERTIES,
    SETTINGS,
    DeviceSettingsReconciler,
    changes_for,
    reported_word,
)
from ems.diagnostics import diagnose_reported_property_names
from ems.models import DEVICE_STATE_PROPERTIES
from ems.mqtt_control.zendure_profiles import HARDWARE_PROFILES
from ems.runtime_commands import RuntimeWriteError, build_validation_context, run_command
from ems.runtime_state import RuntimeState
from tests._emsctl_test_helpers import write_config

pytestmark = [
    pytest.mark.contract,
    pytest.mark.power_control,
    pytest.mark.simulation,
]

REPORT = {
    "electricLevel": 60,
    "minSoc": 150,
    "socSet": 1000,
    "outputLimit": 200,
    "acMode": 2,
    "smartMode": 1,
    "gridOffMode": 2,
    "lampSwitch": 1,
    "Fanmode": 1,
    "Fanspeed": 0,
}


class Inverter:
    """A local-API device: it answers its report and takes a write unless told not to."""

    def __init__(self, product="SolarFlow 800 Pro 2", takes_writes=True, refuses=(), hangs=False, **changes):
        self.product = product
        self.takes_writes = takes_writes
        self.refuses = set(refuses)
        self.hangs = hangs
        self.properties = dict(REPORT)
        for name, value in changes.items():
            if value is None:
                self.properties.pop(name, None)
            else:
                self.properties[name] = value
        self.writes = []

    def get(self, url, timeout):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"product": self.product, "properties": dict(self.properties)},
        )

    def post(self, url, json, timeout):
        self.writes.append(dict(json["properties"]))
        if self.hangs:
            raise TimeoutError("no answer")
        if self.refuses & set(json["properties"]):
            return SimpleNamespace(status_code=400, text="unknown property")
        if self.takes_writes:
            self.properties.update(json["properties"])
        return SimpleNamespace(status_code=200, text="")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_install(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    runtime_path = tmp_path / "runtime-state.json"
    assert emsctl.main(["--config", str(config_path), "--runtime-state", str(runtime_path), "status"]) == 0
    runtime_state = RuntimeState(str(runtime_path), {"devices": {}})
    runtime_state.load_if_changed(force=True)
    return SimpleNamespace(config=config, runtime_state=runtime_state, path=runtime_path)


@pytest.fixture
def install(tmp_path):
    return make_install(tmp_path)


def _set(install, command, value, device="WR1"):
    run_command(
        install.runtime_state,
        "device",
        command,
        value,
        device=device,
        validation_context=build_validation_context(install.config),
        source="emsctl",
    )


def _ems(install, inverter, name="WR1"):
    dev = ZendureClient(name, "192.0.2.10", f"{name}-SN", inverter, 15, 100, 1, None, 800)
    controller = EMSController(
        devices=[dev], shelly=None, sleep_enabled=False, runtime_state=install.runtime_state
    )
    clock = Clock()
    controller.device_settings = DeviceSettingsReconciler(clock=clock)
    return controller, dev, clock


def _cycle(controller, dev):
    controller.apply_device_settings(dev, dev.fetch())


def _gate(open_=True):
    return patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=open_)


def test_a_setting_nobody_set_is_never_written(install):
    inverter = Inverter(lampSwitch=0, Fanmode=0)
    controller, dev, _ = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)

    assert inverter.writes == []


def test_a_light_set_off_is_written_once_and_then_held(install):
    _set(install, "light", "off")
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)
        _cycle(controller, dev)

    assert inverter.writes == [{"lampSwitch": 0}]


@pytest.mark.parametrize(
    ("fanmode", "fanspeed", "value", "written"),
    [
        (1, 0, "auto", []),
        (1, 0, "1", [{"Fanspeed": 1}]),
        (1, 0, "2", [{"Fanspeed": 2}]),
        (1, 1, "off", [{"Fanmode": 0}]),
        (0, 2, "auto", [{"Fanmode": 1, "Fanspeed": 0}]),
        (0, 2, "2", [{"Fanmode": 1}]),
        (0, 1, "off", []),
    ],
)
def test_the_fan_is_one_setting_over_fanmode_and_fanspeed(install, fanmode, fanspeed, value, written):
    _set(install, "fan", value)
    inverter = Inverter(Fanmode=fanmode, Fanspeed=fanspeed)
    controller, dev, _ = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)

    assert inverter.writes == written


def test_each_setting_that_differs_goes_out_as_its_own_write(install):
    _set(install, "light", "off")
    _set(install, "fan", "2")
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)

    assert inverter.writes == [{"lampSwitch": 0}, {"Fanspeed": 2}]


def test_a_setting_the_device_refuses_holds_back_no_other(install):
    """Buzzer and battery heating have been written on no hardware, so a model
    that reports one and refuses its write must not cost the light its sends."""

    _set(install, "light", "off")
    _set(install, "auto-heat", "on")
    inverter = Inverter(autoHeat=0, refuses={"autoHeat"})
    controller, dev, clock = _ems(install, inverter)

    with _gate():
        for _ in range(MAX_ATTEMPTS + 2):
            _cycle(controller, dev)
            clock.now += RESEND_SECONDS
        inverter.properties["lampSwitch"] = 1
        _cycle(controller, dev)

    assert inverter.writes.count({"autoHeat": 1}) == MAX_ATTEMPTS
    assert inverter.writes.count({"lampSwitch": 0}) == 2


def test_unmanaged_hands_the_setting_back_to_the_device(install):
    _set(install, "light", "off")
    _set(install, "light", "unmanaged")
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)

    assert install.runtime_state.get_device("WR1", "light", "absent") is None
    assert inverter.writes == []


def test_nothing_is_written_behind_a_shut_state_gate_and_it_is_said_once(install, caplog):
    _set(install, "light", "off")
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate(False), caplog.at_level(logging.INFO):
        _cycle(controller, dev)
        _cycle(controller, dev)

    assert inverter.writes == []
    assert sum("event=dry_run_device_settings_write" in line for line in caplog.messages) == 1


@pytest.mark.parametrize("switched_off", ["control_disabled", "device_disabled"])
def test_off_is_off(install, caplog, switched_off):
    _set(install, "light", "off")
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)
    if switched_off == "control_disabled":
        controller.control_enabled = False
    else:
        run_command(install.runtime_state, "device", "disable", device="WR1", source="emsctl")

    with _gate(), caplog.at_level(logging.INFO):
        _cycle(controller, dev)

    assert inverter.writes == []
    blocked = [line for line in caplog.messages if "event=dry_run_device_settings_write" in line]
    assert len(blocked) == 1 and switched_off in blocked[0]


@pytest.mark.parametrize(
    ("product", "changes", "reason"),
    [
        ("Hub 1200", {}, "model_does_not_list"),
        ("Unknown Box", {}, "model_does_not_list"),
        ("SolarFlow 800 Pro 2", {"lampSwitch": None}, "not_reported"),
    ],
)
def test_a_setting_the_device_does_not_have_is_not_written(install, caplog, product, changes, reason):
    _set(install, "light", "off")
    inverter = Inverter(product=product, **changes)
    controller, dev, _ = _ems(install, inverter)

    with _gate(), caplog.at_level(logging.INFO):
        _cycle(controller, dev)
        _cycle(controller, dev)

    assert inverter.writes == []
    noted = [line for line in caplog.messages if "event=device_setting_not_applied" in line]
    assert len(noted) == 1 and f"reason={reason}" in noted[0]


def test_a_device_without_state_reconciliation_is_never_written(install, caplog):
    _set(install, "light", "off")
    dev = SimpleNamespace(
        name="WR1",
        supports_state_reconciliation=False,
        control_gate="local_mqtt",
        resolved_hardware_profile=lambda: "solarflow_800_pro_2",
        write_properties=Mock(),
    )
    controller = EMSController(
        devices=[dev], shelly=None, sleep_enabled=False, runtime_state=install.runtime_state
    )

    with _gate(), caplog.at_level(logging.DEBUG):
        controller.apply_device_settings(dev, parse_device({"properties": REPORT}))

    dev.write_properties.assert_not_called()
    assert any(
        "event=state_reconciliation_skipped" in line and "path=device_settings" in line
        for line in caplog.messages
    )
    dev.observed_settings = device_settings.reported_properties(REPORT)
    assert device_settings.offered_by_device([dev]) == {"WR1": []}


def test_a_value_the_device_does_not_take_is_sent_a_few_times_then_left(install, caplog):
    _set(install, "light", "off")
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    sent_at = []
    with _gate(), caplog.at_level(logging.INFO):
        for _ in range(MAX_ATTEMPTS):
            for _ in range(2):
                before = len(inverter.writes)
                _cycle(controller, dev)
                sent_at += [clock.now] * (len(inverter.writes) - before)
                clock.now += RESEND_SECONDS / 2
        for _ in range(4):
            _cycle(controller, dev)
            clock.now += RESEND_SECONDS

    assert inverter.writes == [{"lampSwitch": 0}] * MAX_ATTEMPTS
    assert sent_at == [1000.0 + RESEND_SECONDS * attempt for attempt in range(MAX_ATTEMPTS)]
    assert sum("event=device_setting_not_taken" in line for line in caplog.messages) == 1


def test_the_device_is_given_the_full_wait_after_the_last_send_before_the_warning(install, caplog):
    _set(install, "light", "off")
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    with _gate(), caplog.at_level(logging.INFO):
        for _ in range(MAX_ATTEMPTS):
            _cycle(controller, dev)
            clock.now += RESEND_SECONDS
        clock.now -= RESEND_SECONDS / 2
        _cycle(controller, dev)
        warned_early = any("event=device_setting_not_taken" in line for line in caplog.messages)
        clock.now += RESEND_SECONDS / 2
        _cycle(controller, dev)

    assert not warned_early
    assert sum("event=device_setting_not_taken" in line for line in caplog.messages) == 1


@pytest.mark.parametrize("again", ["another_value", "handed_back"])
def test_a_setting_left_alone_is_sent_again_once_its_value_changes(install, again):
    _set(install, "fan", "2")
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    with _gate():
        for _ in range(MAX_ATTEMPTS + 1):
            _cycle(controller, dev)
            clock.now += RESEND_SECONDS
        if again == "another_value":
            _set(install, "fan", "1")
        else:
            _set(install, "fan", "unmanaged")
            _cycle(controller, dev)
            _set(install, "fan", "2")
        _cycle(controller, dev)

    expected = {"Fanspeed": 1} if again == "another_value" else {"Fanspeed": 2}
    assert inverter.writes == [{"Fanspeed": 2}] * MAX_ATTEMPTS + [expected]


def test_each_invalid_value_is_named_once(install, caplog):
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate(), caplog.at_level(logging.INFO):
        for value in ("dim", "dim", "blink"):
            install.runtime_state.update_device("WR1", {"light": value})
            _cycle(controller, dev)

    invalid = [line for line in caplog.messages if "reason=invalid_value" in line]
    assert len(invalid) == 2 and "dim" in invalid[0] and "blink" in invalid[1]
    assert inverter.writes == []


def test_a_device_that_once_showed_the_value_is_kept_at_it_again(install):
    """The counter starts afresh once the device has shown the value, so a
    change made in the app later is written back."""

    _set(install, "light", "off")
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    with _gate():
        for _ in range(MAX_ATTEMPTS + 1):
            _cycle(controller, dev)
            clock.now += RESEND_SECONDS
        inverter.properties["lampSwitch"] = 0
        _cycle(controller, dev)
        inverter.properties["lampSwitch"] = 1
        _cycle(controller, dev)

    assert inverter.writes == [{"lampSwitch": 0}] * (MAX_ATTEMPTS + 1)


def test_the_control_loop_keeps_the_settings_each_cycle(install):
    _set(install, "fan", "off")
    inverter = Inverter()
    dev = ZendureClient("WR1", "192.0.2.10", "WR1-SN", inverter, 15, 100, 1, None, 800)
    controller = EMSController(
        devices=[dev],
        shelly=SimpleNamespace(get_power=lambda: 0),
        sleep_enabled=False,
        runtime_state=install.runtime_state,
    )
    controller.set_output_limit = Mock()

    with _gate(), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0):
        controller.run_once()

    assert {"Fanmode": 0} in inverter.writes


def test_the_report_carries_the_settings_as_whole_numbers():
    state = parse_device(
        {"properties": {**REPORT, "Fanspeed": 2.0, "buzzerSwitch": "1", "autoHeat": 0.5}}
    )

    assert state.setting_properties == {"lampSwitch": 1, "Fanmode": 1, "Fanspeed": 2}
    assert reported_word(device_settings.SETTINGS_BY_KEY["fan"], state.setting_properties) == "2"


def test_a_setting_property_in_a_report_is_one_the_ems_reads():
    reported = diagnose_reported_property_names(
        {"properties": {**REPORT, "buzzerSwitch": 0, "autoHeat": 1}}
    )

    assert SETTING_PROPERTIES <= set(reported["reported"])
    assert SETTING_PROPERTIES.isdisjoint(reported["unmapped"])
    assert SETTING_PROPERTIES <= DEVICE_STATE_PROPERTIES


def test_a_fan_speed_the_report_does_not_carry_is_not_written_over():
    fan = device_settings.SETTINGS_BY_KEY["fan"]

    assert changes_for(fan, "auto", {"Fanmode": 0}) is None
    assert changes_for(fan, "off", {"Fanmode": 1}) == {"Fanmode": 0}
    assert reported_word(fan, {"Fanmode": 1}) is None
    assert reported_word(fan, {"Fanmode": 1, "Fanspeed": 7}) is None


def test_every_catalogue_setting_is_one_the_ems_knows():
    keys = {setting.key for setting in SETTINGS}
    for profile in HARDWARE_PROFILES.values():
        assert set(profile.device_settings) <= keys, profile.canonical_name
        if profile.hardware_generation == "solarflow_zensdk":
            assert set(profile.device_settings) == keys, profile.canonical_name
        else:
            assert profile.device_settings == (), profile.canonical_name


def test_every_setting_is_a_runtime_field_and_an_emsctl_command():
    assert set(runtime_commands.DEVICE_SETTING_FIELDS) == {setting.key for setting in SETTINGS}
    for setting in SETTINGS:
        assert runtime_commands.DEVICE_COMMANDS[setting.command].key == setting.key
        assert setting.command in emsctl.DEVICE_ACTIONS


@pytest.mark.parametrize(
    ("command", "value", "stored"),
    [
        ("light", "off", "off"),
        ("light", "ON", "on"),
        ("fan", "2", "2"),
        ("fan", "auto", "auto"),
        ("buzzer", "off", "off"),
        ("auto-heat", "on", "on"),
        ("fan", "unmanaged", None),
    ],
)
def test_emsctl_saves_a_setting_through_the_command_path(install, command, value, stored):
    argv = ["--config", str(install.path.parent / "config.json"), "--runtime-state", str(install.path)]

    assert emsctl.main([*argv, "device", "WR1", command, value]) == 0

    key = runtime_commands.DEVICE_COMMANDS[command].key
    assert json.loads(install.path.read_text())["devices"]["WR1"][key] == stored


@pytest.mark.parametrize(("key", "value"), [("light", "dim"), ("fan", "3"), ("light", True), ("fan", 1.5)])
def test_a_value_a_setting_does_not_have_is_refused(key, value):
    with pytest.raises(RuntimeWriteError, match=f"{key} must be one of"):
        runtime_commands.validate_device_values("WR1", {key: value})


def test_a_setting_is_refused_over_mqtt_but_can_always_be_handed_back():
    context = {"ac_role_unsupported": ["WR1"]}

    with pytest.raises(RuntimeWriteError, match="writes no device settings"):
        runtime_commands.validate_device_values("WR1", {"light": "off"}, context)
    assert runtime_commands.validate_device_values("WR1", {"light": "unmanaged"}, context) == {"light": None}


def test_a_device_offers_what_its_model_lists_and_its_report_carries(install):
    controller, dev, _ = _ems(install, Inverter())
    dev.fetch()

    assert device_settings.offered_by_device([dev]) == {"WR1": ["light", "fan"]}

    _, legacy, _ = _ems(install, Inverter(product="Hub 1200"))
    legacy.fetch()
    assert device_settings.offered_by_device([legacy]) == {"WR1": []}


def test_the_live_snapshot_shows_each_setting_as_reported_and_as_set(install):
    _set(install, "light", "off")
    controller, dev, _ = _ems(install, Inverter())
    state = dev.fetch()
    controller.last_states["WR1"] = state

    from ems.control_status import _device_entry

    assert _device_entry(controller, dev, None)["settings"] == {
        "light": {"offered": True, "reported": "on", "desired": "off"},
        "fan": {"offered": True, "reported": "auto", "desired": None},
    }


def test_the_schedule_offers_a_setting_only_for_a_device_that_has_it(install):
    from dashboard.schedule_api import ScheduleApi
    from ems.schedule_store import ScheduleStore

    def catalogue(provider):
        api = ScheduleApi(
            ScheduleStore(install.path.parent / "schedules.json"),
            runtime_state=install.runtime_state,
            devices=lambda: {"WR1": "SN-TEST-0001"},
            validation_context=lambda: build_validation_context(install.config),
            zone=None,
            clock=None,
            device_settings=provider,
        )
        return {
            action["id"]: action
            for action in api.catalogue({"WR1": "SN-TEST-0001"}, build_validation_context(install.config))
        }

    offered = catalogue(lambda: {"WR1": ["light", "fan"]})
    assert offered["light"]["devices"] == offered["fan"]["devices"] == ["WR1"]
    assert offered["buzzer"]["devices"] == offered["auto_heat"]["devices"] == []
    assert offered["fan"]["choice_labels"] == {
        "auto": "Auto", "1": "Speed 1", "2": "Speed 2", "off": "Off", "unmanaged": "Device decides",
    }
    assert offered["device_output_limit"]["devices"] == ["WR1"]
    assert all(catalogue(None)[setting.key]["devices"] == [] for setting in SETTINGS)


def test_a_scheduled_fan_speed_reads_as_a_speed():
    from ems.schedule_actions import schedule_action
    from ems.schedule_text import describe_value

    assert describe_value(schedule_action("fan"), "1") == "speed 1"
    assert describe_value(schedule_action("light"), "off") == "off"
    assert describe_value(schedule_action("light"), "unmanaged") == "device decides"
    assert describe_value(schedule_action("offgrid_socket"), "eco") == "eco"


def test_emsctl_status_shows_the_settings_the_running_ems_saw(install, capsys):
    from tests._emsctl_test_helpers import write_live_control_status

    argv = ["--config", str(install.path.parent / "config.json"), "--runtime-state", str(install.path)]
    assert emsctl.main([*argv, "status"]) == 0
    assert "device_settings" not in json.loads(capsys.readouterr().out)

    write_live_control_status(install.path.parent)
    status_path = install.path.parent / "control-status.json"
    snapshot = json.loads(status_path.read_text())
    settings = {"light": {"offered": True, "reported": "on", "desired": "off"}}
    snapshot["devices"]["WR1"]["settings"] = settings
    status_path.write_text(json.dumps(snapshot))

    assert emsctl.main([*argv, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["device_settings"] == {"WR1": settings}


def test_a_setting_outlives_a_restart_of_the_ems(install):
    from ems.runtime_state import build_runtime_defaults

    _set(install, "fan", "auto")
    dev = ZendureClient("WR1", "192.0.2.10", "SN-TEST-0001", Inverter(), 15, 100, 1, None, 800)
    restarted = RuntimeState(str(install.path), build_runtime_defaults([dev]))
    restarted.load_or_create()

    assert restarted.get_device("WR1", "fan") == "auto"


def test_a_value_left_alone_gets_another_round_after_the_hold_and_is_said_once(install, caplog):
    """Set again before any cycle saw it handed back, the value is the same as
    before; the hold, not a seen hand-back, is what sends it again."""

    _set(install, "light", "off")
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    with _gate(), caplog.at_level(logging.INFO):
        for _ in range(MAX_ATTEMPTS + 1):
            _cycle(controller, dev)
            clock.now += RESEND_SECONDS
        _set(install, "light", "unmanaged")
        _set(install, "light", "off")
        _cycle(controller, dev)
        held = len(inverter.writes)
        clock.now += HOLD_SECONDS
        _cycle(controller, dev)

    assert held == MAX_ATTEMPTS
    assert len(inverter.writes) == MAX_ATTEMPTS + 1
    assert sum("event=device_setting_not_taken" in line for line in caplog.messages) == 1


def test_a_device_that_does_not_answer_costs_one_write_a_cycle(install, caplog):
    for command, value in (("light", "off"), ("fan", "2"), ("buzzer", "off"), ("auto-heat", "on")):
        _set(install, command, value)
    inverter = Inverter(hangs=True, buzzerSwitch=1, autoHeat=0)
    controller, dev, clock = _ems(install, inverter)

    per_cycle = []
    with _gate(), caplog.at_level(logging.INFO):
        for _ in range(4 * MAX_ATTEMPTS + 2):
            before = len(inverter.writes)
            _cycle(controller, dev)
            per_cycle.append(len(inverter.writes) - before)
            clock.now += RESEND_SECONDS

    assert max(per_cycle) == 1
    assert len(inverter.writes) == 4 * MAX_ATTEMPTS
    assert sum("event=device_setting_not_taken" in line for line in caplog.messages) == 4


def test_a_report_that_drops_a_setting_now_and_then_is_named_once(install, caplog):
    _set(install, "light", "off")
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate(False), caplog.at_level(logging.INFO):
        for present in (True, False, True, False):
            if present:
                inverter.properties["lampSwitch"] = 1
            else:
                inverter.properties.pop("lampSwitch")
            _cycle(controller, dev)

    assert sum("reason=not_reported" in line for line in caplog.messages) == 1


def test_a_held_back_setting_is_said_again_for_each_new_value_and_after_the_gate_was_open(install, caplog):
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    def blocked():
        return sum("event=dry_run_device_settings_write" in line for line in caplog.messages)

    with caplog.at_level(logging.INFO):
        _set(install, "fan", "2")
        with _gate(False):
            _cycle(controller, dev)
            _cycle(controller, dev)
        assert blocked() == 1
        _set(install, "fan", "1")
        with _gate(False):
            _cycle(controller, dev)
        assert blocked() == 2
        with _gate():
            _cycle(controller, dev)
        clock.now += RESEND_SECONDS
        with _gate(False):
            _cycle(controller, dev)
        assert blocked() == 3


def test_a_hand_edited_whole_number_is_the_value_it_names(install):
    install.runtime_state.update_device("WR1", {"fan": 1.0})
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)

    assert inverter.writes == [{"Fanspeed": 1}]


def test_a_setting_refused_first_holds_back_no_later_one_whatever_the_loop_interval(install):
    _set(install, "light", "off")
    _set(install, "fan", "2")
    inverter = Inverter(refuses={"lampSwitch"})
    controller, dev, clock = _ems(install, inverter)

    with _gate():
        _cycle(controller, dev)
        clock.now += HOLD_SECONDS
        _cycle(controller, dev)

    assert inverter.writes[:2] == [{"lampSwitch": 0}, {"Fanspeed": 2}]
    assert inverter.properties["Fanspeed"] == 2


def test_the_warning_is_said_once_across_rounds_and_names_the_wait_left(install, caplog):
    _set(install, "light", "off")
    inverter = Inverter(takes_writes=False)
    controller, dev, clock = _ems(install, inverter)

    with _gate(), caplog.at_level(logging.INFO):
        for _ in range(2):
            for _ in range(MAX_ATTEMPTS + 1):
                _cycle(controller, dev)
                clock.now += RESEND_SECONDS
            clock.now += HOLD_SECONDS

    warnings = [line for line in caplog.messages if "event=device_setting_not_taken" in line]
    assert len(inverter.writes) == 2 * MAX_ATTEMPTS
    assert len(warnings) == 1
    assert f"retry_after_s={int(HOLD_SECONDS - RESEND_SECONDS)}" in warnings[0]


def test_a_hand_edited_number_no_float_holds_is_an_invalid_value_not_a_failed_cycle(install, caplog):
    install.runtime_state.update_device("WR1", {"fan": 10 ** 400})
    inverter = Inverter()
    controller, dev, _ = _ems(install, inverter)

    with _gate(), caplog.at_level(logging.INFO):
        _cycle(controller, dev)

    assert inverter.writes == []
    assert any("reason=invalid_value" in line for line in caplog.messages)


def test_a_failure_in_the_settings_never_ends_the_control_cycle(install, caplog):
    _set(install, "light", "off")
    inverter = Inverter()
    dev = ZendureClient("WR1", "192.0.2.10", "WR1-SN", inverter, 15, 100, 1, None, 800)
    controller = EMSController(
        devices=[dev],
        shelly=SimpleNamespace(get_power=lambda: 200),
        sleep_enabled=False,
        runtime_state=install.runtime_state,
    )
    controller.set_output_limit = Mock()
    broken = Mock(side_effect=RuntimeError("broken"))
    working = controller.device_settings.reconcile

    def cycle():
        with _gate(), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0):
            controller.run_once()

    with caplog.at_level(logging.INFO):
        controller.device_settings.reconcile = broken
        cycle()
        cycle()
        controller.device_settings.reconcile = working
        cycle()
        controller.device_settings.reconcile = broken
        cycle()
        _set(install, "light", "unmanaged")
        cycle()
        _set(install, "light", "off")
        cycle()

    failed = [record for record in caplog.records if "event=device_settings_failed" in record.getMessage()]
    assert len(failed) == 3
    assert failed[0].exc_info is not None
    assert controller.set_output_limit.call_count >= 4
