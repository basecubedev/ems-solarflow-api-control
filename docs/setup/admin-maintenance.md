# Admin Console: Manage my existing system

Use Maintenance for an existing EMS installation. The Admin Console offers
exactly two flows; this page covers the second one, **Manage my existing
system** — the counterpart to [admin-setup.md](admin-setup.md) (Set up a new
system). Use it to update, inspect, edit, or back up an installation that already
exists.

Maintenance reads the real system state. It can inspect Docker, config, backups
and diagnostics. It never silently replaces config or deletes data.

The Admin Console is orchestration/UI only; the EMS core stays the source of
truth. All maintenance actions reuse the same EMS Core helpers and the standard
`config/config.json` layout — the Admin Console never invents a second config.

## Opening Maintenance

Start the Admin Console and open `http://127.0.0.1:8090` (see
[admin-setup.md](admin-setup.md#start)). The start/router screen detects the
current install state and recommends the safest flow. Any existing, legacy,
partial, or Admin-prepared install is routed to **Manage my existing system** and
never auto-starts the setup wizard. A legacy root `config.json` is offered a
migration to `config/config.json` first (see [config-layout.md](config-layout.md)).

The Maintenance hub presents three paths:

| Path | What it is | Status |
|---|---|---|
| **Guided upgrade** | Plan and validate an EMS update | Recommended |
| **Manual configuration / existing system** | Inspect, edit, and restart an existing EMS setup | Available |
| **Backup / restore** | Create, inspect, restore or delete EMS backups | Available |

Maintenance is for existing systems and is conservative by default. It reads the
host Docker state, your `docker-compose.yml`, and `config/config.json`. It can
update EMS, edit config, run diagnostics, and manage backups. It never silently
deletes runtime data.

## Guided upgrade (recommended)

Guided upgrade moves a running installation to a newer EMS release. It is a
**conservative, single-shot** workflow: it only ever pulls the target image,
bumps the EMS image reference in your `docker-compose.yml`, and force-recreates
the `ems` service. It **never** removes containers, volumes, or data, and never
runs `docker compose down`/`rm`/`down -v`.

### What runs, in order

The planning page lets you choose which optional steps run; the fixed order is:

1. **Verify target image** — always runs, before any mutating step. It confirms
   the target is a real upgrade of the running EMS build (pulling the target
   image first if it is not local yet). A target that is the same image (no-op),
   older than the running build (downgrade), or whose identity cannot be verified
   aborts the run here — no backup, config, compose or container change is made.
   For the full build-identity and SemVer-fallback rules, see the release
   selection section in [../admin-discovery.md](../admin-discovery.md).
2. **Preflight checks** — always runs. Confirms the target is the currently
   prepared release, and that `config/config.json` and `docker-compose.yml`
   exist. Fast, side-effect-free — a failed check rejects the upgrade before any
   change is made.
3. **Create backup** (optional) — runs the real EMS Core backup through the best
   available EMS tool context (a running EMS container, otherwise a one-off
   Compose container), so you get the normal encryptable EMS backup format, not
   an Admin-side file copy.
4. **Check config** — compares your config against the prepared release's
   template and reports how many keys are missing and how many comment updates
   are available. Runs when you request a config check, and always when a config
   write is requested.
5. **Update config** (optional) — adds missing keys and/or refreshes template
   comments against the prepared target release template, then writes atomically
   through the EMS Core config helpers. If nothing changed, the step is skipped.
   Existing values are never overwritten; only missing keys are added. If you
   change config without also taking a backup, the run records a warning.
6. **Pull image** (optional) — pulls the target EMS image
   (`ghcr.io/basecubedev/ems-solarflow-api-control:<tag>`).
7. **Update compose** — always runs. Rewrites the EMS image reference in
   `docker-compose.yml` (a `.bak` copy is written first). If Compose already
   targets the release image, the step is skipped. The bundled InfluxDB image is
   never touched.
8. **Recreate EMS** (optional) — `docker compose up -d --force-recreate ems`
   only. InfluxDB and other services are left running.
9. **Run diagnostics** (optional) — runs `emsctl diagnose` after the upgrade.
   Diagnostics never fail the applied upgrade; failures/warnings are surfaced as
   warnings on the finished job.

Progress is reported live per step (pending → running → done/skipped/failed).

### Upgrade-only guarantee

Guided upgrade only ever moves **forward**. The target must be the release you
prepared in the setup/release step, and a target that is not a real upgrade is
refused in the backend, not just hidden in the UI. Downgrades belong to the
Backup / restore flow. For the full upgrade-vs-downgrade and build-identity
gating rules, see the release selection section in
[../admin-discovery.md](../admin-discovery.md).

## Manual configuration / existing system

This path inspects and edits an existing installation. It has two parts.

### Read-only overview

The overview **never builds, starts, stops, or mutates anything**. If the host
Docker engine is unavailable it degrades to file/config/compose facts instead of
breaking. It reports:

- **Install state** — classified as one of: standard installation,
  Admin-prepared installation, config without compose, legacy root config,
  compose without config, partial installation, or no installation found. A
  partial state is inspectable but shows a clear warning that repair actions are
  not part of this read-only overview.
- **Installation layout** — the resolved `config/config.json`, `data/`, and
  `docker-compose.yml` paths, and whether each exists.
- **Runtime containers** — the EMS and InfluxDB container name, declared/actual
  image, tag, and running status (read from the host Docker engine when
  available; otherwise reported as `unknown`).
- **Dashboard link** — a link to the local dashboard, using the port/scheme from
  your `config.json` (`http://localhost:8080` by default).

### Config editor

The editor loads your real resolved `config/config.json` as an **in-memory
draft** you can edit, then shows a bounded diff **preview**. Preview is
non-destructive — nothing is written by editing or previewing.

Applying the prepared draft is the one action that writes the real config. It
reuses the shared atomic config-apply service: it validates the merged config,
backs up any existing config to `data/admin/backups/` first, then writes
atomically. Apply touches only `config.json` — it never moves `data/`,
`docker-compose.yml`, or runtime databases.

## Backup / restore

The Maintenance hub's **Backup / restore** path is a full, preview-first
workflow that orchestrates the EMS-owned backup tooling. It lets you create
config / database / system backups, inspect what is inside a backup, preview a
restore before anything is written, and restore behind an automatic rollback
backup. See [admin-backup-restore.md](admin-backup-restore.md) for the full
reference.

Admin Console restore currently supports **config** and **database** archives.
InfluxDB backups can be created, listed, inspected and deleted, but **InfluxDB
restore is intentionally blocked in the Admin Console** until the dedicated EMS
InfluxDB restore runner is wired in — use the EMS CLI to restore InfluxDB backups
for now. A system set that contains an InfluxDB member is blocked for restore as a
whole; restore its config/database members separately.

The Admin Console never invents a new archive format: every backup is a normal
EMS backup archive created through the EMS Core helpers. The CLI equivalents are
documented in [../backup-restore.md](../backup-restore.md).

## Safety

- The deployment-capable Admin container controls the **host** Docker engine
  over the mounted `/var/run/docker.sock`. That grants effectively
  root-equivalent control of the host — run Admin only on a trusted local
  machine and never expose the Admin Console to the internet (see the security notes
  in [../admin-discovery.md](../admin-discovery.md)).
- The overview is strictly read-only. The maintenance actions that change
  anything are an explicit config **apply**, a confirmed **guided upgrade**, and
  the **Backup / restore** actions — **create backup**, **delete backup**, and a
  confirmed **restore** of a config/database backup. Config apply, guided upgrade
  and restore all back up what they replace before writing. Restore is
  preview-first and confirmed; InfluxDB restore is blocked in Admin (see
  [admin-backup-restore.md](admin-backup-restore.md)).
