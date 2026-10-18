# Scheduler

The scheduler sets runtime values at given times. A rule is nothing but an
`emsctl` command run at a time: "on weekdays from 18:00 to 23:00 set WR1's
off-grid socket to standard, then to off" does exactly what
`emsctl device WR1 offgrid standard` and, five hours later,
`emsctl device WR1 offgrid off` would do.

It writes no hardware itself. A switch point changes an operator value through
the same command path as `emsctl` and the dashboard (see
[runtime-state.md](technical/runtime-state.md#one-command-path)); what then
reaches an inverter is decided by the control loop as for a value you set by
hand.

## What a rule can set

| Action | Sets | Same as |
|---|---|---|
| Total output limit | `system.max_total_power` | `emsctl system max-power W` |
| Device output limit | `devices.X.max_power` (0 W keeps it from feeding in) | `emsctl device X max-power W` |
| PV priority | `devices.X.pv_priority_factor` | `emsctl device X pv-priority-factor F` |
| Off-grid socket | `devices.X.offgrid_socket_mode` | `emsctl device X offgrid off\|eco\|standard` |
| Charge from the grid | AC role and charge power; 0 W stops | `emsctl device X ac-charge-power W` then `ac-mode input`; `ac-mode output` |
| Charging from surplus | `ac_charge_control.enabled` | `emsctl ac-charge enable\|disable` |
| Charging from surplus, this device | `devices.X.ac_charge_enabled` | `emsctl device X ac-charge on\|off` |
| Winter mode | `winter.enabled` | `emsctl winter enable\|disable` |
| EMS control | `system.enabled` | `emsctl system enable\|disable` |
| Device control | `devices.X.enabled` | `emsctl device X enable\|disable` |
| Minimum output limit | `system.min_output_limit` | `emsctl system min-output-limit W` |

That is every runtime value the EMS has except `system.loop_interval`, a tuning
value rather than a control target; a test keeps the list complete. Values are
absolute, as with `emsctl`: "raise the power in the morning" is "06:00 set to
1000 W" and "09:00 set to 800 W".

*EMS control* and *Device control* off mean the EMS lets go: the inverter keeps
its last output value. To lower the output, use the output limits.

## How a rule fires

A rule is **at a time** (one switch point) or **from – to** (two: the value at
the start and the value it sets when the window ends).

- **The latest switch point wins**, scheduled or set by hand. A value you change
  in the dashboard or with `emsctl` holds until that setting's next switch point.
- **Each switch point fires once.** After the EMS was down, only the latest
  missed switch point of each setting fires; a window missed entirely fires its
  end value. It fires even over a value changed with `emsctl` while the EMS was
  stopped, which the EMS cannot see.
- **Nothing fires retroactively.** A switch point earlier than the rule's last
  save, or than the last *Resume*, never fires: a rule saved at 19:00 for
  18:00–23:00 first acts at 23:00. *Apply now* sets what the rule says for this
  moment; inside a window it counts as the window's start, so the window still
  ends. It needs the rule switched on and the schedule running.
- **At one instant** a start beats an end, so windows 18–23 and 23–06 hand over
  cleanly; then the rule higher in the list wins.
- **Windows on the same setting may not overlap.** The editor names the rule a
  new window clashes with; the check runs on the rules as they are when the
  save lands, so two editors saving at once cannot both pass it. Windows are
  compared in wall-clock time: daily and weekly ones over the cycle they
  repeat in together, others over the coming 28 years, the cycle in which
  every date falls on every weekday. A single switch point may fall inside a
  window.
- **A value the installation no longer takes is not written** — a limit lowered
  in Admin, a device moved to MQTT. The rule's card reads *Refused* with the
  reason until the rule is changed or a later switch point of it is written;
  `schedule_switch_refused` is logged.
- **A device removed from config** leaves its rules inactive. A device renamed
  in config is followed by its serial, which the EMS stores with the rule.
- **A rule switched off is only checked for its shape**, so a rule whose device
  is gone or whose value is now refused can still be switched off. Switching it
  on runs the full check again.
- **A window whose rule goes away ends at once.** Deleting, switching off or
  pausing a rule, or skipping today's date, while its window runs sets the
  window's end value at the next cycle: a charge from the grid stops rather
  than running on without the end that would have stopped it. The same holds
  when an edit moves the rule to another device or cuts its end to a time that
  has passed; the end value is then the one just saved. An edit that keeps the
  present moment inside the window, such as an earlier start or a later end,
  keeps it running. A file replaced under
  a running window -- a backup restored, the file removed -- does not forget
  it: the window ends at its time, or at once when its rule went with the
  old file.
  `schedule_window_closed` is logged. A later switch point of another rule on
  the same setting replaces the window instead, as it would have had the EMS
  run through.
- **An action is one write.** A charge sets its power and the AC role together,
  or neither when one is refused.

The scheduler holds no priority of its own. A scheduled charge from the grid is
an operator AC-input claim, as from `emsctl`: it outranks charging from surplus,
and maintenance and the full-charge assist outrank it. Its end (*stop*) sets
the AC role back to output, under which charging from surplus works as before.
With EMS or device control off nothing reaches that device until a switch point
or a person turns control on again.

## Repeats

Presets: once, every day, every 2nd day, weekdays (Mon–Fri), weekends, Sundays
only, every week, every 2nd week, every month, the last day of the month, every
year. *Custom* repeats every N days, weeks, months or years, on chosen
weekdays, on a day of the month, the last day, or the n-th weekday ("the 2nd
Sunday", "the last Friday"). Any repeat can be kept to some months ("Oct–Mar")
and end on a date or after a number of times. *Skip next* leaves out one date,
the running window's if one runs, which ends it;
up to 50 coming dates can be skipped, and dates that have passed are dropped.

"Every 2nd day" counts from the start date. A monthly rule on the 31st skips
months without one. Times are wall-clock times: a switch point in the
spring-forward gap comes that much later (02:30 becomes 03:30), one in the
repeated autumn hour falls once, and a 22:00–06:00 window lasts 7 or 9 real
hours on those nights. A window the gap would make end before it starts, such
as 02:30–03:15, keeps its length: 03:30–04:15.

## Time zone and clock

Rules run in the zone the EMS's other hour-based windows use — `TZ`, else
`/etc/localtime` (in Docker see [docker.md](docker.md#time-zone)). The Schedule
tab and `emsctl schedule list` name it. A `TZ` that names no zone runs on UTC,
as it does for those windows; one without daylight saving, such as `UTC0` or
`CET-1`, is the fixed offset it names. A POSIX rule string with daylight saving,
such as `CET-1CEST,M3.5.0,M10.5.0/3`, is one the scheduler cannot follow across
a clock change: nothing fires, `schedule_zone_unsupported` is logged, and the
dashboard and `emsctl schedule` say to set a zone name such as `Europe/Berlin`;
`emsctl schedule status`, `pause`, `resume`, `show`, `remove`, `disable` and
`move` still work, and the Schedule tab still lists, pauses, moves and deletes
rules.

Nothing fires while the clock reads a time before this release: a Raspberry Pi
without a clock of its own boots far in the past until NTP answers, and
`schedule_clock_untrusted` is logged. A clock stepped back by up to a day fires
nothing twice: a setting waits until the clock has caught up with what was
applied. After a clock that ran further ahead is corrected, each setting's
current scheduled value is applied again.

`emsctl schedule` reads and checks times in the zone the running EMS reports in
`control-status.json`, so a host and a container with different `TZ` agree; it
falls back to its own zone when no EMS has reported one.

## In the dashboard

The **Schedule** tab (signed in) shows whether the rules run, the switch points
of the next seven days and a card per rule: *Edit*, *Switch off*, *Skip next*,
*Apply now*, move up or down, *Delete*. *Pause all* stops every rule, and the
list of coming switch points stays empty until *Resume*, which fires nothing
that fell into the pause.

The editor has four steps — what, when, repeat, check. While you type, the EMS
checks the draft and answers with the rule as a sentence ("Every weekday,
18:00–23:00, from Mon 19 Oct 2026"), the `emsctl` commands it equals and its
next switch points, or the reason it cannot be saved. *Save rule* follows that
answer.

## emsctl schedule

```bash
python3 emsctl.py schedule list
python3 emsctl.py schedule add --name "Evening socket" --device WR1 --action offgrid_socket \
    --value standard --from 18:00 --to 23:00 --end-value off --repeat weekdays
python3 emsctl.py schedule add --name "Morning boost" --system --action total_output_limit \
    --value 1000 --at 06:00 --repeat weekdays
python3 emsctl.py schedule add --name Vacation --system --action total_output_limit \
    --value 200 --from 2026-08-01T00:00 --to 2026-08-15T00:00 --end-value 800
python3 emsctl.py schedule add --name "Cheap night" --device WR1 --action charge_from_grid \
    --value 600 --from 02:00 --to 05:00 --end-value stop --repeat every:2d --months 10-3
python3 emsctl.py schedule edit r_4f9c2a1b --to 22:30
python3 emsctl.py schedule skip r_4f9c2a1b --date 2026-12-24
python3 emsctl.py schedule preview --days 7
python3 emsctl.py schedule pause
python3 emsctl.py schedule status
```

Further commands: `show`, `enable`, `disable`, `remove`, `move ID up|down`,
`resume`. `--repeat` takes `once`, `daily`, `weekdays`, `weekends`, `sundays`,
`weekly[:mo,we]`, `every:2d`, `every:2w[:sa,su]`,
`monthly[:15|:last|:2su|:lastfr]` and `yearly`; `--months 10-3`, `--count N` or
`--until DATE` (not both) limit it. `--rrule` takes an RFC 5545 RRULE within the
same subset (FREQ, INTERVAL, BYDAY, BYMONTHDAY, BYMONTH, UNTIL, COUNT, weeks
from Monday; a BYDAY position on one weekday only, as `2SU`) and refuses the
rest. A yearly rule stays in its start's month unless `by_month` names months.
`preview` of a paused schedule lists what would fire after a resume. `status`
prints what the running EMS last applied and does next, from a control snapshot
fresh enough to be the running EMS's. `edit --at` keeps the rule's date unless
`--start` names another. `edit` with only `--from` or only `--to` moves that
edge alone, to the nearest such time: 22:00–02:00 with `--from 01:00` becomes
01:00–02:00, with `--to 03:00` 22:00–03:00, and a fortnight's window keeps its
length. With `--start` the window first moves to that day. An edge that would
end the window before it starts is refused.

## Where the rules live

`data/schedules.json`, beside `runtime-state.json`, written only through the
EMS's schedule store, one change at a time across processes (a lock on
`schedules.json.lock`). An entry that is not a valid rule is shown as broken,
applies nothing and can be deleted, even one without an id. While the file as a
whole cannot be read nothing starts, but a window already running still ends at
its time with the end value its start recorded. Every edit names the revision it
was made against, so a dashboard tab and `emsctl` cannot overwrite each other
unseen. A config backup includes the file and a restore puts the backed-up rules
back, under the file's lock. Each restored rule counts as saved at the restore,
so none of its switch points from before then fires, as for a rule saved by
hand. What the scheduler applied goes with the runtime state it was applied to:
restored with it from the backup, a window that ran when the backup was taken
ends at once; with the current runtime state kept, the current records stay, so
a window running now still ends. A restore that resumes a paused schedule fires
nothing from the pause; a backup from before this release leaves the current
rules in place. A fresh Setup sets the rules aside with the runtime state, as
`schedules.json.before-setup-<UTC time>`.

The scheduler does not run in simulation or replay.

## Events

| Event | Meaning |
|---|---|
| `schedule_switch_applied` | a switch point was applied (`rule`, `slot`, `value`, `at`) |
| `schedule_catch_up` | the same, for a switch point missed while the EMS was down |
| `schedule_switch_refused` | the value did not pass the validators; nothing was written |
| `schedule_switch_failed` | the runtime write or the record of it failed, for example a full disk |
| `schedule_window_closed` | a window whose rule went away was ended with its end value |
| `schedule_zone_unsupported` | `TZ` is a POSIX rule string; nothing fires |
| `schedule_rule_orphaned` | the rule's device is not in config |
| `schedule_rename_deferred` | a device renamed in config could not be followed yet, as another process held the schedules lock; it is tried again within a minute |
| `schedule_clock_untrusted` | the clock cannot be right; nothing fires |
| `schedule_store_load_error` | `schedules.json` cannot be read; nothing starts, a window already running still ends at its time |
| `schedule_tick_failed` | the scheduler failed in a cycle; the cycle ran on. Logged once per failure |
| `runtime_values_updated` | every runtime edit, with `source=schedule` for the scheduler's |

`control-status.json` carries a `schedule` block with the same state.
