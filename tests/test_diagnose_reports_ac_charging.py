# SPDX-License-Identifier: AGPL-3.0-or-later
"""`diagnose --control` said nothing about the one feature that spends energy.

An operator asking "why is my system drawing from the grid?" reaches for this
tool, and AC charging did not appear in it at all -- not whether it was on, not
under what limits, not which devices were allowed to do it. The live *direction*
genuinely cannot go here (the regulator never writes it to runtime state, by
design), but everything else was readable the whole time.
"""

import pytest

from ems.diagnostics import (
    diagnose_ac_charging_snapshot,
    diagnose_ac_charging_text,
    diagnose_format_threshold_watts,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.config,
]


def config(**over):
    base = {
        "ac_charge_control": {
            "enabled": True,
            "charge_start_w": 150,
            "charge_hysteresis_w": 50,
            "max_total_charge_power_w": 1200,
        },
        "devices": [
            {"name": "WR1", "ac_charge_enabled": True},
            {"name": "WR2", "ac_charge_enabled": False},
        ],
    }
    base["ac_charge_control"].update(over)
    return base


def test_the_band_is_reported_as_the_loop_derives_it():
    snapshot = diagnose_ac_charging_snapshot(config(), {}, {})

    assert snapshot["ac_charging_enabled"] is True
    assert snapshot["ac_charge_start_w"] == 150
    # Derived, never configured -- the same max(0, start - hysteresis).
    assert snapshot["ac_charge_stop_w"] == 100
    assert snapshot["ac_charge_system_limit_w"] == 1200


def test_a_collapsed_band_is_visible_here_too():
    snapshot = diagnose_ac_charging_snapshot(config(charge_hysteresis_w=0), {}, {})

    assert snapshot["ac_charge_start_w"] == snapshot["ac_charge_stop_w"] == 150


def test_runtime_state_wins_over_config_exactly_as_the_loop_resolves_it():
    """Reporting a feature as on that an operator switched off an hour ago would
    make this tool worse than silent."""

    runtime = {"ac_charge_control": {"enabled": False}}

    assert diagnose_ac_charging_snapshot(config(), runtime, {})["ac_charging_enabled"] is False
    # And the other way: config off, runtime on.
    configured_off = config()
    configured_off["ac_charge_control"]["enabled"] = False
    assert diagnose_ac_charging_snapshot(
        configured_off, {"ac_charge_control": {"enabled": True}}, {}
    )["ac_charging_enabled"] is True


def test_the_permitted_devices_follow_the_same_precedence():
    snapshot = diagnose_ac_charging_snapshot(config(), {}, {})
    assert snapshot["ac_charge_permitted_devices"] == ["WR1"]

    # A runtime switch on WR2 and off WR1 flips both.
    snapshot = diagnose_ac_charging_snapshot(
        config(),
        {},
        {"WR1": {"ac_charge_enabled": False}, "WR2": {"ac_charge_enabled": True}},
    )
    assert snapshot["ac_charge_permitted_devices"] == ["WR2"]


def test_a_device_without_the_key_is_permitted_like_the_loop_permits_it():
    """The default is on, and the report must not disagree with the controller."""

    minimal = {"ac_charge_control": {"enabled": True}, "devices": [{"name": "WR1"}]}

    assert diagnose_ac_charging_snapshot(minimal, {}, {})["ac_charge_permitted_devices"] == ["WR1"]


def test_a_threshold_is_not_labelled_as_an_import():
    """`diagnose_format_watts` labels a sign because a meter reading has a
    direction. A threshold does not -- calling a 150 W surplus threshold
    "150 W import" says the opposite of what it gates."""

    assert diagnose_format_threshold_watts(150) == "150 W"
    assert diagnose_format_threshold_watts(None) == "unknown"

    text = "\n".join(diagnose_ac_charging_text(diagnose_ac_charging_snapshot(config(), {}, {})))
    assert "import" not in text
    assert "export" not in text


def test_the_text_says_plainly_when_charging_is_off():
    configured_off = config()
    configured_off["ac_charge_control"]["enabled"] = False

    text = "\n".join(
        diagnose_ac_charging_text(diagnose_ac_charging_snapshot(configured_off, {}, {}))
    )

    assert "AC Charging:          disabled" in text
    # Nothing about bands or devices when it cannot act.
    assert "Band" not in text


def test_the_live_direction_is_named_as_absent_rather_than_left_out():
    """It cannot be reported, so the tool says where to find it instead of
    leaving a reader to conclude nothing is happening."""

    text = "\n".join(diagnose_ac_charging_text(diagnose_ac_charging_snapshot(config(), {}, {})))

    assert "ac_charge_direction" in text


def test_a_support_bundle_carries_the_control_diagnostics_it_promises():
    """The documented file list reads as a promise of content, and was not.

    `--support-bundle` alone wrote `control-diagnostics.json` and
    `control-quality.json` as empty objects, because both sections are opt-in
    behind their own flags. An operator following the documentation sent a
    bundle with nothing about the control behaviour in it -- and the point of a
    bundle is not needing a second round trip.
    """

    from argparse import Namespace

    from ems.diagnostics import diagnose_service_args

    plain = diagnose_service_args(Namespace(support_bundle=False, control=False))
    assert plain.control is False
    assert plain.control_quality is False

    bundled = diagnose_service_args(Namespace(support_bundle=True, control=False))
    assert bundled.control is True
    assert bundled.control_quality is True

    # An explicit request is not overridden into something narrower.
    asked = diagnose_service_args(
        Namespace(support_bundle=True, control=True, sample_seconds=30)
    )
    assert asked.sample_seconds == 30
