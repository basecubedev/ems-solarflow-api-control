# Runtime State

`data/runtime-state.json` stores the operating settings, the values an
operator changes while the EMS runs, in new generated configs. It is their
only home ([settings-operation-store.md](../developer/settings-operation-store.md)):
back it up with the rest, and do not delete it to reset values.

```text
config.json
= static installation, safety flags, IPs, serial numbers, technical defaults

data/runtime-state.json
= operating settings: the values an operator changes while the EMS runs

data/ems_state.sqlite
= durable core EMS lifecycle state such as battery full-charge assist tracking

data/control-status.json
= live control snapshot the running EMS rewrites after every cycle; read by
  diagnose, never by the EMS

data/schedules.json
= schedule rules and the switch point last applied to each runtime value;
  see scheduler.md

data/pv-input-watch.json
= each PV input's highest power of the day and the dark days in a row, behind
  the Admin console's PV input hints; see "The PV input watch" below
```

The EMS creates the runtime state file on first start from config defaults. A
file first written while `config.json` still held the template's placeholders,
which force control off, is marked (`seeded_from_placeholders`) and built
afresh from the config at the first start without them, so a new installation
runs with the values it was set up with. Until then, and before any EMS start
while the config still holds placeholders, **Settings & devices** writes the
operating values it changes into `config.json`, which that start reads;
values the dashboard or `emsctl` set meanwhile are not kept. The file is ignored by Git. A missing or unreadable file comes back from its
last good copy, and where that copy cannot be read, or an unreadable file has
none, the EMS starts with control off ([Last good copy](#last-good-copy));
deleting it does not reset the values. Older
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
    "min_output_limit": 35,
    "grid_target_discharge_w": 0,
    "grid_target_charge_w": 0
  },
  "winter": {
    "enabled": true
  },
  "ac_charge_control": {
    "enabled": true
  },
  "battery_care": {
    "enabled": true,
    "summer_full_points": 3,
    "winter_full_points": 4,
    "pv_only_points": 1,
    "min_discharge_w": 50,
    "pv_margin_percent": 20,
    "pv_margin_min_w": 35
  },
  "devices": {
    "WR1": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0,
      "ac_charge_enabled": true,
      "ac_discharge_enabled": true
    },
    "WR2": {
      "enabled": true,
      "max_power": 800,
      "offgrid_socket_mode": "off",
      "pv_priority_factor": 1.0,
      "ac_charge_enabled": true,
      "ac_discharge_enabled": true
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
| `grid_target_discharge_w` | The meter reading the regulation settles on while the battery discharges, -200 to 200 W, import positive, export negative; 0 aims for 0 W ([Grid target](control-logic.md#grid-target)). A value outside that range, or not a number, reads as 0 |
| `grid_target_charge_w` | The same while a surplus charge runs |

`min_output_limit=35` is the default, for a config or a runtime state that
leaves it out as for the template, and is useful on
installations where `outputLimit=0` behaves like a stop, idle, or sleep state. A battery
that [battery care](control-logic.md#battery-care) holds between minSoc and its
full point gets its ceiling instead where that is lower.
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
| `ac_discharge_enabled` | Whether this device may supply the house. `false` ends a running discharge on the next cycle, below `min_output_limit` too, and leaves charging allowed |
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

Every edit of an operator value -- `emsctl`, the dashboard, the Admin's
[settings](#admin-save--runtime-state) -- goes
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
(add/rename/remove) — that stays EMS-authoritative. It writes the operating
settings an Apply changed, and what a saved device activation or a lowered limit
means for them; see [Admin save → runtime state](#admin-save--runtime-state).

Devices are matched to their config entry by a stable `identity` (serial), so:

- **Add** — a new controllable config device gets default runtime settings.
- **Rename** — when a device's name changes in config but its identity is
  unchanged, the operator's runtime settings (`max_power`,
  `pv_priority_factor`, `ac_charge_power_w`, `offgrid_socket_mode`, …) migrate
  to the new name instead of being lost.
- **Switch off** — a device switched off, or read over MQTT without control,
  keeps its entry and its values; the EMS only stops building it. One
  switched off in the Admin also keeps control off in its entry, so switched
  back on by hand in `config.json` it comes back with control off; the
  Admin's switch-on sets control on with it. A rollback to v0.10.8 and back
  loses that: v0.10.8 parks only the operating values, so the device comes
  back with control on.
- **Remove** — a device no longer in config is pruned.

Telemetry-only Zendure MQTT devices carry no controllable runtime settings and
get no runtime-state entry created (they are not flagged as missing by
`diagnose`); one they had while they controlled stays, and so does one the
first start of this release makes from values v0.10.8 parked for them.

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

## Last good copy

Every save writes the same bytes to `runtime-state.json.last-good` beside the
file, and an EMS start that reads the file without changing it refreshes the
copy from what it read, so a hand edit becomes the copy at the next start or
save. A copy that cannot be written only warns (`runtime_state_last_good_failed`);
the save itself stands, and a later fallback finds the copy the last
successful write left. While the file is missing or unreadable, the Admin
shows the copy, which the next EMS start puts back.

- **Unreadable file** -- no JSON object, or a read that fails: the EMS start
  logs `runtime_state_load_error`, keeps the file as
  `runtime-state.json.unreadable-<UTC time>` for the operator (linked there
  before anything replaces it, so a write that fails leaves it in place), and
  puts the copy in its place (`runtime_state_restored`, `source=last_good`). It then
  starts as on any other read, so the operating values move from `config.json`
  as they would have. A copy that cannot be written in place -- a full card --
  runs in memory, nothing is written at that start, and the next save puts it
  where the file is; the same holds for a missing file.
- **No usable copy either** -- missing, or itself unreadable: the EMS starts on
  the built-in defaults with control **off**, writes that to the file so the
  dashboard and `emsctl status` show it, and logs `runtime_state_unusable`.
  Until control is on again the dashboard's top bar reads **Control off:
  settings unreadable** and its **Control** page says what to do, and
  `emsctl diagnose` names it as the error root cause `runtime_state_unusable`
  once that state is in the file; while the file itself cannot be read, or
  the state could not be written, only the dashboard says so.
  The values in `config.json` stay where they are for this start. The state
  carries `started_control_off` until a save or a start finds control
  switched on in it, by whatever means: while a copy exists, no save makes
  such a state the copy, which keeps only what an operator saved. Without
  any copy it becomes the copy, so a file lost later comes back with control
  off. A save of the Admin that would create a missing file whose copy cannot
  be read does the same.
- **A file that cannot be kept aside** -- the directory refuses the link and
  the copy -- stays where it is at that start: the EMS runs on the copy in
  memory, or with control off without one, and its next save replaces the
  file.
- **Missing file** -- deleted, or lost: the EMS and `emsctl` put the copy back
  (the EMS logs `runtime_state_restored`, `reason=missing`) rather than
  creating a new state, and so does the Admin where a save would create the
  file. A running EMS writes back the state it runs instead (`source=memory`),
  which may be newer than the copy. A copy that is there but cannot be used starts the EMS with control
  off, as above, and `emsctl` refuses to create the file. Only where neither
  the file nor a copy exists is the state created afresh, as on a first start.
- A writer that never read the file it would replace -- the Admin's or
  `emsctl`'s own instance over a file it cannot read -- refuses to save, so
  neither the file nor its copy is overwritten with what it does not know.
- A running EMS that reads an unreadable file between its saves keeps the
  state in memory, as before, and its next save writes it back.

A [fresh Setup reset](#fresh-setup-reset) removes the copy with the file it
sets aside, so the new installation does not get the old values back; a copy
it cannot remove is named in the log (`runtime_state_last_good_failed`), the
start builds the state afresh all the same, and its first save replaces it. To start
over from the built-in defaults by hand, stop the EMS and remove both files.

## Admin save → runtime state

Every setting lives in one file ([configuration.md](configuration.md#config-vs-runtime-state),
[settings-operation-store.md](../developer/settings-operation-store.md)). The
operating settings (`OPERATION_SETTING_PATHS`, `OPERATION_DEVICE_KEYS` in
`ems/runtime_commands.py`) live here only: **Settings & devices** shows each as
the EMS runs it, and an Apply writes a changed one here through the runtime
service (`admin/operation_settings.py`), creating this file where the EMS has
not yet, with the config's defaults revision. Only a value the page changed
from the one it loaded is written (the draft carries the loaded ones as
`operating_base`), so a value the dashboard, the scheduler or `emsctl` set
after the page loaded stays. A device the save adds gets its runtime entry
from the Admin before `config.json` is written, with the values the page set
for it (the preview's `runtime_entries`) and its serial as `identity`, and its
config entry keeps no operating value; when that entry cannot be written, or
`config.json` changed since the review, the apply saves nothing. The Admin
holds this file's lock from the entry until `config.json` is written, a
start that read the config before keeps an entry the config on disk names,
and the start's move of operating values saves nothing over a file another
writer changed since the start read it (the values stay in `config.json` for
the next start). An entry written before a later step of the apply failed
belongs to no device in `config.json`, and the next start removes it.
Two cases keep the old way, operating values in the config entry until the
next live start moves them: a save while a fresh setup's reset is pending
(the next start builds this file from the config), and a device added under a
name an entry of another device still holds, one renamed or removed since the
EMS started included (until its next start the running EMS drives that device
under that name). A device that was in the config
before but never ran keeps its values there as well. The preview lists the
values of a device the running EMS drives as taking effect at once, and those
of a device switched off, added or switched on since the EMS started (by the
devices its control snapshot names, `loaded_devices`) as waiting for it to
run.

Every device the EMS can control has an entry, switched on or off: the EMS
creates one at its start, seeded from the device's config entry, and drops it
only when the device is removed from `config.json`, so a device switched off,
or read over MQTT without control for a while, comes back with the values it
ran with. A device whose serial was changed by hand, as for a replaced
inverter, starts like a new one from what its entry holds. The
`parked_devices` that v0.10.8 kept for a switched-off device fill its entry
once at the first start of this release (`runtime_device_values_restored`);
a device that gets no entry of its own (read without control) gets them as its
entry, and those of a device no longer in `config.json` are dropped.
Nothing parks values any more. Until that first start the Admin shows such a
device's values from its config entry, and a value saved there in that window
gives way to the parked one; the Admin's update restarts the EMS, so the
window is short. Two devices under one name -- possible only by
a hand edit -- share one entry: the one the EMS builds keeps it, and the other
loses what it held. **Settings & devices** shows a switched-off device's values
from its entry and changes them there; a device that never ran keeps its
values in its config entry instead. A save
that changes the serial a device's entry is keyed by writes the device's
values into its config entry, since the next start finds no entry for the new
serial. While a fresh setup's reset request still names the
installed config, an Admin save writes the operating values it changes into
`config.json` instead, since the next EMS start builds this file from that
config.

A save of the installation reaches the running EMS through this file in two
ways, written by `admin/runtime_convergence.py` through the same validated
writers the dashboard uses:

- **A device's activation takes its control along.** Switching a device off
  in `config.json`, or turning it telemetry-only, takes it out of control at
  once; switching it on switches its control on; a telemetry entry never
  switches control on. Control on or off is otherwise a setting of its own,
  changed on the dashboard: the device stays active, read and shown, and the
  EMS stops writing to it.
- **A lowered limit holds the operating value at it.** Where the save lowers
  `system.max_total_power` (or `system.max_total_power_limit`), a device's
  `max_power` or the fallback `system.max_device_power` below the operating
  value this file holds, the operating value is set to the new limit. A raised
  limit leaves the operating value where it is; it is changed on the
  dashboard, up to the limit. The runtime service refuses a value above the
  limit it reads live from `config.json`, and the EMS holds every operating
  power at that live limit each cycle, warning once per run and setting
  (`operating_power_above_limit`), so a value written by hand never drives the
  devices past it either.

A device's writes go to the runtime entry the device owns. EMS seeds one at
its start for each device it can control over HTTP or MQTT, switched on or
off, and keeps it until the device leaves `config.json`; a read-only device
has none, and one read over MQTT without control (`external_mqtt`, or
`zendure_mqtt` without `write_output_limit`) keeps an entry it had but gets
none created. The entries
that name one serial (or, without a serial, one name) are one device, and its
runtime entry is the one the EMS starts with
(`ems.runtime_devices.runtime_device_for`): the entry stamped with that serial,
else the one under the name that carries no other serial. A device renamed
since the EMS started, or two devices that swapped names, so reach the entry
the running EMS reads for them. The serial is read as the EMS stamps it (`sn`,
else `serial_number`, else `mqtt.device_id`, as text). One config entry speaks
for the device: the one the EMS builds to control, else the first one it would
build if it were switched on, else the first; a telemetry-only or external
entry never speaks for a device another entry controls
(`admin/config_runtime_overlap.py` `device_runtime_plan`). Left alone are a
device either config holds several buildable entries for, one whose runtime
serial several entries carry, an entry whose `sn` and `serial_number`
disagree, and an entry that changed or lost its serial in this save: none of
these is tied to a running device by its name; the EMS holds their power at
the limit from its next start. A device switched on under another entry than
the one the running EMS drives it as is switched on, and the save reports that
the EMS keeps that entry until it restarts, so the console offers the restart.
A switch-off is written before the values that come with it and a switch-on
after them.

Every operating setting is seeded by the EMS when it loads runtime-state, so
the dashboard always shows the value the EMS applies; a contract test walks
the whitelist against the seeded defaults. The writes are **best-effort**:
`config.json` is already written when they run, so a device that owns no
runtime entry or a read-only or absent runtime file is reported as a per-key
skip and never fails the save. The entry a write goes to is looked up again
under the runtime-state file lock, and the writes run inside the Admin's apply
transaction, so an earlier save cannot write over a later one.

## Old defaults

When the EMS starts with a config that records `config_defaults_revision` (see
[configuration.md](configuration.md#old-defaults)), a value in
`runtime-state.json` still at a default that revision replaced moves to the
value the config gives it, once: battery care's 5, 10 and 0 points,
`system.loop_interval` 3. Each is logged as `runtime_default_refreshed`, and
the file then records `defaults_revision`, so a value set afterwards stays. A
value the config itself still holds at the old default stays too, and a config
that does not record the revision yet leaves the file as it is. A new file
records the config's revision at once. Only a live EMS start does this, not a
dry run; emsctl, the dashboard and the Admin keep the key as they find it.

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
The Admin console reads `loaded_devices` from it to say, for each mapped device,
whether the running EMS reads it as saved or waits for a restart. A snapshot
older than three loop intervals, and at least 60 s, is reported as stale, and
its values are not used.

Shape (`schema_version: 1`):

| Field | Meaning |
|---|---|
| `written_at` | When the EMS wrote the snapshot (ISO-8601, UTC) |
| `loop_interval_s` | Loop interval the EMS applied |
| `started_at` | When the EMS process that wrote the snapshot started (ISO-8601, UTC); a restarted EMS writes another one |
| `cycle.fetched_at`, `cycle.failed_cycles`, `cycle.mode` | When device telemetry was read, consecutive failed cycles, the controller's allocation mode |
| `control.grid_power_w`, `control.filtered_load_w` | Grid meter reading the cycle used and the load it regulated on: the reading less the writes it cannot show yet, smoothed while the load filter is on with the `median_ema` method and a window above 1 or an EMA below 1, which by default it is not (positive is import). Outside the night/min-SoC idle, which regulates nothing and records every reading, a cycle that left its reading alone (`grid_meter.reading_repeated` or `reading_in_dead_time`) did not regulate the output on it, though a running charge may still end on it, and records the load of the last reading it regulated on; `diagnose --control` then names why instead of a regulated load |
| `control.grid_target_w` | The [grid target](control-logic.md#grid-target) the cycle's reading was regulated on, import positive and export negative: the charge target while a charge ran as the cycle started, otherwise the discharge target. The cycle that ends a charge still names the charge target, though it sizes the discharge after it on the discharge target; the night/min-SoC idle, which regulates nothing, names the discharge target |
| `control.commanded_total_w`, `control.allocated_target_total_w`, `control.effective_target_total_w` | The cycle's totals; `effective_target_total_w` is what the EMS commands |
| `grid_meter.consecutive_read_failures`, `grid_meter.measured_at` | From the meter's health record; `null` for a meter without one |
| `grid_meter.reading_repeated` | `true` when the cycle saw a reading an earlier cycle had already regulated on ([One correction per reading](control-logic.md#one-correction-per-reading)) |
| `grid_meter.reading_in_dead_time` | `true` when a set write dead time held the cycle's new reading, which was not regulated on ([Dead time after a write](control-logic.md#dead-time-after-a-write)) |
| `devices.<name>` | `online`, `last_seen_at`, `max_power_w`, `soc`, `min_soc`, `pv_input_w`, `output_w`, `output_limit_w`, `battery_charge_w`, `battery_discharge_w`, `battery`, `allocated_target_w`, `effective_target_w`, `limiting_reason`; telemetry fields are `null` for a device that never answered. `battery` is `yes`, `no` or `unknown` from the pack count the device reports; `diagnose --control` and `--control-quality` leave a `no` device out of their SOC analysis. `settings` holds each [device setting](#device-settings) the device has or that is set, as `{offered, reported, desired}` |
| `grid_samples` | The last 30 cycles' `{cycle_at, grid_power_w, grid_target_w, measured_at, reading_repeated, reading_in_dead_time}`; `grid_target_w` is the cycle's grid target, from which `diagnose --control-quality` judges the regulation |
| `pv_input_hints` | The [PV input watch](#the-pv-input-watch)'s hints for the devices it saw since the EMS started: `{device, input, kind, days}` for an input that stayed dark (`kind` `dark`, `days` the dark days counted in its run, a day on which no input reached 50 W left out) and `{device, input, kind, peak_w}` for one `unused_pv_inputs` names that delivered today or the day before (`kind` `delivers`). Left out by simulation and replay runs |
| `loaded_devices` | `{version, devices}`: one `{name, device_id, enabled, digest}` per device entry `config.json` held when this EMS started, after the startup upgrade; `device_id` is the id an MQTT mapping publishes an external device as (`null` otherwise), and `digest` covers the whole entry, `enabled` included. `ems/device_fingerprints.py` makes them, for the EMS and for the Admin console, which compares them with the file as saved now; `version` names the rule, so an Admin of another release does not compare. Left out by an EMS that was not given the devices it read |

When a cycle fails before it decides, the `control` values are `null` rather
than carried over; device telemetry is the last reading the EMS holds, dated by
`last_seen_at`.

## The PV input watch

`pv-input-watch.json` sits beside the runtime-state file. After every cycle the
EMS takes the PV inputs of each device the dashboard shows and that is online,
controlled or read-only, and keeps each input's highest power of the day, a
day beginning at midnight local time. When a day ends, an input that stayed at
or below 1 W while another input of the same device reached 50 W counts one
dark day; a day the input went above 1 W, or that its report did not carry it,
starts the count again, and a day on which no input reached 50 W leaves it as
it is. The days count in a row as the calendar has them: a day the EMS did not
see the device, because it was stopped or the device offline all day, starts
every count of that device again. Three dark days make a `dark` hint, until
the input goes above 1 W again, the same day included; an input
`unused_pv_inputs` names that reaches 50 W today or reached it the day before a
`delivers` hint. Either lasts only while the device was seen today or the day
before. Both go into the [live control snapshot](#live-control-snapshot)
as `pv_input_hints`. The watch keeps each device by its name, so a renamed
device starts afresh, and a name not seen for 30 days is dropped. A clock that
goes back more than a day behind the day the watch holds, as when a wrong clock
is corrected, is taken as the new date (`event=pv_input_watch_clock_back`); a
smaller step back is left alone until the clock reaches the day again.

The Admin console shows each hint the saved config has not settled on the
device's card, with a button that marks the input empty or in use in the
draft; nothing changes until the settings are applied. The EMS never changes
the config itself. The file is written when a day ends and at most every ten
minutes after a new peak, so a restart keeps the count. A file that is no JSON
starts a new one (`event=pv_input_watch_unreadable`); one of another format
version, or a device in it this cannot read, is left out without an event. A
failure to write it never interrupts the control loop
(`event=pv_input_watch_save_failed` once, `event=pv_input_watch_save_recovered`
when a write succeeds again). Deleting it while the EMS is stopped starts every
count afresh; a running EMS writes what it holds again. Simulation and replay
runs neither read nor write it.

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
