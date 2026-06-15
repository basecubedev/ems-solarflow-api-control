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

Only devices with telemetry `packNum > 0` participate. Devices reporting
`packNum == 0` are ignored and never receive assist writes.

On first enable with an empty EMS state database, EMS assumes the battery was
recently full and seeds `last_full_charge_at` from the current time. The first
due date is scheduled for `now + interval_days`. EMS does not immediately start
AC charging only because the feature was enabled.

When battery full-charge assist is disabled and later enabled again, EMS treats
that as a new tracking start. If no assist or restore is active, it resets the
schedule from the current time and ignores old overdue dates from the previous
enabled period. This avoids immediate AC charging after seasonal re-enable.

When a device is due, or due soon and already above `assist_start_soc`, EMS:

1. marks full-charge assist active in the core state store
2. requests `socSet=1000` through the normal safe SOC write path
3. optionally requests AC input mode through the existing runtime AC intent path
4. optionally applies `inputLimit=<ac_charge_power>` through the existing safe
   AC charge input-limit writer
5. blocks normal output allocation for the device while assist or restore is
   pending

When firmware reports `socLimit == 1` while assist is active, EMS marks the
assist complete, stores `last_full_charge_at`, schedules `next_due_at`, and
restores normal Max-SoC from the currently loaded `config.json` device
`max_soc`. If the user changed `max_soc` while assist was active, the new
configured value is used. EMS does not persist a previous Max-SoC value.

If AC charge mode was used, restore requests `acMode=2` through the existing
runtime AC intent reconciler. Output control remains blocked until restore is
done or fresh telemetry confirms the restored state.

All hardware writes respect the existing write gates: dry-run, simulation mode,
replay mode, `allow_hardware_writes`, and
`allow_state_reconciliation_writes`.

## Configuration

```json
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
```

`enabled` defaults to `false`.

`interval_days` defines when a new assist is required after the last
firmware-reported Max-SoC event.

`assist_window_days` allows EMS to start early before the due date when current
SOC is already at or above `assist_start_soc`.

`force_time` starts assist on or after the due day at the configured local time,
regardless of current SOC, unless firmware already reports `socLimit == 1`.

`enable_ac_charge_mode` controls whether active assist also requests AC input
mode. The AC mode transition uses the runtime AC intent foundation; there is no
separate assist-owned `acMode` writer.

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

## Dashboard

When enabled, the Devices view shows a compact "Full-charge assist" section on
each battery-backed device card. It reflects the same state as `diagnose`:

- **Assist active** — EMS is helping the device reach firmware Max-SoC. This
  is normal and can take several hours to days; it is not an error state.
  - **AC charge running** is shown only when telemetry confirms `acMode=1`
    and `acStatus=2`. Assist can also run without AC charge mode
    (`enable_ac_charge_mode=false`).
  - While active, EMS internally marks Max-SoC and (if AC charge mode is
    used) AC output mode restore as pending for *after* charging finishes.
    The dashboard shows this as **"Restore planned"** — it does not mean a
    restore is currently stuck or failed.
- **Assist window active** — the device is inside the configured
  `assist_window_days` before its next due date. EMS may start an assist
  charge soon if SOC is already high enough.
- **Restore pending** — assist has finished (no longer active) and EMS is
  still waiting for write gates to restore the configured Max-SoC
  (**Max-SoC restore pending**) and, if AC charge mode was used, normal
  output mode `acMode=2` (**AC output mode restore pending**). These details
  are only shown once assist is no longer active.
- **Assist overdue** — the device passed its due date without a completed
  assist. The dashboard shows how many days overdue.
- Otherwise the card quietly shows the last full-charge timestamp and the next
  due date.

This is EMS support for reaching the firmware-reported Max-SoC state in time,
not direct firmware calibration control. Devices without a detected battery
(`packNum == 0`), or when the feature is globally disabled, do not show this
section.
