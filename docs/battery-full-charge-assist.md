# Battery Full-Charge Assist

Battery full-charge assist is an optional EMS lifecycle helper. It is not direct
firmware calibration control and it never writes `batCalTime`. Zendure firmware
still owns any internal calibration behavior.

When enabled, EMS tracks battery-backed devices and tries to make sure each
device reaches the firmware-reported Max-SoC state within the configured
interval. Completion is based only on active assist state plus `socLimit == 1`.
SOC percentage is not used as a completion threshold.

## Behavior

EMS passively records fresh telemetry in `data/ems_state.sqlite` by default.
This core database is independent of the dashboard database, so the feature can
work when the dashboard is disabled.

The database is written only when something in it changes, so it does not wear
an appliance's SD card with one write per device and cycle (K8b): a report that
changes nothing but its time stays in memory, and `last_seen_at` is written at
most every 15 minutes. While the battery stays full, the time it was last seen
full is held the same way and written the moment it leaves full, so the
schedule is computed from the exact last full report. Only a stop while the
battery is full can lose up to the last 15 minutes of it, which moves the next
due time as much earlier. `diagnose` reads the database from outside the EMS
process and therefore shows values up to 15 minutes old; nothing decides on
`last_seen_at`. The feature switch is written when it changes.

Only devices with telemetry `packNum > 0` participate. Devices reporting
`packNum == 0`, and devices that never report the field at all, are ignored and
never receive assist writes. The EMS keeps those two cases apart elsewhere — see
"Devices Without A Battery" in
[technical/control-logic.md](technical/control-logic.md) — but for this feature
both mean the same thing: no confirmed battery, no assist.

On first enable with an empty EMS state database, EMS assumes the battery was
recently full and seeds `last_full_charge_at` from the current time. The first
due date is scheduled for `now + interval_days`. EMS does not immediately start
AC charging only because the feature was enabled.

When battery full-charge assist is disabled and later enabled again, EMS treats
that as a new tracking start. If no assist or restore is active, it resets the
schedule from the current time and ignores old overdue dates from the previous
enabled period. This avoids immediate AC charging after seasonal re-enable.

When a device is due, or due soon and already above `assist_start_soc`, and
the assist can write what it needs there (see
[Behind a shut write gate](#behind-a-shut-write-gate),
[Over MQTT control](#over-mqtt-control) and
[Over a report without a field it writes](#over-a-report-without-a-field-it-writes)),
EMS:

1. marks full-charge assist active in the core state store
2. requests `socSet=1000` through the normal safe SOC write path; the write
   carries no `minSoc` (see [The assist and `minSoc`](#the-assist-and-minsoc))
3. optionally requests AC input mode through the existing runtime AC intent path
4. optionally applies `inputLimit=<ac_charge_power>` through the existing safe
   AC charge input-limit writer
5. blocks normal output allocation for the device while assist or restore is
   pending, either only while it can be written (see
   [Behind a shut write gate](#behind-a-shut-write-gate))

When firmware reports `socLimit == 1` while assist is active, EMS marks the
assist complete, stores `last_full_charge_at`, schedules `next_due_at`, and
restores normal Max-SoC from the currently loaded `config.json` device
`max_soc`. If the user changed `max_soc` while assist was active, the new
configured value is used. EMS does not persist a previous Max-SoC value.

If the assist put the device into AC input, restore requests `acMode=2`
through the existing runtime AC intent reconciler. Output control remains
blocked until restore is done or fresh telemetry confirms the restored state.
The assist owes that restore once its `acMode=1` went out, or a report showed
`acMode` 1 while it claimed the device. A write answered with an error, or not
answered at all, went out: the device may have carried it out, so the restore
is owed as for an accepted one. If the device refused it, that costs one exit
to idle at most -- at a switch-off or a stop -- since the next report shows
`acMode` 2 and closes the record the exit opened. An assist that entered no AC
input -- its AC charge mode off, behind a shut write gate, on a transport without state
reconciliation (MQTT control), or over a report without `acMode` or
`inputLimit` -- owes none, and its abort or completion writes no `acMode`. A
restore owed from before the device moved to MQTT control is settled at once,
unwritten, as `full_charge_assist_ac_mode_restore_settled` with
`reason=transport_state_reconciliation_unsupported`.

A report without `socSet` or `acMode` cannot confirm a restore, and nothing is
written over a field the report does not carry
([safety-model.md](technical/safety-model.md#socset-and-inputlimit)), so such a
restore never holds the device out of output control for good. Over a missing
`socSet` the restore stays owed but holds nothing while the report lacks it:
output control goes on, and the assist writes the configured `max_soc` at the
first report that carries `socSet` again, whatever `soc_reconcile_interval`
says. A restore write that fails over a known `socSet` stays owed and holds the
device. Over a missing `acMode` the device may still sit in the assist's AC
input, so the restore is the assist's exit to idle (`smartMode=1`, `acMode=2`,
`inputLimit=0`, `outputLimit=0`) on the power write gate, logged as
`ac_charge_ended_on_disable` with `claim=battery_full_charge_assist_restore`,
once per resend window and three times at most; a device that drops offline
in between keeps the count, and gets no fresh three once it answers again
(K24). It is settled as `full_charge_assist_ac_mode_restore_settled`
once a report after an exit shows the device out of the charge -- without
`acMode` in it, only while the device draws nothing from the grid -- or, with
`left_to=device` at `WARNING`, once the third exit's resend window has passed
and the device still charges; output control returns in the next cycle.
The assist's own `acMode=1` and its `inputLimit` go out together or not at all:
over a report without either it enters no AC input, and holds no charge a stop
or a switch-off would end (see
[Over a report without a field it writes](#over-a-report-without-a-field-it-writes)).

### Behind a shut write gate

Nothing the assist needs can be written while `allow_state_reconciliation_writes`
is off, or any other gate on the state write path is shut (dry run, simulation,
replay, the transport's own gate): neither `socSet=1000` nor `acMode=1` and its
`inputLimit`. So a due assist does not start then, and holds no device out of
output control (K25): it stays due and starts in the first cycle after the gate
opens. An assist already running when the gate is shut -- one a process with
the gate open started before a restart -- stays active in the state store and
claims nothing: none of its writes goes out, and the device is in output
control like any other, so it does not keep its last `outputLimit` into an
export. It completes only if the firmware reports `socLimit == 1` by itself, at
whatever `socSet` the device holds. Once the gate opens it claims the device
again, writes `socSet=1000` and, with its AC mode, `acMode=1` and its
`inputLimit`, and runs on as before. A device still in its AC input from the
process before is handled meanwhile as for a restore, below.

### Over MQTT control

An MQTT control device takes the power command only
(`supports_state_reconciliation=False`): nothing the assist needs goes out
there, whatever the gates, so the assist is local-API only. A due assist does
not start on such a device, so it never holds one out of output control. One
running from before the device moved to MQTT control claims nothing: it stays
active in the state store and completes only if the firmware reports
`socLimit == 1` by itself. Its restore owes nothing there either, see above.
The dashboard shows such a device as **Assist unavailable**, never as due,
overdue or active.

### Over a report without a field it writes

Nothing is written over a field the device's report does not carry
([safety-model.md](technical/safety-model.md#socset-and-inputlimit)): not
`socSet`, not the assist's `acMode=1` over an unknown mode, and that `acMode=1`
goes out with its `inputLimit` or not at all. So a due assist does not start
over a report without `socSet`, or, with its AC mode
(`enable_ac_charge_mode`), without `acMode` or `inputLimit`: it stays due and
starts at the first report that carries them. A running one claims nothing
while the report lacks a field it still has to write -- `socSet` until its
`socSet=1000` went out, `acMode` and `inputLimit` until it entered AC input --
so the device is in output control like any other, its request stays pending,
and it claims the device again at the first report that carries them. A
`socSet` it can write still goes out meanwhile. A charge it already entered
stays its own while it runs: its `acMode=1` and `inputLimit` went out, so it
keeps claiming the device over a report without those fields while the report
shows the charge -- without `acMode`, by a charging `acStatus` -- and a
switch-off or a stop ends that charge as any other. With control on, a field
the report does not carry causes no write. A report without `acMode` that does
not show the device charging -- grid draw alone may be the few watts a device
draws idle, and a model may report no `acStatus` at all -- holds the device for
nothing the assist could write there: the assist sends neither its charge
nor an exit, and the device is in output control like any other. While the
charge is on the assist's record its claim still holds the direction, as the
regulator's charge claim does -- no `acMode` of its own, at the assist's
priority -- so no claim below it, a role the operator set included, writes
`acMode` over the unknown mode into that charge. Output control it leaves as
the best of those claims has it: with the regulator under the default claim,
with nobody under an operator park, so a parked device stays parked. And the
device takes no charge of the regulator's meanwhile: entered over the
unknown mode, that charge put `acMode=1` and its own `inputLimit` into the
assist's, on a second record beside the assist's. The regulator's output
target reaches the device only as the bare `outputLimit` a device in AC input
ignores. It takes the device back at the next report that carries `acMode`
or shows it charging.
The record is left as it is, neither closed nor put back on record. Only a
report with a known `acMode` puts the charge back on record, so an exit
already out stands across reports that show the charge by `acStatus` alone:
such a charge stays on record with its exit out until a report shows the
device out of it -- `acMode` 2, or without `acMode` no grid draw -- or one
with `acMode` 1 puts it back on record, and a stop or the next switch-off ends
it as any charge on record, with the exits still owed; after three exits that
only grid draw followed the charge is the device's, as on a transport's
record (`ac_charge_left_to_device` with `charge=battery_full_charge_assist`).
Until then a stop the EMS reaches by itself sends it the release, three failed
cycles in a row return it, and a stop by signal names it in
`ac_charge_kept_across_stop`. Once the device has left that charge -- a report
after an exit, a release after failed cycles or a stop shows it out of the
charge, or its three exits are spent on grid draw alone -- the assist needs
`acMode` and `inputLimit` again as one that never entered: over a report
without them it claims nothing, not even the direction, and the device is in
output control. The record of the charge stays in the process,
closed, so a few watts of draw shown later are no charge of the assist's. Only
a process that never had that record -- after a restart -- lets the report
decide: the charge is the assist's while the report shows the device in it, by
`acMode` or without one by a charging `acStatus`. One predicate
decides the start, the claim
and the dashboard's **Assist waiting** (or **Assist unavailable** for the
transport): what keeps the assist from writing what it needs on the device now
-- its transport, a shut gate, control switched off, or such a report. While
other devices charge from surplus, the dashboard's **Control** page says of a device the
assist or its restore holds, by its claim or by the direction hold, that the
full-charge assist holds it, not that the device may not charge.

A restore cannot be written while `allow_state_reconciliation_writes` is off,
or any other gate on the state write path is shut (dry run, simulation, replay,
the transport's own gate), or while control is switched off (`system.enabled`
or the device's `enabled`), and it holds no device out of output control while
that lasts (K22): it stays owed in the state store and output control goes on.
Nothing of the restore goes out meanwhile -- no `socSet`, no `acMode=2`, and
over a report without `acMode` not its exit to idle. A device still in the
assist's AC input is in output control like any other then. With AC charging
on (`ac_charge_control.enabled`, the default) and a device the EMS can charge,
the process that finds it in AC input after its start cannot tell the assist's
charge from one a stopped EMS left running, and sends it the one exit to AC
input found after a start (see [ac-charging.md](ac-charging.md#safety)):
`smartMode=1`, `acMode=2`, `inputLimit=0` and the target as `outputLimit`, on
the power write gate, which `allow_state_reconciliation_writes` does not shut,
once per resend window and three times at most until the device takes it --
with control switched off as its final command. The device leaves the charge,
and the restore itself still waits for the gate and for control. Where
the EMS cannot charge the device -- AC charging off, the device's
`ac_charge_enabled` off, a model without an AC charge path -- nothing ends the
charge: the device keeps charging at the assist's `inputLimit`, up to the
`socSet` the assist raised, and an output target reaches it only as the bare
`outputLimit` a device in AC input ignores. Once the gate opens and control is
on, the restore claims the device again and goes out as above: `acMode=2`, or
over a report without `acMode` the exit to idle, three times at most, and the
configured `max_soc`. A restore write that fails behind an open gate still
holds the device.

### The assist and `minSoc`

The assist writes `socSet` alone, at its start, on every loop while it runs and
on its restore. Reaching Max-SoC needs no floor, so `minSoc` stays with the
periodic SoC reconcile while an assist runs, as at any other time: winter
mode's target, or where winter mode plans nothing the configured `min_soc`,
each raise held to the rule in
[winter-mode.md](winter-mode.md#why-three-points) (written once it leads the
SoC by at most three points and does not land on it; a lowering at once). The
assist used to write the configured `min_soc` with its `socSet`: without a look
at the SoC, which can make the firmware charge from the grid at full power, and
against winter's target, which the reconcile then wrote back, once each per
loop for as long as the assist ran.

All of the assist's own writes respect the existing write gates: dry-run,
simulation mode, replay mode, `allow_hardware_writes`, and
`allow_state_reconciliation_writes`.

Switching the EMS off (`system.enabled`) or the device (its runtime `enabled`)
ends a running assist's grid charge like any charge the EMS started (owner
decision 2026-10-10): one exit to idle (`smartMode=1`, `acMode=2`,
`inputLimit=0`, `outputLimit=0`), a power command on the transport's gate,
logged as `ac_charge_ended_on_disable` with
`charge=battery_full_charge_assist`. The controller keeps the same record of
this charge that a transport keeps of its own. While control is on and the EMS
holds the assist's charge -- `allow_state_reconciliation_writes` on, the
assist's `acMode=1` gone out (whatever its answer) or shown in a report under
its claim, and the assist claiming the device, or restoring `acMode` after a
completion or an abort -- the charge is on record, and the record reads every fresh report, so a
clamp the device shows counts as the assist's. A device the assist never put
into AC input holds no charge of the assist's and gets neither this exit nor
the release below. Each restore cycle counts as the charge's exit, whether its
write reached the device or not, so a device that took the restore is sent
nothing more and, at worst, the first exit after a switch-off waits one resend
window. Once control is off, that record is owed
the exit. It goes out again once per resend window until the device reports it
left the charge or shows a setpoint the assist never wrote, which is someone
else's charge. A setpoint the device shows before the exit -- a clamp, or a
charge it has yet to leave -- counts as the assist's. Two things differ from a
transport's own record: a reported `inputLimit` of 0 counts as no setpoint
shown, since a device that does not report one reads as 0 too, and the exit is
sent at most three times each time control goes off, and three times in all
until a cycle with control on finds the charge held over a known `acMode`, so
a firmware protection charge that shows no setpoint is not written to for
good. Besides that cap the record is bounded as a transport's is for a charge
only grid draw shows: after three exits that only such draw followed, once the
last resend window has passed, the charge is the device's and the record
closes (`ac_charge_left_to_device` with `charge=battery_full_charge_assist`,
see [ac-charging.md](ac-charging.md)). An exit to such a charge stands when
control comes back on: over a report without `acMode` the assist sends
neither its charge nor an exit with control on, and does not put the charge
back on record, so the exits still owed go out at the next switch-off
or stop, up to the same three. The record is the only owner of that
exit: a device this process had the assist's charge on record for is no AC
input found after a start, so it never also gets that one exit, and once the
record's three exits of a switch-off went out, nothing more goes to it while
control stays off. Only fresh reports
count: an unreachable device gets the exit when it answers again. An exit the
device answered with an error waits its window like an accepted one, and one
that never reached it goes out again at once; both log `ac_charge_end_failed`,
told apart by `delivered=True` or `delivered=False`. The assist stays due:
`socSet` stays at 100 % and the restore waits, and once control is back the
assist goes on, or completes and restores if the battery got full meanwhile. No
assist starts while control is off. A charge from before a restart with control
off was never on that record: neither the running assist nor its restore claims
the device while control is off, so it is handled as any AC input found after
a start (see [ac-charging.md](ac-charging.md#safety)) -- the one exit where the
EMS could have charged the device, otherwise left to the device.

A stop the EMS reaches by itself -- `--once`, `--max-cycles`, `--duration`, an
error that ends the process -- ends the assist's charge with the same release
that returns the regulator's (see [ac-charging.md](ac-charging.md#safety)):
the exit to idle on the transport's gate, logged as
`ac_charge_released_on_shutdown` with `charge=battery_full_charge_assist`. It
reads the same record, so a charge the device left, or one already sent its
three exits while control was off, gets nothing. Three failed control cycles
in a row return it the same way, logged as
`ac_charge_released_after_failed_cycles`, on the record's resend window and
three times at most: while cycles fail the assist's claim asks for no `acMode`,
so that release is the only exit. The assist takes its AC input back once three
cycles in a row have run through, as many as failed before the release; until
then its claim asks for `acMode=2` as with its AC mode off, so a cycle that
fails three times out of four does not move the device in and out of AC input.
It takes it back at most `ac_charge_control.max_charge_entries_per_hour` times
an hour, the regulator's entry limit (K24): three failed cycles and three that
ran through, over and over, moved the device into AC input every six cycles.
While the trailing hour leaves no entry the assist waits on, said once as
`full_charge_assist_ac_input_rate_limited` (`warning`).
A stop by SIGTERM or SIGHUP (`docker stop`, `docker compose down`,
`systemctl stop`) leaves the charge running, as every charge, and names the
device in `ac_charge_kept_across_stop`; Ctrl-C returns it, as a stop the EMS
reaches by itself does. The assist itself stays due and goes on when the EMS is
back.

## Configuration

```json
"battery_full_charge_assist": {
  "enabled": false,
  "interval_days": 28,
  "assist_window_days": 7,
  "assist_start_soc": 80,
  "force_time": "14:00",
  "ac_charge_power": 600,
  "enable_ac_charge_mode": true,
  "state_database_path": "data/ems_state.sqlite"
}
```

`enabled` defaults to `true` for new configs. Set it to `false` in
`config.json` to opt out.

`interval_days` defines when a new assist is required after the last
firmware-reported Max-SoC event.

`assist_window_days` allows EMS to start early before the due date when current
SOC is already at or above `assist_start_soc`.

`force_time` starts assist on or after the due day at the configured local time
(in Docker, the container's [time zone](docker.md#time-zone)), regardless of
current SOC, unless firmware already reports `socLimit == 1`.

`enable_ac_charge_mode` controls whether active assist also requests AC input
mode. The AC mode transition uses the runtime AC intent foundation; there is no
separate assist-owned `acMode` writer. Switched off by a restart while an assist
holds the device in AC input, the assist claims `acMode=2` back through the same
reconciler and gates, as its restore does; once the device reports it, the
assist leaves the direction alone for the rest of its run. Switched on again,
the assist owes the restore of `acMode` after it once more.

Deleting `data/ems_state.sqlite`, or the configured state database, resets the
full-charge assist history and the remembered enabled/disabled state. On the
next enable/run, EMS seeds a new schedule from the current time unless firmware
telemetry already reports `socLimit == 1`, which remains the best known truth.

## Diagnostics

Use:

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --json
```

Diagnose reports whether the feature is enabled, the core state database path,
per-device last full-charge timestamps, next due timestamps, pending restore
flags, and read-only firmware diagnostics such as `socLimit`, `socStatus`, and
`batCalTime` when seen in telemetry.

Its per-device `status` reads `not available over MQTT` for a Zendure MQTT
device, and `active, waiting for state writes` for an active assist kept from
writing: by the device transport's state write gate as the EMS resolves it from
the stored config (`dry_run`, `simulation_mode`, the transport's own gate such
as `allow_hardware_writes`, and `allow_state_reconciliation_writes`), or by
control switched off -- `system.enabled` or the device's `enabled`, read from
runtime-state over the stored config as the EMS reads them. Diagnose does not
see the running process, so a gate shut on the command line (`--dry-run`,
`--simulate`, `--replay`) or a device report without a field the assist writes
still reads `active` there; the dashboard shows those.

## Dashboard

When enabled, the dashboard's **Devices** page shows a compact **Full-charge
assist** section on each battery-backed device card (see
[Devices](user/dashboard/devices.md)). It reflects the same state as `diagnose`:

- **Assist active** — EMS is helping the device reach firmware Max-SoC. This
  is normal and can take several hours to days; it is not an error state.
  - **AC charge** · **Running** is shown only when telemetry confirms `acMode=1`
    and `acStatus=2`. Assist can also run without AC charge mode
    (`enable_ac_charge_mode=false`).
  - While active, EMS internally marks Max-SoC and (once the assist put the
    device into AC input) AC output mode restore as pending for *after*
    charging finishes.
    The dashboard shows this as **After charge** · **Restore planned** — it does not mean a
    restore is currently stuck or failed.
- **Assist waiting** — the assist is active in the state store but cannot
  write what it needs, so it claims no device: while state writes are not
  allowed or control is switched off (see
  [Behind a shut write gate](#behind-a-shut-write-gate)), or while the device's
  report lacks a field it still has to write (see
  [Over a report without a field it writes](#over-a-report-without-a-field-it-writes));
  it goes on once it can.
- **Assist unavailable** — the device is on MQTT control, which takes no state
  write (see [Over MQTT control](#over-mqtt-control)); the assist never runs
  there, and the card shows only the last full charge the firmware reported.
- **Assist window active** — the device is inside the configured
  `assist_window_days` before its next due date. EMS may start an assist
  charge soon if SOC is already high enough.
- **Restore pending** — assist has finished (no longer active) and EMS is
  still waiting for write gates or control switched on, or for a report that
  carries `socSet`, to restore the configured Max-SoC
  (**Max-SoC restore** · **Pending**) and, if the assist put the device into AC
  input, normal output mode `acMode=2` -- over a report without `acMode`, the
  exit to idle, three times at most (**AC output mode** · **Restore pending**). The card says which of the two it
  waits for. These details are only shown once assist is no longer active.
- **Assist overdue** — the device passed its due date without a completed
  assist. The dashboard shows how many days overdue.
- Otherwise the card quietly shows the last full-charge timestamp and the next
  due date.

This is EMS support for reaching the firmware-reported Max-SoC state in time,
not direct firmware calibration control. Devices without a detected battery
(`packNum == 0`), or when the feature is globally disabled, do not show this
section.
