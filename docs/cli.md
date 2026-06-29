# CLI Tool

`emsctl.py` safely edits the configured runtime-state file.

It does not contact Zendure hardware and does not contact Home Assistant.

## Quickstart

Running the CLI without arguments prints a short start screen:

```bash
python3 emsctl.py
```

Start with the interactive menu, built-in help, or the command cookbook:

```bash
python3 emsctl.py interactive
python3 emsctl.py --help
python3 emsctl.py examples
```

Common runtime edits:

```bash
python3 emsctl.py status
python3 emsctl.py system disable
python3 emsctl.py system max-power 1200
python3 emsctl.py device WR1 max-power 600
python3 emsctl.py device WR1 ac-mode output
python3 emsctl.py device WR1 ac-mode input
python3 emsctl.py device WR1 ac-charge-power 200
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py winter enable
python3 emsctl.py dashboard auth-status
python3 emsctl.py diagnose
```

## Diagnose

`diagnose` is designed for local support/debug output. Normal diagnose is
read-only and does not contact Zendure hardware, Home Assistant, MQTT, Shelly,
or other external services.

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --deep
python3 emsctl.py diagnose --hardware
python3 emsctl.py diagnose --control
python3 emsctl.py diagnose --control --sample-seconds 30
python3 emsctl.py diagnose --control-quality --sample-seconds 60
python3 emsctl.py diagnose --quality --json
python3 emsctl.py diagnose --json
python3 emsctl.py diagnose --support-bundle
python3 emsctl.py diagnose --support-bundle --output /tmp/ems-support.zip
```

## Diagnose Command Matrix

| Command | Purpose |
| --- | --- |
| `diagnose` | Installation health |
| `diagnose --deep` | Advanced health checks |
| `diagnose --hardware` | Hardware connectivity |
| `diagnose --control` | Explain EMS decisions |
| `diagnose --control-quality` | Evaluate EMS quality |
| `diagnose --support-bundle` | Generate support bundle |

Use `diagnose` first when something is unclear. Use `--control` when EMS is
running but the current output looks surprising. Use `--control-quality` when
the system regulates but export/import, PV usage, or SOC balancing looks poor
over time. Use `--support-bundle` before opening a GitHub issue or forum
support request.

Normal `diagnose` also includes the optional battery full-charge assist section.
It reads the core EMS state database when present and reports per-device
`last_full_charge_at`, `next_due_at`, pending restore flags, and read-only
firmware diagnostics. Diagnose does not create the assist database.

Modes:

- `--json` prints the same result structure as machine-readable JSON. The
  diagnose API contract is versioned with top-level `schema_version: 1`.
- `--deep` adds local operational checks: runtime-state plausibility, SQLite
  integrity/table summaries, recent configured log patterns, Docker host hints,
  and a dashboard loopback check when enabled.
- `--hardware` performs explicit short-timeout read-only network probes for
  configured grid meters and Zendure read endpoints. It never writes hardware
  state. The output also includes compact grid-meter and per-device
  communication health (see "Communication health" below).
- `--control` explains the current regulation path from local config and
  runtime-state: grid power, filtered grid power, target output, final output,
  deadband state, device allocation, SOC protection, write-path blockers, and
  likely root causes.
- `--sample-seconds N` can be combined with `--control` to collect local
  runtime-state meter samples. The output reports average/min/max, standard
  deviation, sign changes, and stale/noisy meter hints.
- `--control-quality` or `--quality` evaluates real operation over local
  samples: export/import quality, a coarse regulation quality score, PV usage
  plausibility, SOC balancing, and higher-level root-cause hints.
- `--support-bundle` creates a redacted ZIP with a stable file layout:
  `diagnosis.json`, `diagnosis.txt`, `control-diagnostics.json`,
  `control-diagnostics.txt`, `control-quality.json`, `control-quality.txt`,
  `redacted-config.json`, `runtime-state.json`, and `bundle-metadata.json`.

Control interpretation:

- `Control disabled` means runtime control is explicitly off.
- `Dry run enabled` means EMS calculates targets but skips hardware writes.
- `Deadband active` means the filtered meter value is inside the configured
  threshold and output may be held.
- `Grid meter signal appears noisy` means frequent sign changes or high
  variance were observed in local samples.
- `Minimum SOC protection active` means at least one device is at or below its
  minimum SOC.
- Runtime AC input mode is shown as blocked device writes in control
  explanation data. AC input devices are excluded from normal output allocation
  until their runtime intent returns to output mode and `acMode=2` has been
  reconciled.

## Communication health

`diagnose --hardware` reports lightweight communication health so intermittent
read/write problems are visible without digging through logs:

```text
Grid meter health:
  provider: Shelly
  status: ok
  last success: 3s ago
  consecutive errors: 0
  last latency: 42 ms
  stale value used: no

Device health:
  WR1:
    read: ok, last success 2s ago, consecutive errors 0, last latency 180 ms
    write: ok, last success 35s ago, consecutive errors 0
```

- **Grid-meter health** tracks reads of the configured grid meter (Shelly,
  EcoTracker, Tasmota, MQTT, ...). `stale value used: yes` means the read
  failed, no fresh MQTT value arrived, or a cached MQTT value exceeded
  `grid_meter.max_age_seconds`; EMS kept the last known value rather than
  reacting to a bad reading. This is intentional fallback behavior, not a
  control bug.
- **Device read/write health** is tracked separately per device. Reads and
  writes never share state: a device can be readable while writes fail or are
  intentionally blocked (for example a device parked in AC-input mode).
- **Status** is `ok` (recent success), `degraded` (recent failures or a stale
  value, but still a recent good value), `failed` (no recent good value or
  repeated consecutive failures), or `unknown` (nothing attempted yet).

Health counters are in-memory only and reset when EMS restarts; they describe
current runtime health, not long-term history.

Runtime communication health is kept in memory by the running EMS process. It
resets when EMS restarts and is intended for live diagnostics, dashboard/API
exposure, or future telemetry export.

`emsctl diagnose --hardware` is a fresh read-only probe from the CLI process. It
checks the currently configured meter and devices at the time the command is
run. It does not read historic runtime counters from the running EMS process.

Repeated grid-meter read timeouts (for example Shelly `ReadTimeoutError` on
`/rpc/Shelly.GetStatus`) usually indicate a slow or unresponsive meter or a
flaky network, not an EMS control problem. Power-cycling the meter often clears
it. Use `grid-meter test` to confirm:

```bash
python3 emsctl.py grid-meter test
python3 emsctl.py grid-meter test --duration 120 --interval 1
```

```text
Grid meter read test: Shelly 192.168.100.93
Duration: 120s
Reads: 120
OK: 117
Failed: 3
p50 latency: 42 ms
p95 latency: 310 ms
max latency: 3012 ms
```

For MQTT grid meters the command subscribes through the configured broker and
reports `Latest power` once a fresh value has arrived. If the broker is
reachable but the topic is wrong or stale, the output says no fresh MQTT value
was received.

`grid-meter test` is read-only and does not write to any device. It exits
non-zero if any read fails.

## AC mode runtime role

```bash
python3 emsctl.py device WR1 ac-mode output
python3 emsctl.py device WR1 ac-mode input
python3 emsctl.py device WR1 ac-charge-power 200
```

`ac-mode output` writes `runtime_role=ac_output` to runtime-state. The
controller reconciles the inverter to `acMode=2` during the normal control loop
and allows normal EMS output allocation. `ac-mode input` writes
`runtime_role=ac_input`, targets `acMode=1`, and excludes the device from
normal EMS output allocation. `emsctl` changes runtime-state only and does not
write raw `acMode` numbers or contact inverter hardware.

AC charge power is runtime-only:

```bash
python3 emsctl.py device WR1 ac-charge-power 200
python3 emsctl.py device WR1 ac-mode input

# Later return to normal EMS output regulation
python3 emsctl.py device WR1 ac-mode output
```

`ac-charge-power` writes `ac_charge_power_w` to runtime-state and preserves
the current runtime role. The controller applies it as Zendure `inputLimit` on
the next EMS loop only while the device role is `ac_input`, and only when
telemetry reports a different current `inputLimit`. While the role is
`ac_output`, the stored charge power is ignored for hardware writes so it can
be prepared before switching to input mode.

Control quality interpretation:

- The quality score is a coarse support indicator from 0 to 100, not a
  certified measurement. It starts at 100 and subtracts bounded penalties for
  average grid deviation, export duration, export peaks, and large import
  peaks.
- `excellent`, `good`, `acceptable`, `poor`, and `critical` are intended for
  triage. Use the detailed export/import metrics to understand the cause.
- Export peaks mean grid power went negative during the sample window. Short
  small peaks can be normal; long or large peaks indicate the zero-export
  target is not being held consistently.
- PV diagnostics can show that PV telemetry is missing, PV is likely limited by
  system/device limits, or PV is available but not used. It cannot prove a
  hardware fault without additional read-only hardware checks.
- SOC balancing warnings mean the SOC spread is high, a low-SOC device is
  contributing more than expected, or a device is protected by minimum SOC.

Exit codes:

```text
0  diagnose status is ok or warning
1  at least one diagnostic error was found
2  invalid CLI usage
```

Before opening an issue:

```bash
python3 emsctl.py diagnose --support-bundle
```

Attach the generated ZIP. The bundle is redacted and includes the diagnostic
summary, redacted config/runtime-state snapshots, bundle metadata, and
control/quality diagnostics. Control files are present even when the
corresponding mode was not enabled so automated tooling can rely on the same
file names.

Machine-readable root causes always use this shape:

```json
{
  "code": "control_disabled",
  "severity": "warning",
  "title": "Control disabled",
  "message": "Control disabled",
  "suggested_next_check": "Review the related diagnose section for details."
}
```

## Config Discovery

By default, `emsctl.py` uses this config lookup order:

```text
--config PATH
EMS_CONFIG_FILE
config.json
config/config.json
```

This preserves legacy local setups that keep `config.json` next to
`emsctl.py`, while allowing the recommended Docker setup to use
`/app/config/config.json` automatically.

Relative `runtime_state_path` and dashboard `auth_file` values are still
resolved relative to the application directory. The runtime-state path always
comes from the selected config unless `--runtime-state` is passed explicitly.

## Config Commands

`config init` is the optional first setup assistant. It helps fill common
settings and does not blindly replace edited configs.

Native Python:

```bash
python3 emsctl.py config init
python3 emsctl.py config init --dry-run
python3 emsctl.py config init --yes --backup
python3 emsctl.py config init --yes --no-backup
```

Docker:

```bash
docker compose exec ems python3 emsctl.py config init
```

Edited configs need an explicit backup decision for non-interactive writes:
use `--yes --backup` to create a backup first, or `--yes --no-backup` when you
intentionally do not want one.

`config upgrade` is different from `config init`. It fills missing persisted
keys after updates by comparing your config with `config.template.json`.
Dry-run output includes missing keys, missing explanatory comments, and the
number of existing template-managed `_comment*` entries whose text differs from
the current template.

Native Python:

```bash
python3 emsctl.py config upgrade --dry-run
python3 emsctl.py config upgrade
```

Docker:

```bash
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade
```

When run interactively, `config upgrade` can optionally refresh outdated
template-managed explanatory comments after the normal upgrade completes. The
refresh changes only exact `_comment*` paths from the template; configuration
values and unknown user keys are preserved. Non-interactive `--yes` runs do not
prompt for or apply this comment refresh.

## Docker Usage

With the recommended Compose service name `ems`, common commands can be run
without an explicit config path:

```bash
docker compose exec ems python3 emsctl.py status
docker compose exec ems python3 emsctl.py interactive
docker compose exec ems python3 emsctl.py dashboard auth-status
docker compose exec ems python3 emsctl.py config init
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --control
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
docker compose exec ems python3 emsctl.py diagnose --deep
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

For unusual mounts or troubleshooting, an explicit config path still works:

```bash
docker compose exec ems python3 emsctl.py --config /app/config/config.json status
```

Each command group also has focused help:

```bash
python3 emsctl.py system --help
python3 emsctl.py device --help
python3 emsctl.py dashboard --help
```

## Interactive Mode

`interactive` opens a dependency-free menu for common runtime edits:

```bash
python3 emsctl.py interactive
```

Alias:

```bash
python3 emsctl.py menu
```

The menu works directly with Python standard input/output and does not require
Bash or Zsh completion setup. It can show status, edit system limits, toggle HA
publishing/helper control, toggle winter mode, edit configured devices, show
or manage dashboard authentication, and open the Backup / Restore submenu (see
[Backup / Restore](#backup--restore)).

Interactive mode preserves the same safety rules as direct commands:

- no Zendure hardware access
- no Home Assistant access
- no dashboard server access
- no plaintext password echo
- same value validation
- same atomic runtime-state writes

## Examples Command

`examples` prints a longer read-only cookbook grouped by topic:

```bash
python3 emsctl.py examples
```

This command does not create or modify the runtime-state file.

## Shell Completion

Completion is optional and generated without third-party packages. It is not
required for interactive mode.

Bash, current shell:

```bash
source <(python3 emsctl.py completion bash)
```

Bash, persistent user install:

```bash
mkdir -p ~/.local/share/bash-completion/completions
python3 emsctl.py completion bash > ~/.local/share/bash-completion/completions/emsctl
```

Zsh, current shell:

```bash
source <(python3 emsctl.py completion zsh)
```

The generated completion covers top-level commands, command actions, dashboard
subcommands, offgrid modes (`off`, `eco`, `standard`), and configured device
names when `config.json` is readable. Completion generation does not create or
modify `runtime-state.json`.

Use explicit config paths when generating completion for a non-default
installation:

```bash
python3 emsctl.py --config /etc/ems/config.json completion bash
python3 emsctl.py --config /etc/ems/config.json completion zsh
```

## Status

```bash
python3 emsctl.py status
```

With explicit paths:

```bash
python3 emsctl.py --config config.json status
python3 emsctl.py --runtime-state runtime-state.json status
python3 emsctl.py --runtime-state data/runtime-state.json status
```

`status` creates the configured runtime-state file from config defaults when
the file is missing.

## System Commands

```bash
python3 emsctl.py system enable
python3 emsctl.py system disable
python3 emsctl.py system max-power 1600
python3 emsctl.py system loop-interval 5
python3 emsctl.py system min-output-limit 30
```

## HA Commands

```bash
python3 emsctl.py ha enable
python3 emsctl.py ha disable
python3 emsctl.py ha-control enable
python3 emsctl.py ha-control disable
```

`ha` controls runtime HA publishing. `ha-control` controls runtime HA helper
sync. Neither command edits HA URL or token.

## Winter Commands

```bash
python3 emsctl.py winter status
python3 emsctl.py winter enable
python3 emsctl.py winter disable
```

This only toggles winter mode at runtime. Winter SOC targets, months, ramp
timing, and AC charge power remain static `config.json` settings.

## Device Commands

```bash
python3 emsctl.py device WR1 enable
python3 emsctl.py device WR1 disable
python3 emsctl.py device WR1 max-power 800
python3 emsctl.py device WR1 pv-priority-factor 1.3
python3 emsctl.py device WR2 pv-priority-factor 0.7
python3 emsctl.py device WR1 offgrid off
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py device WR1 offgrid standard
```

`pv-priority-factor` adjusts the device's PV-first allocation weight at
runtime. Values above `1.0` increase the device's PV-first share, values below
`1.0` reduce it. The value is still limited by real PV availability, device
state, SOC logic, and configured power limits.

## Dashboard Auth Commands

Dashboard write mode is disabled until a local admin password is configured:

```bash
python3 emsctl.py dashboard set-password
python3 emsctl.py dashboard change-password
python3 emsctl.py dashboard disable-auth
python3 emsctl.py dashboard auth-status
```

Passwords are prompted without echo. The password file contains only
PBKDF2-SHA256 hash metadata and no plaintext password. `disable-auth` removes
the password file and makes dashboard write mode unavailable again.

Hidden password automation flags exist for tests and non-interactive automation
but are intentionally omitted from normal help. Do not use them for interactive
terminal sessions because shell history and process listings can expose command
arguments.

## Backup / Restore

Manual config and database backup/restore for moving a setup to another device
or recovering a broken installation without copying files by hand.

> For a beginner-friendly, step-by-step workflow (backup before an update,
> dry-run restore checks, full local restore order) see the
> [Backup and Restore Guide](backup-restore.md). This page is the detailed
> command reference.

```bash
python3 emsctl.py backup                       # interactive menu
python3 emsctl.py backup create                # config backup
python3 emsctl.py backup create --type databases
python3 emsctl.py backup create --type influxdb
python3 emsctl.py backup create --compression-level 3
python3 emsctl.py backup inspect data/backups/ems-config-manual-2026-06-18-221500.tar.gz
python3 emsctl.py backup restore data/backups/ems-config-manual-2026-06-18-221500.tar.gz
python3 emsctl.py backup restore data/backups/ems-databases-manual-2026-06-18-221500.tar.gz
python3 emsctl.py backup restore data/backups/ems-influxdb-manual-2026-06-18-221500.tar.gz
python3 emsctl.py backup diff data/backups/ems-config-manual-2026-06-18-221500.tar.gz --file config.json
```

Backups are stored in `data/backups/` by default. Docker users see the same
folder on the host because `data/` is mounted into the container. Older
versions may have used `backup/`; existing archives there can still be restored
by passing the archive path.

`python3 emsctl.py backup` (no action) opens a small menu:

```text
Backup / Restore
  [1] Create config backup
  [2] Create database backup
  [3] Create InfluxDB backup
  [4] Restore backup
  [5] Inspect backup
  [6] Exit
```

### What a config backup contains

A config backup is a sortable `tar.gz` archive written to `data/backups/`:

```text
data/backups/ems-config-manual-2026-06-18-221500.tar.gz
data/backups/ems-config-manual-2026-06-18-221500.tar.gz.enc   # password-protected
data/backups/ems-config-rollback-2026-06-18-222000.tar.gz     # auto rollback
```

Included files (when present and configured):

- `config.json`
- the runtime state (`system.runtime_state_path`)
- dashboard auth/cert/key (`dashboard.auth_file`, `ssl_cert_file`, `ssl_key_file`)
- the bundled InfluxDB secret (`influxdb.secret_file`) — only when
  `influxdb.enabled` is true **and** `influxdb.mode` is `bundled`

Every archive contains a `backup-manifest.json` with the backup format, type,
purpose, UTC timestamp, build metadata (git commit/branch/describe), contract
versions, and per-file sensitivity flags and SHA256 checksums.

> **Config backups may contain secrets.** `config.json`, the dashboard auth
> file, the dashboard TLS key and the InfluxDB secret are flagged sensitive.
> The CLI prints a sensitive-data warning before creating a backup.

### What a database backup contains

`backup create --type databases` backs up the local SQLite databases into a
`tar.gz` archive written to `data/backups/`:

```text
data/backups/ems-databases-manual-2026-06-18-221500.tar.gz
data/backups/ems-databases-manual-2026-06-18-221500.tar.gz.enc   # password-protected
data/backups/ems-databases-rollback-2026-06-18-222000.tar.gz     # auto rollback
```

Included databases (only when present):

- `data/ems_dashboard.sqlite` (`dashboard.database_path`) — dashboard/history DB
- `data/ems_state.sqlite` (`battery_full_charge_assist.state_database_path`) —
  local EMS state (calibration / full-charge assist)

Each database is snapshotted with the SQLite online backup API into a temporary
staging directory before archiving, so the stored copy is internally consistent
even while the EMS is running — live files are never copied directly. Missing
databases do not fail the backup; they are recorded in the manifest as
`included: false`.

Database and InfluxDB backups may contain historical energy and runtime data —
not classic secrets such as passwords or tokens, but local usage history that
can reveal usage patterns. SQLite database files are flagged `privacy_relevant`
in the manifest (not `sensitive`). Use an encrypted backup if you want to
protect local usage history. The manifest records a `databases` list and an
`influxdb` block describing detected analytics.

> **InfluxDB data is not part of a database backup.** A database backup covers
> the local SQLite files only. When InfluxDB analytics is enabled (bundled or
> external) the CLI notes that InfluxDB data is detected but not in this archive,
> and the manifest records it as
> `{"included": false, "reason": "use_influxdb_backup_type"}`. Use
> `backup create --type influxdb` (below) to back up bundled InfluxDB data.

### What an InfluxDB backup contains

`backup create --type influxdb` backs up the **bundled** InfluxDB analytics data
using the official `influx backup` CLI, then packages the output into a `tar.gz`
archive written to `data/backups/`. Docker users run the command inside the
`ems` container (the CLI ships in the image and talks to the bundled InfluxDB
over the Docker network — no Docker socket required); native users run it from
the repo, where it drives the bundled `ems-influxdb` container via
`docker compose`:

```text
data/backups/ems-influxdb-manual-2026-06-18-221500.tar.gz
data/backups/ems-influxdb-manual-2026-06-18-221500.tar.gz.enc    # password-protected
data/backups/ems-influxdb-rollback-2026-06-18-222000.tar.gz      # auto rollback
```

The archive contains `backup-manifest.json` plus an `influxdb/` directory with
the official backup output (KV/SQL store and bucket data). The live
`data/influxdb` bind mount is **never** copied directly. The manifest records an
`influxdb` block:

```json
"influxdb": {
  "included": true,
  "mode": "bundled",
  "service": "influxdb",
  "container_name": "ems-influxdb",
  "org": "ems",
  "bucket_prefix": "ems",
  "backup_method": "influx backup",
  "data_dir": "data/influxdb"
}
```

What is **included**: all InfluxDB buckets, tasks and history captured by the
official backup, packaged with checksums. What is **not** included: config,
SQLite databases, and any external InfluxDB (use your provider's backup tool).

Supported modes:

- **Bundled** (`influxdb.enabled: true`, `influxdb.mode: bundled`) — supported.
  Requires Docker/Compose and a usable token (`influxdb.token`, the
  `influxdb.token_env` variable, or the generated `deploy/docker/influxdb.env`).
  The container is started if it is not already running.
- **External** (`influxdb.mode: external`) — rejected with a clear message; use
  your external InfluxDB backup strategy.
- **Disabled** — nothing to back up; the command is a no-op.

> InfluxDB backups may contain historical energy and runtime data and InfluxDB
> metadata. The CLI offers optional password protection (same `.tar.gz.enc`
> format as config/database backups). Tokens are never embedded in the archive
> or the manifest, and the admin token is passed to the container via its
> environment, never on the command line.

### Unique archive names (no silent overwrite)

Backup archives are timestamped to the second. If two backups of the same type
and purpose land in the same second, the second one is written to a unique
`...-2`, `...-3`, … name instead of overwriting the first. The archive is built
into a temporary file and atomically linked into place, so an existing backup is
never silently clobbered and no partial archive is left behind on failure.

```text
ems-config-manual-2026-06-20-120000.tar.gz
ems-config-manual-2026-06-20-120000-2.tar.gz
ems-config-manual-2026-06-20-120000.tar.gz.enc
ems-config-manual-2026-06-20-120000-2.tar.gz.enc
```

### Symlinks

A backup source file that is a symlink is **rejected** with a clear error
(`Refusing to back up symlink path: <path>`) and no partial archive is written.
On restore, only regular-file manifest entries are accepted; a crafted archive
whose member is a symlink is rejected.

### Optional password protection (streaming encryption)

Manual backups can be encrypted into a `.tar.gz.enc` file:

```bash
python3 emsctl.py backup create --password
python3 emsctl.py backup create --type config --password --encryption aes-256-gcm
python3 emsctl.py backup create --type influxdb --password --chunk-size 4M --kdf-iterations 300000
```

New encrypted backups use a versioned **streaming** format (format version 2):
the archive is encrypted in independently authenticated chunks, so neither
encryption nor decryption ever loads the whole archive into memory — suitable
for larger InfluxDB backups on a Raspberry Pi 4.

- Default algorithm: **ChaCha20-Poly1305** (fast on ARM hardware without AES
  acceleration). Optional: **AES-256-GCM** (`--encryption aes-256-gcm`).
- KDF: **PBKDF2-HMAC-SHA256**, default 300000 iterations (`--kdf-iterations`).
- Default chunk size: **4 MiB** (`--chunk-size`, accepts e.g. `4M`/`512K`/bytes).
- All parameters (algorithm, KDF, iterations, chunk size, salt, base nonce) are
  stored in a self-describing header, so restore auto-detects the algorithm.

Each chunk binds the format version, algorithm, chunk index and final-chunk
marker as authenticated data, so a wrong password, modified ciphertext, a
truncated file, reordered chunks, and unsupported algorithms/versions all fail
cleanly. The encrypted header and per-chunk metadata are bounds-checked before
any decryption, so a malformed or hostile `.enc` file is rejected with a
backup-format error before any restored file is written. Invalid `--encryption`,
`--chunk-size`, `--kdf-iterations` or `--compression-level` values are rejected
with a clear message (no traceback) and no partial archive.

**Legacy compatibility:** existing whole-file Fernet encrypted backups (format
version 1) remain restorable — restore detects the format from the header and
decrypts them through the legacy path. Only new backups use the streaming
format.

The password is entered twice, **never stored**, and never logged. Restoring or
inspecting an encrypted backup prompts for the password; a wrong password aborts
cleanly. In non-interactive mode an encrypted restore fails with a clear message.

### Restore

```bash
python3 emsctl.py backup restore data/backups/ems-config-manual-2026-06-18-221500.tar.gz
python3 emsctl.py backup restore data/backups/...tar.gz --on-conflict keep --no-rollback
python3 emsctl.py backup restore data/backups/...tar.gz --dry-run
```

Restore detects the backup type from its manifest. Config and database backups
follow the file-restore flow below; InfluxDB backups follow the dedicated
replace flow described in [Restoring an InfluxDB backup](#restoring-an-influxdb-backup).
Interactive restore first asks whether to create a
rollback backup (`backup_purpose=rollback`, matching the backup type). When you
choose to create one, the CLI then asks whether to **password-protect the
rollback backup**:

```text
Create rollback backup before restore? [y/n/a]
Rollback backup may contain sensitive data.
Protect rollback backup with password? [y/n/a]
Enter rollback backup password:
Repeat rollback backup password:
```

The rollback password is **independent of the source backup password** and is
never reused automatically — you may restore an encrypted backup while keeping
an unencrypted rollback, or the reverse. A password-protected rollback is
written as `...-rollback-....tar.gz.enc`; an unprotected one stays a plain
`.tar.gz`. If the two rollback passwords do not match the restore aborts and **no
partial rollback archive is created**; if rollback creation fails the restore
does not start. In non-interactive mode the rollback (when requested) is created
unencrypted — the CLI never silently produces an encrypted rollback.

After the rollback step, restore continues. For each existing file that differs
you can keep the current file, replace it with the backup version, show a
unified diff (binary databases report that no diff is shown), or abort.
Identical files are skipped silently. Because a database backup stores a
re-serialized SQLite snapshot, restoring over an unchanged database is normally
reported as a conflict; existing database files are never overwritten without
explicit confirmation or an explicit `--on-conflict replace`. After a database
restore the CLI notes that InfluxDB data was not part of the backup.

Non-interactive options:

- `--on-conflict abort|keep|replace` (default for scripts: `abort`)
- `--rollback` / `--no-rollback`
- `--dry-run` — show the plan without writing

`--dry-run` is conflict-safe: it reports a plan for every file and **never**
writes files, creates rollback archives, requires conflict decisions, or aborts
just because a target file differs. Each file is reported with an explicit
action — `would_restore_new`, `would_replace_conflict`, `would_skip_identical`
(and `would_restore_influxdb` for an InfluxDB backup). Validation still runs in
dry-run: archive structure and manifest checksums are checked, and path
traversal, unsafe entries, corrupted backups, wrong passwords and unsupported
formats still fail.

Restore is safe by construction: only files listed in `backup-manifest.json`
are restored, archive entries with absolute paths or `..` traversal are
rejected, and every file's SHA256 is validated before it is written.

After a successful config/database restore the CLI recommends:

```text
  python3 emsctl.py diagnose --deep
```

### Restoring an InfluxDB backup

Restoring an `ems-influxdb-*` archive uses the official `influx restore --full`
mechanism inside the bundled container, which **replaces all bundled InfluxDB
data** (KV/SQL store and every bucket). It is intentionally conservative:

```text
InfluxDB restore can replace existing bundled analytics data.
Create rollback InfluxDB backup before restore?   [y/n/a]
Protect rollback backup with password?            [y/n/a]
Restore strategy: [r] replace existing bundled InfluxDB data / [a] abort
```

- Only **bundled** mode is restorable; external mode is rejected.
- An encrypted source archive prompts for its password.
- A rollback InfluxDB backup can be created first, and can itself be encrypted.
  If rollback creation fails, the restore does not start (unless you chose no
  rollback).
- The MVP supports **replace-style restore only** — no merge of buckets, tasks
  or history. Non-interactive restores must pass `--on-conflict replace` to
  confirm the destructive replace.
- `--full` restores InfluxDB metadata (org, buckets, users, tokens, dashboards)
  **and** time-series data. Restoring a backup taken from the *same* bundled
  instance keeps `deploy/docker/influxdb.env` in sync; restoring one from a
  different instance may require updating the token.

After an InfluxDB restore the CLI recommends:

```text
  python3 emsctl.py influx status
  python3 emsctl.py diagnose --deep
```

### Recommended restore order (moving / recovering a setup)

Because `influx restore --full` replaces InfluxDB org/buckets/users/tokens as
well as history, restore in this order so the bundled token and config agree:

1. **Restore the config backup first** (`ems-config-...`) — brings back
   `config.json` and the bundled InfluxDB secret (`deploy/docker/influxdb.env`).
2. **Verify** the bundled InfluxDB secret/config files are present (the env file
   exists and `config.json` has `influxdb.enabled: true`, `mode: bundled`).
3. **Restore the InfluxDB backup** (`ems-influxdb-...`) — replace-style restore
   of analytics history (bundled mode only; external mode is not supported).
4. **Verify**:

   ```bash
   python3 emsctl.py influx status
   python3 emsctl.py diagnose --deep
   ```

Restore a database backup (`ems-databases-...`) at any point in this sequence;
it is independent of the InfluxDB data. A rollback backup before the InfluxDB
restore is strongly recommended, and encrypted source backups prompt for the
restore password.

## Validation

The CLI rejects invalid input without changing the file:

- unknown device
- negative watt values
- missing or invalid `pv-priority-factor`
- `pv-priority-factor < 0.01`
- `loop_interval <= 0`
- invalid offgrid value; allowed values are `off`, `eco`, and `standard`
- invalid runtime-state JSON
- unknown command
- dashboard password confirmation mismatch
- wrong dashboard current password

Examples:

```bash
python3 emsctl.py device UNKNOWN disable
python3 emsctl.py system max-power -1
python3 emsctl.py device WR1 pv-priority-factor 0
python3 emsctl.py device WR1 offgrid maybe
```

## Atomic Writes

The CLI writes via a temporary file and atomic rename:

```text
data/runtime-state.json.<pid>.tmp -> data/runtime-state.json
```

This keeps runtime-state edits robust even when the EMS is running.
