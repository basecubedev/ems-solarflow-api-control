# Safety

EMS controls real power hardware. Take a few minutes to check the points below
before you let it run unattended.

## Before enabling live writes

- Check your inverter serial numbers.
- Check inverter IP addresses.
- Check your grid meter direction (import positive, export negative).
- Check the maximum output limit.
- Check minimum and maximum battery SOC.
- Make sure no other controller writes Zendure output limits. Use only one
  active controller: disable Zendure HEMS, Smart Matching, Zendure schedules /
  energy plans, Home Assistant or ioBroker automations that set inverter power,
  and any second EMS instance. This matters especially for Zendure Cloud MQTT
  control — the cloud services write the same setpoints. The EMS cannot disable
  them for you; it reports `external_control_suspected` when a foreign writer
  repeatedly overrides a confirmed target.
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
the Admin Console — or the EMS ports — to the internet. Use them only on a
trusted local network.

The Admin Console requires a password (the same one as the EMS Dashboard) before
any setup, maintenance or backup action. This is a local safeguard, not a
substitute for keeping the appliance off the public internet.

The Admin Console is intended for a trusted local network. Optional HTTPS can
protect local browser traffic, but the generated self-signed certificate is not
a replacement for a VPN or a properly secured reverse proxy for remote access.

Like the Dashboard, the Admin Console can write runtime *overrides*, but only for
a fixed whitelist of keys (system power/loop limits, winter/HA enable, per-device
enabled/max power/PV priority/off-grid mode) and only through the same validated
runtime-write limits — never the hardware `outputLimit` or state-reconciliation
write gates. The safety property is the whitelist, not the store: no other key
can reach `runtime-state.json` from the Admin.

## Technical safety model

For write gates, runtime write types, and control internals, see the
[technical safety model](../technical/safety-model.md).
