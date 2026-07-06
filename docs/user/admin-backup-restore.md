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

The backup directory is inside the EMS install root — this keeps archived paths
resolvable, but it does not put backups out of harm's way. By default backups
live under `data/backups/`. If you manually delete `data/`, you also delete
local backups. Export important backups before a manual reset.

## What the Admin Console can restore

Admin Console restore supports **config**, **database** and **bundled InfluxDB**
archives.

Config and database archives are restored directly through the EMS backup core
(the generic file restore path). **Bundled InfluxDB** archives are different:
InfluxDB has a dedicated restore flow that must never be pushed through the
generic file restore path. The Admin Console does not reimplement it — it
**orchestrates the existing EMS CLI restore flow** (`emsctl.py backup restore`,
see [../backup-restore.md](../technical/backup-restore.md)) instead. Admin runs the
EMS CLI in the current EMS context (the running EMS container, or a one-off
compose container) and lets the EMS CLI own the InfluxDB restore and its
rollback.

- **External InfluxDB** is not covered by EMS backup/restore. Only bundled
  InfluxDB can be backed up and restored; an external InfluxDB restore is
  rejected with a clear message.
- **Restore is replace-style.** A bundled InfluxDB restore replaces the current
  bundled analytics data (`--on-conflict replace`).
- **Preview and confirmation are required.** The preview runs the EMS CLI
  dry-run (`emsctl.py backup restore … --dry-run`); if it fails, the plan is
  blocked and the EMS CLI error is shown. Nothing is restored until you
  explicitly confirm.
- **Rollback is enabled by default.** When rollback is selected, Admin passes
  `--rollback` and the EMS CLI creates and owns the InfluxDB rollback backup
  before restoring. Selecting "no rollback" passes `--no-rollback`. Admin never
  copies InfluxDB files to build its own rollback.
- **Encrypted InfluxDB archives** are restored by entering the backup password;
  Admin feeds it to the EMS CLI over stdin for that one command and never logs
  or persists it. If the password cannot be handled safely the restore is
  blocked with a clear message.

A **system set** that contains config + databases + a bundled InfluxDB member is
no longer blocked as a whole. Each member is restored through the right path:
config and databases through the generic restore path, and the InfluxDB member
through the EMS CLI restore flow. The InfluxDB member is applied last. If the
InfluxDB dry-run preview fails, the whole system-set restore is blocked rather
than silently skipping the member.

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
- For **bundled InfluxDB** the rollback is owned by the EMS CLI: Admin passes
  `--rollback`/`--no-rollback` and the EMS CLI creates the InfluxDB rollback and
  restores it if the InfluxDB restore fails. Admin never rolls InfluxDB back by
  copying files.

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
