# Winter Mode

Winter mode raises the battery reserve (`minSoc`) during the winter months. How
a device's reserve rises is decided by its **winter policy**: each device type
-- PV with a battery, a battery without PV of its own, PV only -- has a default
policy, and a single device can override it. The policies are declared once, in
`ems/winter_policies.py`; the config catalog, the Admin console and the EMS all
read that registry, so a new energy plan is one more registry entry. What
winter mode decides per device -- the daily step, the follow and the export
hold -- lives in `ems/winter_reserve.py`, one state object per device.

Winter mode works through SoC/state reconciliation, plus one rule in target
calculation for the `solar_morning_step` policy: a device whose battery is
below its `minSoc` while PV flows exports at most `min_output_limit` for a
while (see [PV Charges the Battery First](#pv-charges-the-battery-first)).

The template default enables state reconciliation writes as part of normal
standalone regulation. Winter still only writes during its reconciliation
context when winter mode is enabled and active. Review SOC limits and battery
capacity for the installation before unattended winter operation.

`winter.enabled` can be toggled at runtime through `runtime-state.json`,
`emsctl.py`, or the optional Home Assistant helper. The technical winter
settings below remain static config.

## Configuration

```json
{
  "winter": {
    "enabled": true,
    "months": [10, 11, 12, 1, 2, 3],
    "summer_min_soc": 15,
    "winter_min_soc": 40,
    "ramp_step_percent": 3,
    "adjust_hour": 12,
    "ac_charge_power": 200,
    "policies": {
      "pv_battery": "solar_morning_step",
      "battery_only": "noon_step"
    }
  },
  "devices": [
    {"name": "WR1", "pv_kwp": 1.0, "winter_policy": "auto"},
    {"name": "AKKU", "pv_kwp": 0, "winter_policy": "auto"}
  ]
}
```

`ramp_step_percent` is the daily step, from 1 to 3; older configs carry 5, which
steps by 3 (see [Why Three Points](#why-three-points)). `adjust_hour` is the
local hour in which the `noon_step` policy's step is due; in Docker that is the
container's [time zone](docker.md#time-zone).

## Device Types and Policies

| Device type | Recognised by | Default policy | Policies it may use |
|---|---|---|---|
| PV with battery (`pv_battery`) | a battery in telemetry and `pv_kwp` other than 0 | `solar_morning_step` | `solar_morning_step`, `noon_step`, `none` |
| Battery only (`battery_only`) | a battery in telemetry and `pv_kwp: 0` | `noon_step` | `noon_step`, `none` |
| PV only (`pv_only`) | no battery in telemetry (`packNum: 0`) | `none` | `none` |

A missing PV array is something only the configuration can state: a dark or
shaded array reads exactly like none, so `pv_kwp: 0` is what makes a device
battery-only. Earlier releases read `pv_kwp: 0` as "not set", so a
battery-only device that has ever reported PV (50 W or more) takes no noon step
and logs `winter_pv_kwp_zero_but_pv_reported` once a day: a PV device saved
with `pv_kwp: 0` gets no step rather than one that may end in a grid charge.
Set its real PV size to give it the morning step. A device of that kind that
has not reported PV since the update can still take noon steps on dark days.

`winter.policies` sets the default for PV with battery and for battery only; a
PV-only device has no battery to plan for. `devices[].winter_policy` overrides
the default for one device; `auto` keeps it. A name that is unknown or does not
fit the device's type falls back to the type's default, and the EMS logs
`winter_policy_invalid` once per device. Setup and Maintenance refuse such a
name before it is applied; they judge the type from `pv_kwp` alone, because
battery presence is only known at runtime. MQTT control devices are
output-only: they carry the field but get no winter plan.

| Policy | Daily step | How far the step may lead the SoC | PV charges the battery first |
|---|---|---|---|
| `solar_morning_step` | at the day's first PV of 50 W or more, once the battery is at most one point below its `minSoc`: SoC + step | 3 points | yes, for a while |
| `noon_step` | in the hour `adjust_hour`: `minSoc` + step | up to two steps | no |
| `none` | none; the device keeps its configured `min_soc`, also outside winter | -- | no |

Both stepping policies cap `minSoc` at `winter_min_soc` -- a `minSoc` set above
it, in the app or by a higher ceiling, comes down to it -- and after the day's
step `minSoc` follows the SoC the battery reaches, again up to
`winter_min_soc`.

## Behavior

Outside the configured winter months, the EMS resets `minSoc` to
`summer_min_soc` for every device whose policy steps.

### `solar_morning_step`

1. **Step.** At the first reconcile of a day with at least 50 W of PV and no
   step yet, once the battery is at most one point below its `minSoc`,
   `minSoc` is raised to the SoC plus the step, capped at `winter_min_soc`:

   ```text
   minSoc = min(SoC + ramp_step_percent, winter_min_soc)
   ```

   A battery two or more points below its `minSoc` is first refilled from PV
   by the export hold; the step follows once it is at most one point below.
2. **Follow.** For the rest of that day, when PV has charged the battery above
   its `minSoc`, `minSoc` follows the SoC. It never follows before the day's
   step, and never without PV, so a battery charged in the evening keeps its
   charge available for the night.

There is one step per calendar day: PV that drops to nothing under cloud takes
no second step, and a few watts of noise at night are no morning. A device that
has seen no PV by `adjust_hour` logs `winter_step_waits_for_pv` once that day:
a battery without PV of its own needs `pv_kwp: 0`, or it waits for a morning
that never comes.

### `noon_step`

A battery without PV has no source to charge from but the grid. In the hour
`adjust_hour`, once a day, `minSoc` rises by `ramp_step_percent` above its
current value, up to `winter_min_soc`; later that day it follows the SoC when
the battery reaches more. This daily step is **not** limited to three points
above the SoC: once `minSoc` leads the SoC by about five points, the firmware
charges the battery from the grid (see [Why Three Points](#why-three-points)),
and the policy accepts that risk deliberately. It steps only while the battery
is at most one step below its `minSoc`, so `minSoc` leads the SoC by at most two
steps; a pack that does not charge stops the ramp instead of being pushed to
`winter_min_soc`, and a battery too far below at that hour takes no step that
day. The step keeps its lead until the device reports it, so
telemetry that still shows the old `minSoc` does not cut it back.

### State after a restart

A live EMS keeps each device's step and target in the core state database
(`battery_full_charge_assist.state_database_path`, table
`winter_reserve_state`), so a restart neither repeats a day's step nor loses
it. Only what the device can receive is stored: a dry run, or a configuration
without state reconciliation writes, stores nothing, and a step the database
cannot keep is not taken that day, so a restart cannot take it twice. A stored
target is used
only while its step is at most seven days old, and within the configured floor
and `winter_min_soc`; leaving winter clears it.

Without a current record -- the day the update lands, a save that failed, a
simulation without the database -- the device's `minSoc` is adopted within the
configured floor (the device's `min_soc`, or `summer_min_soc` when that is 0)
and `winter_min_soc`, and a device first seen in daylight or after
`adjust_hour` takes no step that day, because one may already have been taken.
A start in heavy overcast before `adjust_hour` cannot tell, and may take a second
step that day; it stays within three points of the SoC. A step or a follow the
database cannot keep is not taken.

## PV Charges the Battery First

While winter is active and state reconciliation writes are allowed, a device on
the `solar_morning_step` policy that

- has a battery and a SoC reading,
- reports a `minSoc` at least two points above its SoC,
- receives PV and is online, and
- is within three hours of the day's first PV after a dark reading, or of the
  day's step

exports at most `min_output_limit` until the battery holds its `minSoc` again.
Target calculation treats it like a device that cannot export, so the rest of
its PV charges the battery; another device, or the grid, covers the house. The
control explanation names it with the capability reason `winter_solar_charge`,
and the controller does not count it as export capacity, so the commanded total
does not wind up while it is held.

The two-point entry keeps a one-point dip after `minSoc` followed the SoC from
switching the output on and off. The three-hour windows refill a battery the
night left below its `minSoc` and the gap the step opened, and they bound the
cost of a pack that cannot take the charge -- a cold pack, a BMS limit: its PV
is curtailed for no longer than that, and a restart in daylight opens no new
window. A device that another rule already keeps
from exporting, such as an `ac_input` runtime role, keeps that reason. MQTT
control devices and PV-only devices are never held; PV-only devices keep
exporting their full PV.

Without this rule the firmware does not refill the gap: measured on 2026-10-05
on two SolarFlow 800 Pro 2 at `minSoc` 23/24 % and SoC 20/21 %, the inverters
stopped discharging, but once the house drew more, the EMS raised `outputLimit`
up to the PV and the batteries received nothing. The firmware offers no switch
for this: `passMode` writes were accepted and ignored.

## Why Three Points

Every raise winter mode writes over a device's reported `minSoc` -- a target
remembered while winter mode was switched off or after a failed write, the
configured floor after a restart, the summer reset of a device found below
`summer_min_soc`, and the morning step -- waits until it leads the SoC by at
most three points; only the `noon_step` policy's own step may lead further. A
raise is never written as a part of itself: cut to three above the SoC and
written again each reconcile, it would climb with the SoC all day. A device that
reports no `minSoc` -- a missing value reads as 0 -- gets no raise the battery
does not already hold, and without a usable SoC reading nothing rises.

On 2026-10-03 an adjustment raised `minSoc` from 25 to 30 at a SoC of 25, and
both inverters charged from the grid at their full AC power, 1.2 to 1.4 kW each,
for five minutes until they had reached it; the 200 W `inputLimit` below does
not limit that while the inverter is in output mode. On 2026-10-05 `minSoc`
three and four points above the SoC caused no grid charging over five minutes;
six points above it started about 1 kW of grid charging within five seconds.
Once started, the firmware kept charging from the grid after `minSoc` was
lowered to four and then one point above the SoC, and stopped only when
`minSoc` was no longer above the SoC. The threshold lies somewhere between four
and five points; three keeps a margin. All observations are from one
installation and one firmware; a longer gap or another firmware has not been
measured.

The rule only limits raises. It does not bring a `minSoc` that is already above
the SoC down to it. Winter mode still lowers `minSoc` to `winter_min_soc` when a
device holds more, and to `summer_min_soc` outside the winter months.

## AC Charge Limit

When a daily step is taken, the EMS may write a conservative AC input limit:

```json
{"inputLimit": 200}
```

This write is only in the winter reconciliation context. It never includes:

```text
acMode
smartMode
outputLimit
```

## Safety

Winter writes require the same state reconciliation gates as SOC/mode writes:

```text
dry_run=false
simulation_mode=false
not replay
allow_hardware_writes=true
allow_state_reconciliation_writes=true
```

The export hold only lowers output; it never writes `minSoc`, `acMode` or
`inputLimit`.

## Logs

```text
winter_mode_state
winter_policy_invalid
winter_step
winter_step_waits_for_pv
winter_pv_kwp_zero_but_pv_reported
winter_follow_soc
winter_raise_waits_for_battery
winter_summer_reset
dry_run_winter_ac_charge_limit
write_winter_ac_charge_limit
write_winter_ac_charge_limit_error
```

`winter_mode_state` is logged at `info` only when the active state changes;
otherwise it is a `debug` trace, so the per-reconcile state does not flood the
default log. `winter_step` and `winter_follow_soc` name the device's policy and
stay at `info`, as do actual writes and `winter_step_waits_for_pv` (once a
day). `winter_policy_invalid` is a `warning` once per device, and
`winter_pv_kwp_zero_but_pv_reported` a `warning` once a day.
`winter_raise_waits_for_battery` reports a raise that waits for the battery:
`info` at the step and when it starts, `debug` while it lasts.
`winter_summer_reset` is `debug` while its raise waits, except the first one
that drops a target remembered in winter, which is `info`; otherwise it is
`info`. Set `system.log_level=debug` to see every reconcile interval.

## Home Assistant

Winter status and calculated targets are published to HA. See
[home-assistant.md](home-assistant.md).

Runtime toggle:

```text
input_boolean.ems_solarflow_winter_enabled
```
