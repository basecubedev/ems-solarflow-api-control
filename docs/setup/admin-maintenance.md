# Manage My Existing System (Admin Maintenance)

The Admin UI offers exactly two flows. This page covers the second one,
**Manage my existing system** — the counterpart to
[admin-setup.md](admin-setup.md) (Set up a new system). Use it to update,
inspect, edit, or back up an installation that already exists.

Admin is orchestration/UI only; the EMS core stays the source of truth. All
maintenance actions reuse the same EMS Core helpers and the standard
`config/config.json` layout — Admin never invents a second config.

## Opening Maintenance

The Admin start/router screen detects the current install state and recommends
the safest flow. Any existing, legacy, partial, or Admin-prepared install is
routed to **Manage my existing system** and never auto-starts the setup wizard
(see [admin-setup.md](admin-setup.md)). A legacy root `config.json` is offered a
migration to `config/config.json` first (see [config-layout.md](config-layout.md)).

The Maintenance hub presents three paths:

| Path | What it is | Status |
|---|---|---|
| **Guided upgrade** | Planned upgrade workflow with backup and diagnostics | Recommended |
| **Manual configuration / existing system** | Inspect, edit, and restart an existing EMS setup | Available |
| **Backup / restore** | EMS-owned backup and restore workflow | Planned |

## Guided upgrade (recommended)

Guided upgrade moves a running installation to a newer EMS release. It is a
**conservative, single-shot** workflow: it only ever pulls the target image,
bumps the EMS image reference in your `docker-compose.yml`, and force-recreates
the `ems` service. It **never** removes containers, volumes, or data, and never
runs `docker compose down`/`rm`/`down -v`.

### What runs, in order

The planning page lets you choose which optional steps run; the fixed order is:

1. **Verify target image** — always runs, before any mutating step. Resolves the
   target image's build identity (pulling it if it is not local yet) and
   compares it against the running EMS build. A target that is the same image
   (no-op), older than the running build (downgrade), or whose identity cannot be
   verified aborts the run here — no backup, config, compose or container change
   is made. Older `v0.6.x` images without build-identity labels are still allowed
   when SemVer proves the upgrade (the step records a SemVer-fallback warning);
   only a genuinely unprovable move (a running `latest` versus an unlabeled
   stable) is blocked, unless the `ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES=true`
   test override is set, which allows it with a clear warning.
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
prepared in the setup/release step, and the same build-identity gate that
governs release selection applies here — a target that is not a real upgrade is
refused in the backend, not just hidden in the UI. Because a stable target may
not be pulled locally yet when the running EMS is `latest`, the **Verify target
image** step pulls and inspects it before any change, so the `latest`-vs-stable
decision uses the build serial rather than defaulting to blocked. Downgrades
belong to the Backup/restore flow. For the full upgrade-vs-downgrade and build-identity gating
rules (`already_current`, `downgrade_blocked`, `upgrade_available`,
`identity_unknown`, and how `latest` is treated as a channel), see the release
selection section in [../admin-discovery.md](../admin-discovery.md).

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

A dedicated EMS-owned backup/restore workflow is **planned** and not yet a
selectable action inside the Admin UI. Today, the pieces that write files back
up the single file they replace before writing it:

- Guided upgrade's optional backup step creates a real EMS Core backup.
- Config apply and deployment prepare/upgrade back up the file they replace to
  `data/admin/backups/`.

There is no full snapshot/rollback yet. For the current, supported backup and
restore procedure (including encrypted backups and dry-run restore checks), use
the EMS CLI as described in [../backup-restore.md](../backup-restore.md).

## Safety

- The deployment-capable Admin container controls the **host** Docker engine
  over the mounted `/var/run/docker.sock`. That grants effectively
  root-equivalent control of the host — run Admin only on a trusted local
  machine and never expose the Admin UI to the internet (see the security notes
  in [../admin-discovery.md](../admin-discovery.md)).
- The overview is strictly read-only. The only maintenance actions that change
  anything are an explicit config **apply** and a confirmed **guided upgrade**,
  and both back up what they replace before writing.
