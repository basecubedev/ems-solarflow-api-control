# Docker

The improved Docker first-run bootstrap is available in `latest` and releases
after `v0.5.6`. Older container tags, including `v0.5.6` and earlier, use the
previous manual setup procedure.

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

## CLI Inside Docker

With the recommended Compose service name `ems`, `emsctl.py` automatically
uses `/app/config/config.json` when no legacy `/app/config.json` exists:

```bash
docker compose exec ems python emsctl.py status
docker compose exec ems python emsctl.py interactive
docker compose exec ems python emsctl.py dashboard auth-status
docker compose exec ems python emsctl.py diagnose
docker compose exec ems python emsctl.py diagnose --deep
docker compose exec ems python emsctl.py diagnose --support-bundle
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

For unusual mounts or troubleshooting, pass the config path explicitly:

```bash
docker compose exec ems python emsctl.py --config /app/config/config.json status
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

The `Uid:` and `Gid:` lines should show non-zero values matching your `.env`
`PUID` and `PGID`.

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
Startup continues, but device settings may be incomplete.
```

Startup continues after this warning. Existing config validation and runtime
errors still report missing IPs, serial numbers, or invalid device settings.
The EMS will likely not control devices until the required values are
configured.

## Updating

For `latest`, pull the current image and recreate the container:

```bash
docker compose pull
docker compose up -d
```

For stable deployments, pin a release tag in `docker-compose.yml`, then update
that tag intentionally when you want to move to a newer release. Existing
`./config` and `./data` files are preserved by the recommended bind mounts.

## Existing Installations

Existing Docker installations continue to work. A legacy bind mount such as
`./config.json:/app/config/config.json:ro` can keep working, but new setups
should use the directory-based `./config:/app/config` mount.

Docker setup has been simplified for images after `v0.5.6`: with the
recommended `docker-compose.yml`, the container creates `./config/config.json`
from the built-in `config.template.json` on first start, stores runtime state
and dashboard database files in `./data`, and never overwrites existing config
files. Older tags, including `v0.5.6` and earlier, still use the previous
manual Docker setup.
