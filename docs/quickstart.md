# Quickstart

This guide starts a normal Docker installation, creates or edits the config,
runs diagnostics, and opens the dashboard.

Home Assistant is optional. Native Python setup is documented separately in
[native-python.md](native-python.md).

## 1. Requirements

You need:

- Docker with the current `docker compose` plugin
- network access from the EMS host to the grid meter
- network access from the EMS host to each Zendure device
- Zendure device IP address and serial number
- grid meter type and IP address

The EMS should not run in parallel with another controller that writes Zendure
`outputLimit`.

## 2. Install Docker

If Docker is not installed yet, see [install-docker.md](install-docker.md).
If you are unsure whether your hardware fits, see
[supported-setups.md](supported-setups.md).

Verify that both commands work:

```bash
docker --version
docker compose version
```

## 3. Start With Docker

```bash
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
mkdir -p config data

curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/docker-compose.example.yml

docker compose pull
docker compose up -d
```

On first start, the container creates `config/config.json` from the built-in
template if the file does not exist yet. Existing `config/config.json` files
are not overwritten.

The downloaded Compose file uses service name `ems`, port mapping
`8080:8080`, and bind mounts `./config:/app/config` and `./data:/app/data`.

Check that the container started:

```bash
docker compose ps
docker compose logs -f
```

Stop following logs with `Ctrl+C`; the container keeps running.

## 4. Configure EMS

### Option A: Guided Setup Assistant

```bash
docker compose exec ems python3 emsctl.py config init
```

The setup assistant is optional. It helps fill common settings and does not
blindly replace an existing edited config.

### Option B: Manual Editing

```bash
nano config/config.json
```

Set at least:

- `grid_meter.type`
- `grid_meter.ip`
- each device `ip`
- each device `sn`
- installation-specific power and SOC limits

Template placeholder values force safe mode until replaced. In safe mode, EMS
control is disabled, dry-run is enabled, and hardware writes are blocked.

## 5. Restart

```bash
docker compose restart
```

## 6. Run Diagnose

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Use the hardware check only when you are ready to probe the configured local
meter and devices. It is read-only.

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

## 7. Open Dashboard

Open:

```text
http://<host-ip>:8080
```

On the same machine, use:

```text
http://127.0.0.1:8080
```

## 8. Next Steps

- Configuration details: [configuration.md](configuration.md)
- Copy/paste examples: [configuration-examples.md](configuration-examples.md)
- Supported setups: [supported-setups.md](supported-setups.md)
- First-run checklist: [first-run-checklist.md](first-run-checklist.md)
- Common commands: [common-commands.md](common-commands.md)
- Docker reference: [docker.md](docker.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
- Backup and restore: [backup-restore.md](backup-restore.md)
- Backups are stored in `data/backups/` by default.
- FAQ: [faq.md](faq.md)

## 9. Safe Updates

Before pulling a new image, create backups. Password-protected backups are
recommended, especially for config archives:

```bash
docker compose exec ems python3 emsctl.py backup create --type config --password
docker compose exec ems python3 emsctl.py backup create --type databases --password
docker compose pull
docker compose up -d
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade --yes --backup
docker compose exec ems python3 emsctl.py diagnose
```

Backups are stored under host path `data/backups/`. Without the password, an
encrypted backup cannot be restored. If you do not use bundled InfluxDB
analytics, you do not need an InfluxDB backup.

## 10. Native Python Setup

Native Python is still supported for developers and advanced/manual installs.
Use [native-python.md](native-python.md) for venv, dependency installation,
manual config creation, dry-run checks, service-manager notes, and native CLI
commands.
