# One value per setting: installation and operation

Status: owner-approved design (2026-11-25), implemented in four phases on
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
  later, one by one, each with live reading in the EMS. Owner decision
  (2026-11-27): each such move makes the values editable in the Admin console
  first; the dashboard and `emsctl` get their fields later. Winter has moved;
  the full-charge assist and the control tuning are next.
- Hard caps stay in `config.json` and change only in the Admin's Safety tab:
  the total (`system.max_total_power`, or `system.max_total_power_limit`
  where a config sets it) and each device's own
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
- `winter.months`, `summer_min_soc`, `winter_min_soc`, `ramp_step_percent`,
  `adjust_hour` (the first of the later moves; the per-class
  `winter.policies` stay installation). Older releases read these only from
  `config.json`, so it keeps a copy (`ROLLBACK_CONFIG_COPIES`): the Admin
  writes both, the switch-over does not move them and the template keeps
  them. Only the Admin may write them (`apply_section_update` refuses every
  other source), and a save whose runtime write fails after the config was
  written names the value it could not hand over, while the copy already
  holds it. Owner decision (2026-11-27): the copy goes in v0.11.0, which
  cannot be rolled back to v0.10.
- `battery_care.enabled`, `min_discharge_w`, `pv_margin_min_w`,
  `pv_margin_percent`, `pv_only_points`, `summer_full_points`,
  `winter_full_points`
- per device: `pv_priority_factor`, `ac_charge_enabled`,
  `ac_discharge_enabled`, `offgrid_socket_mode`

Pairs, cap in `config.json`, operating value in the operation store:

- total power: cap `system.max_total_power` in `config.json` (or
  `system.max_total_power_limit` where a config sets it), operating
  `system.max_total_power` in the operation store
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

All four phases are done. The steps as built:

Phase 2, Admin reads and writes operating values where the dashboard does:

- `ems.runtime_commands` names the operating settings once:
  `OPERATION_SETTING_PATHS` (system, winter, AC charging and battery care
  paths) and `OPERATION_DEVICE_KEYS` (the device keys without `enabled` and
  `max_power`), derived from the whitelists, and one accessor that answers
  each operating value as the EMS takes it (runtime first, then the config,
  then the default). Its `field_specs()` gives each field's type, range and
  choices as the writer checks them; they reach the dashboard in the
  runtime limits (`fields`), whose control page offers no other, and a test
  holds the Admin catalogue's ranges for the operating fields to them.
- `admin/maintenance_config.py`: the draft carries the operating values for
  those paths and device keys, and the values it loaded (`operating_base`);
  `_merge_draft` writes them into the config only for a new device and a
  device the running EMS keeps no runtime entry for, and the apply then moves
  a new device's into the runtime entry it creates for it before the config
  is written, holding the runtime-state lock until it is
  (`new_device_entries`, `device_entries_written`; refused as a whole when
  that write fails or the config changed since the review); the apply writes those
  the page changed from the loaded ones through the runtime writer
  (`admin/runtime_convergence.py`), creating `runtime-state.json` where it is
  missing, so a value set elsewhere after the page loaded stays; the preview
  lists them as taking effect at once, and the result names any it could not
  hand over.
- The operating settings leave the override provenance; the power limits and
  the device activation keep it until phase 4 replaces it.

Phase 3, switch-over:

- A live start that may write `config.json` (the gate of the old-defaults
  refresh, the config upgrade not `disabled`, the defaults revision recorded)
  moves each operating value the runtime state holds out of `config.json`
  (`ems.config.move_startup_operating_values`): the runtime state, which the
  start filled from its own values first and the config's second, is saved
  before the config is rewritten behind the upgrade backup, with the config's
  own layout, and only while the file is what the start read; not when the
  runtime state could not be read. Every device that owns a runtime entry
  loses its values from `config.json`, switched on or off.
- One place per device (backlog 73): every device the EMS can control has a
  runtime entry, switched on or off, from its first start until it is removed
  from `config.json` (`ems.runtime_devices`, the one module the EMS and the
  Admin join a config entry to its runtime entry with). Switching a
  device off moves nothing; one read over MQTT without control keeps its
  entry while it stays in the config. The values v0.10.8 parked for a
  switched-off device (`parked_devices`) fill its entry once, a device read
  without control gets them as its entry, and nothing parks values any more. A
  device a save adds gets its entry from the Admin before the config names it
  (`ems.runtime_devices.new_runtime_entry`, the entry its first start would
  leave), so its config entry never holds an operating value; not while a
  setup reset is pending, and not under a name an entry of another device
  still holds (one renamed or removed since the EMS started). Those, a device that was in the config but never
  ran, and one whose serial a save changes, since its next start finds no
  runtime entry for the new serial, keep their values in their config entry
  until the next live start.
- While a fresh setup's reset request names the installed config, an Admin
  save writes the operating values it changes into `config.json`: the next
  start builds the runtime state from that config.
- The template keeps the operating keys as the starting values of a config
  that Guided Setup or a hand writes, and the catalogue's defaults come from
  it; `emsctl config init` writes the ones it asks about (control, minimum
  output limit, winter mode). The config upgrade and `diagnose` skip them
  (`ems.config.without_operating_values`), so a moved value is never added
  back or reported missing, and the next live start moves a seeded one.
  Guided Setup's deployment marker accepts the installed config with seeded
  values removed (`ems.config.only_operating_values_removed`), so that move
  does not unprepare the deployment it started
  ([Docker setup](../technical/admin-discovery.md#docker-setup-default-deployment-capable)).
- The `ac_discharge_config_overridden` event is gone: the runtime switch is
  the one permission.

Phase 4, caps:

- The EMS holds each operating power at the limit the live validation context
  names each cycle (`EMSController.within_power_limit`, warned once per run as
  `operating_power_above_limit`), so a runtime value written by hand, or kept
  from before a limit was lowered, never drives the devices past it.
- An Admin save that lowers a limit below the operating value sets the
  operating value to the limit; a raised limit leaves it
  (`admin/runtime_convergence.py`). The save no longer mirrors any other value:
  the operating settings are written through `admin/operation_settings.py`,
  and a device's activation still takes its control along.
- The Maintenance load answers `operating` (the total, and each device's power
  and control switch) instead of the override provenance; the limit fields
  show **Operating value: …** below them, a device's activation shows
  **Control off on the Dashboard** where that is so, and the control card
  shows the total the EMS runs below its limit. The reset-runtime endpoint,
  "Running: …", "Put saved values back" and the Customized tab's running
  column are gone. The device limit's help names the dashboard value and the
  fallback (`system.max_device_power`, "Fallback device limit").

Backlog 59 (a device switch spelled as text) is settled by phase 3: the device
switches that move leave `config.json`, and the operation store's writer
accepts only real booleans; the switch-over translates a text value the way
the EMS read it. The activation `devices[].enabled` stays; the startup
rewrite of switches written as text (`ems.spelled_switches`) writes anything
but a boolean or `null` as `false`, as the EMS reads it, and the Admin's device
drafts read it with the EMS's own reader (`config_entry_enabled`).
