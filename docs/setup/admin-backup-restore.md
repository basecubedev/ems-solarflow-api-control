# Admin Backup / Restore

The **Backup / restore** path under Maintenance (see
[admin-maintenance.md](admin-maintenance.md)) is a preview-first workflow for
creating, inspecting and restoring backups from the Admin UI. It is a
*maintenance* flow for an existing system — not part of Fresh Install.

Admin is orchestration/UI only. It never invents a new backup format: every
archive is a normal EMS backup archive created and read through the EMS Core
helpers (the same ones behind `emsctl.py backup`, documented in
[../backup-restore.md](../backup-restore.md)). Optional Admin "backup set"
metadata only groups existing EMS archives — it is not a new artifact.

## Backup types

- **Config** — `config.json`, runtime state and the dashboard auth/cert/key
  files (plus the bundled InfluxDB secret when applicable).
- **Databases** — consistent snapshots of the local SQLite databases.
- **InfluxDB** — bundled InfluxDB data, offered only when bundled InfluxDB is
  enabled. Disabled or external InfluxDB is skipped with a clear note, not an
  error.
- **System set** — a grouped backup that contains config + databases (+ bundled
  InfluxDB when supported). The set is Admin metadata that references the normal
  EMS archives.

Every created backup is verified before the job reports success: the archive
must exist inside the backup directory, be non-empty, have a readable manifest
of the requested type, and pass its internal checksums.

## Where backups live

Backups live in the EMS backup directory, `data/backups/`, by default. Admin
backup-set metadata lives under `data/admin/backups/sets/`. The status stage
warns if the backup directory is outside the install root or if archives are
locked (encrypted) or invalid.

## What Admin can restore

Admin restore currently supports **config** and **database** archives. InfluxDB
backups can be created, listed, inspected and deleted from Admin, but **InfluxDB
restore is intentionally blocked** in the Admin UI: InfluxDB has a dedicated
EMS/CLI restore flow and must never be pushed through the generic file restore
path. Until an EMS-tool-backed InfluxDB restore runner is wired in, use the EMS
CLI (`emsctl.py backup`, see [../backup-restore.md](../backup-restore.md)) to
restore InfluxDB backups.

The block is enforced in the backend, not just hidden in the UI: an InfluxDB
archive cannot enter a restore preview, no restore plan containing an InfluxDB
target can execute, and a **system set** that contains an InfluxDB member is
blocked as a whole (the UI does not yet offer per-member exclusion, so blocking
is safer than silently skipping a member). Restore the set's config/database
members individually instead.

## Restore is preview-first

A restore always starts with a **preview** (a dry run). The preview reports,
per file, whether the restore would create a new file, replace a conflicting
file, or skip an identical file, and blocks up front on invalid checksums. No
files are written during a preview.

Conflict handling is explicit. With the default **Abort on conflicts** policy,
a preview that finds conflicts is blocked until you choose **Replace** or
**Keep**. Restore requires an explicit confirmation before it runs.

## Rollback and automatic rollback

- A **rollback backup** of the current state is created before any file is
  written. This is on by default. If the rollback backup cannot be created, the
  restore does not start.
- **Automatic rollback** (on by default for config/database restores) undoes the
  restore if a post-restore check fails, returning the system to its
  pre-restore state. If the automatic rollback itself fails, the job reports
  that manual recovery is required and names the rollback archive.

## Encrypted backups

Encrypted backups appear **locked** in the list and details until you supply a
password. Inspecting or restoring an encrypted backup requires the password;
it is used for that request only and is never logged or persisted. (Creating a
new encrypted backup from Admin is planned; existing encrypted backups can be
inspected and restored today.)

## Delete

Deletion always requires explicit confirmation and can only remove backups
resolved through a server-side safe lookup inside the backup directory —
traversal, absolute paths, symlinks and unknown files are rejected. Deleting a
backup set asks whether to remove only the grouping metadata or the metadata
and its member archives.

## No hidden EMS image downgrade

Restore only restores data/config files after compatibility checks. It never
installs or switches to an older EMS image and never changes `docker-compose.yml`
or containers. A backup's recorded source version/commit is informational only.
After a restore, EMS may need a restart/recreate to use the restored files; the
UI says so rather than restarting anything silently.
