# Safety

EMS controls real power hardware. Take a few minutes to check the points below
before you let it run unattended.

## Before enabling live writes

- Check your inverter serial numbers.
- Check inverter IP addresses.
- Check your grid meter direction (import positive, export negative).
- Check the maximum output limit.
- Check minimum and maximum battery SOC.
- Make sure no other controller writes Zendure output limits (Zendure app HEMS,
  another automation, or a second EMS).
- Start with conservative settings.
- Monitor the first live run.

Until your real values are filled in, EMS stays in safe mode: it calculates
targets but does not write to hardware. Review your settings, then enable live
writes.

## During the first live run

- Watch grid power.
- Watch inverter output.
- Watch battery SOC.
- Stop EMS if values look wrong.

The [Admin Console](admin-console.md) dashboard and diagnostics make it easy to
watch these values during the first run.

## Backups before risky changes

Create a backup before updates, restore, config changes, or a reinstall. The
Admin Console can create and restore backups for you — see
[Backup and restore](admin-backup-restore.md).

## Do not expose local control interfaces

EMS and the Admin Console are designed for trusted local networks. Do not expose
them to the internet.

## Technical safety model

For write gates, runtime write types, and control internals, see the
[technical safety model](../technical/safety-model.md).
