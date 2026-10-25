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

data/control-status.json
= live control snapshot the running EMS rewrites after every cycle; read by
  diagnose, never by the EMS

data/schedules.json
= schedule rules and the switch point last applied to each runtime value;
  see scheduler.md
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
  "winter": {
    "enabled": true
  },
  "ac_charge_control": {
    "enabled": true
  },
  "devices": {
    "WR1": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0,
      "ac_charge_enabled": true
    },
    "WR2": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0,
      "ac_charge_enabled": true
    }
  }
}
```

## System Fields

| Field | Meaning |
|---|---|
| `enabled` | Enables or disables every EMS write to the inverters, state reconciliation included. A charge the EMS started (the regulator's or the full-charge assist's) gets one final command that ends it, then nothing |
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
further `outputLimit` writes until PV telemetry becomes positive again, or until
an AC charge surplus is confirmed (see
[control-logic.md](control-logic.md#night--minsoc-idle)).

## Device Fields

| Field | Meaning |
|---|---|
| `enabled` | Skip every write to this device when false, state reconciliation included. If the EMS started a charge on it, one final command ends the charge first |
| `ac_charge_enabled` | Whether this device may be charged from surplus |
| `max_power` | Runtime per-device power limit |
| `offgrid_socket_mode` | Operator intent for Zendure offgrid socket mode; the only writer of `gridOffMode`. A legacy config `devices[].grid_off_mode` only seeds it for a new device; when it differs from the runtime value, `event=legacy_grid_off_mode_ignored` names the command that sets it. |
| `pv_priority_factor` | Runtime PV-first allocation weight override |
| `light`, `fan`, `buzzer`, `auto_heat` | Device settings the EMS keeps on the inverter; absent or `null` leaves them to the device (see [Device settings](#device-settings)) |
| `identity` | Stable hardware identity (serial) used to track a device across renames; managed by the EMS |

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

The CLI and the dashboard only change the intent. The EMS is the only
component that writes `gridOffMode` to hardware.

## Device settings

Light, fan, buzzer and battery heating are settings of the inverter itself. By
default the EMS leaves them alone: a device entry carries none of the keys, and
nothing is written. Once you set one, the EMS keeps the inverter at that value,
also when the Zendure app changes it back; `unmanaged` hands it to the device
and the app again.

| Key | emsctl | Values | Zendure property |
|---|---|---|---|
| `light` | `device NAME light` | `on`, `off` | `lampSwitch` 1 / 0 |
| `fan` | `device NAME fan` | `auto`, `1`, `2`, `off` | `Fanmode` 1 with `Fanspeed` 0 / 1 / 2; `Fanmode` 0 |
| `buzzer` | `device NAME buzzer` | `on`, `off` | `buzzerSwitch` 1 / 0 |
| `auto_heat` | `device NAME auto-heat` | `on`, `off` | `autoHeat` 1 / 0 |

```bash
python3 emsctl.py device WR1 light off
python3 emsctl.py device WR1 fan auto
python3 emsctl.py device WR1 light unmanaged
```

`fan auto` lets the inverter run its fan by its own temperature, and is what
the inverter does unless told otherwise. `fan off` keeps the fan off; an
inverter that runs too warm lowers its power by itself.

A setting is written only where all of this holds:

- the device is connected over the local API; over MQTT the EMS writes no
  device settings, and `emsctl` and the dashboard refuse to set one there
  (`unmanaged` is always accepted);
- its model is in the hardware catalogue with the setting, which the ZenSDK
  models (SolarFlow 800, 800 Pro, 800 Pro 2, 800 Plus, 1600 AC+, 2400 AC,
  2400 AC+, 2400 Pro, 3000 and 4000 Mix AC+) are, and its report carries the
  setting's properties; the dashboard's runtime settings and its **Schedule**
  page offer a setting only then, and the device's card under **Runtime
  settings** keeps one set earlier listed with its value, so it can be handed
  back;
- `allow_state_reconciliation_writes` is on, the EMS is not in dry run,
  simulation or replay, and neither the EMS nor the device is switched off
  (off is off).

The EMS reads the report every cycle and writes only what differs, each
setting in its own `properties/write`, so one the inverter refuses holds back
no other; a write that gets no answer ends that inverter's settings for the
cycle, so an inverter that does not answer costs one timeout. A value the
inverter has not taken is sent again 30 s after the last send at the earliest.
Once it has been sent three times and the report still does not show it 30 s
after the third, it is named once with `event=device_setting_not_taken` and
held until an hour after the third send, then sent three times more. Another
value or handing it back, once a control cycle has seen it, the inverter
showing it, or a restart of the EMS starts the count afresh. `lampSwitch`, `Fanmode` and `Fanspeed` were written on a
SolarFlow 800 Pro 2; `buzzerSwitch` and `autoHeat` follow Zendure-HA and have
not been written on hardware yet. `emsctl status` lists each setting as the
running EMS last saw it, beside the value set.

## One command path

Every edit of an operator value -- `emsctl`, the dashboard, the Admin's [config
→ runtime mirror](#config--runtime-convergence-admin-maintenance) -- goes
through `ems/runtime_commands.py`: one whitelist, one set of validators, named
commands that are `emsctl`'s (`system max-power`, `device NAME offgrid`, `winter
enable`, …), and one writer. The writer re-reads the file immediately before it
saves, so a value another process saved in the meantime is kept, and writes
through a temp file named after its process and thread and a random token, so
two processes saving at once never share one. Each save is logged as
`event=runtime_values_updated` with its `source` (`emsctl`, `dashboard`,
`admin`); an AC role also records it in `runtime_role_reason`.

Every save holds an exclusive lock on `runtime-state.json.lock` -- an edit from
its re-read to its rename, the EMS's start-up reconcile from its read to its
save -- so two processes cannot interleave there. An edit waits at most two
seconds for it and then fails with "runtime state ... is busy"; the start-up
reconcile waits thirty and then goes on, logging `runtime_state_lock_busy`, and
so does re-creating a file removed while the EMS runs, after two seconds. A
writer waits for the lock before it takes the process's own, so the control
loop's reads never queue behind it. A writer that cannot open the lock file -- a
directory it may not create files in -- or whose filesystem has no flock -- an
NFS mount without a lock daemon -- saves without it, as every writer did before,
and `file_lock_unavailable` says so once. Temp files a killed writer left
(`runtime-state.json.<pid>.<thread>.<token>.tmp`, the same for `schedules.json`)
are removed when the EMS starts once they are an hour old.

## Device Lifecycle Reconciliation

The EMS is authoritative for runtime device state. On every load it reconciles
the device entries against the configured (controllable) devices, so the file
stays clean as devices are added, renamed, or removed in `config.json`. The
Admin Console never edits `runtime-state.json` to reconcile the device lifecycle
(add/rename/remove) — that stays EMS-authoritative. It does mirror a small,
whitelisted set of overlapping scalar values into runtime-state on Apply; see
[Config → runtime convergence](#config--runtime-convergence-admin-maintenance).

Devices are matched to their config entry by a stable `identity` (serial), so:

- **Add** — a new controllable config device gets default runtime settings.
- **Rename** — when a device's name changes in config but its identity is
  unchanged, the operator's runtime settings (`max_power`,
  `pv_priority_factor`, `ac_charge_power_w`, `offgrid_socket_mode`, …) migrate
  to the new name instead of being lost.
- **Remove** — a device no longer in config is pruned.

Telemetry-only Zendure MQTT devices carry no controllable runtime settings and
deliberately have no runtime-state entry (they are not flagged as missing by
`diagnose`).

Reconciliation is **fail-closed**: if the configured device list is empty or the
config is unreadable, nothing is pruned. Before any destructive change
(rename/prune) the EMS writes a one-step backup to `runtime-state.json.bak` and
logs an audit event (`event=runtime_device_renamed`,
`event=runtime_device_pruned`, `event=runtime_device_added`). Identities are
backfilled onto pre-existing entries by name on first load, so subsequent
renames migrate settings.

## Fresh Setup reset

A fresh Setup (`/api/setup/config/apply`, and the deployment prepare that writes
the generated config) replaces `config.json` and takes nothing from it, so the
runtime values the dashboard saved would otherwise override the new config. It
does not touch `runtime-state.json` itself: an EMS still running on the replaced
config would write a file set aside now straight back from memory within a
cycle. The Setup leaves `runtime-state.json.reset-requested` beside it instead,
naming the config it wrote by its SHA-256, and only the EMS process that starts
on the new config settles it (`start_runtime_state`). While the installed
config is the one the request names, the existing file is kept as
`runtime-state.json.before-setup-<UTC time>` and the state is built from the
config's values (`runtime_state_reset_by_setup`); a device switched off in the
dashboard is therefore active again. The schedule rules beside it are set aside
the same way, as `schedules.json.before-setup-<UTC time>`: they name devices and
limits of the installation the Setup replaced. A request naming any other config -- a
backup restored since, a Setup write that never arrived -- is dropped
(`runtime_reset_request_dropped`), so a restore keeps the runtime state it puts
back, and an automatic rollback onto the Setup's config still resets. Every edit
of the installed config moves the request to the config it writes -- Maintenance,
the MQTT migration and a Guided Upgrade through the Admin's apply, the Guided
Upgrade's config step, `emsctl`, the dashboard and the startup config upgrade
through `write_config_json_atomic` -- so the reset survives the edit. Setup's start recreates an
EMS that already runs, because Compose leaves a running service alone when only
its mounted config changed. A running EMS reloading the file, a `--preflight`
run, the Admin's maintenance mirror and `emsctl` never settle the request.

## Config → runtime convergence (Admin maintenance)

A few scalar values are writable in **both** `config.json` (the Admin console) and
`runtime-state.json` (the dashboard's **Control** page and `emsctl`). For these keys the
live EMS resolves the runtime value first, with the config value as the fallback,
so an Admin edit to `config.json` alone would not take effect until a restart —
and the runtime value would keep winning even then.

To keep the two stores coherent, the Admin's **Save and apply** on **Settings &
devices** mirrors the keys it actually changed into runtime-state,
one-directionally (config/Admin → runtime; runtime is never merged back into
config). The overlapping keys are exactly the
Dashboard runtime-write whitelist:

- `system.enabled`, `system.max_total_power`, `system.loop_interval`,
  `system.min_output_limit`
- `winter.enabled`, `ac_charge_control.enabled`
- per device: `enabled`, `max_power`, `pv_priority_factor`,
  `offgrid_socket_mode`, `ac_charge_enabled`

Every key on this list is seeded from `config.json` by the EMS when it loads
runtime-state, so the Dashboard's runtime controls always show the value the
EMS applies. A key the Dashboard could write but the EMS did not seed would
read as *off* in the Dashboard while the EMS applied the config default; that
is how `ac_charge_control.enabled` shipped, and a contract test now walks the
whitelist against the seeded defaults.

The mirror reuses the same validated whitelist writers the Dashboard uses (so the
safety property — only whitelisted keys ever reach runtime-state — is preserved)
and is **best-effort**: `config.json` is already written when it runs, so a value
above the runtime power ceiling, a device not yet present in runtime (newly added
or renamed), or a read-only/absent runtime file is reported as a per-key skip and
never fails the save. New devices are seeded from config by the EMS on its next
start. **Settings & devices** also marks a field whose live Dashboard override
differs from the installed config value with **Running: …**, and its **Put saved
values back** action writes the installed config values back into runtime-state
(it always writes the value; it never just clears the override, because the EMS loads
`config.json` once per process).

## Live control snapshot

`control-status.json` sits beside the runtime-state file
(`data/control-status.json` in new generated configs) and is not runtime state.
The running EMS rewrites it atomically after every control cycle, from what the
controller holds after the cycle, and never reads it back; deleting it changes
nothing but what diagnose can report until the next cycle. A failure to write it
never interrupts the control loop: it is logged as
`event=control_status_write_failed` once until a write succeeds again
(`event=control_status_write_recovered`). Simulation, replay, self-test and
preflight runs do not write it.

`emsctl diagnose --control`, `--control-quality` and the dashboard's **Diagnose**
page read it as their only source of cycle values (see [cli.md](../cli.md#diagnose)).
A snapshot older than three loop intervals, and at least 60 s, is reported as
stale, and its values are not used.

Shape (`schema_version: 1`):

| Field | Meaning |
|---|---|
| `written_at` | When the EMS wrote the snapshot (ISO-8601, UTC) |
| `loop_interval_s` | Loop interval the EMS applied |
| `cycle.fetched_at`, `cycle.failed_cycles`, `cycle.mode` | When device telemetry was read, consecutive failed cycles, the controller's allocation mode |
| `control.grid_power_w`, `control.filtered_load_w` | Grid meter reading the cycle used and its filtered value (positive is import) |
| `control.commanded_total_w`, `control.allocated_target_total_w`, `control.effective_target_total_w` | The cycle's totals; `effective_target_total_w` is what the EMS commands |
| `grid_meter.consecutive_read_failures`, `grid_meter.measured_at` | From the meter's health record; `null` for a meter without one |
| `devices.<name>` | `online`, `last_seen_at`, `max_power_w`, `soc`, `min_soc`, `pv_input_w`, `output_w`, `output_limit_w`, `battery_charge_w`, `battery_discharge_w`, `battery`, `allocated_target_w`, `effective_target_w`, `limiting_reason`; telemetry fields are `null` for a device that never answered. `battery` is `yes`, `no` or `unknown` from the pack count the device reports; `diagnose --control` and `--control-quality` leave a `no` device out of their SOC analysis. `settings` holds each [device setting](#device-settings) the device has or that is set, as `{offered, reported, desired}` |
| `grid_samples` | The last 30 cycles' `{cycle_at, grid_power_w, measured_at}` |

When a cycle fails before it decides, the `control` values are `null` rather
than carried over; device telemetry is the last reading the EMS holds, dated by
`last_seen_at`.

## Winter Fields

| Field | Meaning |
|---|---|
| `enabled` | Enables or disables winter mode at runtime |

Winter months, SOC limits, ramp step, step hour and policies stay static in
`config.json`.

## AC Charging Fields

| Field | Meaning |
|---|---|
| `enabled` | Switches AC charging from grid surplus on or off at runtime |

Seeded from `ac_charge_control.enabled` (on by default). The thresholds stay
static in `config.json`; per device, `ac_charge_enabled` in the device entry
stops one device drawing from the grid without a restart.

## Former Home Assistant Fields

The Home Assistant integration is removed, and the EMS no longer seeds an `ha`
section. A runtime state that still carries `ha.enabled` /
`ha.control_enabled` loads unchanged, and the fields stay in the file as any key
the EMS does not know: the merge with the defaults keeps unknown keys, and only
device entries are reconciled on load, so the next EMS write leaves them as they
are. Nothing reads them; a rollback to a release that still had the
integration finds them as they were. The dashboard and `emsctl` no longer write
them (`PATCH /api/runtime/ha` is answered `404 not_found`, as any unknown
section is).
