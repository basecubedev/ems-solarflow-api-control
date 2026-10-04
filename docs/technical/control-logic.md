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
- `electricLevel <= minSoc`, or `socLimit == 2`, or the device reports no
  battery (`packNum == 0`) — it has none that could still deliver

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

It is also left, and not entered, while an AC charge is running. A device with
no PV input and an empty pack reports exactly the idle values above while the
surplus it is meant to [charge from AC](../ac-charging.md) leaves through the
meter, so the idle would return early every cycle, before the charge decision
is reached. The charge decision therefore also runs while the idle holds, with
nothing commanded and the raw meter reading as its load: the same
`entry_confirm_cycles` of the last `entry_window_cycles` observations, the same
freshness rule for the meter and the same hourly entry limit. When it enters a
charge, the next cycle leaves the idle with `reason=ac_charge_surplus`. One
export spike therefore never ends the idle, and the idle is never left in
anticipation of a charge that is then not taken.

A running charge always holds the idle off. Its way back belongs to the charge
decision, which stops the devices and logs why — including
`ac_charge_stopped_stale_meter` — and an idle entered mid-charge would skip it.

The idle parks only devices that may supply the house, and it ends the cycle
before any other write path. A device forbidden to discharge
(`ac_discharge_enabled = false`) is therefore never parked, but it is still
sent the exit it is owed while the idle holds: the exit to idle
(`outputLimit = 0`) to the EMS's own charge it has not left, again once per
resend window, and the one exit to an AC input found at start. Each is logged as
`night_min_soc_idle_charge_exit_write`; nothing else is written to such a
device.

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

## Grid Meter Unavailable Hold

When a grid-meter read fails, the meter client still returns its last good
value. The EMS does not integrate that value: while the meter reports a failed
or stale read, `commanded_total_w` is held where it was and the load filter is
cleared, so the target cannot ramp to `max_total_power` on a reading that no
longer changes. When there is no commanded total to hold — the first cycle after
startup, after night/minSoc idle or after control is switched back on — it is
seeded from what the devices are doing and held there; a held reading is never
integrated, and never counts as a charge-entry observation. The log shows `event=grid_meter_unavailable_holding_target`
when the hold starts and `event=grid_meter_recovered` when a fresh reading
arrives.

That seeded hold is a deliberate change, and it applies with AC charging off as
well: it is not part of the charge feature. Earlier releases integrated the
held reading into the fresh total on that one cycle and wrote the result, so
switching control back on, or leaving the idle, during a meter outage applied
the stale reading once before the hold began. Now the devices keep the
`outputLimit` they already have — or their output where none is set, never
above `max_total_power` — until a fresh reading arrives. The hold cannot wind
up and never commands more than the devices were already set to deliver; what
it gives up is following a load that changed while control was off, until the
meter is back.

Holding is not parking: devices keep their last output while the meter is down.
If a long meter outage must stop discharge, disable the EMS
(`emsctl.py system disable`) until the meter is back.

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

## Devices Without A Battery

Battery presence comes from telemetry `packNum`, and it has three values:
present (`> 0`), absent (an observed `0`), and unknown. A device that never
reports the field stays unknown and is treated exactly as every device was
before this distinction existed. Absence is never inferred from silence, and a
device the EMS has not reached is unknown rather than battery-less.

A device that reports no battery differs in four places:

- **Charge balance.** The PV-first bias asks which device should keep its PV and
  charge instead. A device that cannot charge has no stake in that question, so
  its weight is not biased. Without this it reports SOC 0, reads as the emptiest
  device in the plant, and collects the full penalty permanently.
- **PV-first priority.** It shares the priority a full battery gets, for the same
  reason: PV it is not allowed to export is lost rather than stored. The
  priority is exclusive — those devices are served first and the rest share the
  remainder, so whoever holds it is expected to deliver it. A device the EMS
  cannot write to this cycle — offline, disabled, or on a transport whose write
  gate is closed — will not follow a claim that moves it, so its claim is capped
  at what it is currently delivering. A battery that filled up while being
  commanded keeps its claim; one that was never commanded claims nothing. See
  "Offline Devices" below.
- **Battery top-up and battery balancing.** It receives no discharge share,
  whatever SOC it reports.
- **State reconciliation.** `minSoc`/`socSet` and the winter reserve are not
  applied to it, because there is no battery window to manage.

Its own `max_power`, the system `max_total_power`, ramps, `min_output_limit`,
the write deadband and every write gate apply unchanged.

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
observations to show that — five of seven by default. The output the chargeable
devices feed out at that moment — the standby floor — is added back first: it is
export that stops when they switch direction, not surplus they could take.

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
device is in the charge mode and charging, or in the charge mode with a charge
setpoint while current has yet to follow (the ~2 s settling window), the
battery is at its floor, and the EMS did not ask for the charge — rather than by
reimplementing the firmware's trigger, which is a threshold the EMS does not
own. The state reconciler follows the same judgement: it used to judge the
settling window on its own and wrote `acMode = 2` into a charge the firmware was
starting. A charge mode with no setpoint is not a charge.

"Did not ask for it" is read from two records of the EMS's own charge: the
regulator's last target, and the device's transport's record of the charge it
put on the wire. The second outlives what the first does not — the reset of the
output memory when control is switched back on, and the zero a device is given
while it cannot be reached — so the EMS's own charge is never read back as the
firmware's. It ends only when the device reports that it left the charge after
an exit (out of the AC-input direction, the exit's `inputLimit = 0`, or a charge
at a setpoint the EMS never wrote), not when the device answered the exit: a
device still charging at the EMS's setpoint after an exit it did not carry out is
the EMS's own, and is sent the exit again once per resend window (30 s). The
exit counts as sent once the device answered it, accepted or rejected with an
error, and the one exit to AC input found after a start (below) keeps the same
window whether the device accepted or rejected it; only an exit the transport
could not deliver is due again at once. Over
MQTT a changed target still replaces an exit in flight at once; every MQTT
command carries the direction, so that is an exit too. The shutdown release is
the last command and is sent whatever the window says.

Telemetry alone cannot say who put a device into AC input, so the EMS also asks
whether it can tell. It can when it saw the device out of AC input (or in its
own charge) since the process started, when a claim that outranks the unproven
one holds it there — an operator's AC-input role, a maintenance routine —
and once the device took the exit (below); then a
charge at the floor is the firmware's from the first cycle. A failed read or an
offline spell does not change that, because the transport's record of the EMS's
own charge survives the gap. It cannot tell after a restart: a stop by signal
leaves the EMS's charge running on purpose, so the process before may have left
it drawing from the grid. A device found in AC input — above the floor as at
it, charging or not — therefore stays with the power command until the device
takes the exit (`smartMode = 1`, `acMode = 2`, `inputLimit = 0`, the target as
`outputLimit`): until a report after it shows the device out of the charge, or
charging at a setpoint it did not show when the exit went out. Answered and not
taken, the exit goes out again once per resend window (on MQTT not while a
command is still in flight), three times in all, a count the transport keeps
and never sends past; then, once the last window has passed, the AC input is
its holder's, logged once as `ac_charge_start_exit_unconfirmed`, because a firmware
that puts its protection charge straight back at the same setpoint cannot be
told from a device that ignores the exit. The exit travels on the transport's
own write gate and not behind
`allow_state_reconciliation_writes`: the regular write, the night idle's park
write, and the night idle's exit to a device it does not park alike, whatever
`outputLimit` the device still shows and inside the write deadband too. A
disabled device, and every device while control is switched off, gets it as
its final command (`ac_charge_ended_on_disable`, `charge=unproven_charge`):
the claim stands the state reconciler down until that exit is taken, so
waiting for the device to be enabled again left it with neither. With its write
gate shut — dry run, simulation — the EMS cannot write the exit at all, and the
AC input is its holder's from that cycle on. A device in AC input again after
it took that exit, or still after the third attempt, is someone else's: at the
floor the
firmware's protection charge, respected with nothing written until it leaves
that state; above the
floor an app's or a schedule's, which gets what it got before charging existed
(below). The provenance is process memory and never written to runtime-state
(owner decisions 2026-10-04). A device the EMS could never have charged — the
feature or the device's switch off, or a model without an AC charge path — gets
no such exit: no charge on it can be the EMS's own, so its floor charge is the
firmware's from the first cycle, and above the floor nothing changes.

Above the floor, AC input the EMS can attribute to someone else — an app, a
schedule — is taken back by the normal acMode reconcile where state
reconciliation is allowed, and over MQTT by the next power command, which
carries the direction. The local-API power command does not: it writes such a
device the bare `outputLimit` it ignores, measured against that `outputLimit`
rather than against a charge the write cannot end, so the value is not repeated
every cycle.

### Who owns a device's AC direction

Every cycle each device gets a default claim of `ac_output`, whose desired mode
is `acMode = 2`, and the state reconciler writes `acMode` whenever telemetry
disagrees with the winning claim. A device the regulator is charging therefore
needs a claim of its own, or the reconciler writes `acMode = 2` once per loop
against the power command writing `acMode = 1`.

A claim whose `desired_ac_mode` is `None` means *the power command owns this
device's direction*, and the reconciler skips it. Three claims do that:

| Claim | Priority | Raised when |
|---|---:|---|
| `regulator_charge_intent` | 100 | the EMS commanded this device a charge last cycle |
| `firmware_charge_intent` | 50 | the firmware charges an empty pack by itself |
| `firmware_charge_intent` (`unproven_charge`) | 50 | AC input nobody can attribute, found after a start, above the floor or at it; the power command writes its exit until the device takes it, three attempts at most |

An operator park (150) and maintenance (200) outrank all three, so either still
takes a device away mid-charge.

## Deadband

The EMS compares the calculated target with what the device is currently doing,
on the same signed axis as the target: the measured AC input (negative) while it
charges, otherwise the runtime `outputLimit`, falling back to current output
when that is missing or zero. A charging device reports `outputLimit` 0 and no
output while drawing hundreds of watts, so a discharge-only reference would read
it as idle — and "switch this device off" would then compare 0 against 0 and
skip the write that stops the charge.

The measured AC input is that reference only where a non-negative write can end
the charge: the EMS's own charge, the one exit to an AC input nobody can
attribute, and every command on a transport that carries the modes with it
(MQTT). On the local API a device someone else holds in AC input gets a bare
`outputLimit` that cannot end that charge, so it is compared by its
`outputLimit`, as it was before charging existed, and the same value is not
written every cycle. A device that reports the output direction written and no
charge setpoint has taken the exit; the current it still shows for about two
seconds is the tail of a charge it left, not one to end, so the exit is not sent
a second time.

Small changes below `deadband` are skipped, except the one exit to an AC input
nobody can attribute: a device held in AC input with nothing drawn shows no
change against an idle target, and skipping it would leave the device there
with the state reconciler stood down for that exit.

## Offline Devices

If a device cannot be read, the EMS may use a cached state, or a zero fallback
when no cached state exists, so the loop can continue safely. Offline devices
are marked offline and skipped for output writes.

Cached telemetry is last-known data. Offline does not automatically mean the
device is currently producing `0W`; it means the EMS does not have fresh device
telemetry for that cycle.
