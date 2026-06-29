# Configuration Examples

These examples are starting points for `config.json` or Docker
`config/config.json`. The template profile is intended for normal standalone
live control after real local values are configured and installation limits are
reviewed.

If required placeholders are still present, EMS forces safe mode: control
disabled, dry-run enabled, and hardware writes blocked. Set `dry_run=true`
manually when you want an explicit no-write validation run after configuration.

Use example IP addresses and serial numbers as placeholders only. Before
unattended operation, enter real grid meter and Zendure values, review power and
SOC limits, confirm battery and PV metadata, run
[first-run-checklist.md](first-run-checklist.md), and monitor the first live
run.

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
    "runtime_state_path": "data/runtime-state.json",
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
- `grid_meter.type`, `grid_meter.ip`, and Shelly `grid_meter.channels` if needed
- `pv_kwp`
- `battery_kwh`
- `dashboard.port` or `dashboard.database_path` only when needed
- `energy_savings.price_per_kwh`
- `energy_savings.currency`
- `energy_savings.timezone` only when you do not want Europe/Berlin calendar days

Docker check:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Native Python validation:

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
    "runtime_state_path": "data/runtime-state.json",
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
- `grid_meter.type`, `grid_meter.ip`, and Shelly `grid_meter.channels` if needed
- PV and battery metadata

Docker check:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Native Python validation:

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

Docker check:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Native Python validation:

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

This is not the normal template profile. It blocks both normal `outputLimit`
writes and state reconciliation writes until you change the flags back.

Docker check:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Native Python validation:

```bash
python3 -B ems-solarflow-api-control.py --preflight --dry-run
python3 -B ems-solarflow-api-control.py --dry-run --once
```

## Example 5: Default Live Control Profile

This is the normal template policy for standalone operation after required
placeholders are replaced, real local values are configured, and installation
limits are reviewed.

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
reconciliation. Runtime AC mode intent is evaluated during the control loop,
but startup `acMode` reconciliation is conservative when telemetry is unknown.
`ac_output` maps to `acMode=2`; `ac_input` maps to `acMode=1` and blocks normal
output regulation. An explicit runtime `ac-mode output` command can still write
`acMode=2` when the reported mode is `0`.

Manual runtime AC charging uses the same loop-owned reconciliation path:

```bash
python3 emsctl.py device WR1 ac-charge-power 200
python3 emsctl.py device WR1 ac-mode input
python3 emsctl.py device WR1 ac-mode output
```

`ac-charge-power` stores `ac_charge_power_w` in runtime-state only. The
controller applies it as `inputLimit` on the next EMS loop while the role is
`ac_input`, and ignores it for hardware writes while the role is `ac_output`.

The defaults are intended to expose the main regulation features with minimal
setup. They are not a universal safety profile; review device limits, SOC
limits, grid meter readings, and installation-specific constraints for each
installation.

## Optional Battery Full-Charge Assist

The template keeps EMS full-charge assist disabled by default:

```json
{
  "battery_full_charge_assist": {
    "enabled": false,
    "interval_days": 28,
    "assist_window_days": 7,
    "assist_start_soc": 80,
    "force_time": "14:00",
    "ac_charge_power": 200,
    "enable_ac_charge_mode": true,
    "state_database_path": "data/ems_state.sqlite"
  }
}
```

When enabled, EMS temporarily requests `socSet=1000` and waits for firmware
`socLimit == 1`. AC-assisted charging reuses the runtime AC intent path; EMS
does not write firmware calibration properties.

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

## Example 7: Zendure SmartMeter D0 via MQTT

Use Zendure SmartMeter D0 (MQTT) when the D0 publishes signed `totalPower` to
an existing broker. EMS subscribes as a client; it does not run a broker and
does not publish or write values back to the meter.

Zendure SmartMeter D0 `totalPower` example:

```json
{
  "grid_meter": {
    "type": "zendure_smartmeter_d0",
    "mqtt": {
      "host": "192.168.1.10",
      "port": 1883,
      "username": "YOUR_MQTT_USER",
      "password": "YOUR_MQTT_PASSWORD",
      "topic": "Zendure/sensor/YOUR_D0_SERIAL/totalPower",
      "payload_format": "number",
      "max_age_seconds": 15
    }
  }
}
```

Known D0 samples use positive values for grid import and negative values for
grid export. This integration has unit-test coverage and mocked MQTT coverage;
live D0 hardware validation depends on external tester feedback.

Generic JSON MQTT payload example:

```json
{
  "grid_meter": {
    "type": "mqtt",
    "mqtt": {
      "host": "192.168.1.10",
      "port": 1883,
      "topic": "meter/grid",
      "payload_format": "json",
      "value_path": "power.total",
      "max_age_seconds": 15
    }
  }
}
```

## Example 8: Shelly Clamp/Phase Selection

Shelly uses `em:0.total_act_power` by default and falls back to summing
`em1:0`, `em1:1`, and `em1:2` `act_power` values when the aggregate is not
available.

Use `channels` when only selected clamps should be summed. A single item list
such as `["c"]` is valid and reads only clamp C:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50",
    "channels": ["c"]
  }
}
```

Multiple items such as `["a", "c"]` sum only those selected clamps:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50",
    "channels": ["a", "c"]
  }
}
```

`channels` entries may be `a`, `b`, `c`, `em1:0`, `em1:1`, or `em1:2`. The
values `total` and `sum` are not valid inside `channels`.

## Example 8b: Shelly 3EM Gen1 Grid Meter

The older non-Pro Shelly 3EM Gen1 meter uses the classic `/status` endpoint
instead of `/rpc/Shelly.GetStatus`. Use the `shelly_3em_gen1` type for it:

```text
shelly            = Shelly Pro / Gen2 / Gen3 via /rpc/Shelly.GetStatus
shelly_3em_gen1   = Shelly 3EM Gen1 via /status
```

A Shelly 3EM Gen1 reads the top-level `total_power` by default, falling back to
summing all three `emeters[].power` values:

```json
{
  "grid_meter": {
    "type": "shelly_3em_gen1",
    "ip": "192.168.1.50"
  }
}
```

Use `channels` only when you intentionally want to read a subset of
phases/clamps. Valid entries are `a`, `b`, `c`, `0`, `1`, `2`, `emeter:0`,
`emeter:1`, and `emeter:2`. Phase letters are normalized to lowercase, and when
`channels` is configured `total_power` is ignored:

```json
{
  "grid_meter": {
    "type": "shelly_3em_gen1",
    "ip": "192.168.1.50",
    "channels": ["a", "c"]
  }
}
```

Clamp direction must match EMS expectations: `positive = grid import`,
`negative = grid export`. The sign is not inverted automatically.

## Example 9: Runtime-State Explained

On first start, EMS creates the configured runtime-state file automatically.
New generated configs use `data/runtime-state.json`.

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

Runtime state is temporary runtime data. Do not copy it into `config.json` or
maintain it as a second static config.

Reset runtime values from config defaults:

```bash
rm data/runtime-state.json
python3 -B ems-solarflow-api-control.py --dry-run --once
```

Older root-level `runtime-state.json` files from previous setups are no longer
required after switching to `data/runtime-state.json` and may be removed
manually.

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

## Example 10: Winter Mode Enabled

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

## Example 11: Advanced Output Control Defaults

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
