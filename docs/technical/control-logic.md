# Control Logic

The EMS calculates power targets from local load and device telemetry.

For a visual user-facing map of where each `config.json` parameter affects the
control chain, see [control-flow.md](control-flow.md).

## Pipeline

1. Reload `runtime-state.json` if changed.
2. Optionally sync Home Assistant helper values with runtime state.
3. Read Shelly house load.
4. Read Zendure telemetry.
5. Detect runtime capabilities.
6. Run state reconciliation when due.
7. Detect strict night/minSoc idle.
8. Stabilize total target.
9. Decide the charge direction.
10. Allocate target across devices.
11. Apply device ramp and limits.
12. Apply `min_output_limit` while enabled (output direction only).
13. Apply deadband and write gates.
14. Write the power command only behind safety gates.

## Stable Fast Output Control

The controller keeps an internal `commanded_total_w`. It is signed: positive is
power supplied to the house, negative is power drawn from AC into the batteries.
Its lower bound is zero unless the system is in charge direction, which is what
keeps the integrator from winding down into a charge nobody decided to take.

It calculates:

```text
desired_total_w = commanded_total_w + filtered_load_w
```

Positive load is integrated only when at least one active online device has
export capacity. Export capacity means current PV on `solarInputPower` or
`solarPower1` through `solarPower4`, confirmed discharge capability, or current
home output. Without export capacity, the controller holds `commanded_total_w`
at the standby total derived from `min_output_limit` and the number of active
online devices.

Then it applies:

- load deadband
- no-export-capacity hold
- target deadband
- median/EMA filtering
- optional sign-change fast response for large import/export direction flips
- total ramp limit
- per-device ramp limit
- stale telemetry ramp reduction
- large import/export ramp bypass multiplier

This makes short loop intervals usable without large alternating target swings.

The sign-change fast response only adjusts the filtered load value inside the
median/EMA stage. It does not bypass total ramping, per-device ramping, write
deadbands, write gates, or state reconciliation safeguards.

## Calculated Values And Targets

`home` is a calculated runtime/dashboard value derived from currently available
telemetry. It is useful for visibility, but it is not the same thing as the
smoothed control target.

The control target can be filtered, smoothed, ramped, clamped, limited by
device state, and held back by write gates. The `outputLimit` written to a
Zendure device is the EMS command limit for that device; the actual Zendure
output can differ for a short time because of API delay, device behavior,
available PV/battery power, or firmware state.

Off-grid socket mode is an operator/device mode state. It is not output power
and should not be added to the home-load, target, or output calculation.

## Night / minSoc Idle

When all active and online devices are blocked at their discharge floor, the EMS
can enter a strict night/minSoc idle state. This state exists to avoid repeated
night-time API writes while still keeping the inverter wakeup value configured.

The state is entered only when every controlled device reports all of these
values exactly:

- `solarInputPower == 0`
- `solarPower1 == 0`
- `solarPower2 == 0`
- `solarPower3 == 0`
- `solarPower4 == 0`
- `packInputPower == 0`
- `outputPackPower == 0`
- `outputHomePower == 0`
- `electricLevel <= minSoc` or `socLimit == 2`

In this state the existing runtime `min_output_limit` is used as the
standby/wakeup `outputLimit`. If a device is already at that value, no write is
sent. If it is not, the EMS writes the value once and then suppresses further
`outputLimit` writes until the state is left.

Night/minSoc idle is a control-idle state, not a system-idle state. The EMS loop
continues to fetch device state, process runtime state, publish Home Assistant
telemetry, and expose status and safety visibility. Only repeated output-control
writes are suppressed after the optional parking write.

The idle state is left as soon as any controlled device reports positive PV on
`solarInputPower` or one of `solarPower1` through `solarPower4`. The output
control memory is reset so the normal controller initializes from fresh
telemetry.

## No Export Capacity Hold

If house load is positive but no active online device currently has export
capacity, the EMS does not add that load to `commanded_total_w`. This prevents a
night or blocked-battery state from ramping the global target to
`max_total_power` when no device can actually serve the load.

The hold uses:

```text
standby_total_w = min_output_limit * active_online_device_count
```

With two active devices and `min_output_limit=35`, the global target is held at
`70W` instead of integrating toward `800W`. Once PV, discharge capability, or
current output is observed again, the normal fast output controller resumes.

## PV-First Allocation

When PV can cover the requested target, the EMS allocates output using PV-first
weights and PV-only limits.

PV-first weights can include a charge-balancing bias. When SOC spread is above
the configured deadband, fuller batteries receive more PV-first output weight
so lower-SOC batteries can keep more local PV for charging. The allocation still
uses each device's PV-only limit.

If PV-first allocation leaves unmet demand, the EMS may top up from battery only
on devices that:

- can export
- can discharge
- have SOC above minSoc
- have target headroom

When battery top-up is used, the final constraint pass keeps the normal device
and capability limits but does not clamp the intentional top-up back to the
PV-only limits.

## Battery Balancing

Battery discharge is weighted by usable battery energy:

```text
usable_percent = max(0, soc - minSoc)
weight = battery_kwh * usable_percent / 100
```

This favors devices with more usable energy while avoiding devices at or below
their discharge floor.

## AC Charging From Surplus

On by default (`ac_charge_control.enabled`). A device charges
only if every one of these agrees: the feature, the device's own
`ac_charge_enabled`, a hardware model whose AC charge path is established,
current telemetry that allows charging, the device being online and enabled, and
nobody else owning its AC mode. Any single no is enough.

### Direction

Entry and exit deliberately measure different quantities.

Before charging there is no charge to observe, so the signal is the surplus the
discharge side could not absorb: the integrator sits at its floor and the
filtered load is still negative. Entry needs
`ac_charge_control.entry_confirm_cycles` of the last `entry_window_cycles`
observations to show that — five of seven by default.

Counting observations rather than averaging them is deliberate. A mean lets
height substitute for duration: one spike ten times the threshold averages to a
sustained surplus and would move a relay for something already over. Counting
within a window rather than in a row is equally deliberate: one brief dip costs
a single observation instead of discarding the whole confirmation.

Once charging, that surplus has been consumed by the charging itself and the
meter reads roughly balanced, so the exit reads the desired total instead —
the commanded charge plus the current load, **before** the ramp limits how far
it may move this cycle. Reading the ramped value would make an exit that is
meant to be immediate wait for the ramp.

Exit is immediate and unconditional. It is never gated by a threshold, a
confirmation counter or the rate limit, because failing closed on the way *back*
would leave hardware drawing from the grid.

### Why the zero crossing is quiet

Idle and discharge are the same AC mode, so a target crossing zero moves no
relay. The mode boundary sits at `charge_start_w`, far from zero. A load
oscillating around zero produces no direction change at all.

The band's lower edge is derived, not configured:

```text
stop = max(0, charge_start_w - charge_hysteresis_w)
```

A configuration whose stop threshold sits above its start threshold cannot be
expressed.

### Switch rate

Hysteresis alone cannot bound the switch rate — a surplus swinging wider than
the band crosses both edges. What bounds it is the asymmetry (immediate exit,
deliberate entry) and `max_charge_entries_per_hour`. Reaching that cap is
logged as a warning rather than silently applied: it means the thresholds do not
fit the installation.

### Allocation

The charge total is split by absorbable energy — `(max_soc - soc) x battery_kwh`
— which is the mirror of the discharge side's usable energy above the floor. It
uses the same weighted allocation primitive, so there is one allocator with two
weight functions rather than two allocators. Each device is capped by
`max_charge_power_w`, or by its output limit when that is left at 0.

### Firmware-owned charging

A device the firmware itself put into AC charge is left alone: the EMS neither
writes its mode nor its charge power. This is recognised from telemetry — the
mode and the observed status agree, the battery is at its floor, and the EMS did
not ask for the charge — rather than by reimplementing the firmware's trigger,
which is a threshold the EMS does not own.

Above the floor, a charge nobody is commanding is a leftover from an EMS that
stopped mid-charge, or one started from the vendor app, and the normal acMode
reconcile takes it back.

### Who owns a device's AC direction

Every cycle each device gets a default claim of `ac_output`, whose desired mode
is `acMode = 2`, and the state reconciler writes `acMode` whenever telemetry
disagrees with the winning claim. A device the regulator is charging therefore
needs a claim of its own, or the reconciler writes `acMode = 2` once per loop
against the power command writing `acMode = 1`.

A claim whose `desired_ac_mode` is `None` means *the power command owns this
device's direction*, and the reconciler skips it. Two claims do that:

| Claim | Priority | Raised when |
|---|---:|---|
| `regulator_charge_intent` | 100 | the EMS commanded this device a charge last cycle |
| `firmware_charge_intent` | 50 | the firmware charges an empty pack by itself |

An operator park (150) and maintenance (200) outrank both, so either still takes
a device away mid-charge.

## Deadband

The EMS compares the calculated target with what the device is currently doing,
on the same signed axis as the target: the measured AC input (negative) while it
charges, otherwise the runtime `outputLimit`, falling back to current output
when that is missing or zero. A charging device reports `outputLimit` 0 and no
output while drawing hundreds of watts, so a discharge-only reference would read
it as idle — and "switch this device off" would then compare 0 against 0 and
skip the write that stops the charge.

Small changes below `deadband` are skipped.

## Offline Devices

If a device cannot be read, the EMS may use a cached state, or a zero fallback
when no cached state exists, so the loop can continue safely. Offline devices
are marked offline and skipped for output writes.

Cached telemetry is last-known data. Offline does not automatically mean the
device is currently producing `0W`; it means the EMS does not have fresh device
telemetry for that cycle.
