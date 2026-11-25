# One value per setting: installation and operation

Status: owner-approved design (2026-11-25), implemented in phases on
`feat/settings-operation-store`.

## Problem

Since the dashboard and the Admin console became one app, eighteen settings and
two power values live in two files, and a nineteenth pair shares a name. `config.json` is written by the Admin
console, `runtime-state.json` by the dashboard, `emsctl` and the scheduler, and
the EMS reads the runtime value first. The Admin mirrors what it saves into the
runtime state, nothing goes the other way, so the console shows "Running: …"
and offers "Put saved values back". A value changed in the dashboard shows
differently in the settings.

`runtime-state.json` holds no machine state: full-charge assist and winter
progress live in `ems_state.sqlite`, the cycle snapshot in
`control-status.json`, schedules in `schedules.json`. It holds operator
settings only.

## Decision

Two files, split by kind, every setting in exactly one of them:

| | `config.json`: installation | `runtime-state.json`: operation |
|---|---|---|
| Holds | devices and connections, credentials, write gates, safety caps, ports and paths, everything that needs a restart | every setting an operator changes while the EMS runs |
| Writer | Admin save (validated, backed up) | one service, `ems.runtime_commands`, for the dashboard, the Admin, `emsctl` and the scheduler |
| Takes effect | at the next start | at once |

Owner decisions (2026-11-25):

- Target A above, not one file: a single `config.json` would need live reload,
  rewrite the file holding credentials and write gates on every dashboard
  click and make Admin drafts conflict.
- First the settings that are live today; feature settings that need a restart
  today (winter months, minimum SoC, full-charge assist, control tuning) move
  later, one by one, each with live reading in the EMS.
- Hard caps stay in `config.json` and change only in the Admin's Safety tab:
  the total (`system.max_total_power_limit`) and each device's own
  `devices[].max_power`; `system.max_device_power` is only the fallback for a
  device without its own. The operating values below them live in the
  operation store, changeable everywhere, never above the cap.
- At the switch-over, where both files hold a value, the running one (from
  `runtime-state.json`) is kept.
- Schedules keep working as today: a switch point sets an operating value the
  way an operator does, and the latest switch point wins.

## What moves

Into the operation store, out of `config.json`:

- `system.enabled`, `system.loop_interval`, `system.min_output_limit`,
  `system.grid_target_discharge_w`, `system.grid_target_charge_w`
- `winter.enabled`, `ac_charge_control.enabled`
- `battery_care.enabled`, `min_discharge_w`, `pv_margin_min_w`,
  `pv_margin_percent`, `pv_only_points`, `summer_full_points`,
  `winter_full_points`
- per device: `pv_priority_factor`, `ac_charge_enabled`,
  `ac_discharge_enabled`, `offgrid_socket_mode`

Pairs, cap in `config.json`, operating value in the operation store:

- total power: cap `system.max_total_power_limit` (filled from today's
  `system.max_total_power` where absent), operating `system.max_total_power`
- device power: cap `devices[].max_power`, operating `devices.<name>.max_power`

Two settings that share the name `enabled` per device, and stay apart:

- `devices[].enabled` in `config.json` is the device's activation, the only
  activation authority: a device switched off there is not read, shown or
  controlled. It stays installation.
- `devices.<name>.enabled` in the operation store is control on or off: the
  device is still read and shown, the EMS only stops writing to it. It is an
  operating value already.

The Admin compares the two today as if they were one setting; every surface
names them apart ("Active" and "Control") instead.

Already only in the operation store: the AC role and AC charge power per
device, the control switch per device, and the device settings (lamp, fan,
buzzer, battery heating).

## Phases

1. **Readers.** Every reader of a moving setting reads the operating value
   (runtime first, config only as the seed), with one accessor; a test walks
   the whitelist and the readers. No data moves.
2. **Admin writes through the service.** The settings page writes operating
   fields through `ems.runtime_commands`, the config apply writes installation
   fields only; the catalogue marks each field's store, paired with the
   whitelist by a test. "Running: …", "Put saved values back" and the Running
   column go.
3. **Switch-over.** At a live start, behind a backup and only after the
   old-defaults refresh, the moving settings leave `config.json` for the
   operation store, the running value winning; template and setup seed the
   operation store. A rollback still reads the same values, since older
   releases merge the runtime state over the config's defaults.
4. **Caps.** The Safety tab shows and edits the caps; every surface shows cap
   and operating value apart.

## Implementation steps

Phases 1 to 3 are done. The steps as built:

Phase 2, Admin reads and writes operating values where the dashboard does:

- `ems.runtime_commands` names the operating settings once:
  `OPERATION_SETTING_PATHS` (system, winter, AC charging and battery care
  paths) and `OPERATION_DEVICE_KEYS` (the device keys without `enabled` and
  `max_power`), derived from the whitelists, and one accessor that answers
  each operating value as the EMS takes it (runtime first, then the config,
  then the default).
- `admin/maintenance_config.py`: the draft carries the operating values for
  those paths and device keys; `_merge_draft` never writes them into the
  config; the apply writes the changed ones through the runtime writer
  (`admin/runtime_convergence.py`), creating `runtime-state.json` where it is
  missing; the preview lists them as taking effect at once.
- The override provenance, the reset-runtime endpoint and the UI's
  "Running: …", "Put saved values back" and Customized running column go;
  the Admin bundle is rebuilt.

Phase 3, switch-over:

- A live start that may write `config.json` (the gate of the old-defaults
  refresh, the config upgrade not `disabled`, the defaults revision recorded)
  moves each operating value the runtime state holds out of `config.json`
  (`ems.config.move_startup_operating_values`): the runtime state, which the
  start filled from its own values first and the config's second, is saved
  before the config is rewritten behind the upgrade backup, with the config's
  own layout, and only if the file is unchanged since it was read. A disabled
  device keeps its values; it has no runtime entry.
- The template keeps the operating keys as the starting values of a config
  that Guided Setup or a hand writes, and the catalogue's defaults come from
  it; `emsctl config init` writes the ones it asks about (control, minimum
  output limit, winter mode). The config upgrade and `diagnose` skip them
  (`ems.config.without_operating_values`), so a moved value is never added
  back or reported missing, and the next live start moves a seeded one.
- The `ac_discharge_config_overridden` event is gone: the runtime switch is
  the one permission.

Phase 4, caps:

- The Safety tab shows each cap with the operating value below it; changing
  a cap below the operating value clamps the operating value; labels name
  `system.max_device_power` as the fallback for a device without its own.

Backlog 59 (a device switch spelled as text) is settled by phase 3: the device
switches that move leave `config.json`, and the operation store's writer
accepts only real booleans; the switch-over translates a text value the way
the EMS read it. The activation `devices[].enabled` stays and joins the
startup rewrite of switches written as text (`ems.spelled_switches`) once its
readers agree.
