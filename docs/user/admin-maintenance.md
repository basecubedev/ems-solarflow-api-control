# Admin Maintenance

Use Maintenance for an existing EMS installation. This is the second of the two
Admin Console flows; the first is [Admin setup](admin-setup.md) (Set up a new
system).

## What Maintenance does

- detects the current installation
- checks config and containers
- runs diagnostics
- creates backups
- guides updates
- helps with restore

Maintenance reads the real system state. It never silently replaces config or
deletes data. The Admin Console is orchestration and UI only; the EMS core stays
the source of truth and owns the standard `config/config.json` layout.

## Opening Maintenance

Start the Admin Console and open `http://127.0.0.1:8090` (see
[Admin setup](admin-setup.md#start)). The start screen detects the current
install and recommends the safest flow. Any existing installation is routed to
**Manage my existing system**; the setup wizard is never started automatically.
A legacy root `config.json` is offered a migration to `config/config.json` first
(see [config-layout.md](config-layout.md)).

Maintenance offers three paths:

| Path | What it is |
|---|---|
| **Guided upgrade** | Plan and apply an EMS update (recommended) |
| **Manual configuration** | Inspect, edit, and restart an existing EMS setup |
| **Backup / restore** | Create, inspect, restore, or delete EMS backups |

## Guided upgrade

1. Choose the target version.
2. Review the plan.
3. Create a backup.
4. Check config compatibility.
5. Apply the update.
6. Run diagnostics.
7. Review the result.

Guided upgrade only ever moves **forward**. It updates EMS to a newer release;
it never removes containers, volumes, or data. Downgrades belong to the
Backup / restore flow.

The Admin Console asks for confirmation before changing config, compose files,
containers, or data. If you change config without also taking a backup, the run
records a warning.

### Updating the Admin Console

During Guided upgrade, the Admin Console may update itself before updating EMS.
The page will show a reconnect screen and then continue from the saved plan.

If you are asked to log in again after the Admin Console restart, use the same
EMS Dashboard/Admin password. The pending upgrade will be shown again after login.

The plan shows whether an Admin update is needed for the selected release:

- **Admin Console image unchanged for this release** — nothing to do; the EMS
  upgrade can proceed.
- **Admin Console update required before EMS upgrade** — click **Update Admin
  Console**. The page shows a reconnect screen while the Admin Console restarts
  on the new image, then offers **Continue EMS upgrade?**. A required Admin update
  blocks the EMS upgrade until it completes — the block is enforced by the server,
  not only hidden in the page.
- **Admin update requires Docker access** — the Admin Console is running in
  discovery-only mode (no Docker socket) and cannot update itself. Reinstall it in
  deployment mode to enable Guided upgrade.

The Admin update only replaces the Admin Console container. It never touches your
EMS config, EMS data, or the EMS container — those changes only happen later in
the Guided EMS Upgrade, after you confirm them.

If the new Admin Console does not come back on its own, check its logs and start
it again:

```bash
docker compose -f docker-compose.admin.yml logs
docker compose -f docker-compose.admin.yml up -d
```

## Manual configuration

This path inspects and edits an existing installation.

- **Overview** is read-only. It shows the install state, the resolved
  `config/config.json`, `data/`, and `docker-compose.yml` paths, the EMS and
  InfluxDB containers, and a link to the local dashboard
  (`http://localhost:8080` by default). It never builds, starts, stops, or
  changes anything.
- **Config editor** loads your real config as a draft you can edit, then shows a
  preview of the changes. Nothing is written by editing or previewing. Applying
  the draft is the one action that writes config — it validates the change,
  backs up the current config first, then writes it. Apply only touches
  `config.json`; it never moves `data/`, `docker-compose.yml`, or databases.

## Backup / restore

The **Backup / restore** path creates config, database, and system backups,
inspects what is inside a backup, previews a restore before anything is written,
and restores behind an automatic rollback backup. See
[Backup and restore](admin-backup-restore.md) for the full workflow.

Restore currently supports **config** and **database** backups. InfluxDB backups
can be created, listed, inspected, and deleted, but InfluxDB restore is done with
the EMS CLI for now.

## Safety

Maintenance is conservative by default:

- **Backup before risky changes.** Config apply, guided upgrade, and restore all
  back up what they replace before writing.
- **No silent downgrade.** Guided upgrade only moves forward; restore never
  installs an older EMS image.
- **Preview before restore.** A restore always starts with a preview and is only
  applied after you confirm it.
- **Confirmation before writes.** The Admin Console asks before it changes
  config, compose files, containers, or data.
- **Diagnostics after update.** Run diagnostics after an upgrade and review the
  result.

Run the Admin Console only on a trusted local machine and never expose it to the
internet — it controls Docker on the host.

## Advanced details

For version detection, release checks, upgrade gating, and Docker execution
details, see the [Admin technical reference](../technical/admin-discovery.md).
The CLI equivalents for backup and restore are documented in
[backup-restore.md](../technical/backup-restore.md).
