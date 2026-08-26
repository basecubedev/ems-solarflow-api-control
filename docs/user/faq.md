# FAQ

Short answers, grouped so you can scan. For detail, follow the links.

## Admin Console

### What is the Admin Console?

The Admin Console (product name **EMS SolarFlow Admin**) is a local browser tool
for setup, device discovery, maintenance, updates and backups. EMS still owns the
control logic. See [Admin Console](admin-console.md).

### Do I have to use the Admin Console?

No. It is recommended for most users, but you can also use the Docker Bootstrap
installer with Docker commands and `emsctl.py`.

### How do I start the Admin Console?

Run the installer:

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
backups. See the [Admin setup guide](admin-setup.md) and
[Admin maintenance guide](admin-maintenance.md).

### Does the Admin Console overwrite my config?

Not silently. Config changes are previewed and confirmed before writing, and any
existing config is backed up first.

## Operating models

### Which setup path should I choose?

There are three. Use the **Admin Console** if you want a browser-guided setup
with device discovery and maintenance. Use **Docker Bootstrap** if you prefer
copy/paste shell commands without the browser wizard. Use the **appliance
image** if you want a Raspberry Pi that does nothing else and manages itself.
The first two use the same `config/config.json` layout, so you can switch later.
See the [Docker Bootstrap guide](docker-bootstrap.md) and the
[appliance guides](appliance/index.md).

### What is Developer Setup?

Developer Setup is a Git checkout with a local Python environment, for
development, debugging and contributing. It is **not** the normal user path. See
the [Developer Setup guide](../developer/developer-setup.md).

## Appliance image

### What is the appliance image?

A ready-made Raspberry Pi system that runs EMS and nothing else: operating
system, containers, an update mechanism that keeps a second copy of the system
so a bad update can fall back, and a small web interface to drive all of it. You
flash one card. See the [appliance guides](appliance/index.md).

### Which Raspberry Pi do I need?

A Raspberry Pi 4 or 5 for the fail-safe two-slot image, and a card of 32 GB or
larger. The images for the two boards are not interchangeable.

A Raspberry Pi 3 or 3B+ is built the **single-slot** image instead — one root
patched in place, 16 GB card — because the two-slot image needs a boot chain a
Pi 3 does not have. Nobody has booted it on one yet, and 1 GB of RAM against
Docker, EMS and InfluxDB is unmeasured. Anything older than a Pi 3 cannot run
any of them.

### Has anyone run it on a real Pi?

Not yet. It is built and exercised automatically on every change, but it is
**not confirmed on physical hardware** — the same wording this project uses for
the Zendure Hub and Hyper generations. See
[what that means](appliance/index.md#what-not-confirmed-means), and
[if you are the first](appliance/index.md#if-you-are-the-first) if you try one.

### Do I really need an Ethernet cable?

For the first start, yes. There is no way to put WLAN credentials on the card
beforehand; you set up WLAN afterwards from the appliance itself. See
[Network](appliance/network.md).

### How do I reach it?

`http://ems-solarflow.local:8088`, or the address your router lists for a device
called `ems-solarflow`. The first page asks you to choose a password. See
[First start](appliance/first-start.md).

### Can I install other software on it?

No. The card is managed as a whole, so anything installed by hand disappears at
the next system update.

### What happens if an OS update fails?

The new system is written into a second slot and booted on trial. A trial that
does not become healthy falls back to the slot that was working. Configuration
and data live on a separate partition and survive both directions. See
[Updates](appliance/updates.md).

### What happens if an Appliance Manager update fails?

The appliance keeps the package it was running and arms a deadline before the
new one is unpacked. If the new manager does not report itself healthy in time,
the previous package is installed again by itself.

That covers the Appliance Manager and nothing else. It does **not** cover the
kernel, the firmware or the operating system: a single-slot appliance is
patched in place and there is no second slot to fall back into. If a kernel
upgrade leaves the board unable to boot, the way back is a keyboard at the
console, and failing that, re-flashing the card. That is a deliberate trade and
it is written down in
[the decision record](../appliance/adr/manager-self-update.md).

### Where is my config on the appliance?

Under `/opt/ems-solarflow`, on the shared partition that survives system
updates. The normal way to reach it is the appliance's SSH backup export rather
than a login. There *is* a console rescue account for when the appliance will
not come up — see [When it stops working](appliance/recovery.md) — but it is a
last resort, not the everyday path. See [Backups](appliance/backup.md).

### It does not come up at all. What now?

Two things are readable without the network. Three of the card's six partitions
are FAT, so any computer opens them and can show which slot the firmware chose.
And the appliance narrates its whole start-up on a serial line, which is the
only way to see *why* a boot failed. Both are in
[When it stops working](appliance/recovery.md).

## Config and files

### Where is my config?

`config/config.json`

### Where is my data?

`data/`

### Where are backups?

`data/backups/` by default. Docker users see that folder on the host via the
existing `./data:/app/data` mount; no separate backup volume is needed.

### What is `data/admin/`?

`data/admin/` holds the Admin Console's own state: temporary files, logs and
backup-set metadata. It is not a second EMS config and not a live EMS runtime
layout.

### I already have `config.json` in the project root. What should I do?

That is the legacy layout. New Docker and Admin Console setups use
`config/config.json`. The Admin Console routes a legacy root config to
Maintenance and offers to migrate it. See the
[config layout guide](config-layout.md).

## Updates and backups

### What should I do before updating?

If you use the Admin Console, open **Maintenance** and create a backup before
starting **Guided upgrade**. The upgrade flow shows the plan, runs checks, and
asks for confirmation before changing the system.

For Docker Bootstrap or advanced shell use, create a backup and run diagnostics
before updating:

```bash
docker compose exec ems python3 emsctl.py backup create --type config
docker compose exec ems python3 emsctl.py diagnose
```

### How do I make sure EMS and the Admin Console really use the latest Docker image?

Pull the newest image **and** recreate the container. A pull alone does not
restart a running container, so it keeps running the old image.

Update EMS:

```bash
docker compose pull
docker compose up -d --force-recreate
```

Or, if your Compose version supports it, in one step:

```bash
docker compose up -d --pull always --force-recreate
```

Update the Admin Console separately, using its own compose file:

```bash
docker compose -f docker-compose.admin.yml pull
docker compose -f docker-compose.admin.yml up -d --force-recreate
```

What these do:

- `docker compose pull` downloads the latest image for the configured tag.
- `docker compose up -d --force-recreate` recreates the container so it actually
  uses the newly pulled image.

If you use `:latest`, the tag name stays the same while the image digest may
change. Pulling alone is not enough — the container must be recreated.

### Why do I see "Found orphan containers ([ems-solarflow-admin])"?

This happens when EMS and the Admin Console are managed by **separate compose
files**. Running `docker compose up -d` from the EMS `docker-compose.yml` does
not know about the Admin Console container, so Compose reports it as an orphan:

```text
WARN Found orphan containers ([ems-solarflow-admin]) for this project.
```

It does not automatically mean something is broken.

Do **not** run `--remove-orphans` unless you intentionally want Compose to remove
containers that are not part of the current compose file. If the Admin Console is
listed as an orphan and you still want to keep it, leave it alone and update it
separately with `docker-compose.admin.yml` (see the previous entry).

### How do I check which image a container is actually running?

List running containers with their image and status:

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

Show the images used by the current compose project:

```bash
docker compose images
```

Compare the configured image tag with the resolved image digest per container:

```bash
docker inspect ems-solarflow-api-control --format '{{.Config.Image}} {{.Image}}'
docker inspect ems-solarflow-admin --format '{{.Config.Image}} {{.Image}}'
```

If a container still shows an old image digest after an update, recreate it with
`--force-recreate` as shown above.

### Are Admin Console backups normal EMS backups?

Yes. The Admin Console uses the EMS backup tooling. Backup archives live in
`data/backups/` by default. See the
[Admin backup and restore guide](admin-backup-restore.md).

### Can I create encrypted backups in the Admin Console?

Yes. Provide a password when creating the backup. Without the password, an
encrypted backup cannot be restored.

### Can I delete backups in the Admin Console?

Yes, when the Backup / restore page offers the delete action. Deletion always
requires confirmation and only removes archives inside the backup directory.

### Can I restore InfluxDB from the Admin Console?

Yes, for **bundled** InfluxDB. The Admin Console orchestrates the existing EMS
CLI restore flow (`emsctl.py backup restore`) instead of implementing a separate
InfluxDB restore engine: it previews with the EMS CLI dry-run, replaces bundled
analytics data on explicit confirmation, and lets the EMS CLI own the rollback.
External InfluxDB is not covered by EMS backup/restore.

## Security

### Is the Admin Console safe to expose to the internet?

No. Run it only on a trusted local network, or behind a deliberate reverse-proxy
and auth setup. A deployment-capable Admin container controls the host Docker
engine, which is effectively root-equivalent.

### Is the appliance web interface safe to expose to the internet?

No, for the same reason. It serves plain HTTP on your local network, so anyone
who can reach its port sees the login page — which is why the password you set
on the first start matters even at home. It manages the host it runs on.

### Does the Admin Console use the EMS Dashboard password?

Yes. The Admin Console uses the same password file as the EMS Dashboard:
`config/dashboard-auth.json`.

On first start, if no password exists yet, the first browser user creates it.
After that, use the EMS Dashboard password to log in.

## Troubleshooting

### The dashboard is not reachable. What should I check?

If you installed through the Admin Console, first check the Maintenance overview
for container state and dashboard link.

For Docker Bootstrap or shell checks:

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

If the Admin Console offers a support bundle action, use that first. On the
appliance, use **Support archive** in its own web interface.

For Docker Bootstrap or advanced shell use:

```bash
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

## General

### Do I need Home Assistant?

No. Home Assistant is optional.

### Do I have to use Docker?

No, but Docker is recommended for normal users. The appliance image runs the
same containers, without asking you to manage Docker yourself.

### Which grid meters are supported?

Shelly, Shelly 3EM Gen1, EcoTracker, and Tasmota HTTP setups are documented in
[supported-setups.md](supported-setups.md).

### How does EMS reach my Zendure devices?

Over one of three connections: **Local API** (HTTP to the device's own address),
**Local MQTT** (your own broker) or **Zendure cloud MQTT**. Which ones a given
model supports, and how far each is validated, is in
[supported-setups.md](supported-setups.md).

### Can I use multiple Zendure inverters?

Yes. Each configured device needs a serial number, suitable limits, and a way to
be reached — an address for Local API, or a broker and its own device id for
either MQTT path. Devices on different connections can be mixed.

### Can I keep using the Zendure app?

Read-only use is usually fine. Avoid running another controller that writes
Zendure `outputLimit`.

### Does EMS need internet or cloud access?

Control is local-first: with Local API or Local MQTT devices, nothing about a
control decision leaves your network. Docker image pulls, updates and optional
support workflows need internet access. The one exception is a device you
deliberately configure on **Zendure cloud MQTT** — that connection is the cloud,
by definition.

### Is native Python still supported?

Yes. It is documented as an advanced/manual setup in
[native-python.md](../native-python.md).

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

If you changed config through the Admin Console, follow the shown restart and
diagnostic result.

For Docker Bootstrap or manual file edits, restart EMS and run diagnostics:

```bash
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

The [first-run checklist](../first-run-checklist.md) is a good next step after a
larger manual config edit.

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
type you use. For the appliance, the report that helps most is described under
[if you are the first](appliance/index.md#if-you-are-the-first).
