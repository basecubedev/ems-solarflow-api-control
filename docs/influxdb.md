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
| [`deploy/docker/influxdb.env.example`](../deploy/docker/influxdb.env.example) | Environment template (copy to `influxdb.env`) |

For the standalone developer telemetry-capture and state-transition analysis
workflow (separate org/buckets, read-only capture scripts), see
[develop/influxdb/](../develop/influxdb/),
[docs/develop-tool-influxdb-telemetry.md](develop-tool-influxdb-telemetry.md)
and
[docs/develop-tool-influxdb-state-transition-analysis.md](develop-tool-influxdb-state-transition-analysis.md).
That setup is development-only and independent of this production path.

## Recommended setup (local or production)

1. Create the environment file and set strong secrets:

   ```bash
   cp deploy/docker/influxdb.env.example deploy/docker/influxdb.env
   # edit deploy/docker/influxdb.env: set INFLUXDB_TOKEN, INFLUXDB_ADMIN_PASSWORD
   ```

   For a simple single-token setup keep `INFLUXDB_ADMIN_TOKEN` equal to
   `INFLUXDB_TOKEN`. The first-start values are applied **only** when the data
   volume is empty.

2. Start InfluxDB:

   ```bash
   docker compose -f deploy/docker/compose.influxdb.yml up -d
   ```

3. Enable InfluxDB in `config.json` and make sure the connection matches the
   env file. Defaults align with the env template:

   ```json
   "influxdb": {
     "enabled": true,
     "url": "http://influxdb:8086",
     "org": "ems",
     "token": "",
     "token_env": "INFLUXDB_TOKEN",
     "bucket_prefix": "ems"
   }
   ```

   Leave `token` empty and provide the token via the env var named by
   `token_env` (default `INFLUXDB_TOKEN`) so secrets are not committed. If the
   EMS runs outside the Docker network, set `url` to the reachable address
   (e.g. `http://localhost:8086`).

   The native writer enqueues one raw sample **every EMS control loop**, so the
   raw bucket keeps the highest available sampling resolution. This is
   independent of `dashboard.write_interval_seconds` (which only governs the
   SQLite dashboard history). To throttle raw writes, set
   `influxdb.raw_write_interval_seconds` to a positive number of seconds; `0`
   (the default) or `null` writes every loop.

4. Reconcile buckets, retention and downsampling tasks from config (see below).

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

`sync` requires `influxdb.enabled = true` and a resolvable token (from
`influxdb.token` or the `token_env` variable). Running `sync` twice with
unchanged config performs no writes the second time.

## Migration from `develop/influxdb/`

Earlier versions only shipped a development compose file under
`develop/influxdb/`. That path still exists for the developer capture workflow,
but the supported deployment assets are now under `deploy/docker/`. To migrate:

- use `deploy/docker/compose.influxdb.yml` and `deploy/docker/influxdb.env`
  instead of the `develop/influxdb/` equivalents,
- let `emsctl.py influx sync` manage buckets and tasks instead of importing the
  developer Flux task files manually.

Existing developer captures under `develop/influxdb/` are unaffected.
