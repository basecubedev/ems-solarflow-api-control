# Runtime State

`data/runtime-state.json` stores temporary mutable operator state in new
generated configs.

```text
config.json
= static installation, safety flags, IPs, serial numbers, technical defaults

data/runtime-state.json
= temporary local runtime/operator data

data/ems_state.sqlite
= durable core EMS lifecycle state such as battery full-charge assist tracking
```

The EMS creates the runtime state file on first start from config defaults. The
file is ignored by Git and is recreated automatically if missing. Older
root-level `runtime-state.json` files from previous setups are no longer
required after switching to `data/runtime-state.json` and may be removed
manually.

Battery full-charge assist does not store lifecycle state in
`runtime-state.json`. It uses the core SQLite database configured by
`battery_full_charge_assist.state_database_path`, so it can recover active
assist and restore state after restart without overwriting operator runtime
intent.

Example:

```json
{
  "system": {
    "enabled": true,
    "max_total_power": 800,
    "loop_interval": 5,
    "min_output_limit": 35
  },
  "ha": {
    "enabled": false,
    "control_enabled": false
  },
  "winter": {
    "enabled": true
  },
  "devices": {
    "WR1": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0
    },
    "WR2": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0
    }
  }
}
```

## System Fields

| Field | Meaning |
|---|---|
| `enabled` | Enables or disables EMS output writes |
| `max_total_power` | Runtime total power limit |
| `loop_interval` | Runtime loop interval |
| `min_output_limit` | Runtime guard against very low enabled `outputLimit` writes |

`min_output_limit=35` is the current template default and is useful on
installations where `outputLimit=0` behaves like a stop, idle, or sleep state.
The guard is applied before deadband handling and only while EMS control is
enabled.

The same value is also used as the standby/wakeup `outputLimit` for strict
night/minSoc idle. When all active online devices report exactly no PV, no
charge/discharge flow, no home output, and a blocked battery at `minSoc`, the
EMS parks each device at `min_output_limit` once if needed. It then suppresses
further `outputLimit` writes until PV telemetry becomes positive again.

## Device Fields

| Field | Meaning |
|---|---|
| `enabled` | Skip writes for this device when false |
| `max_power` | Runtime per-device power limit |
| `offgrid_socket_mode` | Operator intent for Zendure offgrid socket mode |
| `pv_priority_factor` | Runtime PV-first allocation weight override |

`pv_priority_factor` defaults from `config.json` and can be changed at runtime:

```bash
python3 emsctl.py device WR1 pv-priority-factor 1.3
python3 emsctl.py device WR2 pv-priority-factor 0.7
```

Values above `1.0` increase the device's PV-first share, values below `1.0`
reduce it. The setting only changes allocation weight. It does not create
additional PV power and does not override real PV availability, device state,
SOC logic, or configured power limits.

Offgrid mapping:

```text
off      -> gridOffMode=2
eco      -> gridOffMode=1
standard -> gridOffMode=0
```

The CLI and Home Assistant only change the intent. The EMS is the only
component that writes `gridOffMode` to hardware.

## HA Fields

| Field | Meaning |
|---|---|
| `enabled` | Enables or disables HA publishing in the EMS loop |
| `control_enabled` | Enables or disables HA helper sync |

These fields can only affect HA when HA is statically configured and the EMS has
an HA client. They do not edit HA URL or token.

The template default is standalone operation, so both HA runtime fields start as
`false` unless Home Assistant is enabled in `config.json`.

## Winter Fields

| Field | Meaning |
|---|---|
| `enabled` | Enables or disables winter mode at runtime |

Winter months, SOC limits, ramp step, adjustment hour, and AC charge power stay
static in `config.json`.

## Home Assistant Sync

Home Assistant helper values can act as a UI over runtime state. The EMS avoids
writing HA every cycle and avoids interpreting its own HA writes as user changes.

Helpers are optional. If a helper is missing, the EMS continues with local
runtime-state values.

HA sync failures do not block the EMS loop. The EMS logs the failed sync and
continues with the previous local runtime-state values so telemetry fetch,
safety decisions, and output-control decisions can still run.
