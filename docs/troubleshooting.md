# Troubleshooting

The EMS uses structured logs:

```text
event=<name> key=value key=value
```

Use these logs to validate behavior before enabling live writes.

## Basic Checks

Compile:

```bash
python3 -m py_compile ems-solarflow-api-control.py emsctl.py
```

Run self-tests:

```bash
python3 -B ems-solarflow-api-control.py --self-test
```

Run simulation:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Check required events:

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

## No Power Changes

Check safety flags:

```text
dry_run
simulation_mode
allow_hardware_writes
allow_state_reconciliation_writes
```

Expected dry-run event:

```text
event=dry_run_output_limit
```

## Device Offline

Look for:

```text
offline_skip_write
```

The EMS suppresses writes to devices without fresh telemetry. It may use cached
state for calculation, but it does not write to an offline device.

## Unexpected SOC Or Mode Changes

Check whether state reconciliation writes are enabled:

```json
{
  "allow_state_reconciliation_writes": true
}
```

Relevant events:

```text
dry_run_soc_limits
write_soc_limits
dry_run_device_modes
write_device_modes
dry_run_runtime_device_state_write
write_runtime_device_state
```

## Winter Mode

Relevant events:

```text
winter_mode_state
winter_ramp
winter_summer_reset
dry_run_winter_ac_charge_limit
write_winter_ac_charge_limit
```

If no winter event appears, check:

- `winter.enabled`
- current month
- `soc_reconcile_interval`
- current hour versus `winter.adjust_hour`

## Home Assistant Entities Missing

Home Assistant entities are created by REST state writes. They appear after the
EMS has published at least once.

Check:

- `ha.enabled=true`
- `ha.control_enabled=true` if helpers should sync
- valid HA URL and token
- not running with `--no-ha`
- not running simulation or replay

