# Winter Mode

Winter mode is implemented as SOC/state reconciliation, not output control.

It never changes normal target calculation.

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
    "ramp_step_percent": 5,
    "adjust_hour": 12,
    "ac_charge_power": 200
  }
}
```

## Behavior

When winter is active, the EMS raises `minSoc` gradually toward
`winter_min_soc`.

It adjusts once daily during this window:

```text
adjust_hour <= now.hour < adjust_hour + 1
```

If the process starts after the window, the next adjustment waits until the next
day.

Outside configured winter months, the EMS resets `minSoc` to `summer_min_soc`.

## Ramp Rule

```text
if not winter_active:
    return summer_min_soc

if current_soc >= winter_min_soc:
    return winter_min_soc

if current_soc > effective_min_soc + ramp_step:
    return min(current_soc, winter_min_soc)

return min(effective_min_soc + ramp_step, winter_min_soc)
```

`effective_min_soc` is the remembered target, or the device's own `minSoc` when
that is lower -- a target the battery never reached is not stepped up again. A
device that reports no `minSoc` keeps the remembered target. After a restart
nothing is remembered: outside the adjustment hour the device's `minSoc` is kept
within the configured floor (the device's `min_soc`, or `summer_min_soc` when
that is 0) and `winter_min_soc`, and that value is remembered; inside it the
adjustment steps up from the device's `minSoc`, or from the device's `min_soc`
when it reports none.

## A Raise Waits for the Battery

Winter mode writes a raise of `minSoc` only once the battery's SoC has reached
it. Until then the device keeps its current `minSoc`, and the raise follows when
PV has charged the battery that far. This applies to every raise winter mode
writes: the daily adjustment, a target remembered while winter mode was switched
off or after a failed write, the configured floor after a restart, and the
summer reset of a device found below `summer_min_soc`. A device that reports no
`minSoc` -- a missing value reads as 0 -- gets no raise the battery does not
already hold, and without a usable SoC reading nothing rises.

On 2026-10-03 an adjustment raised `minSoc` from 25 to 30 at a SoC of 25, and
both inverters charged from the grid at their full AC power, 1.2 to 1.4 kW each,
for five minutes until they had reached it; the 200 W `inputLimit` below does
not limit that while the inverter is in output mode. In the same field data a
`minSoc` that had stood two or three points above the SoC overnight caused no
grid charging, so the threshold lies somewhere in between; the rule above does
not depend on it.

The rule only holds raises back. It does not bring a `minSoc` that is already
above the SoC down to it. Winter mode still lowers `minSoc` where it did before:
to the remembered target or `winter_min_soc` when a device holds more, and to
`summer_min_soc` outside the winter months.

## AC Charge Limit

During a winter adjustment, the EMS may write a conservative AC input limit:

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

## Logs

```text
winter_mode_state
winter_ramp
winter_raise_waits_for_battery
winter_summer_reset
dry_run_winter_ac_charge_limit
write_winter_ac_charge_limit
write_winter_ac_charge_limit_error
```

`winter_mode_state` is logged at `info` only when the active state changes or an
adjustment is due; otherwise it is a `debug` trace, so the per-reconcile state
no longer floods the default log. Actual writes and ramps stay at `info`.
`winter_raise_waits_for_battery` is `info` at the adjustment and when a raise
starts waiting, and `debug` while it waits. `winter_summer_reset` is `debug`
while its raise waits for the battery, except the first one that drops a target
remembered in winter, which is `info`; otherwise it is `info`. Set
`system.log_level=debug` to see every reconcile interval.

## Home Assistant

Winter status and calculated targets are published to HA. See
[home-assistant.md](home-assistant.md).

Runtime toggle:

```text
input_boolean.ems_solarflow_winter_enabled
```
