# Admin Console: Backup / restore

The **Backup / restore** path under Maintenance (see
[admin-maintenance.md](admin-maintenance.md)) is a preview-first workflow for
creating, inspecting and restoring backups from the Admin Console. It is a
*maintenance* flow for an existing system — not part of Set up a new system.

The Admin Console is orchestration/UI only. It never invents a new backup format:
every archive is a normal EMS backup archive created and read through the EMS Core
helpers (the same ones behind `emsctl.py backup`, documented in
[../backup-restore.md](../technical/backup-restore.md)). A **backup set** is optional Admin
metadata that groups existing EMS archives (config + databases, plus bundled
InfluxDB when supported) — it is not a new artifact.

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

The actual backup archives live in the EMS backup directory, `data/backups/`, by
default. The Admin Console keeps only its own metadata under `data/admin/`.

| Path | What it holds |
| --- | --- |
| `data/backups/` | EMS backup archives (the real `.tar.gz` / `.tar.gz.enc` files) |
| `data/admin/` | Admin Console state, temporary files, logs and backup-set metadata |

Admin backup-set metadata lives under `data/admin/backups/sets/`; the archives it
references still live in `data/backups/`. The status stage warns if the backup
directory is outside the install root or if archives are locked (encrypted) or
invalid.

## What the Admin Console can restore

Admin Console restore currently supports **config** and **database** archives.
InfluxDB backups can be created, listed, inspected and deleted from the Admin
Console, but **InfluxDB restore is intentionally blocked** in the Admin Console:
InfluxDB has a dedicated EMS/CLI restore flow and must never be pushed through the
generic file restore path. Until an EMS-tool-backed InfluxDB restore runner is
wired in, use the EMS CLI (`emsctl.py backup`, see
[../backup-restore.md](../technical/backup-restore.md)) to restore InfluxDB backups.

The block is enforced in the backend, not just hidden in the UI: an InfluxDB
archive cannot enter a restore preview, no restore plan containing an InfluxDB
target can execute, and a **system set** that contains an InfluxDB member is
blocked as a whole (the UI does not yet offer per-member exclusion, so blocking
is safer than silently skipping a member). Restore the set's config/database
members individually instead.

## Restore is preview-first

**Preview-first** means a restore always starts with a **preview** (a dry run).
The preview reports, per file, whether the restore would create a new file,
replace a conflicting file, or skip an identical file, and blocks up front on
invalid checksums. No files are written during a preview.

**Replace-on-conflict**: because a restore normally replaces the current state
with the backup state, Admin Console restore treats differing files as replace
candidates. The preview lists those files and counts them under "Will replace".
Nothing is written until you explicitly confirm the restore. The lower-level EMS
restore tooling (`emsctl.py backup`, see
[../backup-restore.md](../technical/backup-restore.md)) still supports stricter conflict
policies for CLI/advanced workflows.

## Rollback and automatic rollback

- A **rollback backup** of the current state is created before any file is
  written. This is on by default. If the rollback backup cannot be created, the
  restore does not start.
- **Automatic rollback** (on by default for config/database restores) undoes the
  restore if a post-restore check fails, returning the system to its
  pre-restore state. If the automatic rollback itself fails, the job reports
  that manual recovery is required and names the rollback archive.

## Encrypted backups

Yes — the Admin Console can **create** encrypted backups: supply an encryption password when you
create the backup. Encrypted backups then appear **locked** in the list and
details until you supply the password again. Inspecting or restoring an encrypted
backup requires the password; it is used for that request only and is never
logged or persisted. Without the password, an encrypted backup cannot be
restored.

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
