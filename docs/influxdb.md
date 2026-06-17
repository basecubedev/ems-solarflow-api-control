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

## Beginner / bundled path (zero-config, recommended)

The bundled Docker InfluxDB is the primary supported path. You do **not** need
to understand InfluxDB tokens, create env files, or pick passwords.

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

2. Start the whole stack with one command:

   ```bash
   python3 emsctl.py stack up
   ```

   This generates `deploy/docker/influxdb.env` with secure random secrets (if
   missing), starts the bundled InfluxDB and the EMS with the **same**
   `INFLUXDB_TOKEN`, waits for readiness, and reconciles buckets/retention/tasks
   from `config.json`. Analytics is then available — no manual token handling.

If you only want to set up InfluxDB (without starting the EMS container yet):

```bash
python3 emsctl.py influx init      # generate secrets, start InfluxDB, sync schema
python3 emsctl.py influx init --no-start   # only create/merge the secret file
python3 emsctl.py influx init --no-sync    # start, but skip the schema sync
python3 emsctl.py influx status
```

`influx init` is **idempotent and safe to rerun**: it never overwrites an
existing token or password, only fills in missing values, and prints a redacted
summary (never raw secrets). The generated file uses `0600` permissions where
the filesystem supports it.

> Leave `influxdb.token` empty: bundled mode reads the token from the generated
> secret file (the variable named by `token_env`, default `INFLUXDB_TOKEN`), so
> no secrets live in `config.json`. The `DOCKER_INFLUXDB_INIT_*` bootstrap
> values are applied **only** on an empty data directory (`data/influxdb`);
> changing them later does not re-initialize InfluxDB.

> **`url` vs `host_url`:** the EMS container reaches InfluxDB by its Docker
> service name (`url`, `http://influxdb:8086`). The host-side `emsctl`
> commands (`influx init`/`sync`/`status`, `stack up`) cannot resolve that name,
> so for bundled mode they connect via `host_url` (default
> `http://127.0.0.1:8086`, the published loopback port). Legacy configs without
> `host_url` fall back to that default automatically. External mode uses `url`
> for both runtime and CLI.

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

To back up the important local EMS state (config, all runtime/history data and
the generated secrets), archive `config.json`, `data/` and the secret file:

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
provide a token. The setup helpers do not create secrets or manage containers
for external mode.

```json
"influxdb": {
  "enabled": true,
  "mode": "external",
  "auto_init": false,
  "auto_sync": false,
  "url": "http://192.168.1.50:8086",
  "org": "ems",
  "token": "",
  "token_env": "INFLUXDB_TOKEN"
}
```

```bash
export INFLUXDB_TOKEN=...        # or set influxdb.token directly
python3 emsctl.py influx sync
python3 emsctl.py influx status
```

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

## Migration from `develop/influxdb/`

Earlier versions only shipped a development compose file under
`develop/influxdb/`. That path still exists for the developer capture workflow,
but the supported deployment assets are now under `deploy/docker/`. To migrate:

- use `deploy/docker/compose.influxdb.yml` and `deploy/docker/influxdb.env`
  instead of the `develop/influxdb/` equivalents,
- let `emsctl.py influx sync` manage buckets and tasks instead of importing the
  developer Flux task files manually.

Existing developer captures under `develop/influxdb/` are unaffected.
