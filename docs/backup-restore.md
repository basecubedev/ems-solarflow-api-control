# Backup and Restore Guide

This is the practical, step-by-step guide for backing up and restoring your EMS
setup. For the full command reference (every flag and the exact archive format)
see [Backup / Restore](cli.md#backup--restore).

All commands run from the project directory. Docker backups are written to
`data/backups/` on the host. Native Python backups are written to `./backup/`.
Docker commands are shown first for common workflows. For detailed restore
examples later in this page, Docker users can run the same `emsctl.py` command
inside the service, for example:

```bash
docker compose exec ems python3 emsctl.py backup restore /app/data/backups/example.tar.gz --dry-run
```

## When should I create a backup?

Create a backup:

- **Before an update** (pulling new code, migrating config).
- **Before changing `config.json`** in a big way.
- **Before moving the setup** to another device.
- Now and then, so you have recent local history to restore.

## Quick start: backup before an update

Run these before an update to capture config, local history and (if you use
bundled InfluxDB) analytics history:

Docker:

```bash
docker compose exec ems python3 emsctl.py backup create --type config --password
docker compose exec ems python3 emsctl.py backup create --type databases --password
docker compose exec ems python3 emsctl.py backup create --type influxdb --password
```

Native Python:

```bash
python3 emsctl.py backup create --type config --password
python3 emsctl.py backup create --type databases --password
python3 emsctl.py backup create --type influxdb --password
```

`--password` encrypts each backup. You will be asked for a password (entered
twice). **Keep the password safe — without it the encrypted backup cannot be
restored.**

If you do not want password protection, drop `--password`:

Docker:

```bash
docker compose exec ems python3 emsctl.py backup create --type config
docker compose exec ems python3 emsctl.py backup create --type databases
docker compose exec ems python3 emsctl.py backup create --type influxdb
```

Native Python:

```bash
python3 emsctl.py backup create --type config
python3 emsctl.py backup create --type databases
python3 emsctl.py backup create --type influxdb
```

What to keep in mind when deciding:

- **config** backups may contain credentials or tokens (the dashboard auth
  file, TLS key, and the bundled InfluxDB secret). Encrypt these if you store
  them anywhere shared.
- **databases** and **influxdb** backups hold local energy/runtime history. This
  is not a secret like a password, but it can reveal usage patterns, so it can
  be privacy-relevant.
- You decide whether password protection is needed for your situation.

If you only run the bundled InfluxDB, the `influxdb` backup is the one that
preserves your analytics history. If you do not use bundled InfluxDB, you can
skip it.

## Which backup type do I need?

| Backup type | What it contains | Typical use |
|---|---|---|
| `config` | configuration and related local config files | before updates or config migrations |
| `databases` | local SQLite dashboard/history databases | preserve local dashboard/history data |
| `influxdb` | bundled InfluxDB analytics data | preserve analytics history when using bundled InfluxDB |

Notes:

- A **config** backup includes `config.json`, the runtime state, the dashboard
  auth/cert/key files, and the bundled InfluxDB secret (only when bundled
  InfluxDB is enabled).
- A **databases** backup includes the local SQLite files
  (`data/ems_dashboard.sqlite`, `data/ems_state.sqlite`). It does **not**
  include InfluxDB data.
- An **influxdb** backup covers **bundled** InfluxDB only. **External InfluxDB
  instances are not backed up by this tool** — they stay user-managed, so use
  your provider's own backup tool for those.

## Where are backups stored?

Docker:

```text
data/backups/ems-config-manual-2026-06-20-120000.tar.gz
data/backups/ems-databases-manual-2026-06-20-120000.tar.gz.enc   # password-protected
data/backups/ems-influxdb-rollback-2026-06-20-122000.tar.gz      # auto rollback
```

Inside the container, the same files are under `/app/data/backups/`.

Native Python:

```text
./backup/ems-config-manual-2026-06-20-120000.tar.gz
./backup/ems-databases-manual-2026-06-20-120000.tar.gz.enc   # password-protected
./backup/ems-influxdb-rollback-2026-06-20-122000.tar.gz      # auto rollback
```

- `.tar.gz` is a normal (unencrypted) backup.
- `.tar.gz.enc` is a password-protected backup.
- `rollback` in the name marks a safety backup made automatically just before a
  restore.

A new backup never silently overwrites an existing one.

## Should I use password protection?

Use `--password` when:

- the backup may leave the device (cloud storage, USB stick, another machine),
  **or**
- it is a **config** backup (it can contain credentials/tokens), **or**
- you want to protect local usage history from others.

You can skip it for quick local backups you keep only on the same trusted
machine. The choice is yours — just remember an encrypted backup needs its
password to restore.

## How to list existing backups

List the backup folder:

Docker:

```bash
ls -lh data/backups/
```

Native Python:

```bash
ls -lh backup/
```

Or open the interactive menu, which also lists available backups when you choose
to restore or inspect:

Docker:

```bash
docker compose exec ems python3 emsctl.py backup
```

Native Python:

```bash
python3 emsctl.py backup
```

## How to inspect a backup

Print a backup's manifest (type, timestamp, included files, checksums):

```bash
python3 emsctl.py backup inspect ./backup/ems-config-manual-2026-06-20-120000.tar.gz
```

For an encrypted backup, the CLI detects the `.enc` file and asks for the
password automatically. A wrong password aborts cleanly:

```bash
python3 emsctl.py backup inspect ./backup/ems-config-manual-2026-06-20-120000.tar.gz.enc
```

You can also pass `--password` to force password mode explicitly:

```bash
python3 emsctl.py backup inspect ./backup/ems-config-manual-2026-06-20-120000.tar.gz.enc --password
```

To compare a single config file in the backup against your current file:

```bash
python3 emsctl.py backup diff ./backup/ems-config-manual-2026-06-20-120000.tar.gz --file config.json
```

## How to verify a backup

There is no separate "verify" command — verification is built into inspect and
dry-run restore:

1. `backup inspect` reads the manifest and (for encrypted backups) confirms the
   password works.
2. `backup restore --dry-run` validates the archive structure and checks every
   file's checksum **without writing anything**:

```bash
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz --dry-run
```

If the archive is corrupted, has a wrong password, or contains unsafe entries,
these checks fail before any file would be touched.

## How to test a restore with dry-run

Always test first with `--dry-run`. It shows the plan and **never** changes local
files, creates rollback archives, or asks conflict questions:

```bash
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz --dry-run
```

Each file is reported as `would_restore_new`, `would_replace_conflict`,
`would_skip_identical` (or `would_restore_influxdb` for an InfluxDB backup).
When the plan looks right, run the same command without `--dry-run`.

## Restore config

```bash
# 1. Preview (no changes)
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz --dry-run

# 2. Restore for real
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz
```

For an encrypted config backup, just point at the `.enc` file — the CLI asks for
the password automatically:

```bash
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz.enc
```

Because this is an encrypted backup, the CLI will ask for the password. You can
also pass `--password` if you want to force password mode explicitly. Either
way, an encrypted backup cannot be restored without the correct password.

## Restore local SQLite dashboard/history data

```bash
python3 emsctl.py backup restore ./backup/ems-databases-manual-2026-06-20-120000.tar.gz --dry-run
python3 emsctl.py backup restore ./backup/ems-databases-manual-2026-06-20-120000.tar.gz
```

A database backup stores a re-serialized SQLite snapshot, so restoring over an
existing database is normally reported as a conflict even when the data matches.
Existing database files are never overwritten without your confirmation.

## Restore bundled InfluxDB analytics history

InfluxDB restore uses a **replace-style** restore (`influx restore --full`)
inside the bundled container. It replaces **all** bundled InfluxDB data (every
bucket plus org/users/tokens). Test first, then restore:

```bash
python3 emsctl.py backup restore ./backup/ems-influxdb-manual-2026-06-20-120000.tar.gz --dry-run
python3 emsctl.py backup restore ./backup/ems-influxdb-manual-2026-06-20-120000.tar.gz
```

For an encrypted InfluxDB backup, the CLI prompts for the password
automatically (you can still pass `--password` to force it). Only **bundled**
mode can be restored — external InfluxDB is rejected with a clear message.

## Restore a complete local setup

When moving or recovering a full setup, restore in this order so the bundled
InfluxDB token and config stay in sync:

```text
1. Restore config
2. Check config and secret files
3. Restore local SQLite databases
4. Restore bundled InfluxDB analytics data
5. Run status/diagnostic checks
```

Why this order: the config backup brings back `config.json` and the bundled
InfluxDB secret. Because the InfluxDB restore replaces org/users/tokens as well
as history, restoring config first keeps the token and config agreeing.

Example (preview each step first):

```bash
# 1. Config
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz --dry-run
python3 emsctl.py backup restore ./backup/ems-config-manual-2026-06-20-120000.tar.gz

# 2. Check the bundled InfluxDB secret/config are present
#    (deploy/docker/influxdb.env exists; config.json has influxdb enabled, mode bundled)

# 3. SQLite databases
python3 emsctl.py backup restore ./backup/ems-databases-manual-2026-06-20-120000.tar.gz --dry-run
python3 emsctl.py backup restore ./backup/ems-databases-manual-2026-06-20-120000.tar.gz

# 4. Bundled InfluxDB
python3 emsctl.py backup restore ./backup/ems-influxdb-manual-2026-06-20-120000.tar.gz --dry-run
python3 emsctl.py backup restore ./backup/ems-influxdb-manual-2026-06-20-120000.tar.gz

# 5. Checks (next section)
```

The SQLite database restore is independent of InfluxDB, so you can run it at any
point in the sequence.

## What do the restore questions mean?

An interactive restore asks a few questions. In plain language:

- **Create rollback backup before restore? `[y/n/a]`** — make a safety backup of
  your current files first, so you can undo the restore. `y` = yes, `n` = no,
  `a` = abort. Choosing `y` is recommended.
- **Protect rollback backup with password? `[y/n/a]`** — encrypt that safety
  backup. Its password is independent of the source backup's password.
- **Keep / replace / diff / abort (per conflicting file)** — for each existing
  file that differs from the backup: `keep` your current file, `replace` it with
  the backup version, show a `diff`, or `abort` the whole restore. Identical
  files are skipped automatically.
- **InfluxDB replace warning** — confirms that restoring InfluxDB replaces all
  bundled analytics data. Choose `r` to replace or `a` to abort.

If two rollback passwords do not match, or rollback creation fails, the restore
aborts and no partial files are written.

## What should I check after restore?

```bash
python3 emsctl.py status
python3 emsctl.py diagnose --deep
python3 emsctl.py influx status
```

`influx status` is only relevant when you use bundled/local InfluxDB analytics.
After a config or database restore the CLI also reminds you to run
`diagnose --deep`.

## Common problems

- **Backup is encrypted and the password is missing or wrong.** An encrypted
  `.tar.gz.enc` backup cannot be restored or inspected without the correct
  password. A wrong password aborts cleanly without changing files. Store the
  password somewhere safe when you create the backup.
- **Dry-run reports conflicts.** That is expected when local files differ from
  the backup. Review the plan, then run the real restore and choose keep/replace
  per file. Database backups almost always show as conflicts (the snapshot is
  re-serialized) even when the data is the same.
- **Bundled InfluxDB is not running.** The InfluxDB backup/restore needs the
  bundled container. Start it (e.g. `python3 emsctl.py influx init` or
  `python3 emsctl.py stack up`) and check `python3 emsctl.py influx status`.
- **External InfluxDB is configured.** This tool does not back up or restore
  external InfluxDB. Use your provider's backup tool; the `influxdb` backup type
  is rejected for external mode.
- **No local SQLite history exists yet.** A fresh install may have no database
  files. A database backup simply records them as not included — it does not
  fail.
- **Restore was aborted before changing files.** Aborting (or any validation
  failure) leaves your files untouched. Fix the cause and try again.
- **Unsure whether to keep or replace a conflicting file.** Use `diff` to see
  the difference, or run `backup diff` beforehand. When in doubt, keep your
  current file and re-check later — you can always restore again.

## What is not backed up?

- **External InfluxDB** data — not handled by this tool; stays user-managed.
- Anything outside the configured config/database/InfluxDB paths.
- A config backup does not include SQLite or InfluxDB data; a database backup
  does not include InfluxDB data. Use the matching backup type for each.

Do **not** copy `data/influxdb` by hand while InfluxDB is running — use
`backup create --type influxdb`, which takes a consistent snapshot via the
official `influx backup` CLI.
