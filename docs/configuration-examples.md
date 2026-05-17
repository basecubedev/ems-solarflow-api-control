# Configuration Examples

These examples are starting points for `config.json`. Keep `dry_run=true` and
`allow_hardware_writes=false` for first tests.

Use example IP addresses and serial numbers as placeholders only.

## Example 1: One Zendure Device Without Home Assistant

Use this for standalone EMS operation without Home Assistant.

```json
{
  "ha": {
    "enabled": false,
    "control_enabled": false,
    "url": "",
    "token": ""
  },

  "system": {
    "enabled": true,
    "dry_run": true,
    "simulation_mode": false,
    "allow_hardware_writes": false,
    "allow_state_reconciliation_writes": false,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true,
    "log_level": "info",
    "max_total_power": 800,
    "max_device_power": 800,
    "deadband": 10,
    "runtime_state_path": "runtime-state.json",
    "min_output_limit": 30,
    "loop_interval": 5,
    "redistribute_clamped_power": true,
    "pv_kwp_weighting": true,
    "pv_charge_balance_enabled": true,
    "pv_charge_balance_deadband_percent": 5,
    "pv_charge_balance_full_bias_percent": 15,
    "pv_charge_balance_strength": 1.0,
    "battery_kwh_weighting": true,
    "soc_reconcile_interval": 10
  },

  "winter": {
    "enabled": false,
    "months": [10, 11, 12, 1, 2],
    "summer_min_soc": 15,
    "winter_min_soc": 40,
    "ramp_step_percent": 5,
    "adjust_hour": 12,
    "ac_charge_power": 200
  },

  "devices": [
    {
      "name": "WR1",
      "ip": "192.168.1.100",
      "sn": "YOUR_SN",
      "smart_mode": 1,
      "max_power": 800,
      "pv_kwp": 1.0,
      "pv_priority_factor": 1.0,
      "battery_kwh": 1.0,
      "min_soc": 15,
      "max_soc": 100
    }
  ],

  "shelly": {
    "ip": "192.168.1.50"
  }
}
```

Change:

- `devices[0].ip`
- `devices[0].sn`
- `shelly.ip`
- `pv_kwp`
- `battery_kwh`

Run:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --preflight --dry-run
```

## Example 2: Two Zendure Devices With Home Assistant

Use this when Home Assistant should receive EMS sensors and optionally provide
runtime helper controls.

```json
{
  "ha": {
    "enabled": true,
    "control_enabled": true,
    "url": "http://homeassistant.local:8123",
    "token": "YOUR_TOKEN_HERE"
  },

  "system": {
    "enabled": true,
    "dry_run": true,
    "simulation_mode": false,
    "allow_hardware_writes": false,
    "allow_state_reconciliation_writes": false,
    "max_total_power": 1600,
    "max_device_power": 800,
    "runtime_state_path": "runtime-state.json",
    "min_output_limit": 30,
    "loop_interval": 5
  },

  "devices": [
    {
      "name": "WR1",
      "ip": "192.168.1.100",
      "sn": "YOUR_SN",
      "smart_mode": 1,
      "max_power": 800,
      "pv_kwp": 1.0,
      "pv_priority_factor": 1.0,
      "battery_kwh": 1.0,
      "min_soc": 15,
      "max_soc": 100
    },
    {
      "name": "WR2",
      "ip": "192.168.1.101",
      "sn": "YOUR_SN",
      "smart_mode": 1,
      "max_power": 800,
      "pv_kwp": 1.0,
      "pv_priority_factor": 1.0,
      "battery_kwh": 1.0,
      "min_soc": 15,
      "max_soc": 100
    }
  ],

  "shelly": {
    "ip": "192.168.1.50"
  }
}
```

Change:

- `ha.url`
- `ha.token`
- both device IPs and serial numbers
- `shelly.ip`
- PV and battery metadata

Run:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Example 3: Safe Dry-Run Setup

This setup allows live telemetry reads and target calculation, but blocks
Zendure hardware writes.

```json
{
  "system": {
    "dry_run": true,
    "allow_hardware_writes": false,
    "allow_state_reconciliation_writes": false
  }
}
```

Run:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Example 4: Live Control Enabled

Use this only after preflight and dry-run output control have been validated.

```json
{
  "system": {
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": false
  }
}
```

This allows `outputLimit` writes only. SOC and mode reconciliation writes remain
disabled.

Start with a bounded run:

```bash
python3 -B ems-solarflow-api-control.py --duration 120
```

## Example 5: State Reconciliation Enabled

Use this only after live output control has been validated.

```json
{
  "system": {
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true
  }
}
```

This allows SOC/mode reconciliation writes too. It should not be the first live
test mode.

## Example 6: Runtime-State Explained

On first start, EMS creates `runtime-state.json` automatically.

Runtime-state contains operator values like:

- system enabled
- runtime max total power
- runtime loop interval
- runtime minimum output limit
- device enabled
- device runtime max power
- device offgrid socket mode
- winter runtime toggle

Do not copy `runtime-state.json` into `config.json`. Do not maintain it as a
second static config.

Reset runtime values from config defaults:

```bash
rm runtime-state.json
python3 -B ems-solarflow-api-control.py --dry-run --once
```

Use the CLI for safe runtime edits:

```bash
python3 emsctl.py status
python3 emsctl.py system min-output-limit 30
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py winter enable
```

## Example 7: Winter Mode Enabled

Winter mode is optional and runs as SOC reconciliation, not as normal output
control.

```json
{
  "winter": {
    "enabled": true,
    "months": [10, 11, 12, 1, 2],
    "summer_min_soc": 15,
    "winter_min_soc": 40,
    "ramp_step_percent": 5,
    "adjust_hour": 12,
    "ac_charge_power": 200
  }
}
```

To allow winter SOC adjustments, state reconciliation writes must also be
enabled after validation:

```json
{
  "system": {
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true
  }
}
```

`inputLimit` is only used in the winter/SOC reconciliation context. Do not use
winter mode as a per-cycle mode write mechanism.

Run first:

```bash
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Example 8: Advanced Output Control Defaults

Most installations should keep these defaults. Change them only when the live
logs show target oscillation, stale telemetry, or excessive write frequency.

```json
{
  "system": {
    "output_control": {
      "load_deadband_w": 5,
      "target_deadband_w": 10,
      "filter_enabled": true,
      "filter_method": "median_ema",
      "median_window": 2,
      "ema_alpha": 0.85,
      "sign_change_fast_response_enabled": true,
      "sign_change_threshold_w": 50,
      "sign_change_filter_reset_factor": 1.0,
      "ramp_enabled": true,
      "ramp_up_w_per_cycle": 300,
      "ramp_down_w_per_cycle": 500,
      "device_ramp_enabled": true,
      "device_ramp_up_w_per_cycle": 250,
      "device_ramp_down_w_per_cycle": 400,
      "large_import_bypass_w": 600,
      "large_export_bypass_w": 500,
      "bypass_ramp_multiplier": 1.5,
      "telemetry_max_age_seconds": 10,
      "stale_telemetry_ramp_factor": 0.5
    }
  }
}
```

Validate after tuning:

```bash
python3 -B ems-solarflow-api-control.py --dry-run --duration 120
```
