# Configuration

The EMS uses JSON configuration.

```text
config.template.json
= versioned reference configuration

config.json
= local installation config, ignored by Git
```

Create a local config:

```bash
cp config.template.json config.json
```

Then edit local values:

- Zendure device IP addresses
- Zendure serial numbers
- Shelly IP address
- Home Assistant URL and token if used
- safety flags
- power limits
- SOC limits

## System

Important `system` settings:

| Option | Purpose |
|---|---|
| `enabled` | Default EMS enabled state |
| `dry_run` | Calculates targets but blocks hardware writes |
| `simulation_mode` | Runs without real hardware |
| `allow_hardware_writes` | Allows Zendure `/properties/write` calls |
| `allow_state_reconciliation_writes` | Allows SOC and mode reconciliation writes |
| `reconcile_ac_mode_on_start` | Allows one safe startup `acMode=2` initialization |
| `reconcile_smart_mode` | Allows cyclic `smartMode` reconciliation |
| `runtime_state_path` | Path to mutable runtime state |
| `min_output_limit` | Default runtime minimum `outputLimit` while EMS is enabled |
| `loop_interval` | Control loop interval in seconds |
| `soc_reconcile_interval` | Interval in EMS cycles for SOC/mode checks |

Recommended safe development flags:

```json
{
  "dry_run": true,
  "allow_hardware_writes": false,
  "allow_state_reconciliation_writes": false
}
```

## Output Control

`system.output_control` stabilizes fast control loops.

```json
{
  "load_deadband_w": 5,
  "target_deadband_w": 10,
  "filter_enabled": true,
  "filter_method": "median_ema",
  "median_window": 3,
  "ema_alpha": 0.65,
  "ramp_enabled": true,
  "ramp_up_w_per_cycle": 300,
  "ramp_down_w_per_cycle": 500,
  "device_ramp_enabled": true,
  "device_ramp_up_w_per_cycle": 250,
  "device_ramp_down_w_per_cycle": 400,
  "write_cooldown_seconds": 2,
  "large_import_bypass_w": 600,
  "large_export_bypass_w": 500,
  "bypass_ramp_multiplier": 1.5,
  "telemetry_max_age_seconds": 10,
  "stale_telemetry_ramp_factor": 0.5
}
```

The controller uses an internal commanded target instead of reacting directly
to every telemetry frame. This avoids large target swings when the loop interval
is short and inverter telemetry lags behind the last command.

## Devices

Each Zendure device entry defines static installation data.

```json
{
  "name": "WR1",
  "ip": "192.168.100.77",
  "sn": "YOUR_SN",
  "smart_mode": 1,
  "max_power": 800,
  "pv_kwp": 2.0,
  "pv_priority_factor": 1.0,
  "battery_kwh": 1.92,
  "min_soc": 15,
  "max_soc": 100
}
```

Static config is not the operator UI. Mutable runtime choices such as enabled,
max power, and offgrid socket intent belong in `runtime-state.json`.

## Winter

Winter mode is configured at top level:

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

More detail: [winter-mode.md](winter-mode.md).

