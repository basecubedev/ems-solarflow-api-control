# SPDX-License-Identifier: AGPL-3.0-or-later
"""Winter reserve policies: one plan per device type, overridable per device.

A battery without PV of its own cannot take the solar morning step, so it gets
a timed step instead and accepts that the firmware may charge it from the grid.
The policy registry in ``ems.winter_policies`` is the single source the config
catalog, the template and the controller read.
"""

import contextlib
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems import winter_policies as wp
from ems.config_catalog import get_config_catalog
from ems.controller import EMSController
from pathlib import Path

from ems.paths import BASE_DIR

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]

MORNING = datetime(2026, 11, 2, 9, 0)
NOON = datetime(2026, 11, 2, 12, 0)
AFTERNOON = datetime(2026, 11, 2, 15, 0)
NEXT_NOON = datetime(2026, 11, 3, 12, 0)


def _device(name="WR1"):
    return SimpleNamespace(name=name, min_soc=15, max_soc=95)


def _state(min_soc=20, soc=20, pv=0, pack_num=1):
    return SimpleNamespace(
        min_soc=min_soc,
        max_soc=95,
        soc=soc,
        pack_num=pack_num,
        solar=pv,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
    )


@contextlib.contextmanager
def _configured(entry=None, policies=None):
    winter = dict(cfg.WINTER_DEFAULTS)
    if policies is not None:
        winter["policies"] = policies
    with patch.object(cfg, "WINTER_CONFIG", winter), patch.object(
        cfg, "ZENDURE_CONFIG", [{"name": "WR1", **(entry or {})}]
    ), patch.object(cfg, "winter_feature_enabled", lambda runtime: True):
        yield


def _target(controller, state, now, **configured):
    with _configured(**configured):
        return controller.winter.reconciliation_target(_device(), state, True, now)


def _remember(controller, target, step_date="2026-11-01"):
    """A target this winter's ramp reached, the day before the test's day."""

    item = controller.winter.device("WR1")
    item.target = target
    item.step_date = step_date


BATTERY_ONLY = {"pv_kwp": 0}


# --- which policy a device gets --------------------------------------------


@pytest.mark.parametrize(
    "battery_absent, pv_kwp, expected",
    [
        (True, 1.0, wp.PV_ONLY),
        (True, 0, wp.PV_ONLY),
        (False, 0, wp.BATTERY_ONLY),
        (False, "0", wp.BATTERY_ONLY),
        (False, 0.0, wp.BATTERY_ONLY),
        (False, 1.2, wp.PV_BATTERY),
        (False, None, wp.PV_BATTERY),
        (False, "n/a", wp.PV_BATTERY),
    ],
)
def test_the_device_class_comes_from_the_battery_and_the_configured_pv(battery_absent, pv_kwp, expected):
    """A dark or shaded array reads like none; only ``pv_kwp: 0`` says there is none."""

    assert wp.device_energy_class(battery_absent, pv_kwp) == expected


@pytest.mark.parametrize("device_class", wp.DEVICE_CLASSES)
def test_every_class_defaults_to_its_built_in_policy(device_class):
    policy, valid = wp.resolve_policy(device_class)

    assert (policy.name, valid) == (wp.CLASS_DEFAULT_POLICIES[device_class], True)


def test_a_device_override_wins_over_the_class_default():
    policy, valid = wp.resolve_policy(wp.PV_BATTERY, wp.NOON_STEP, {wp.PV_BATTERY: wp.SOLAR_MORNING_STEP})

    assert (policy.name, valid) == (wp.NOON_STEP, True)


@pytest.mark.parametrize(
    "override, class_defaults",
    [
        (wp.SOLAR_MORNING_STEP, None),
        ("no_such_policy", None),
        (wp.AUTO, {wp.BATTERY_ONLY: wp.SOLAR_MORNING_STEP}),
        (wp.AUTO, {wp.BATTERY_ONLY: "no_such_policy"}),
    ],
    ids=["override-for-another-class", "unknown-override", "class-default-for-another-class", "unknown-class-default"],
)
def test_a_policy_that_does_not_fit_falls_back_and_says_so(override, class_defaults):
    """The solar morning step needs PV at the device; a battery-only device cannot take it."""

    policy, valid = wp.resolve_policy(wp.BATTERY_ONLY, override, class_defaults)

    assert (policy.name, valid) == (wp.NOON_STEP, False)


def test_an_invalid_policy_is_logged_once_per_device(caplog):
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    caplog.set_level(logging.WARNING)

    for _ in range(3):
        _target(controller, _state(), MORNING, entry={"pv_kwp": 0, "winter_policy": wp.SOLAR_MORNING_STEP})

    assert sum("event=winter_policy_invalid" in r.getMessage() for r in caplog.records) == 1


# --- the registry is the one source ----------------------------------------


def _catalog_fields():
    return {field["path"]: field for section in get_config_catalog()["sections"] for field in section["fields"]}


def test_the_catalog_offers_exactly_the_registered_policies():
    fields = _catalog_fields()

    assert fields["devices[].winter_policy"]["options"] == list(wp.device_policy_options())
    for device_class in wp.CONFIGURABLE_CLASSES:
        assert fields[f"winter.policies.{device_class}"]["options"] == list(wp.policy_names_for(device_class))
    assert "winter.policies.pv_only" not in fields


def test_the_template_and_the_runtime_defaults_name_the_built_in_class_policies():
    template = json.loads((Path(BASE_DIR) / "config" / "config.template.json").read_text(encoding="utf-8"))

    assert template["winter"]["policies"] == wp.configurable_class_defaults()
    assert cfg.WINTER_DEFAULTS["policies"] == wp.configurable_class_defaults()
    assert {device["winter_policy"] for device in template["devices"]} == {wp.AUTO}


# --- battery only: the noon step ---------------------------------------------


def test_a_battery_only_device_steps_at_noon_not_in_the_morning():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)

    assert _target(controller, _state(20, soc=20), MORNING, entry=BATTERY_ONLY) == (20, False)
    assert _target(controller, _state(20, soc=20), NOON, entry=BATTERY_ONLY) == (23, True)
    assert _target(controller, _state(23, soc=20), AFTERNOON, entry=BATTERY_ONLY) == (23, False)
    assert _target(controller, _state(23, soc=20), NEXT_NOON, entry=BATTERY_ONLY) == (26, True)


def test_the_noon_step_is_written_although_it_leads_the_soc_by_more_than_three():
    """The risk of a firmware grid charge is accepted for a battery without PV."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _remember(controller, 26)
    _target(controller, _state(26, soc=23), MORNING, entry=BATTERY_ONLY)

    assert _target(controller, _state(26, soc=23), NOON, entry=BATTERY_ONLY) == (29, True)


def test_the_noon_step_does_not_run_away_from_a_battery_that_does_not_charge():
    """It leads the SoC by at most two steps; a pack that never charges stops the ramp."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _remember(controller, 26)
    _target(controller, _state(26, soc=20), MORNING, entry=BATTERY_ONLY)

    assert _target(controller, _state(26, soc=20), NOON, entry=BATTERY_ONLY) == (26, False)


def test_only_the_noon_step_itself_may_lead_the_soc_further():
    """The configured floor adopted after a start is cut like any other raise."""

    controller = EMSController(devices=[_device()], shelly=None, sleep_enabled=False)

    assert _target(controller, _state(10, soc=5), MORNING, entry=BATTERY_ONLY) == (10, False)


def test_a_battery_only_device_follows_a_point_below_a_soc_that_rises_after_its_step():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=20), MORNING, entry=BATTERY_ONLY)
    assert _target(controller, _state(20, soc=20), NOON, entry=BATTERY_ONLY) == (23, True)

    assert _target(controller, _state(23, soc=31), AFTERNOON, entry=BATTERY_ONLY) == (30, False)
    assert _target(controller, _state(30, soc=55), AFTERNOON, entry=BATTERY_ONLY) == (40, False)


def test_a_noon_step_that_would_land_on_the_soc_waits_for_it_to_move():
    """minSoc + step equals the SoC whenever the battery sits a step above its
    minSoc; written there, it would put the battery at its floor."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=23), MORNING, entry=BATTERY_ONLY)

    assert _target(controller, _state(20, soc=23), NOON, entry=BATTERY_ONLY) == (20, True)
    assert _target(controller, _state(20, soc=22), AFTERNOON, entry=BATTERY_ONLY) == (23, False)


def test_a_restart_after_noon_takes_no_second_step_that_day():
    """The step date lives in memory; a restart must not read as a new day.

    Starting from the reported minSoc, every restart after noon added another
    unlimited step: 20, 23, 26 ... until the firmware charged from the grid.
    """

    first = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(first, _state(20, soc=20), MORNING, entry=BATTERY_ONLY)
    assert _target(first, _state(20, soc=20), NOON, entry=BATTERY_ONLY) == (23, True)

    restarted = EMSController(devices=[], shelly=None, sleep_enabled=False)
    assert _target(restarted, _state(23, soc=20), AFTERNOON, entry=BATTERY_ONLY) == (23, False)
    assert _target(restarted, _state(23, soc=20), NEXT_NOON, entry=BATTERY_ONLY) == (26, True)


def test_a_start_before_noon_still_takes_the_days_step():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)

    assert _target(controller, _state(20, soc=20), MORNING, entry=BATTERY_ONLY) == (20, False)
    assert _target(controller, _state(20, soc=20), NOON, entry=BATTERY_ONLY) == (23, True)


def test_a_pv_device_that_sees_no_pv_by_the_step_hour_says_so_once_a_day(caplog):
    """A battery without PV left at pv_kwp 1.0 would wait for a morning that never comes."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    caplog.set_level(logging.INFO)

    for now in (MORNING, NOON, AFTERNOON, AFTERNOON, NEXT_NOON):
        assert _target(controller, _state(20, soc=20, pv=0), now) == (20, False)

    waits = [r for r in caplog.records if "event=winter_step_waits_for_pv" in r.getMessage()]
    assert len(waits) == 2
    assert "pv_kwp" in waits[0].getMessage()


def test_a_battery_missing_for_one_report_is_no_invalid_policy(caplog):
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    caplog.set_level(logging.WARNING)

    with _configured(entry={"winter_policy": wp.NOON_STEP}):
        policy = controller.winter.policy(_device(), _state(pack_num=0))

    assert policy.name == wp.NO_POLICY
    assert not any("event=winter_policy_invalid" in r.getMessage() for r in caplog.records)
    assert not controller.winter.device("WR1").policy_warned


def test_home_assistant_shows_the_step_in_effect_for_an_old_config_of_five():
    class _Ha:
        def __init__(self):
            self.states = {}

        def set_state(self, entity, value, **kwargs):
            self.states[entity] = value

    ha = _Ha()
    controller = EMSController(devices=[_device()], shelly=None, ha=ha, sleep_enabled=False)
    with _configured(entry=BATTERY_ONLY), patch.object(
        cfg, "WINTER_CONFIG", {**cfg.WINTER_DEFAULTS, "ramp_step_percent": 5}
    ), patch.object(cfg, "winter_month_active", lambda now: True), patch.object(
        controller, "device_ha_extra", lambda dev, extra=None: extra
    ):
        controller.publish_winter_to_ha([_state(20, soc=20)])

    assert ha.states["sensor.ems_solarflow_winter_ramp_step"] == 3
    assert ha.states["sensor.ems_solarflow_wr1_winter_estimated_ramp_days"] == 7


def test_the_step_hour_is_configurable():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    with patch.object(cfg, "winter_adjust_hour", lambda: 8):
        _target(controller, _state(20, soc=20), datetime(2026, 11, 2, 7, 0), entry=BATTERY_ONLY)
        assert _target(controller, _state(20, soc=20), datetime(2026, 11, 2, 8, 30), entry=BATTERY_ONLY) == (23, True)


def test_a_battery_only_device_is_never_held_from_export():
    dev = _device()
    controller = EMSController(devices=[dev], shelly=None, sleep_enabled=False)
    with _configured(entry=BATTERY_ONLY):
        assert not controller.winter.solar_charge_hold(dev, _state(24, soc=20, pv=0), MORNING)


def test_a_device_at_pv_kwp_0_that_reports_pv_takes_no_noon_step(caplog):
    """pv_kwp 0 long meant 'unset'; a PV device saved that way must not be pushed into a grid charge."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    caplog.set_level(logging.WARNING)

    _target(controller, _state(20, soc=20, pv=300), MORNING, entry=BATTERY_ONLY)
    assert _target(controller, _state(20, soc=20, pv=300), NOON, entry=BATTERY_ONLY) == (20, False)
    assert _target(controller, _state(20, soc=20, pv=300), AFTERNOON, entry=BATTERY_ONLY) == (20, False)

    warnings = [r for r in caplog.records if "event=winter_pv_kwp_zero_but_pv_reported" in r.getMessage()]
    assert len(warnings) == 1

def test_a_pv_device_set_to_the_noon_step_steps_at_noon_and_is_not_held():
    dev = _device()
    controller = EMSController(devices=[dev], shelly=None, sleep_enabled=False)
    entry = {"winter_policy": wp.NOON_STEP}

    assert _target(controller, _state(20, soc=20, pv=0), MORNING, entry=entry) == (20, False)
    assert _target(controller, _state(20, soc=20, pv=300), MORNING, entry=entry) == (20, False)
    assert _target(controller, _state(20, soc=20, pv=300), NOON, entry=entry) == (23, True)
    with _configured(entry=entry):
        assert not controller.winter.solar_charge_hold(dev, _state(23, soc=20, pv=300), NOON)


@pytest.mark.parametrize("winter_active", [True, False])
def test_a_device_set_to_no_policy_is_left_to_its_configured_min_soc(winter_active):
    """``none`` takes the device out of winter mode, summer reset included."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    with _configured(entry={"winter_policy": wp.NO_POLICY}):
        assert controller.winter.reconciliation_target(
            _device(), _state(30, soc=40, pv=300), winter_active, NOON
        ) == (None, False)


def test_the_class_default_can_be_changed_for_every_pv_device():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    policies = {**wp.CLASS_DEFAULT_POLICIES, wp.PV_BATTERY: wp.NOON_STEP}
    _target(controller, _state(20, soc=20, pv=300), MORNING, policies=policies)

    assert _target(controller, _state(20, soc=20, pv=300), NOON, policies=policies) == (23, True)


# --- the Maintenance editor carries the device policy --------------------------


def test_maintenance_writes_the_device_policy_like_any_common_device_value():
    from admin.device_common_fields import apply_common_device_values, common_device_value_fields

    fields = common_device_value_fields()
    device = {"name": "WR1", "winter_policy": wp.AUTO}
    apply_common_device_values(device, {"winter_policy": wp.NOON_STEP}, fields)

    assert fields["winter_policy"]["options"] == list(wp.device_policy_options())
    assert device["winter_policy"] == wp.NOON_STEP


# --- restarts, MQTT devices and the config entry -------------------------------


def test_a_restart_in_daylight_takes_no_second_morning_step_after_a_pv_dip():
    """The step date lives in memory; snow at 10:30 must not read as a new night."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    ten = datetime(2026, 11, 2, 10, 0)

    assert _target(controller, _state(23, soc=23, pv=300), ten) == (23, False)
    assert _target(controller, _state(23, soc=23, pv=0), datetime(2026, 11, 2, 10, 30)) == (23, False)
    assert _target(controller, _state(23, soc=23, pv=300), datetime(2026, 11, 2, 11, 0)) == (23, False)
    assert _target(controller, _state(23, soc=23, pv=0), datetime(2026, 11, 3, 5, 0)) == (23, False)
    assert _target(controller, _state(23, soc=23, pv=60), datetime(2026, 11, 3, 8, 0)) == (26, True)


def test_a_device_back_in_winter_after_noon_takes_no_step_despite_an_older_date():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    controller.winter.device("WR1").step_date = "2026-03-31"

    assert _target(controller, _state(20, soc=20), AFTERNOON, entry=BATTERY_ONLY) == (20, False)


def test_a_skipped_day_is_not_reported_as_a_step():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=20), AFTERNOON, entry=BATTERY_ONLY)

    assert controller.winter.last_step_date() is None


def test_a_device_without_state_writes_plans_no_winter_step(caplog):
    """MQTT control devices are output-only; a logged daily step would be fiction."""

    dev = SimpleNamespace(name="WR1", min_soc=15, max_soc=95, supports_state_reconciliation=False)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    caplog.set_level(logging.INFO)
    with _configured(entry=BATTERY_ONLY):
        for now in (MORNING, NOON):
            assert controller.winter.reconciliation_target(dev, _state(20, soc=20), True, now) == (None, False)

    assert not any("event=winter_step" in r.getMessage() for r in caplog.records)
    assert controller.winter.last_step_date() is None


@pytest.mark.parametrize(
    "entries, expected",
    [
        ([{"name": "WR1", "pv_kwp": 0}], 0),
        ([{"name": "WR1", "pv_kwp": 0, "enabled": False}, {"name": "WR1", "pv_kwp": 2}], 2),
        ([{"name": "WR1", "pv_kwp": 0}, {"name": "WR1", "pv_kwp": 2}], None),
        ([{"name": "WR2", "pv_kwp": 0}], None),
    ],
    ids=["one", "disabled-ignored", "ambiguous", "missing"],
)
def test_a_device_reads_its_settings_from_its_one_enabled_entry(entries, expected):
    with patch.object(cfg, "ZENDURE_CONFIG", entries):
        entry = cfg.device_config_entry("WR1")

    assert (entry or {}).get("pv_kwp") == expected


# --- Admin refuses a policy the EMS would not run ------------------------------


@pytest.mark.parametrize(
    "device, code",
    [
        ({"name": "AKKU", "pv_kwp": 0, "winter_policy": wp.SOLAR_MORNING_STEP}, "winter_policy_device_class"),
        ({"name": "WR1", "pv_kwp": 1.0, "winter_policy": "store_solar"}, "winter_policy_unknown"),
    ],
)
def test_a_policy_that_cannot_apply_is_an_issue(device, code):
    assert [issue["code"] for issue in wp.find_winter_policy_issues({"devices": [device]})] == [code]


@pytest.mark.parametrize(
    "device",
    [
        {"name": "AKKU", "pv_kwp": 0, "winter_policy": wp.NOON_STEP},
        {"name": "WR1", "pv_kwp": 1.0, "winter_policy": wp.NOON_STEP},
        {"name": "WR1", "pv_kwp": 1.0, "winter_policy": wp.AUTO},
        {"name": "WR1", "pv_kwp": 1.0},
    ],
)
def test_a_policy_that_fits_is_no_issue(device):
    assert wp.find_winter_policy_issues({"devices": [device]}) == []


def test_maintenance_refuses_a_policy_the_device_type_cannot_run():
    from admin.maintenance_config import _validate

    config = {
        "devices": [
            {
                "name": "AKKU",
                "ip": "192.168.1.100",
                "sn": "SERIAL-1",
                "pv_kwp": 0,
                "winter_policy": wp.SOLAR_MORNING_STEP,
            }
        ]
    }

    codes = [issue["code"] for issue in _validate(config)["errors"]]

    assert "winter_policy_device_class" in codes



# --- a live EMS keeps its step across a restart --------------------------------


@pytest.fixture
def writes_allowed():
    with patch.object(cfg, "state_reconciliation_writes_allowed", lambda gate: True):
        yield


def _store(tmp_path):
    from ems.state_store import WinterReserveStore

    return WinterReserveStore(str(tmp_path / "ems_state.sqlite"))


def test_with_a_store_a_restart_repeats_no_step_and_keeps_the_target(tmp_path, writes_allowed):
    store = _store(tmp_path)
    first = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    _target(first, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))
    assert _target(first, _state(20, soc=20, pv=60), datetime(2026, 11, 2, 8, 0)) == (23, True)

    restarted = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    assert _target(restarted, _state(20, soc=21, pv=0), datetime(2026, 11, 2, 10, 0)) == (23, False)
    assert _target(restarted, _state(20, soc=21, pv=300), datetime(2026, 11, 2, 10, 30)) == (23, False)


def test_with_a_store_a_start_in_daylight_after_yesterdays_step_takes_todays(tmp_path, writes_allowed):
    """A record of yesterday says today has no step yet; the day is not lost."""

    store = _store(tmp_path)
    yesterday = datetime(2026, 11, 1, 8, 0)
    store.save("WR1", yesterday, step_date="2026-11-01", step_at=yesterday, step_target=20, target=20, pv_ever=True)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)

    assert _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 10, 0)) == (23, True)


def test_with_a_store_but_no_record_a_start_in_daylight_takes_no_step(tmp_path, writes_allowed):
    """The day the upgrade lands, or after a failed save: a step may have been taken."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=_store(tmp_path))

    assert _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 10, 0)) == (20, False)


def test_pv_noise_at_night_is_no_morning():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 1, 0))

    assert _target(controller, _state(20, soc=20, pv=3), datetime(2026, 11, 2, 3, 0)) == (20, False)
    assert _target(controller, _state(20, soc=20, pv=60), datetime(2026, 11, 2, 7, 30)) == (23, True)


def test_a_device_offline_at_night_still_steps_when_it_returns_in_the_morning():
    """The step is the first PV of a day without one, not a dark reading followed by light."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 1, 22, 0))

    assert _target(controller, _state(20, soc=20, pv=80), datetime(2026, 11, 2, 8, 0)) == (23, True)


def test_the_class_defaults_are_validated_too():
    config = {"winter": {"policies": {wp.BATTERY_ONLY: wp.SOLAR_MORNING_STEP, wp.PV_BATTERY: wp.NOON_STEP}}}

    issues = wp.find_winter_policy_issues(config)

    assert [issue["code"] for issue in issues] == ["winter_policy_device_class"]
    assert "winter.policies.battery_only" in issues[0]["message"]



def test_a_dry_run_stores_no_step(tmp_path):
    """Nothing was written; a stored 40 would be the first live start's target."""

    store = _store(tmp_path)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    with patch.object(cfg, "state_reconciliation_writes_allowed", lambda gate: False):
        _target(controller, _state(20, soc=20, pv=60), datetime(2026, 11, 2, 8, 0))

    assert store.load("WR1") is None


def test_a_target_stored_last_winter_is_not_restored(tmp_path, writes_allowed):
    store = _store(tmp_path)
    store.save("WR1", datetime(2026, 3, 31, 12, 0), step_date="2026-03-31", step_at=None, step_target=40, target=40, pv_ever=False)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)

    assert _target(controller, _state(15, soc=70, pv=0), datetime(2026, 10, 1, 0, 5)) == (15, False)


def test_a_stored_target_is_kept_within_the_configured_ceiling(tmp_path, writes_allowed):
    store = _store(tmp_path)
    store.save("WR1", datetime(2026, 11, 1, 12, 0), step_date="2026-11-01", step_at=None, step_target=40, target=40, pv_ever=False)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    with _configured(), patch.object(cfg, "WINTER_CONFIG", {**cfg.WINTER_DEFAULTS, "winter_min_soc": 30}):
        result = controller.winter.reconciliation_target(_device(), _state(30, soc=35, pv=0), True, MORNING)

    assert result == (30, False)


def test_summer_forgets_the_stored_winter_target(tmp_path, writes_allowed):
    store = _store(tmp_path)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 3, 30, 5, 0))
    _target(controller, _state(20, soc=20, pv=60), datetime(2026, 3, 30, 8, 0))
    assert store.load("WR1")["target"] == 23

    with _configured():
        controller.winter.reconciliation_target(_device(), _state(23, soc=50), False, datetime(2026, 4, 1, 8, 0))

    record = store.load("WR1")
    assert (record["step_date"], record["target"]) == (None, None)


def test_a_noon_step_that_has_not_landed_yet_is_not_cut_back(writes_allowed):
    """Lagging telemetry still shows the old minSoc; the day's step keeps its lead."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(15, soc=13), MORNING, entry=BATTERY_ONLY)
    assert _target(controller, _state(15, soc=13), NOON, entry=BATTERY_ONLY) == (18, True)

    assert _target(controller, _state(15, soc=13), AFTERNOON, entry=BATTERY_ONLY) == (18, False)



def test_a_pv_device_set_to_the_noon_step_is_not_vetoed_by_its_pv():
    """The veto is for a battery-only class that reports PV, not for a chosen noon step."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    entry = {"winter_policy": wp.NOON_STEP}
    _target(controller, _state(20, soc=20, pv=300), MORNING, entry=entry)

    assert _target(controller, _state(20, soc=20, pv=300), NOON, entry=entry) == (23, True)


def test_noise_at_night_is_no_morning():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 0, 30))

    assert _target(controller, _state(20, soc=20, pv=12), datetime(2026, 11, 2, 1, 0)) == (20, False)
    assert _target(controller, _state(20, soc=20, pv=45), datetime(2026, 11, 2, 7, 30)) == (20, False)
    assert _target(controller, _state(20, soc=20, pv=60), datetime(2026, 11, 2, 8, 0)) == (23, True)


def test_after_a_restart_on_the_step_day_the_noon_step_keeps_its_lead(tmp_path, writes_allowed):
    store = _store(tmp_path)
    store.save("WR1", NOON, step_date=NOON.date().isoformat(), step_at=None, step_target=18, target=18, pv_ever=False)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)

    assert _target(controller, _state(15, soc=13), AFTERNOON, entry=BATTERY_ONLY) == (18, False)


def test_an_old_config_step_of_five_steps_by_three():
    """A noon step of five could lead the SoC by ten points."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    winter = {**cfg.WINTER_DEFAULTS, "ramp_step_percent": 5}
    with _configured(entry=BATTERY_ONLY), patch.object(cfg, "WINTER_CONFIG", winter):
        controller.winter.reconciliation_target(_device(), _state(20, soc=20), True, MORNING)
        assert controller.winter.reconciliation_target(_device(), _state(20, soc=20), True, NOON) == (23, True)


@pytest.mark.parametrize(
    "policies, codes",
    [
        ({wp.BATTERY_ONLY: None}, []),
        ({"battery-only": wp.NO_POLICY}, ["winter_policy_device_class_unknown"]),
    ],
    ids=["null-is-unset", "misspelled-type"],
)
def test_class_defaults_validate_like_the_runtime_reads_them(policies, codes):
    issues = wp.find_winter_policy_issues({"winter": {"policies": policies}})

    assert [issue["code"] for issue in issues] == codes


def test_after_a_restart_a_followed_target_gets_no_step_lead(tmp_path, writes_allowed):
    """Only the step's own target may lead the SoC; a follow saved the same day may not."""

    store = _store(tmp_path)
    store.save("WR1", AFTERNOON, step_date=NOON.date().isoformat(), step_at=None, step_target=33, target=38, pv_ever=False)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)

    assert _target(controller, _state(33, soc=33), AFTERNOON, entry=BATTERY_ONLY) == (33, False)


def test_the_noon_steps_lead_is_two_steps_even_on_its_own_day(writes_allowed):
    """A step whose battery then drained further is not written ten points above it."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(15, soc=13), MORNING, entry=BATTERY_ONLY)
    assert _target(controller, _state(15, soc=13), NOON, entry=BATTERY_ONLY) == (18, True)

    assert _target(controller, _state(15, soc=8), AFTERNOON, entry=BATTERY_ONLY) == (15, False)


def test_a_restart_in_weak_morning_light_takes_no_second_step():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)

    assert _target(controller, _state(23, soc=23, pv=30), datetime(2026, 11, 2, 9, 30)) == (23, False)
    assert _target(controller, _state(23, soc=23, pv=300), datetime(2026, 11, 2, 10, 30)) == (23, False)


def test_switching_winter_off_and_on_keeps_todays_step_in_memory(tmp_path):
    """A store without a record -- a save that failed -- must not erase what memory knows."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=_store(tmp_path))
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))
    with patch.object(cfg, "state_reconciliation_writes_allowed", lambda gate: False):
        assert _target(controller, _state(20, soc=20, pv=60), datetime(2026, 11, 2, 8, 0)) == (23, True)
    with _configured(), patch.object(cfg, "winter_feature_enabled", lambda runtime: False):
        controller.winter.reconciliation_target(_device(), _state(23, soc=23), True, datetime(2026, 11, 2, 9, 0))

    assert _target(controller, _state(23, soc=23, pv=300), datetime(2026, 11, 2, 9, 30)) == (23, False)


def test_comment_keys_in_the_class_defaults_are_no_issue():
    config = {"winter": {"policies": {"_comment": ["see winter-mode.md"], wp.BATTERY_ONLY: wp.NOON_STEP}}}

    assert wp.find_winter_policy_issues(config) == []



@pytest.mark.parametrize("policy_entry", [{}, BATTERY_ONLY], ids=["morning-step", "noon-step"])
def test_a_min_soc_above_the_ceiling_comes_down_to_it(policy_entry):
    """Set in the app, or left by a higher ceiling, it is not kept and raised further."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(50, soc=60, pv=0), datetime(2026, 11, 2, 5, 0), entry=policy_entry)

    target, _ = _target(controller, _state(50, soc=60, pv=300), NOON, entry=policy_entry)

    assert target == 40


def test_a_noon_step_is_taken_only_in_the_step_hour():
    """A battery too low at noon is not stepped late in the evening once it was charged."""

    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(15, soc=10), MORNING, entry=BATTERY_ONLY)
    assert _target(controller, _state(15, soc=10), NOON, entry=BATTERY_ONLY) == (15, False)

    assert _target(controller, _state(15, soc=14), datetime(2026, 11, 2, 23, 30), entry=BATTERY_ONLY) == (15, False)


def test_a_step_the_store_cannot_keep_is_not_taken(tmp_path, writes_allowed):
    """A restart would read yesterday's record and take it again."""

    store = _store(tmp_path)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))
    with patch.object(store, "save", side_effect=OSError("disk full")):
        assert _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 8, 0)) == (20, False)

    assert _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 9, 0)) == (20, False)


def test_a_store_that_failed_to_load_is_not_overwritten(tmp_path, writes_allowed):
    store = _store(tmp_path)
    store.save("WR1", NOON, step_date="2026-11-01", step_at=None, step_target=23, target=23, pv_ever=True)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    with patch.object(store, "load", side_effect=OSError("locked")):
        _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 8, 0))

    assert store.load("WR1")["target"] == 23


def test_noise_does_not_mark_a_battery_only_device_as_pv():
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    _target(controller, _state(20, soc=20, pv=12), MORNING, entry=BATTERY_ONLY)

    assert _target(controller, _state(20, soc=20, pv=0), NOON, entry=BATTERY_ONLY) == (23, True)


def test_a_failed_load_is_retried_and_does_not_stop_the_ramp(tmp_path, writes_allowed):
    store = _store(tmp_path)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    with patch.object(store, "load", side_effect=OSError("locked")):
        _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))

    assert _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 8, 0)) == (23, True)
    assert store.load("WR1")["target"] == 23


def test_a_retried_load_takes_over_todays_stored_step(tmp_path, writes_allowed):
    """A restart whose first read failed must not step again over the stored step."""

    store = _store(tmp_path)
    first = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    _target(first, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))
    assert _target(first, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 8, 0)) == (23, True)

    restarted = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    with patch.object(store, "load", side_effect=OSError("locked")):
        _target(restarted, _state(23, soc=23, pv=0), datetime(2026, 11, 2, 10, 0))

    assert _target(restarted, _state(23, soc=23, pv=300), datetime(2026, 11, 2, 11, 0)) == (23, False)
    assert store.load("WR1")["step_target"] == 23


def test_a_load_that_succeeds_again_clears_an_earlier_failure(tmp_path, writes_allowed):
    store = _store(tmp_path)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    controller.winter.device("WR1").store_failed = True
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))

    assert _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 8, 0)) == (23, True)


def test_a_follow_the_store_cannot_keep_is_not_taken(tmp_path, writes_allowed):
    store = _store(tmp_path)
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False, winter_store=store)
    _target(controller, _state(20, soc=20, pv=0), datetime(2026, 11, 2, 6, 0))
    _target(controller, _state(20, soc=20, pv=300), datetime(2026, 11, 2, 8, 0))
    with patch.object(store, "save", side_effect=OSError("disk full")):
        assert _target(controller, _state(23, soc=30, pv=300), datetime(2026, 11, 2, 10, 0)) == (23, False)
