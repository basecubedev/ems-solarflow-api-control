# Winter Mode

Winter mode is implemented as SOC/state reconciliation, not output control.

It never changes normal target calculation.

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
winter_summer_reset
dry_run_winter_ac_charge_limit
write_winter_ac_charge_limit
write_winter_ac_charge_limit_error
```

## Home Assistant

Winter status and calculated targets are published to HA. See
[home-assistant.md](home-assistant.md).

Runtime toggle:

```text
input_boolean.ems_solarflow_winter_enabled
```
