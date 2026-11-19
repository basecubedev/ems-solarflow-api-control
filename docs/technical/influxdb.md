# InfluxDB Analytics Backend (optional)

The EMS dashboard can store telemetry history in InfluxDB 2.x to power its
**Analytics** page with long-range, downsampled charts. This is an **optional**
feature.

> InfluxDB is not required for normal EMS operation. The controller writes power
> targets to hardware regardless, and the dashboard keeps a built-in SQLite
> history when InfluxDB is disabled. `influxdb.enabled` defaults to `true` for
> new configs so long-term analytics support is available out of the box; set it
> to `false` in `config.json` to opt out. A config without the key counts as on
> everywhere: the EMS, the Admin's container plan and status, the dashboard and
> the backups read it the same way. Without a config.json the Admin and the
> dashboard configure no analytics; every `emsctl` command treats a missing file
> as an empty config, so its explicit `influx init`, `stack up` and
> `backup create --type influxdb` still count analytics as on. The Admin sets
> the bundled InfluxDB up only while `influxdb.secret_file` is
> `config/influxdb.env`, the file the Docker containers read; with any other
> path, the release default included, it shows "Configured · not set up", since
> the setup would write the secret into a container that is thrown away.
> Enabling the flag does not start
> anything by itself — bundled InfluxDB still needs the normal Docker/Admin/`emsctl`
> setup flow (secrets, schema, container startup), and the EMS control loop never
> starts Docker on its own.

Config is the source of truth: the `influxdb` block in `config.json` defines the
org, bucket prefix, retention and downsampling tasks, and `emsctl.py influx
sync` reconciles the live InfluxDB instance to match it.

## Deployment assets

Official, supported deployment assets live under `deploy/`:

| Path | Purpose |
|---|---|
| [`deploy/docker/compose.influxdb.yml`](../../deploy/docker/compose.influxdb.yml) | Docker Compose service for the bundled InfluxDB 2.x; the one pin of its image |
| [`deploy/docker/compose.ems-influx-env.yml`](../../deploy/docker/compose.ems-influx-env.yml) | Overlay giving the EMS container the shared `INFLUXDB_TOKEN` |
| [`deploy/docker/influxdb.env.example`](../../deploy/docker/influxdb.env.example) | Reference template; the env file is normally generated for you |

`deploy/docker/influxdb.env` itself is **gitignored** and holds local secrets.
The zero-config flow generates it for you with secure random values; never
commit it.

For the standalone developer telemetry-capture and state-transition analysis
workflow (separate org/buckets, read-only capture scripts), see
[develop/influxdb/](../../develop/influxdb/),
[docs/develop-tool-influxdb-telemetry.md](../developer/design-notes/develop-tool-influxdb-telemetry.md)
and
[docs/develop-tool-influxdb-state-transition-analysis.md](../developer/design-notes/develop-tool-influxdb-state-transition-analysis.md).
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
   `config.json`. After it prints success, the dashboard's Analytics page can
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

- **`enabled: true`** turns on InfluxDB/Analytics usage and is the default for
  new configs. It only enables support; the bundled backend still relies on the
  Docker/Admin/`emsctl` setup/init flow for secrets, schema and container
  startup. While disabled, `influx init` exits with a clear message and does
  nothing else.
- **`auto_init: true`** lets the `emsctl` **setup commands** bootstrap the
  bundled InfluxDB backend automatically (this is what makes `stack up` prepare
  secrets and start InfluxDB for you). It does **not** mean the EMS controller
  starts Docker containers during the normal control loop — it never does.
- **`auto_sync: true`** tells the setup commands to apply the bucket, retention
  and task schema automatically once InfluxDB is reachable, and lets the
  running EMS keep it in place (see
  [The EMS keeps the schema in place](#the-ems-keeps-the-schema-in-place)).
  With `auto_sync: false`, nothing creates the schema by itself: `influx init`
  still prepares and starts bundled InfluxDB, then prints the next step to run
  manually:

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

- **Config backup** — `config.json`, runtime state, dashboard auth/cert/key,
  the bundled InfluxDB secret file and the credential store. May contain secrets.
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
  [Backup / Restore](../cli.md#backup--restore) for the full flow, password
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

See [Backup / Restore](../cli.md#backup--restore) for the full flow.

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

## Upgrading the bundled InfluxDB

The bundled InfluxDB image is pinned in one place,
[`deploy/docker/compose.influxdb.yml`](../../deploy/docker/compose.influxdb.yml),
to an exact `2.x.y` tag. The installers, the root `docker-compose.yml`, the
developer stack under `develop/influxdb/` and the licence inventory repeat
that line; `python3 tools/bundled_influxdb.py --check` names a copy that
differs and `--sync` rewrites them, and a test fails while one differs. CI's
analytics job starts the pinned image itself rather than a copy of it.

**New versions are tracked by Dependabot.** Its `docker-compose` block watches
`deploy/docker/` and opens a pull request when Docker Hub carries a newer
`influxdb` tag of the same form; **Repo dependency refresh** then carries the
pin into the copies. The `docker` block only ever read the `Dockerfile`, whose
`influxdb` stage ships the `influx` CLI and not the server, which is how the
server stayed on 2.7 while the CLI stage moved to 2.9. A release on GitHub
reaches Docker Hub a few days later, through the official-images review;
Dependabot sees it from then on.

**What an upgrade does to the data.** InfluxDB 2.x upgrades in place: the new
`influxd` converts the metadata (`influxd.bolt`, `influxd.sqlite`) at its first
start, keeping a copy of the old `influxd.bolt` beside it, and leaves the time
series as they are. The step is one way. From 2.8 on the conversion hashes the
API tokens, and from 2.9 that is the default; an older `influxd` refuses the
converted directory with *DB contains record of unknown migration*, and
`influxd downgrade` below 2.8 erases every token. The EMS never reads a token
back from InfluxDB (it keeps its own in `influxdb.env`), so hashing changes
nothing for it.

A Guided Setup over an existing installation keeps the InfluxDB version its
data was written by in the new `docker-compose.yml`, so the version moves
only on purpose: the version the InfluxDB container ran, or else the one the old
compose file named. When a Setup with Analytics on finds InfluxDB data in
`data/influxdb` and neither names a version, it stops, before it writes
anything, and asks for it rather than start some version on the data. A Setup
that fails after the installer wrote the new compose file still leaves the
dated version named in it, and while the installer runs the dating is noted
beside the Setup's marker, so a retry dates the data the same way even when
the Admin stopped in between. The note counts only while `docker-compose.yml`
names nothing or the version that Setup wrote; a file changed since says
more. `install-docker.sh --force` and a native `emsctl
stack up` after a `git pull` take the version the release pins, which
upgrades InfluxDB in place without a backup.

**By hand**: back up first, then move the image and recreate only InfluxDB. In
a Docker-first installation directory, after setting `image:` of the
`influxdb` service in `docker-compose.yml` to the release's pin:

```bash
docker compose exec ems python3 emsctl.py backup create --type influxdb
docker compose --profile with-analytics up -d influxdb
docker compose exec ems python3 emsctl.py influx status
```

In a native checkout, whose `git pull` already moved the pin:

```bash
python3 emsctl.py backup create --type influxdb
docker compose -f docker-compose.example.yml -f deploy/docker/compose.influxdb.yml \
  -f deploy/docker/compose.ems-influx-env.yml up -d --force-recreate influxdb
python3 emsctl.py influx status
```

The way back by hand: stop InfluxDB, rename `data/influxdb` and create it
empty, put the previous `image:` back, start it
with `docker compose --profile with-analytics up -d --force-recreate
--renew-anon-volumes influxdb` (it sets itself up from `influxdb.env`; without
fresh anonymous volumes it finds the old CLI configuration in `/etc/influxdb2`
and refuses), and restore the backup with
`emsctl backup restore <archive> --on-conflict replace`. The files belong to
InfluxDB's user, so the moves need root, for example
`docker run --rm -v "$PWD/data:/d" --entrypoint sh <influxdb image> -c 'mv …'`.

A native checkout's `emsctl backup` only starts the InfluxDB container that
ran on the data and creates none, so a pulled pin never converts the data
before the backup has read it. After `docker compose down` there is no such
container: name the image that wrote the data in
`deploy/docker/compose.influxdb.yml` (after a pull that moved the pin, the
earlier one), start it with `emsctl influx init` and back up then. A restore may
create a container, since what it restores replaces the data. `emsctl stack up` and `influx init` do recreate InfluxDB on a
changed pin. Only an `influxdb:2.x[.y]` image line is managed; a digest, a
registry prefix or another flavour is left to its owner.

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
#  - disable tasks that are no longer configured, and any duplicate of one
#    that is still configured
python3 emsctl.py influx sync

# Create only what is missing and delete nothing (what the Admin console's
# Restart EMS now and Repair analytics run; the EMS runs the same, see below):
python3 emsctl.py influx sync --safe

# Report live buckets, tasks and task health:
python3 emsctl.py influx status
python3 emsctl.py influx status --json

# Delete the tasks sync disabled (list them first):
python3 emsctl.py influx prune --dry-run
python3 emsctl.py influx prune
```

`sync` requires `influxdb.enabled = true` and a resolvable token. In bundled
mode the token is read automatically from the generated secret file, so no
manual `export` is needed; in external mode provide it via `influxdb.token` or
the `token_env` variable. Running `sync` twice with unchanged config performs no
writes the second time.

Rerunning a `sync` is always safe; running two **at the same time** is not.
InfluxDB does not make task names unique, so two syncs that both find a task
missing both create it, and the extra keeps writing the same window. Every sync
on the host therefore takes one lock (see
[The EMS keeps the schema in place](#the-ems-keeps-the-schema-in-place)); a
duplicate can still come from an older release or from a sync run on another
machine against the same InfluxDB. A later `sync` switches the duplicate to
`inactive` and reports it as `disabled (duplicate)`; it is not deleted, because
disabling is reversible and deleting is not. `influx prune` is the separate
second step that removes it.

A second `downsampling` entry with the same `target` asks for the same task, so
it is reported as `disabled (duplicate_target)` instead — that one is a
configuration mistake rather than a race, and the fix is to remove the extra
entry.

### Removing what `sync` retired

`sync` never deletes a task, so disabled ones accumulate and every later `sync`
repeats them. `influx prune` clears them, and it removes **only** what `sync`
already stopped:

- a task owned by this `bucket_prefix` that the config no longer names, or a
  duplicate of one it still names, and that is already `inactive` — deleted,
  with the same reason `sync` gave it;
- the same task while it is still `active` — reported as `kept (still_active)`
  and left alone. Stopping a task and removing it stay two decisions, so a
  `prune` on its own can never take a running task out from under the
  dashboard. Run `sync` first;
- anything not owned by this prefix — never touched, whatever its state.

`--dry-run` lists what would go without deleting anything. A task the API
returns without a usable id is skipped, because nothing can be addressed to it.

Admin removes the container of a one-off run it killed, so a retry does not
normally meet the first run still working. When that removal fails — a wedged
Docker daemon can defeat it — the error names the container it left behind, and
that container has to go before the action is tried again.

### The EMS keeps the schema in place

The setup commands run the sync once, and not every installation path reaches
them: a fresh appliance install starts InfluxDB and the EMS together, and
InfluxDB's own first-start setup creates only `<prefix>_raw` (kept forever,
not for `raw_days`). So while `enabled` and `auto_sync` are true, the running
EMS syncs the schema itself:

- about 20 seconds after it starts;
- again after 30 s, 1, 2, 5 and then every 10 minutes for as long as InfluxDB
  does not answer or refuses — it never gives up;
- and every 30 minutes once the schema is in place, so a bucket or task that
  goes missing later comes back. A complete schema costs a few lookups and no
  writes; a bucket whose retention differs costs one more query, once.

It runs the **safe** sync, like `influx sync --safe` and the Admin console's
**Restart EMS now** and **Repair analytics**: it creates the buckets and tasks
that are missing and deletes nothing. The one thing it does beyond them is to
apply a retention you confirmed in the Admin console, below.

- A retention is shortened only when no point in the bucket is old enough to
  fall out of it. An appliance whose raw bucket has kept months of telemetry
  forever keeps it, because that bucket is then the only history there is — the
  downsampling tasks only aggregate forward from the moment they exist. The EMS
  says so once as `event=influx_schema_retention_kept` and does not ask that
  bucket again while it runs; `influx sync` (without `--safe`) applies the
  configured retention and deletes the older data.
- A retention you change in the Admin console's settings and confirm there is
  applied after all, older data included: confirming the change records each
  bucket, its InfluxDB and its new retention in
  `data/influx-retention-confirmed.json`, as long as `influxdb.auto_sync` is
  on. Once the EMS runs the saved settings on real hardware (not in simulation
  or replay), it applies exactly that retention, logs
  `event=influx_schema_confirmed_retention_applied` and removes the entry.
  An entry counts only for the bucket, InfluxDB and retention it names. An EMS
  running settings that name another retention or InfluxDB leaves it for the
  restart. As the EMS starts, it removes every entry confirmed before it read
  settings that do not ask for it — a backup restored, `config.json` edited by
  hand, analytics or `auto_sync` switched off, another InfluxDB — and logs
  `event=influx_schema_confirmed_retention_dropped`; such a retention keeps the
  rule above, and setting it again later needs a new confirmation. An apply
  that switches analytics or `auto_sync` off or back on clears the earlier
  entries as well; a retention changed in that same apply is still recorded. A retention InfluxDB refuses is logged once as
  `event=influx_schema_confirmed_retention_refused`; the entry stays and the
  rest of the schema is still reconciled. An entry the EMS cannot remove is
  logged once as `event=influx_schema_confirmed_retention_unsettled`.
  `influx status` marks a bucket whose confirmed retention is still to come
  with `"confirmed_retention": true`.
- Every change to a retention in days asks InfluxDB for the shard-group
  duration it would give a new bucket with that retention (1 hour below 2
  days, 1 day below 180 days, 7 days above). A bucket made to keep its data
  forever has week-long shard groups, and InfluxDB refuses a retention shorter
  than them. A retention of 0 is sent as an expiry of 0, which is what makes
  InfluxDB keep a bucket's data forever.
- A check that fails — InfluxDB
  still settling, a query that times out — keeps the retention for that round
  and is asked again in the next one; the first such failure for a bucket is
  logged as `event=influx_schema_retention_unchecked`.
- A task that exists is left as it is, even when it is switched off or its Flux
  differs from the config, and nothing is retired. The EMS names such tasks once
  as `event=influx_schema_task_kept`; `influx sync` reconciles them.

It logs what it created or changed as `event=influx_schema_synced`
(`created_buckets=…`, `created_tasks=…`, `updated_buckets=…`), and a failure as
`event=influx_schema_sync_failed` with `attempt` and `retry_in_s`. The first
three failures are expected while a board's InfluxDB starts and are logged at
info level; a long outage is logged once an hour. A token InfluxDB refuses is
named once as `event=influx_schema_sync_not_permitted`, with `status=401` (the
token is unknown — InfluxDB's data was reset or restored) or `status=403` (the
token may not create buckets or tasks).

Every schema write on the host — the EMS's own, `influx sync`, `influx prune`
and the Admin console's — takes one lock, `data/influx-schema.lock`, so two of
them never create the same task side by side. `influx sync` waits up to a
minute for one already running and then stops with a message; the EMS does not
wait and asks again half a minute later. The EMS's sync works on its own thread,
so neither a slow InfluxDB nor a failing sync touches the control loop.

## Runtime behavior and troubleshooting

The EMS controller **never starts or manages Docker containers**. When InfluxDB
is enabled but not reachable, the controller keeps running (telemetry writes are
failure-isolated and retried) and logs an actionable hint rather than failing
silently. Inside the EMS container (bundled mode) it reads:

```text
InfluxDB is enabled but not reachable. To add the bundled InfluxDB run on the
host: sh install-docker.sh --analytics. Analytics are optional: set
influxdb.enabled=false in config/config.json to run without them.
```

A native EMS names `python3 emsctl.py influx init` and `python3 emsctl.py stack
up` instead, and an external InfluxDB names `influx status` through
`docker compose exec ems` in the container. Each kind of writer warning comes
at once, then a minute later, and from then on half as often each time, down to
once an hour; another kind is named at once, and the first successful write ends
every pause, so the next failure is named at once again.

The dashboard's **Analytics** page shows the same guidance when it cannot reach
InfluxDB:

```text
Analytics history is enabled, but InfluxDB is not reachable.
Run: python3 emsctl.py influx init
```

If you see these, run `python3 emsctl.py influx init` (bundled) or check the
`url`/`token` and reachability of your external InfluxDB.

### InfluxDB answers but refuses the writes

A write InfluxDB answered with an error status reached a running database, so
the hint names its answer instead: a refused token (401, 403), a missing raw
bucket (404, which the EMS creates by itself unless `auto_sync` is off), or any
other status with the place InfluxDB logs its cause — `docker logs ems-influxdb`
for the bundled one. The log line carries the start of InfluxDB's answer as
`detail`, which for a 400 or 422 is the whole reason. `influx status` reports whether telemetry is arriving at
all (`raw_latest`, the newest raw point of the last 15 minutes, or of three
write intervals where `raw_write_interval_seconds` is longer), and the Admin
console's **Run the checks** warns when none is — except in simulation, where
the EMS writes no telemetry.

One cause seen on an appliance: the bundled InfluxDB runs as its own user
inside its container and owns `data/influxdb`. Handing that directory to
another user while InfluxDB runs — a recursive `chown` of `data/` — leaves it
unable to open a new WAL segment or shard, and every write is refused with
HTTP 500 and `permission denied` in its log until it restarts. Its image gives
the directory back to its user on every start, so `docker restart ems-influxdb`
ends it; the Admin console's own permission repair leaves `data/influxdb`
alone for this reason.

### Analytics is empty although InfluxDB is reachable

A different fault with a different fix. Each range reads the bucket its query
profile names: 1h and 6h read `<prefix>_raw`, 24h reads `<prefix>_1m`, and the
longer ranges read `<prefix>_5m` and `<prefix>_1h`. A schema that was never
created leaves every range empty; a partial one leaves the short ranges working
and empties the rest, which is easy to miss because the page opens on 24h. Either
way the **Analytics** page says so:

```text
Analytics history is enabled, but a bucket this range reads is missing.
Unless influxdb.auto_sync is off, the EMS creates it by itself; it checks every
half hour. To create it now, use Repair analytics on the Admin console's
Services page, or run: python3 emsctl.py influx sync --safe
```

The message is the EMS's own text, written before the Modern Admin Console:
**Repair analytics** is on its **Services** page.

The EMS also says so without anyone opening the page. Once the telemetry writer
has completed its first write — which proves InfluxDB is reachable and the token
works, and is the earliest point at which a missing bucket can be told apart
from an unreachable server — it checks the planned buckets *and* the
downsampling tasks and logs:

```text
event=influx_schema_incomplete hint=... missing_buckets=ems_1m,ems_5m,ems_1h
```

The tasks are checked beside the buckets because `sync` creates buckets first. A
run that stops in between leaves every bucket in place with nothing filling
them: the query succeeds, the chart is empty, and `influx status` reports no
missing buckets. That case shows up as `missing_tasks=…` in the same event and
is the only signal for it. A task that exists but is `inactive` counts as
missing there, because it fills nothing either.

A bucket that only a `query_profiles` entry names — one that no `downsampling`
entry produces, so no sync will ever create it — is reported separately as
`event=influx_schema_bucket_not_planned`, because there the two halves of the
config disagree and running a sync changes nothing.

The check stops as soon as it reads a complete schema, so a successful
`influx sync` ends it silently. Until then it repeats at most a few times, a
minute apart: a reading taken while a sync is midway through is true at that
instant and wrong a moment later, and treating the first one as final would
leave a false warning standing for as long as the EMS runs.

`python3 emsctl.py influx status` lists them under `missing_buckets` alongside
an empty `tasks`. The usual causes are a schema sync that ran while InfluxDB was
still starting, and an install path that never ran one. With `auto_sync` on,
the EMS settles both within minutes of InfluxDB answering
([The EMS keeps the schema in place](#the-ems-keeps-the-schema-in-place)); a
sync run by hand against a container that is already up succeeds at once. Note that the downsampling tasks
aggregate forward from the moment they are created — the raw data is kept, but
the longer ranges fill in over the following hours rather than at once.

For a native EMS against bundled Docker InfluxDB, `influx init` is normally
enough: the runtime then resolves `host_url` plus the secret-file token on its
own, with no manual `export INFLUXDB_TOKEN`. If the hint persists after a
successful `emsctl influx status`, confirm the bundled container actually
publishes its port to the host loopback (`host_url`, default
`http://127.0.0.1:8086`) and that the EMS is not unexpectedly detected as
running inside a container (it honors an `EMS_IN_CONTAINER` override).

### Battery cell voltages

Each pack of a device the EMS controls that reports its cell voltages writes
one `battery_pack` point per cycle while the device's telemetry is current (a state held past its last read
writes none), tagged `device` and `pack` (numbered from 1, see
[the Devices page](../dashboard.md#devices-page)), with `cell_min_v` and
`cell_max_v` in volts; a value the pack does not report is left out.

Each downsampling step has a second task for them, `<prefix>-downsample-<target>:cells`
beside `<prefix>-downsample-<target>`, which carries each window's lowest
`cell_min_v` and highest `cell_max_v` into the `1m`, `5m` and `1h` buckets
rather than their means, so a dip of a few minutes still shows in a 30-day
chart; Analytics reads them the same way. Being tasks of their own, they reach
an installation that already has its schema without anyone asking: the EMS's
safe sync ([The EMS keeps the schema in
place](#the-ems-keeps-the-schema-in-place)) creates a task that is missing, and
until it has, `influx status` and the EMS log name them under `missing_tasks`.
The cell history in the downsampled buckets starts when they do.

### The device temperature up to v0.10.0

The raw `temp` field holds the device temperature in °C. Zendure devices report
it in tenths of a kelvin (`hyperTmp`, `(value - 2731) / 10`); releases up to
v0.10.0 divided by 100, so 13.0 °C was stored as 28.61 and 0 °C as 27.31. A
device that reports no temperature now writes no `temp` point, where up to
v0.10.0 it, and every E3/DC (`source=e3dc`), wrote `0`; nor does one that
reports a value outside -60 to 120 °C, which up to v0.10.0 was stored divided
by 100 like any other.

To convert history written before the upgrade, take the `source=zendure` points
from before the upgrade time whose value lies between 21.31 and 39.31 (-60 to
120 °C in the old scale) and apply `°C = (stored - 27.31) * 10`; the others,
the `0` placeholders among them, are not readings and are best dropped. The
same holds for the `1m`, `5m` and `1h` buckets, except for windows that contain
such a value or straddle the upgrade, where old and new scales are averaged
together; recompute those from the converted raw points or leave them out.

## Migration from `develop/influxdb/`

Earlier versions only shipped a development compose file under
`develop/influxdb/`. That path still exists for the developer capture workflow,
but the supported deployment assets are now under `deploy/docker/`. To migrate:

- use `deploy/docker/compose.influxdb.yml` and `deploy/docker/influxdb.env`
  instead of the `develop/influxdb/` equivalents,
- let `emsctl.py influx sync` manage buckets and tasks instead of importing the
  developer Flux task files manually.

Existing developer captures under `develop/influxdb/` are unaffected.
