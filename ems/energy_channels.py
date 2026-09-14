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

    Whether a sample may be integrated at all is decided for the sample as a
    whole, not per channel: see ``sample_is_measured``.
    """

    id: str
    label: str
    source: str
    direction: str
    column: str


# A sample is integrated only while every one of these holds. They are the
# snapshot's own statements about whether its readings are readings at all: a
# meter that never answered publishes its initial 0 W, and an offline device
# keeps publishing its last telemetry.
#
# The gate is deliberately per sample rather than per channel. House load is
# derived from both the meter and the devices while grid import comes from the
# meter alone, so a per-channel gate would let the two sides of the autarky be
# measured over different samples -- which turned a fifty percent day into
# twenty-five, and then, once that was detected instead of prevented, withheld
# the figure for good after a single blip. Skipping the whole sample costs a
# valid meter reading now and then, keeps every channel on one basis, and makes
# the hole visible in all of them.
SAMPLE_VALIDITY_FLAGS = ("grid_power_valid", "device_power_valid")

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
    EnergyChannel(
        "pv_yield", "PV Yield", "pv_total_w", POSITIVE, "pv_yield_wh"
    ),
    # House load is what the household draws: the power the fleet charges
    # from AC is netted out again (``ems.power_direction.derive_house_load_w``),
    # because the meter alone cannot tell a charging device from an appliance.
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


def sample_is_measured(snapshot):
    """Whether this sample's readings were readings at all.

    A snapshot without the flags counts as measured: older writers and test
    stubs do not carry them, and treating their samples as unmeasured would
    drop data that was fine.
    """

    return all(bool(snapshot.get(flag, True)) for flag in SAMPLE_VALIDITY_FLAGS)


def measured_channel_ids(snapshot):
    """Every channel, or none: the gate is per sample, not per channel."""

    if not sample_is_measured(snapshot):
        return ()

    return CHANNEL_IDS


def channel_sample_wh(snapshot, elapsed_hours):
    """Integrate every channel over one sampling interval.

    A non-positive interval yields zero for every channel, which is how a
    skipped or restarted sample is recorded. So does a sample whose readings
    could not be read.
    """

    if not elapsed_hours or elapsed_hours <= 0 or not sample_is_measured(snapshot):
        return {channel.id: 0.0 for channel in ENERGY_CHANNELS}

    return {
        channel.id: channel_power_w(channel, snapshot) * elapsed_hours
        for channel in ENERGY_CHANNELS
    }


def self_sufficiency(home_consumption_wh, grid_import_wh):
    """Share of house consumption that did not come from the grid.

    ``None`` when there was no consumption to divide. The share is floored at
    zero: two independently integrated sums can cross over a short window, and
    a negative share would read as a defect rather than as measurement noise.
    """

    try:
        home = float(home_consumption_wh or 0)
        grid = float(grid_import_wh or 0)
    except (TypeError, ValueError):
        return None

    if home <= 0:
        return None

    return max(0.0, (home - grid) / home)
