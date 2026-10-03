# Docker

The improved Docker first-run bootstrap is available in `latest` and `v0.6.0`
or newer releases. Older container tags use the previous manual setup
procedure.

For the beginner flow, start with [quickstart.md](quickstart.md). This page is
the Docker reference for mounts, tags, CLI commands, permissions, updates, and
troubleshooting details.

If Docker is not installed yet, see [install-docker.md](install-docker.md).
For daily copy/paste commands, see [common-commands.md](common-commands.md).

## Prerequisites

Required: Docker with Docker Compose v2.24.0 or newer (`docker compose`, not the
legacy `docker-compose`). Works on Linux, macOS, and Windows. On macOS/Windows
use Docker Desktop; Windows must use Linux containers.

## Docker-first installer (recommended)

The installer is the shortest path for endusers. It needs only Docker and runs
from an empty folder — no repository clone, no overlay Compose files, and no
host-side Python.

Linux/macOS:

```bash
mkdir -p ems-solarflow-api-control && cd ems-solarflow-api-control
curl -fsSLo install-docker.sh https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh
sh install-docker.sh                 # EMS only
sh install-docker.sh --analytics     # EMS + Analytics (bundled InfluxDB)
```

Windows PowerShell (Docker Desktop with Linux containers):

```powershell
irm https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.ps1 -OutFile install-docker.ps1
powershell -ExecutionPolicy Bypass -File .\install-docker.ps1            # EMS only
powershell -ExecutionPolicy Bypass -File .\install-docker.ps1 -Analytics # EMS + Analytics
```

Installer flags:

| Shell (`sh install-docker.sh`) | PowerShell (`install-docker.ps1`) | Effect |
|---|---|---|
| `--analytics` | `-Analytics` | Enable Analytics (bundled InfluxDB) |
| `--tag v0.6.0` | `-Tag v0.6.0` | Pin the container image tag |
| `--no-start` | `-NoStart` | Prepare files but do not start |
| `--dry-run` | `-DryRun` | Show actions without writing/starting |
| `--force` | `-Force` | Overwrite existing compose/config |
| `--help` | `-Help` | Show help |

The installer checks Docker (requires Docker Compose v2.24.0+, because the
single compose file uses an optional `env_file.required:false`), creates
`config/` and `data/`, writes `docker-compose.yml`, generates
`config/influxdb.env` for Analytics, and starts the stack. For Analytics it also
writes a local `.env` with `COMPOSE_PROFILES=with-analytics` so plain
`docker compose up -d` keeps starting EMS **and** InfluxDB.

For EMS-only installs, `config/config.json` is created on first container start.
With Analytics, the installer creates it during setup because it runs
`config init --analytics`. With `--no-start`/`-NoStart`, an EMS-only
`config/config.json` is created the first time you run `docker compose up -d`.

`--dry-run`/`-DryRun` prints the planned actions without running Docker, so it
is useful even when Docker is missing or older than v2.24.0: prerequisite
problems are reported as warnings instead of aborting.

## Manual install path (full control)

Every step the installer performs, run by hand. Compose V2 uses
`docker compose` (with a space), not the legacy `docker-compose`.

The downloaded `docker-compose.yml` pins an older EMS image tag; the `sed` line
below points it at `latest` before the first pull (the installer does this for
you). Use a release tag such as `v0.8.11` instead of `latest` to stay on one
release.

EMS only:

```bash
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
mkdir -p config data
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/docker-compose.yml
sed -i 's#ems-solarflow-api-control:v[0-9][0-9.]*#ems-solarflow-api-control:latest#' docker-compose.yml
docker compose pull
docker compose up -d
```

EMS + Analytics (bundled InfluxDB):

```bash
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
mkdir -p config data data/influxdb
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/docker-compose.yml
sed -i 's#ems-solarflow-api-control:v[0-9][0-9.]*#ems-solarflow-api-control:latest#' docker-compose.yml
docker compose run --rm ems python3 emsctl.py config init --analytics --yes --no-backup
docker compose run --rm ems python3 emsctl.py influx init --no-start
docker compose --profile with-analytics up -d
docker compose exec ems python3 emsctl.py influx sync
docker compose exec ems python3 emsctl.py influx status
```

`config init --analytics` enables bundled InfluxDB and points
`influxdb.secret_file` at `config/influxdb.env`. `influx init --no-start`
generates the local secrets without starting anything. Analytics secrets stay
local in `config/influxdb.env` (gitignored) and are never printed.

The single `docker-compose.yml` keeps EMS-only simple (`docker compose up -d`)
and adds Analytics behind the `with-analytics` profile — no overlay `-f` chain
and no host-side `python3 emsctl.py stack up`. `stack up` remains a
repo/native poweruser helper; see [influxdb.md](technical/influxdb.md).

## Verifying a Docker-first install

After `sh install-docker.sh` (EMS only) from an empty folder, confirm:

- `docker-compose.yml`, `config/config.json`, and `data/` exist
- the EMS container is up: `docker compose ps`
- the dashboard answers on `http://localhost:8080`
- `docker compose exec ems python3 emsctl.py diagnose` runs

After `sh install-docker.sh --analytics`, also confirm:

- `config/influxdb.env` and `data/influxdb/` exist
- the InfluxDB container is up and reachable on `http://localhost:8086`
- `docker compose exec ems python3 emsctl.py influx status` runs

Docker does not automatically run a container as the same user that runs
`docker compose`. Creating `config` and `data` before the first start as your
normal host user is usually enough: EMS detects the owner of these mounted
directories and runs as that UID/GID.

The recommended Compose file uses service name `ems`, port mapping
`8080:8080`, and bind mounts `./config:/app/config` and `./data:/app/data`.

`PUID` and `PGID` are optional for the standard flow. If you want to set the
runtime UID/GID explicitly, start with:

```bash
PUID=$(id -u) PGID=$(id -g) docker compose up -d
```

On first start, the container creates `config/config.json` from the built-in
`config.template.json` if the file does not exist yet. Existing config files
are never overwritten.

The container starts as root only long enough to select the runtime UID/GID,
then re-executes the entrypoint as that non-root user before creating
`config/config.json` or writing runtime data. Files written into the
bind-mounted `./config` and `./data` directories are owned by that detected or
explicit UID/GID.

`docker compose exec ems python3 emsctl.py ...` starts as root because `exec`
bypasses the entrypoint, so `emsctl.py` drops to the same runtime UID/GID before
running. Backups, restores, and config upgrades stay owned by your host user —
no extra `--user` flag needed.

Edit the generated configuration and restart:

```bash
nano config/config.json
docker compose restart
```

Optional guided setup inside the container:

```bash
docker compose exec ems python3 emsctl.py config init
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

`config init` is optional and does not blindly replace an existing edited
config. Choose your grid meter in the guided setup assistant. For a Zendure
SmartMeter D0, select "Zendure Grid Meter via local HTTP (D0 / Smart Meter
3CT)" (recommended), or "Zendure SmartMeter D0 via MQTT" if it only reaches you
over MQTT. Manual editing of
`config/config.json` remains fully supported.

## CLI Inside Docker

With the recommended Compose service name `ems`, `emsctl.py` automatically
uses `/app/config/config.json` when no legacy `/app/config.json` exists:

```bash
docker compose exec ems python3 emsctl.py status
docker compose exec ems python3 emsctl.py interactive
docker compose exec ems python3 emsctl.py dashboard auth-status
docker compose exec ems python3 emsctl.py config init
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --deep
docker compose exec ems python3 emsctl.py diagnose --hardware
docker compose exec ems python3 emsctl.py diagnose --control
docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

The runtime-state path is still read from the selected config. With the
generated Docker config, `data/runtime-state.json` resolves to
`/app/data/runtime-state.json`.

`diagnose` is safe and read-only by default. In a container it also reports
container detection sources, runtime UID/GID, `/app/config` and `/app/data`
readability/writability, ownership, and whether mutable paths resolve below
`/app/data`. `--deep` adds local SQLite/log/dashboard checks and host-side
Docker Compose hints when Docker is available. `--support-bundle` creates a
redacted ZIP suitable for GitHub/support issues. Exit code `1` means at least
one diagnostic error was found; warnings still exit `0`.

Use `diagnose --control` when EMS is running but current output looks
unexpected. Use `diagnose --control-quality --sample-seconds 60` when export
peaks, PV utilization, or SOC balancing need to be evaluated over a short
sample window. Use `diagnose --support-bundle` before opening an issue; the ZIP
is redacted and contains safe diagnostic material for maintainers.

For unusual mounts or troubleshooting, pass the config path explicitly:

```bash
docker compose exec ems python3 emsctl.py --config /app/config/config.json status
```

## Generated Files

```text
./config/config.json                 user configuration
./data/runtime-state.json            temporary runtime state
./data/ems_dashboard.sqlite          dashboard statistics database
./data/ems_dashboard.sqlite-wal      normal SQLite WAL file
./data/ems_dashboard.sqlite-shm      normal SQLite SHM file
```

The recommended Compose file mounts `./config` to `/app/config` and `./data` to
`/app/data`. Runtime state and the dashboard SQLite database are created
automatically when these paths are writable and survive container recreation.
Do not store runtime state or database files inside the image.

The container does not overwrite existing config files and does not
recursively take ownership of the mounted `./config` and `./data` directories.
If Docker or your host creates directories with unexpected ownership, stop the
container and adjust the directory ownership on the host before starting it:

```bash
mkdir -p config data
sudo chown -R "$(id -u):$(id -g)" config data
docker compose up -d
```

The runtime state file is created automatically on startup if it does not
exist. Older setups may have used `runtime-state.json` in the project root. If
you switch to `data/runtime-state.json`, the old root-level file is no longer
required and may be removed manually. EMS will automatically create a new
runtime-state file if the configured file does not exist.

## Time zone

The winter adjustment hour (`winter.adjust_hour`) and the full-charge assist
`force_time` are local hours, and inside a container local time is whatever
`TZ` says. The Compose file passes `TZ` from `.env` to the EMS container;
without it the container runs on UTC. `emsctl.py diagnose` reports the zone in
effect, judged by the zone file rather than the name, with its offset where it
reads one, and warns where the hours are likely not the ones meant — among
them a `TZ` that is set but empty, no `TZ` reaching the container while its
`/etc/localtime` runs on UTC (a Compose file from before `TZ`), a UTC name that
reads another zone, a `GMT+N` offset written the POSIX way, and a name the
image does not know. A `.env` without `TZ` under the current Compose file runs
on its default and reports UTC.

The installers write the zone into `.env`: a `TZ` exported in the shell that
runs them first, then a `TZ` already in `.env` (the last one, as Compose reads
it), then the host's own zone — except inside a container, where they read none
and the containers stay on UTC until `TZ` is set. A
name has to be a current IANA name such as `Europe/Berlin`; legacy names like
`US/Eastern` or `Europe/Kiev` are not installed in the image and would run on
UTC. Windows PowerShell 5.1 cannot translate a Windows zone name, so on Windows
the installer asks you to set `TZ` in `.env` yourself unless PowerShell 7.2 or
later is used.

Compose takes a `TZ` exported in your shell before the one in `.env`. If your
shell exports one (some systems set `TZ=:/etc/localtime`), unset it or run
compose with `env -u TZ docker compose up -d`.

Set `TZ` rather than mounting the host's `/etc/localtime` into the container.
In the image `/etc/localtime` points at `Etc/UTC`, so the mount lands on the
UTC zone file itself: the container then runs on the host's hours while `TZ`
reads `UTC`, and so does every zone name that resolves to that file.
`diagnose` warns when a UTC name reads another zone.

An installation made before the Compose file carried `TZ` runs on UTC, and an
upgrade does not add the line. Add it to the `ems` service and the zone to
`.env`, then recreate the container:

```yaml
    environment:
      TZ: "${TZ:-UTC}"
```

```bash
echo "TZ=Europe/Berlin" >> .env
docker compose up -d
```

The Admin Console records the zone when it is installed (as `EMS_TIMEZONE`)
and writes it into the `.env` of an EMS deployment it prepares when that file
names no zone: no `TZ`, or an empty last one. The line it
writes ends in `# written by the Admin Console` and follows the Admin's zone on
the next prepare; a `TZ` you set there is kept.

## Verify Non-Root Runtime

Verify the real PID 1 user inside the running container:

```bash
docker compose exec ems sh -c 'cat /proc/1/status | grep -E "^(Name|Uid|Gid):"'
```

The `Uid:` and `Gid:` lines should show non-zero values. They should match
explicit `PUID`/`PGID` values if set, otherwise they should match the detected
owner of the mounted `config` and `data` directories.

Verify host file ownership:

```bash
ls -ln config data
find config data -maxdepth 1 -type f -printf '%u:%g %p\n'
```

The generated files should be owned by your host UID/GID, not `0:0`.

You can also verify writes from inside the container:

```bash
docker compose exec ems sh -c 'touch /app/data/non-root-write-test && id && ls -ln /app/data/non-root-write-test'
ls -ln data/non-root-write-test
```

For an end-to-end Docker validation from the repository checkout:

```bash
scripts/validate_docker_non_root.sh
```

The script builds a local image, verifies `/proc/1/status` reports non-zero
UID/GID, checks file ownership in `/app/config` and `/app/data`, and confirms
root-owned bind mounts fail startup instead of running as root.

## Template Warning

If `config/config.json` still matches the shipped template, the container logs
a warning:

```text
WARNING: config.json still matches the shipped template.
Please review ./config/config.json and configure your installation.
Startup continues in safe mode until required placeholders are replaced.
Hardware writes are disabled while template placeholders remain.
```

Startup continues in safe mode until required placeholders are replaced.
Existing config validation and runtime errors still report missing IPs, serial
numbers, or invalid device settings.

## Updating

For `latest`, create password-protected backups, pull the current image,
recreate the container, check whether new config keys are available, apply the
upgrade with a normal config backup, and run diagnostics:

```bash
docker compose exec ems python3 emsctl.py backup create --type config --password
docker compose exec ems python3 emsctl.py backup create --type databases --password
# Analytics users (bundled InfluxDB) — back up history too:
docker compose exec ems python3 emsctl.py backup create --type influxdb --password
docker compose pull
docker compose up -d
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade --yes --backup
docker compose exec ems python3 emsctl.py diagnose
```

If you intentionally do not want password protection for local backups, use:

```bash
docker compose exec ems python3 emsctl.py backup create --type config
docker compose exec ems python3 emsctl.py backup create --type databases
```

Backups created in Docker are stored on the host under `data/backups/`, using
the existing `./data:/app/data` mount. No separate backup volume is needed for
the standard setup, and existing installs get persistent backups automatically
after pulling the updated image — even without editing their compose file.

Password-protected backups are recommended because config backups may include
secrets, device serials, dashboard authentication paths, TLS keys, and bundled
InfluxDB secrets. Without the password, encrypted backups cannot be restored.
`config upgrade --dry-run` should be reviewed before applying changes.
`config upgrade --yes --backup` creates a normal config backup before writing.
Bundled InfluxDB backups are only needed when bundled InfluxDB analytics is
enabled.

For stable deployments, pin a release tag in `docker-compose.yml`, then update
that tag intentionally when you want to move to a newer release. Existing
`./config` and `./data` files are preserved by the recommended bind mounts.

## Existing Installations

Existing Docker installations continue to work. A legacy bind mount such as
`./config.json:/app/config/config.json:ro` can keep working, but new setups
should use the directory-based `./config:/app/config` mount.

Docker setup has been simplified for `v0.6.0` and newer images: with the
recommended `docker-compose.yml`, the container creates `./config/config.json`
from the built-in `config.template.json` on first start, stores runtime state
and dashboard database files in `./data`, and never overwrites existing config
files. Older tags still use the previous manual Docker setup.
