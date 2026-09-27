# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one place that says what an energy channel is.

A channel is one direction of one snapshot field. The dashboard snapshot
publishes grid power as positive for import and battery power as positive for
charging (``dashboard/telemetry.py``), so splitting those two fields into four
channels is a sign decision — and it is stated here, once. The module is
import-side-effect-free so the store, the CLI and the diagnostics layer can all
read the same table.
"""
from dataclasses import dataclass

POSITIVE = "positive"
NEGATIVE = "negative"


@dataclass(frozen=True)
class EnergyChannel:
    """One measured direction of one snapshot field.

    ``column`` is the SQLite column that carries the channel's daily total. It
    is interpolated into DDL, which is why it is validated below.
    """

    id: str
    label: str
    source: str
    direction: str
    column: str


ENERGY_CHANNELS = (
    EnergyChannel(
        "grid_import", "Grid Import", "grid_power_w", POSITIVE, "grid_import_wh"
    ),
    EnergyChannel(
        "grid_export", "Grid Export", "grid_power_w", NEGATIVE, "grid_export_wh"
    ),
    EnergyChannel(
        "battery_charge", "Charged", "battery_power_w", POSITIVE, "battery_charge_wh"
    ),
    EnergyChannel(
        "battery_discharge",
        "Discharged",
        "battery_power_w",
        NEGATIVE,
        "battery_discharge_wh",
    ),
    EnergyChannel("pv_yield", "PV Yield", "pv_total_w", POSITIVE, "pv_yield_wh"),
    EnergyChannel(
        "home_consumption", "Home", "home_load_w", POSITIVE, "home_consumption_wh"
    ),
)

CHANNEL_IDS = tuple(channel.id for channel in ENERGY_CHANNELS)

_CHANNELS_BY_ID = {channel.id: channel for channel in ENERGY_CHANNELS}

for _channel in ENERGY_CHANNELS:
    if not _channel.column.isidentifier():
        raise ValueError(f"channel column is not an identifier: {_channel.column}")
    if _channel.direction not in (POSITIVE, NEGATIVE):
        raise ValueError(f"channel direction is unknown: {_channel.direction}")
del _channel


def find_channel(channel_id):
    """Return the channel with this id, or raise ``KeyError``."""

    return _CHANNELS_BY_ID[channel_id]


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def channel_power_w(channel, snapshot):
    """Return this channel's share of its signed source field, never negative."""

    value = _as_float(snapshot.get(channel.source))
    if channel.direction == POSITIVE:
        return max(0.0, value)
    return max(0.0, -value)


def channel_sample_wh(snapshot, elapsed_hours):
    """Integrate every channel over one sampling interval.

    A non-positive interval yields zero for every channel, which is how a
    skipped or restarted sample is recorded.
    """

    if not elapsed_hours or elapsed_hours <= 0:
        return {channel.id: 0.0 for channel in ENERGY_CHANNELS}

    return {
        channel.id: channel_power_w(channel, snapshot) * elapsed_hours
        for channel in ENERGY_CHANNELS
    }


def _ratio(numerator, denominator):
    try:
        denominator = float(denominator)
        numerator = float(numerator)
    except (TypeError, ValueError):
        return None

    if denominator <= 0:
        return None

    # Two independently integrated sums can cross over a short window; a
    # negative share would read as a defect rather than as measurement noise.
    return max(0.0, numerator / denominator)


def self_sufficiency(home_consumption_wh, grid_import_wh):
    """Share of house consumption that did not come from the grid."""

    return _ratio(
        _as_float(home_consumption_wh) - _as_float(grid_import_wh),
        home_consumption_wh,
    )


def self_consumption(inverter_output_wh, grid_export_wh):
    """Share of delivered AC energy the house used instead of exporting."""

    return _ratio(
        _as_float(inverter_output_wh) - _as_float(grid_export_wh),
        inverter_output_wh,
    )
