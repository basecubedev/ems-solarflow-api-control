# Troubleshooting

Start with the Admin Console if you installed EMS through the recommended path.

## Start here

1. Open the Admin Console.
2. Go to **Maintenance**.
3. Check **Overview** for container and config status.
4. Open **Diagnostics** and run the recommended checks.
5. Follow the shown warning or next step.

The Admin Console runs at `http://127.0.0.1:8090` on the machine where you
installed it. If it runs on another LAN host, use that host's IP instead.

If you installed with Docker Bootstrap, use the Docker command sections below.

## Common problems

### The Admin Console does not open

Check that the Admin Console container is running.

If you used the installer, start it again from the EMS folder:

```bash
sh install-admin-console.sh
```

Then open:

```text
http://127.0.0.1:8090
```

If the Admin Console runs on another LAN host, use that host IP instead.

### Admin Console says the password file needs repair

The Admin Console and EMS Dashboard share `config/dashboard-auth.json`.

If this file is damaged, the Admin Console will not create a new password
automatically. Repair or remove the file on the EMS host, then reload the Admin
Console.

Do this only if you understand that removing the file resets the local
dashboard/admin password.

### Device discovery does not find my devices

Check that your EMS host is in the same local network as your devices.

The Admin Console uses host networking by default because this usually works
best for local discovery.

If you started with bridge mode, try the default mode again:

```bash
sh install-admin-console.sh
```

Also check that the device is powered on and reachable from the LAN.

### The EMS dashboard is not reachable

Open the dashboard at `http://127.0.0.1:8080`, or `http://<host-ip>:8080` from
another device on the same network.

- In the Admin Console, open **Maintenance → Overview** and use the dashboard
  link shown there.
- Make sure the EMS container is running and healthy.
- If the page loads but has no data yet, wait for the first control cycle.

### EMS is running but values look wrong

Open **Maintenance → Diagnostics** in the Admin Console and run the recommended
checks. They explain what EMS is reading and deciding.

Then check the basics:

- Grid meter type and IP address are correct.
- Device IP addresses and serial numbers are correct.
- The grid meter direction is right — import should read positive, export
  negative.
- Battery SOC is above the minimum SOC, so the battery is allowed to discharge.

### No output change / the inverter does not react

If targets look correct but the inverter output does not change:

- Make sure live writes are enabled. Until your real values are filled in, EMS
  stays in safe mode: it calculates targets but does not write to hardware. See
  [Safety](safety.md).
- Make sure **only one controller** writes Zendure output limits. Do not run
  the Zendure app HEMS, another automation, or a second EMS at the same time.
- Check that the battery is above its minimum SOC.

### Backup or restore failed

- Use **Maintenance → Backup / restore** in the Admin Console.
- Restore always shows a **preview** first; check it before you confirm.
- For an encrypted backup, enter the same password you used to create it. The
  password cannot be recovered.
- Keep backups in the default `data/backups/` folder so the Admin Console can
  find them.

See [Backup and restore](admin-backup-restore.md) for the full workflow.

### Update failed

- Use **Maintenance → Guided upgrade** in the Admin Console.
- Review the plan and create a backup before you apply the update.
- After the update, run **Diagnostics** and review the result.
- If something looks wrong, restore the backup you made before the update.

See [Admin maintenance](admin-maintenance.md).

### I need help

Create a support bundle and attach it to your report. It is redacted and does
not contain your passwords, tokens, or serial numbers.

In the Admin Console, open **Maintenance → Diagnostics** and export a support
bundle. On a Docker Bootstrap install, see the command in the section below.

More help: [Getting help](../../README.md#getting-help) and the
[FAQ](faq.md).

## Docker Bootstrap or advanced shell checks

Use these only if you installed with Docker Bootstrap or you are comfortable
with shell commands.

```bash
docker compose ps
docker compose logs -f ems
docker compose exec ems python3 emsctl.py diagnose
```

Create a redacted support bundle for a report:

```bash
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

## Technical details

For command-level diagnostics and deeper failure analysis, see the
[technical troubleshooting reference](../technical/troubleshooting-reference.md).
