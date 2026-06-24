# InfluxDB Analytics Backend (optional)

The EMS dashboard can store telemetry history in InfluxDB 2.x to power the
**Analytics** tab with long-range, downsampled charts. This is an **optional**
feature.

> InfluxDB is not required for normal EMS operation. The controller writes power
> targets to hardware regardless, and the dashboard keeps a built-in SQLite
> history when InfluxDB is disabled. Enable InfluxDB only if you want the
> Analytics tab backed by long-range downsampled history.

Config is the source of truth: the `influxdb` block in `config.json` defines the
org, bucket prefix, retention and downsampling tasks, and `emsctl.py influx
sync` reconciles the live InfluxDB instance to match it.

## Deployment assets

Official, supported deployment assets live under `deploy/`:

| Path | Purpose |
|---|---|
| [`deploy/docker/compose.influxdb.yml`](../deploy/docker/compose.influxdb.yml) | Docker Compose service for InfluxDB 2.7 |
| [`deploy/docker/compose.ems-influx-env.yml`](../deploy/docker/compose.ems-influx-env.yml) | Overlay giving the EMS container the shared `INFLUXDB_TOKEN` |
| [`deploy/docker/influxdb.env.example`](../deploy/docker/influxdb.env.example) | Reference template; the env file is normally generated for you |

`deploy/docker/influxdb.env` itself is **gitignored** and holds local secrets.
The zero-config flow generates it for you with secure random values; never
commit it.

For the standalone developer telemetry-capture and state-transition analysis
workflow (separate org/buckets, read-only capture scripts), see
[develop/influxdb/](../develop/influxdb/),
[docs/develop-tool-influxdb-telemetry.md](develop-tool-influxdb-telemetry.md)
and
[docs/develop-tool-influxdb-state-transition-analysis.md](develop-tool-influxdb-state-transition-analysis.md).
That setup is development-only and independent of this production path.

## Docker-first Analytics (no repo checkout, recommended for endusers)

If you installed EMS with the Docker-first installer, enabling **Analytics** is
a single flag — you never touch the `deploy/` assets above or run
`stack up`:

```bash
sh install-docker.sh --analytics
```

This generates `config/influxdb.env` (local secrets, gitignored, never
printed), starts the bundled InfluxDB through the `with-analytics` Compose
profile, and syncs the schema. To do the same by hand from an empty folder:

```bash
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/docker-compose.yml
mkdir -p config data data/influxdb
docker compose run --rm ems python3 emsctl.py config init --analytics --yes --no-backup
docker compose run --rm ems python3 emsctl.py influx init --no-start
docker compose --profile with-analytics up -d
docker compose exec ems python3 emsctl.py influx sync
docker compose exec ems python3 emsctl.py influx status
```

In this Docker-first setup the secrets live in `config/influxdb.env` (next to
`config.json`, both under the mounted `config/` folder), so `config init
--analytics` sets:

```json
"influxdb": {
  "enabled": true,
  "mode": "bundled",
  "auto_init": true,
  "auto_sync": true,
  "secret_file": "config/influxdb.env"
}
```

Bundled InfluxDB is the technical backend; the enduser-facing feature is just
**Analytics**. The single `docker-compose.yml` means there is no overlay `-f`
chain to remember, and no host-side `python3 emsctl.py stack up` is required.

> **`stack up` and `deploy/docker/*` are the repo/native poweruser path.** They
> remain supported for repository checkouts and use the default
> `secret_file: deploy/docker/influxdb.env`. Existing configs that still point
> at `deploy/docker/influxdb.env` keep working unchanged. The Docker-first path
> below does not require cloning the repository.

## Native / repo power-user bundled path

> New users should not start here. The default beginner path is the Docker-first
> quickstart above, which uses `config/influxdb.env`. This section is the
> repo/native poweruser path: it runs `emsctl` on the host and uses the default
> `deploy/docker/influxdb.env` secret file.

This bundled path is for repository checkouts running `emsctl` natively. You do
**not** need to understand InfluxDB tokens, create env files, or pick passwords.

1. In `config.json`, enable InfluxDB (the defaults already select bundled mode):

   ```json
   "influxdb": {
     "enabled": true,
     "mode": "bundled",
     "auto_init": true,
     "auto_sync": true,
     "secret_file": "deploy/docker/influxdb.env",
     "url": "http://influxdb:8086",
     "host_url": "http://127.0.0.1:8086",
     "org": "ems",
     "token": "",
     "token_env": "INFLUXDB_TOKEN",
     "bucket_prefix": "ems"
   }
   ```

2. Run the complete, one-command setup for Analytics history:

   ```bash
   python3 emsctl.py influx init
   ```

   This is the full end-to-end bootstrap. It generates
   `deploy/docker/influxdb.env` with secure random secrets (if missing), starts
   the bundled InfluxDB container, waits until it is reachable, and (when
   `auto_sync` is true) reconciles buckets/retention/downsampling tasks from
   `config.json`. After it prints success, the dashboard Analytics tab can
   connect — **no manual Docker Compose, `.env`, bucket, retention or task setup
   is required.**

   To start the whole stack (bundled InfluxDB **and** the EMS container) in one
   go, use:

   ```bash
   python3 emsctl.py stack up
   ```

   With bundled mode and `auto_init: true`, `stack up` runs the same bundled
   bootstrap automatically before starting the EMS, so you never need to call
   Docker or the Influx CLI by hand.

Useful `influx init` variants:

```bash
python3 emsctl.py influx init              # full setup: secrets, start, sync
python3 emsctl.py influx init --no-start   # only create/merge the secret file
python3 emsctl.py influx init --no-sync    # start, but skip the schema sync
python3 emsctl.py influx status
```

`influx init` is **idempotent and safe to rerun**: it never overwrites an
existing token or password, only fills in missing values, and prints a redacted
summary (never raw secrets). The generated file uses `0600` permissions where
the filesystem supports it.

### What `enabled`, `auto_init` and `auto_sync` mean

- **`enabled: true`** turns on InfluxDB/Analytics usage. While disabled,
  `influx init` exits with a clear message and does nothing else.
- **`auto_init: true`** lets the `emsctl` **setup commands** bootstrap the
  bundled InfluxDB backend automatically (this is what makes `stack up` prepare
  secrets and start InfluxDB for you). It does **not** mean the EMS controller
  starts Docker containers during the normal control loop — it never does.
- **`auto_sync: true`** tells the setup commands to apply the bucket, retention
  and task schema automatically once InfluxDB is reachable. With
  `auto_sync: false`, `influx init` still prepares and starts bundled InfluxDB,
  then prints the next step to run manually:

  ```bash
  python3 emsctl.py influx sync
  ```

> Leave `influxdb.token` empty: bundled mode reads the token from the generated
> secret file (the variable named by `token_env`, default `INFLUXDB_TOKEN`), so
> no secrets live in `config.json`. The `DOCKER_INFLUXDB_INIT_*` bootstrap
> values are applied **only** on an empty data directory (`data/influxdb`);
> changing them later does not re-initialize InfluxDB.

> **`url` vs `host_url`:** in bundled mode InfluxDB runs in Docker, where its
> Docker service name (`url`, `http://influxdb:8086`) is only resolvable from
> inside the Docker network. So in bundled mode anything running **on the host**
> connects via `host_url` (default `http://127.0.0.1:8086`, the published
> loopback port) — that includes both the host-side `emsctl` commands
> (`influx init`/`sync`/`status`, `stack up`) **and the natively-running EMS:
> its telemetry writer and the dashboard analytics provider**. Only an EMS
> running *inside* a container uses `url` (the service name); the compose overlay
> sets `EMS_IN_CONTAINER=1` to force that. Legacy configs without `host_url` fall
> back to the loopback default automatically. External mode always uses `url`,
> for both runtime and CLI.
>
> The roles are:
>
> - `url` — runtime URL for EMS running **inside** Docker/a container.
> - `host_url` — host/native URL for `emsctl` and a natively-running EMS in
>   bundled mode (default `http://127.0.0.1:8086`).
> - `secret_file` — local bundled secret file generated by `emsctl influx init`;
>   the natively-running EMS reads `INFLUXDB_TOKEN` from it automatically.
>
> **Native EMS + bundled Docker InfluxDB is a supported setup.** After
> `python3 emsctl.py influx init` neither the runtime nor `emsctl` needs a manual
> `export INFLUXDB_TOKEN=...`: the token is resolved from the secret file.

> **Custom `secret_file` and the bundled stack:** the bundled Compose overlays
> read a fixed `env_file` (`deploy/docker/influxdb.env`). If you point
> `secret_file` at a different path, `influx init` and `stack up` refuse to
> start the bundled containers (so generated secrets can never silently diverge
> from what Compose reads). `influx init --no-start` still writes the custom
> file. Keep `secret_file` at the default to use the one-command bundled flow.

The `--json` flag on `influx init`, `influx sync`, `influx status` and
`stack up` prints exactly one machine-readable JSON object on stdout (Docker
command traces go to stderr), and never includes raw token values.

The native writer enqueues one raw sample **every EMS control loop**, so the raw
bucket keeps the highest available sampling resolution. This is independent of
`dashboard.write_interval_seconds` (which only governs the SQLite dashboard
history). To throttle raw writes, set `influxdb.raw_write_interval_seconds` to a
positive number of seconds; `0` (the default) or `null` writes every loop.

## Local data directory and backups

Bundled InfluxDB stores its database state in a repo-local bind-mounted
directory:

```text
data/influxdb/
```

This keeps all local EMS runtime/history data together under `./data/`:

```text
data/
  ems_dashboard.sqlite
  ems_state.sqlite
  runtime-state.json
  influxdb/            # bundled InfluxDB internal data
```

`data/influxdb` is **gitignored** (the whole `data/` tree is) — never commit it.
`emsctl.py influx init` and `emsctl.py stack up` create the directory (idempotent)
before starting the container, and report it:

```text
InfluxDB data directory: data/influxdb
```

### Recommended: managed backups via `emsctl backup`

For a step-by-step walkthrough, see the
[Backup and Restore Guide](backup-restore.md).

Use the built-in backup tool rather than copying files by hand. It produces
self-describing, optionally encrypted archives and never copies a live database:

```bash
python3 emsctl.py backup create --type config      # config.json, runtime state, secrets
python3 emsctl.py backup create --type databases   # local SQLite (consistent snapshots)
python3 emsctl.py backup create --type influxdb    # bundled InfluxDB data (official backup)
```

- **Config backup** — `config.json`, runtime state, dashboard auth/cert/key and
  the bundled InfluxDB secret file. May contain secrets.
- **Database backup** — `data/ems_dashboard.sqlite` and `data/ems_state.sqlite`,
  snapshotted with the SQLite online backup API. InfluxDB data is **not** part of
  this archive.
- **InfluxDB backup** — bundled InfluxDB data via the official `influx backup`
  CLI, packaged under `influxdb/` in the archive. Docker users run it inside the
  `ems` container (the CLI ships in the image; no Docker socket needed); native
  users run it from the repo, where it drives the `ems-influxdb` container via
  `docker compose`. The live `data/influxdb` directory is never copied while
  InfluxDB is running. **Bundled mode only** — external InfluxDB is rejected (use
  your provider's backup tool). Restore uses `influx restore --full`
  (replace-style) and offers a rollback InfluxDB backup first. See
  [Backup / Restore](cli.md#backup--restore) for the full flow, password
  protection and post-restore checks.

### Recommended restore order (migrating / recovering)

`influx restore --full` is replace-style and restores InfluxDB metadata (org,
buckets, users, tokens, dashboards) **and** time-series data, so the bundled
token and config must agree. Restore in this order:

1. **Restore the config backup first** — brings back `config.json` and the
   bundled InfluxDB secret (`deploy/docker/influxdb.env`).
2. **Verify** the bundled InfluxDB secret/config files are present.
3. **Restore the InfluxDB backup** (bundled mode only — external InfluxDB is not
   supported by EMS backup/restore; restore can replace existing analytics
   history, so create a rollback first; encrypted backups require the password).
4. **Verify**:

   ```bash
   python3 emsctl.py influx status
   python3 emsctl.py diagnose --deep
   ```

See [Backup / Restore](cli.md#backup--restore) for the full flow.

### Manual (offline) archive

When the stack is **stopped**, you can also archive the on-disk state directly
(`config.json`, `data/` and the secret file). Do not copy `data/influxdb` while
InfluxDB is running — use `backup create --type influxdb` instead.

```bash
tar -czf ems-backup.tar.gz config.json data/ deploy/docker/influxdb.env
```

> **Migrating from an earlier RC named volume.** Earlier `v0.6.0`
> release-candidate builds stored bundled InfluxDB data in a Docker **named
> volume** (`influxdb-data`) instead of `./data/influxdb`. New setups use the
> local directory. The old named volume is **not** removed automatically; if you
> need its history, export/import or manually copy the Docker volume into
> `data/influxdb` (with the container stopped) before removing it, e.g.
> `docker volume ls | grep influx` to find it. A fresh `data/influxdb` simply
> starts empty.

## Advanced / external path

To point the EMS at an InfluxDB you run yourself, use `mode: "external"` and
provide a token. External InfluxDB is **user-managed**: the setup helpers never
create secrets or start/stop containers for it.

```json
"influxdb": {
  "enabled": true,
  "mode": "external",
  "auto_init": false,
  "auto_sync": true,
  "url": "http://192.168.1.50:8086",
  "org": "ems",
  "token": "",
  "token_env": "INFLUXDB_TOKEN"
}
```

```bash
export INFLUXDB_TOKEN=...        # or set influxdb.token directly
python3 emsctl.py influx init    # validate settings, check connectivity, sync
python3 emsctl.py influx status
```

For external mode, `influx init` does **not** touch Docker. It validates the
`url`/`org`/`token`/`bucket_prefix` settings, checks that the server is
reachable, and — when `auto_sync` is true — reconciles the schema. With
`auto_sync: false` it only validates and checks connectivity; run
`python3 emsctl.py influx sync` to apply the schema.

If the EMS runs outside the Docker network, set `url` to the reachable address
(e.g. `http://localhost:8086`).

### Manual bundled setup (without the helpers)

If you prefer to manage the bundled compose files by hand, copy the template,
set strong secrets, and run compose from the repo root with the base compose
file first (so the `env_file` path resolves correctly):

```bash
cp deploy/docker/influxdb.env.example deploy/docker/influxdb.env
# edit deploy/docker/influxdb.env: set INFLUXDB_TOKEN and DOCKER_INFLUXDB_INIT_PASSWORD
docker compose \
  -f docker-compose.example.yml \
  -f deploy/docker/compose.influxdb.yml \
  -f deploy/docker/compose.ems-influx-env.yml up -d
python3 emsctl.py influx sync
```

For a simple single-token setup keep `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` equal to
`INFLUXDB_TOKEN`.

## Bucket and task sync (`emsctl influx`)

Buckets and downsampling tasks are **not** created by hand — they are
reconciled from `config.json` by `emsctl.py influx`:

```bash
# Reconcile the live InfluxDB to match config.json (idempotent, safe to rerun):
#  - create missing buckets ({bucket_prefix}_raw, _1m, _5m, _1h)
#  - align bucket retention with influxdb.retention.*_days
#  - create/update downsampling tasks for each influxdb.downsampling entry
#  - disable tasks that are no longer configured
python3 emsctl.py influx sync

# Report live buckets, tasks and task health:
python3 emsctl.py influx status
python3 emsctl.py influx status --json
```

`sync` requires `influxdb.enabled = true` and a resolvable token. In bundled
mode the token is read automatically from the generated secret file, so no
manual `export` is needed; in external mode provide it via `influxdb.token` or
the `token_env` variable. Running `sync` twice with unchanged config performs no
writes the second time.

## Runtime behavior and troubleshooting

The EMS controller **never starts or manages Docker containers**. When InfluxDB
is enabled but not reachable, the controller keeps running (telemetry writes are
failure-isolated and retried) and logs an actionable hint rather than failing
silently:

```text
InfluxDB is enabled but not reachable. For bundled mode run:
python3 emsctl.py influx init or start the full stack with:
python3 emsctl.py stack up
```

The dashboard Analytics tab shows the same guidance when it cannot reach
InfluxDB:

```text
Analytics history is enabled, but InfluxDB is not reachable.
Run: python3 emsctl.py influx init
```

If you see these, run `python3 emsctl.py influx init` (bundled) or check the
`url`/`token` and reachability of your external InfluxDB.

For a native EMS against bundled Docker InfluxDB, `influx init` is normally
enough: the runtime then resolves `host_url` plus the secret-file token on its
own, with no manual `export INFLUXDB_TOKEN`. If the hint persists after a
successful `emsctl influx status`, confirm the bundled container actually
publishes its port to the host loopback (`host_url`, default
`http://127.0.0.1:8086`) and that the EMS is not unexpectedly detected as
running inside a container (it honors an `EMS_IN_CONTAINER` override).

## Migration from `develop/influxdb/`

Earlier versions only shipped a development compose file under
`develop/influxdb/`. That path still exists for the developer capture workflow,
but the supported deployment assets are now under `deploy/docker/`. To migrate:

- use `deploy/docker/compose.influxdb.yml` and `deploy/docker/influxdb.env`
  instead of the `develop/influxdb/` equivalents,
- let `emsctl.py influx sync` manage buckets and tasks instead of importing the
  developer Flux task files manually.

Existing developer captures under `develop/influxdb/` are unaffected.
