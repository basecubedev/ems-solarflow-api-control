# Docker

The improved Docker first-run bootstrap is available in `latest` and `v0.6.0`
or newer releases. Older container tags use the previous manual setup
procedure.

For the beginner flow, start with [quickstart.md](quickstart.md). This page is
the Docker reference for mounts, tags, CLI commands, permissions, updates, and
troubleshooting details.

If Docker is not installed yet, see [install-docker.md](install-docker.md).
For daily copy/paste commands, see [common-commands.md](common-commands.md).

## Recommended Setup

```bash
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
mkdir -p config data
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/docker-compose.example.yml
docker compose pull
docker compose up -d
```

Docker does not automatically run a container as the same user that runs
`docker compose`. Creating `config` and `data` before the first start as your
normal host user is usually enough: EMS detects the owner of these mounted
directories and runs as that UID/GID.

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
`config/config.json` or writing runtime data. Files created under bind-mounted
`./config` and `./data` use the detected or explicit UID/GID.

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
config. Manual editing of `config/config.json` remains fully supported.

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

For `latest`, create a backup, pull the current image, recreate the container,
check whether new config keys are available, and run diagnostics:

```bash
docker compose exec ems python3 emsctl.py backup create
docker compose pull
docker compose up -d
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade
docker compose exec ems python3 emsctl.py diagnose
```

Backups created in Docker are stored on the host under `data/backups/`, using
the existing `./data:/app/data` mount.

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
