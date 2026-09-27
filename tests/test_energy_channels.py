# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for the energy-channel registry.

The sign convention of the dashboard snapshot (grid positive means import,
battery positive means charging) is stated once in ``ems/energy_channels.py``.
These tests hold that statement, because every reading of those two fields
elsewhere is a projection of it.
"""
import pytest

from ems.energy_channels import (
    ENERGY_CHANNELS,
    channel_power_w,
    channel_sample_wh,
    find_channel,
    self_consumption,
    self_sufficiency,
)

pytestmark = [
    pytest.mark.unit,
]


def test_grid_power_splits_into_import_and_export():
    importing = {"grid_power_w": 240.0}
    exporting = {"grid_power_w": -240.0}

    assert channel_power_w(find_channel("grid_import"), importing) == 240.0
    assert channel_power_w(find_channel("grid_export"), importing) == 0.0
    assert channel_power_w(find_channel("grid_import"), exporting) == 0.0
    assert channel_power_w(find_channel("grid_export"), exporting) == 240.0


def test_battery_power_splits_into_charge_and_discharge():
    charging = {"battery_power_w": 600.0}
    discharging = {"battery_power_w": -600.0}

    assert channel_power_w(find_channel("battery_charge"), charging) == 600.0
    assert channel_power_w(find_channel("battery_discharge"), charging) == 0.0
    assert channel_power_w(find_channel("battery_charge"), discharging) == 0.0
    assert channel_power_w(find_channel("battery_discharge"), discharging) == 600.0


def test_missing_or_unparsable_source_reads_as_zero():
    assert channel_power_w(find_channel("pv_yield"), {}) == 0.0
    assert channel_power_w(find_channel("pv_yield"), {"pv_total_w": None}) == 0.0
    assert channel_power_w(find_channel("pv_yield"), {"pv_total_w": "n/a"}) == 0.0


def test_sample_integrates_every_channel_over_the_interval():
    snapshot = {
        "grid_power_w": -100.0,
        "battery_power_w": 200.0,
        "pv_total_w": 400.0,
        "home_load_w": 300.0,
    }

    sample = channel_sample_wh(snapshot, 0.5)

    assert sample["grid_import"] == 0.0
    assert sample["grid_export"] == 50.0
    assert sample["battery_charge"] == 100.0
    assert sample["battery_discharge"] == 0.0
    assert sample["pv_yield"] == 200.0
    assert sample["home_consumption"] == 150.0


def test_sample_without_elapsed_time_is_zero_for_every_channel():
    snapshot = {"grid_power_w": 900.0, "battery_power_w": -900.0}

    assert set(channel_sample_wh(snapshot, 0).values()) == {0.0}
    assert set(channel_sample_wh(snapshot, -5).values()) == {0.0}


def test_self_sufficiency_needs_home_consumption():
    assert self_sufficiency(10_000.0, 2_500.0) == pytest.approx(0.75)
    assert self_sufficiency(0.0, 0.0) is None
    assert self_sufficiency(None, 100.0) is None


def test_self_sufficiency_never_reports_below_zero():
    """More grid import than home consumption is a measurement artefact.

    The two come from different integrations, so a short window can produce
    import above consumption. A negative autarky reads as a defect; zero is the
    honest floor.
    """

    assert self_sufficiency(100.0, 250.0) == 0.0


def test_self_consumption_needs_delivered_energy():
    assert self_consumption(10_000.0, 1_000.0) == pytest.approx(0.9)
    assert self_consumption(0.0, 0.0) is None
    assert self_consumption(100.0, 250.0) == 0.0


def test_channel_columns_are_plain_sql_identifiers():
    """The column names are interpolated into DDL, so they may never be free text."""

    for channel in ENERGY_CHANNELS:
        assert channel.column.isidentifier()
        assert channel.column == f"{channel.id}_wh"


def test_channel_ids_are_unique_and_stable():
    ids = [channel.id for channel in ENERGY_CHANNELS]

    assert len(ids) == len(set(ids))
    assert ids == [
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
        "pv_yield",
        "home_consumption",
    ]


def test_unknown_channel_is_refused():
    with pytest.raises(KeyError):
        find_channel("nuclear_reactor")
