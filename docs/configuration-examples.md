# Configuration Examples

These examples are starting points for `config.json`. The primary example
matches the template default: standalone-first, Home Assistant disabled, live
Zendure output control enabled, and required state reconciliation enabled.

Set `dry_run=true` manually when you want a no-write validation run.

Use example IP addresses and serial numbers as placeholders only. Before
unattended operation, enter real grid meter and Zendure values, review power and SOC
limits, confirm battery and PV metadata, and monitor the first bounded live run.

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
    "dry_run": false,
    "simulation_mode": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true,
    "log_level": "info",
    "max_total_power": 800,
    "max_device_power": 800,
    "deadband": 10,
    "runtime_state_path": "runtime-state.json",
    "min_output_limit": 35,
    "loop_interval": 3,
    "redistribute_clamped_power": true,
    "pv_kwp_weighting": true,
    "pv_charge_balance_enabled": true,
    "pv_charge_balance_deadband_percent": 1,
    "pv_charge_balance_full_bias_percent": 15,
    "pv_charge_balance_strength": 0.7,
    "battery_kwh_weighting": true,
    "soc_reconcile_interval": 10
  },

  "dashboard": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8080,
    "database_path": "data/ems_dashboard.sqlite",
    "history_hours": 48,
    "write_interval_seconds": 5
  },

  "energy_savings": {
    "enabled": true,
    "price_per_kwh": 0.0,
    "currency": "EUR",
    "max_sample_delta_seconds": 20,
    "timezone": "Europe/Berlin"
  },

  "winter": {
    "enabled": false,
    "months": [10, 11, 12, 1, 2, 3],
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

  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50"
  }
}
```

Change:

- `devices[0].ip`
- `devices[0].sn`
- `grid_meter.type` and `grid_meter.ip`
- `pv_kwp`
- `battery_kwh`
- `dashboard.port` or `dashboard.database_path` only when needed
- `energy_savings.price_per_kwh`
- `energy_savings.currency`
- `energy_savings.timezone` only when you do not want Europe/Berlin calendar days

Run:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --preflight
```

## Example 2: Two Zendure Devices With Home Assistant

Use this when Home Assistant should receive EMS sensors and optionally provide
runtime helper controls. The values below intentionally override some
single-device template values for a two-inverter installation.

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
    "dry_run": false,
    "simulation_mode": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true,
    "max_total_power": 1600,
    "max_device_power": 800,
    "runtime_state_path": "runtime-state.json",
    "min_output_limit": 35,
    "loop_interval": 3
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

  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50"
  }
}
```

Change:

- `ha.url`
- `ha.token`
- both device IPs and serial numbers
- `grid_meter.type` and `grid_meter.ip`
- PV and battery metadata

Run:

```bash
python3 -B ems-solarflow-api-control.py --preflight
python3 -B ems-solarflow-api-control.py --duration 120
```

## Example 3: Safe Dry-Run Setup

This setup allows live telemetry reads and target calculation, but blocks
Zendure hardware writes.

```json
{
  "system": {
    "dry_run": true,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true
  }
}
```

Run:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Example 4: Conservative Manual Safety Startup

Use this optional variant when you want to block all Zendure writes until after
manual validation.

```json
{
  "system": {
    "dry_run": true,
    "allow_hardware_writes": false,
    "allow_state_reconciliation_writes": false
  }
}
```

This is not the template default. It blocks both normal `outputLimit` writes and
state reconciliation writes until you change the flags back.

Validate with:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Example 5: Default Live Control Profile

This is the normal default policy for live standalone operation after local
configuration.

```json
{
  "system": {
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true
  }
}
```

This allows normal `outputLimit` writes and required regulation/state
reconciliation. `reconcile_ac_mode_on_start` is a startup helper, not permanent
cyclic forcing of `acMode`.

The defaults are intended to expose the main regulation features with minimal
setup. They are not a universal safety profile; review device limits, SOC
limits, grid meter readings, and installation-specific constraints for each
installation.

## Example 6: Tasmota HTTP Grid Meter

Use Tasmota HTTP when a Tasmota smart meter reader exposes current power in
the `Status 10` JSON response. `power_path` must match your local Tasmota JSON
keys.

SML-style payload path:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "ip": "192.168.1.70",
    "power_path": "StatusSNS.SML.Power_curr"
  }
}
```

OBIS-style key path:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "url": "http://192.168.1.70/cm?cmnd=Status%2010",
    "power_path": "StatusSNS.SM.16_7_0"
  }
}
```

Positive power means grid import. Negative power means export/feed-in when the
meter reports signed values that way.

## Example 7: Runtime-State Explained

On first start, EMS creates `runtime-state.json` automatically.

Runtime-state contains operator values like:

- system enabled
- runtime max total power
- runtime loop interval
- runtime minimum output limit
- device enabled
- device runtime max power
- device offgrid socket mode
- device runtime PV priority factor
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
python3 emsctl.py device WR1 pv-priority-factor 1.3
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py winter enable
```

`pv-priority-factor` changes PV-first weighting only. It does not create
additional PV power and does not override device power limits.

## Example 8: Winter Mode Enabled

Winter mode is optional and runs as SOC reconciliation, not as normal output
control.

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

Winter SOC adjustments use the same state reconciliation gates that are enabled
in the default live profile:

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

## Example 9: Advanced Output Control Defaults

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
      "ramp_up_w_per_cycle": 500,
      "ramp_down_w_per_cycle": 500,
      "device_ramp_enabled": true,
      "device_ramp_up_w_per_cycle": 400,
      "device_ramp_down_w_per_cycle": 400,
      "large_import_bypass_w": 600,
      "large_export_bypass_w": 600,
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
