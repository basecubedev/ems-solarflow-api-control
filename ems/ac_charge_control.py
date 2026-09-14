# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direction decision and charge allocation for AC charging from surplus.

Pure functions over an explicit state value: the controller owns the loop, this
module owns *when* a device changes direction and *how much* each device takes.

Two properties matter more than the arithmetic.

**Entry and exit do not measure the same quantity, and must not.** Before
charging there is no charge to observe, so the signal is the surplus the
discharge side could not absorb — the integrator sits at its floor and the
filtered load is still negative. Once charging, that surplus has been consumed
by the charging itself and the meter reads roughly balanced; measuring it again
would read "no surplus" and leave immediately.

During charging the signal is the *unramped* desired total — the commanded
charge plus the current load, before the ramp limits how far it may move this
cycle. Measuring the ramped value instead makes the exit wait for the ramp: a
house that suddenly draws 1200 W while 900 W is being charged produces a desired
total of zero and a ramped total still deep in charge, so the exit that is meant
to be immediate would take several cycles of drawing from the grid. The ramp
governs how fast the magnitude moves, never which direction is held.

**Entry is deliberate, exit is immediate.** Relays move on an AC mode change and
wear is cumulative, so the asymmetry is the real protection: hysteresis alone
cannot help when the surplus swings wider than the band, but requiring a
sustained surplus to re-enter bounds the switch rate no matter how the load
behaves.

Entry counts *k of the last n* observations rather than k in a row or a mean
over a window. Each observation is judged against the threshold on its own, so
height can never substitute for duration — a single spike ten times the
threshold is one observation, where a mean would read it as a sustained surplus
and move a relay for something already over. Counting within a window rather
than in a row means one brief dip no longer discards the evidence gathered so
far.
"""

from dataclasses import dataclass, replace

from ems.target_control import get_device_battery_kwh, weighted_limited_allocation

# Why a direction decision came out the way it did. Stable, machine-readable.
REASON_NOT_POSSIBLE = "charging_not_possible"
REASON_HELD = "direction_held"
REASON_CONFIRMING = "entry_confirming"
REASON_ENTERED = "charge_entered"
REASON_LEFT_BELOW_STOP = "surplus_below_stop"
REASON_ENTRY_RATE_LIMITED = "entry_rate_limited"

SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class ChargeDirectionSettings:
    """The operator-facing thresholds, resolved once per cycle."""

    start_w: int = 150
    stop_w: int = 100
    entry_confirm_cycles: int = 5
    entry_window_cycles: int = 7
    max_entries_per_hour: int = 12

    @property
    def entry_window(self):
        """Window length, never shorter than the count it must contain."""

        return max(1, self.entry_confirm_cycles, self.entry_window_cycles)


@dataclass(frozen=True)
class ChargeDirectionState:
    """What the direction decision carries between cycles.

    ``entry_window`` holds the most recent entry observations, newest last,
    trimmed to the configured window. ``entries`` holds the times charging was
    entered, kept only for the trailing hour the rate limit looks at.
    """

    charging: bool = False
    entry_window: tuple = ()
    entries: tuple = ()


@dataclass(frozen=True)
class ChargeDirectionDecision:
    charging: bool
    state: ChargeDirectionState
    reason: str
    rate_limited: bool = False


def _recent_entries(entries, now):
    return tuple(t for t in entries if now - t < SECONDS_PER_HOUR)


def decide_charge_direction(
    previous,
    *,
    charging_possible,
    commanded_total_w,
    desired_total_w,
    filtered_load_w,
    now,
    settings,
):
    """Decide whether the system charges this cycle.

    ``commanded_total_w`` is the ramped total the loop is holding and answers
    "is the discharge side done"; ``desired_total_w`` is the same total before
    the ramp and answers "is there still a surplus to absorb". Each question
    gets the quantity that can answer it.

    ``charging_possible`` collapses every permission axis into one answer. When
    it is false the decision is always "not charging": the way back is never
    blocked by a threshold, a counter or a rate limit, because failing closed on
    the *return* path would leave hardware drawing from the grid.
    """

    entries = _recent_entries(previous.entries, now)

    if not charging_possible:
        return ChargeDirectionDecision(
            charging=False,
            state=ChargeDirectionState(False, (), entries),
            reason=REASON_NOT_POSSIBLE,
        )

    if previous.charging:
        if desired_total_w > -settings.stop_w:
            return ChargeDirectionDecision(
                charging=False,
                state=ChargeDirectionState(False, (), entries),
                reason=REASON_LEFT_BELOW_STOP,
            )
        return ChargeDirectionDecision(
            charging=True,
            state=replace(previous, entries=entries),
            reason=REASON_HELD,
        )

    # Not charging: the discharge side must already be at its floor, and the
    # surplus it could not absorb must exceed the start threshold. Each cycle is
    # one observation; a cycle that fails is recorded as such rather than
    # discarding the window.
    observed = commanded_total_w <= 0 and filtered_load_w <= -settings.start_w
    window = (previous.entry_window + (observed,))[-settings.entry_window:]
    confirmed = sum(1 for item in window if item)

    if confirmed < max(1, settings.entry_confirm_cycles):
        return ChargeDirectionDecision(
            charging=False,
            state=ChargeDirectionState(False, window, entries),
            reason=REASON_CONFIRMING if observed else REASON_HELD,
        )

    if len(entries) >= max(1, settings.max_entries_per_hour):
        # Never silent: reaching this means the thresholds do not fit the
        # installation, and the caller logs it as such.
        return ChargeDirectionDecision(
            charging=False,
            state=ChargeDirectionState(False, window, entries),
            reason=REASON_ENTRY_RATE_LIMITED,
            rate_limited=True,
        )

    return ChargeDirectionDecision(
        charging=True,
        state=ChargeDirectionState(True, (), entries + (now,)),
        reason=REASON_ENTERED,
    )


def resolve_max_charge_power_w(device_config, hardware_profile=None):
    """Highest AC charge power for one device.

    Precedence is the contract, not the values: an explicit setting always wins,
    so adding a model catalogue later can never override an installation where
    somebody set the number by hand. The catalogue slots in as the middle tier.
    """

    explicit = getattr(device_config, "max_charge_power_w", 0) or 0
    try:
        explicit = int(explicit)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit

    # Tier two — a per-model limit from the hardware catalogue — does not exist
    # yet; hardware_profile is accepted so adding it stays a one-line change.

    fallback = getattr(device_config, "max_power", 0) or 0
    try:
        return max(0, int(fallback))
    except (TypeError, ValueError):
        return 0


def charge_headroom_weight(state, device_config, capability):
    """Return absorbable energy in weighted units, mirroring the discharge side.

    ``usable_battery_weight`` weights a discharge by the energy above the floor;
    a charge is weighted by the energy still missing below the ceiling. Same
    shape, opposite end.
    """

    if capability and not capability.can_charge:
        return 0

    if state.max_soc <= 0:
        return 0

    headroom_percent = max(0, state.max_soc - state.soc)
    if headroom_percent <= 0:
        return 0

    return max(0, get_device_battery_kwh(device_config) * headroom_percent / 100)


def allocate_charge_targets(
    total_charge_w, states, device_configs, capabilities, chargeable
):
    """Split a positive charge total into signed per-device targets.

    Returns negative watts per device — the controller's sign convention — and
    reuses the same weighted allocation primitive the discharge side uses, so
    there is one allocator with two weight functions rather than two allocators.
    """

    weights = []
    limits = []
    for index, state in enumerate(states):
        device_config = device_configs[index]
        capability = capabilities[index] if index < len(capabilities) else None
        if not chargeable[index]:
            weights.append(0)
            limits.append(0)
            continue
        weights.append(charge_headroom_weight(state, device_config, capability))
        limits.append(resolve_max_charge_power_w(device_config))

    allocation = weighted_limited_allocation(max(0, total_charge_w), weights, limits)
    return [-int(round(value)) for value in allocation]


__all__ = [
    "REASON_NOT_POSSIBLE",
    "REASON_HELD",
    "REASON_CONFIRMING",
    "REASON_ENTERED",
    "REASON_LEFT_BELOW_STOP",
    "REASON_ENTRY_RATE_LIMITED",
    "ChargeDirectionSettings",
    "ChargeDirectionState",
    "ChargeDirectionDecision",
    "decide_charge_direction",
    "resolve_max_charge_power_w",
    "charge_headroom_weight",
    "allocate_charge_targets",
]
