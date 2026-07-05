# FAQ

Short answers, grouped so you can scan. For detail, follow the links.

## Admin Console

### What is the Admin Console?

The Admin Console (product name **EMS SolarFlow Admin**) is a local browser tool
for setup, device discovery, maintenance, updates and backups. EMS still owns the
control logic. See [admin.md](admin.md).

### Do I have to use the Admin Console?

No. It is recommended for most users, but you can also use the Docker Bootstrap
installer with Docker commands and `emsctl.py`.

### How do I start the Admin Console?

No Git checkout is required:

```bash
mkdir -p ems-solarflow-api-control
cd ems-solarflow-api-control
curl -fsSLO https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/deploy/admin/install-admin-console.sh
sh install-admin-console.sh
```

Then open `http://127.0.0.1:8090`. The default uses host networking for reliable
LAN device discovery; use `--bridge` if you need Docker bridge networking.

### Why does the Admin Console use host networking by default?

EMS is a local LAN system. Host networking makes device discovery more reliable
because the container sees the LAN like a local host process. Use `--bridge` if
you want Docker bridge networking instead.

### Can I run the Admin Console in bridge mode?

Yes.

```bash
sh install-admin-console.sh --bridge
```

Discovery may be less reliable in bridge mode. The UI is published on
`127.0.0.1:8090`.

### Should I choose Setup or Maintenance?

Use **Set up a new system** for a fresh install or deliberate reinstall. Use
**Manage my existing system** for updates, diagnostics, config changes and
backups. See [setup/admin-setup.md](setup/admin-setup.md) and
[setup/admin-maintenance.md](setup/admin-maintenance.md).

### Does the Admin Console overwrite my config?

Not silently. Config changes are previewed and confirmed before writing, and any
existing config is backed up first.

## Operating models

### Should I use the Admin Console or Docker Bootstrap?

Use the **Admin Console** if you want a browser-guided setup with device
discovery and maintenance. Use **Docker Bootstrap** if you prefer copy/paste
shell commands without the browser wizard. Both use the same `config/config.json`
layout, so you can switch later. See
[setup/docker-bootstrap.md](setup/docker-bootstrap.md).

### What is Developer Setup?

Developer Setup is a Git checkout with a local Python environment, for
development, debugging and contributing. It is **not** the normal user path. See
[setup/developer-setup.md](setup/developer-setup.md).

## Config and files

### Where is my config?

`config/config.json`

### Where is my data?

`data/`

### Where are backups?

`data/backups/` by default. Docker users see that folder on the host via the
existing `./data:/app/data` mount; no separate backup volume is needed.

### What is `data/admin/`?

`data/admin/` holds the Admin Console's own state: release cache, staging, logs
and backup-set metadata. It is not a second EMS config and not a live EMS runtime
layout.

### I already have `config.json` in the project root. What should I do?

That is the legacy layout. New Docker and Admin Console setups use
`config/config.json`. The Admin Console routes a legacy root config to
Maintenance and offers to migrate it. See
[setup/config-layout.md](setup/config-layout.md).

## Updates and backups

### What should I do before updating?

Create a backup first, then check diagnostics:

```bash
docker compose exec ems python3 emsctl.py backup create --type config
docker compose exec ems python3 emsctl.py diagnose
```

Then use the Admin Console **Guided upgrade** or update with Docker commands.

### Are Admin Console backups normal EMS backups?

Yes. The Admin Console uses the EMS backup tooling. Backup archives live in
`data/backups/` by default. See
[setup/admin-backup-restore.md](setup/admin-backup-restore.md).

### Can I create encrypted backups in the Admin Console?

Yes. Provide a password when creating the backup. Without the password, an
encrypted backup cannot be restored.

### Can I delete backups in the Admin Console?

Yes, when the Backup / restore page offers the delete action. Deletion always
requires confirmation and only removes archives inside the backup directory.

### Can I restore InfluxDB from the Admin Console?

Not yet. The Admin Console can create, inspect, list and delete InfluxDB backups,
but InfluxDB restore is blocked in the Admin Console. Use the EMS CLI restore flow
for InfluxDB.

## Security

### Is the Admin Console safe to expose to the internet?

No. Run it only on a trusted local network, or behind a deliberate reverse-proxy
and auth setup. A deployment-capable Admin container controls the host Docker
engine, which is effectively root-equivalent.

### Does the Admin Console use the EMS Dashboard password?

Not yet. The Admin Console has no built-in authentication, so keep it on a
trusted local network. If a future release adds Admin authentication, it should
reuse the EMS Dashboard password file; check your version's release notes.

## Troubleshooting

### The dashboard is not reachable. What should I check?

Check the containers and logs:

```bash
docker compose ps
docker compose logs -f ems
```

Then open `http://127.0.0.1:8080` (or `http://<host-ip>:8080`) and confirm port
`8080` is reachable on the host.

### Device discovery does not find my devices. What should I try?

The default install already uses host networking, which is the most reliable
mode for LAN discovery. First confirm the container is running and reachable:

```bash
docker compose -f docker-compose.admin.yml ps
```

Then rerun the scan and, if needed, enter your LAN CIDR (for example
`192.168.178.0/24`) manually. If you started the Admin Console in bridge mode,
switch back to the host-networking default (drop `--bridge`) so discovery can see
the real LAN. See [troubleshooting.md](troubleshooting.md) for more.

### How do I create a support bundle?

```bash
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

## General

### Do I need Home Assistant?

No. Home Assistant is optional.

### Do I have to use Docker?

No, but Docker is recommended for normal users.

### Which grid meters are supported?

Shelly, Shelly 3EM Gen1, EcoTracker, and Tasmota HTTP setups are documented in
[supported-setups.md](supported-setups.md).

### Can I use multiple Zendure inverters?

Yes, if each configured device has a real IP address, serial number, and suitable
limits.

### Can I keep using the Zendure app?

Read-only use is usually fine. Avoid running another controller that writes
Zendure `outputLimit`.

### Does EMS need internet or cloud access?

Control is local-first. Docker image pulls, updates, and optional support
workflows need internet access; normal EMS control uses local devices and your
configured local meter.

### Is native Python still supported?

Yes. It is documented as an advanced/manual setup in
[native-python.md](native-python.md).

### Do I need InfluxDB?

No. The dashboard and local history work without InfluxDB. InfluxDB is optional
for long-range analytics.

### What is safe mode?

Required template placeholders force EMS safe mode: control disabled, dry-run
enabled, and hardware writes blocked until placeholders are replaced.

### What is dry-run?

EMS calculates and logs intended values but does not write Zendure hardware
output.

### What happens on first Docker start?

The container creates `config/config.json` from the template if it does not
exist. Existing `config/config.json` is not overwritten.

### What should I do after editing config?

Restart the container and run diagnose:

```bash
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

The [first-run checklist](first-run-checklist.md) is a good next step after a
larger config edit.

### How do I stop EMS?

```bash
docker compose down
```

Use `docker compose up -d` to start it again.

### Where do I find logs?

```bash
docker compose logs -f
```

### What should I do before opening an issue?

Run diagnose, create a support bundle, and include what hardware and grid meter
type you use.
