# Runtime State

`runtime-state.json` stores mutable operator state.

```text
config.json
= static installation, safety flags, IPs, serial numbers, technical defaults

runtime-state.json
= mutable local runtime/operator state
```

The EMS creates the runtime state file on first start from config defaults. The
file is ignored by Git.

Example:

```json
{
  "system": {
    "enabled": true,
    "max_total_power": 800,
    "loop_interval": 5,
    "min_output_limit": 30
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
      "offgrid_socket_mode": "off"
    },
    "WR2": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off"
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

`min_output_limit=30` is useful on installations where `outputLimit=0` behaves
like a stop, idle, or sleep state. The guard is applied before deadband handling
and only while EMS control is enabled.

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
