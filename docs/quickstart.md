# Quickstart

This guide starts a normal Docker installation, creates or edits the config,
runs diagnostics, and opens the dashboard.

![Docker-first install demo](assets/install-demo.gif)

The preview shows the Docker-first Analytics bootstrap — install commands,
installer output, a guided `config init` with example values, and the running
dashboard. It is also available as video: [MP4](assets/install-demo.mp4) ·
[WebM](assets/install-demo.webm).

Home Assistant is optional. The advanced native Python path is documented
separately in [native-python.md](native-python.md).

## 1. Requirements

You need:

- Docker with Docker Compose v2.24.0 or newer (`docker compose`, not the legacy
  `docker-compose`). On macOS/Windows use Docker Desktop; Windows must use Linux
  containers.
- network access from the EMS host to the grid meter
- network access from the EMS host to each Zendure device
- Zendure device IP address and serial number
- grid meter type and endpoint settings

Zendure Local API must be available and enabled for local EMS control. Do not
run Zendure HEMS, Home Assistant automations, MQTT writers, or any other
controller in parallel if they write Zendure `outputLimit`. EMS assumes
exclusive write control over `outputLimit` while active.

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

### Quick install (recommended)

The installer writes `docker-compose.yml`, creates `config/` and `data/`, and
starts EMS. No repository clone is required. For EMS-only installs,
`config/config.json` is created on first container start; with Analytics the
installer creates it during setup because it runs `config init --analytics`.

Linux/macOS, EMS only:

```bash
mkdir -p ems-solarflow-api-control && cd ems-solarflow-api-control
curl -fsSLo install-docker.sh https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh
sh install-docker.sh
```

Linux/macOS, EMS + Analytics (bundled InfluxDB):

```bash
mkdir -p ems-solarflow-api-control && cd ems-solarflow-api-control
curl -fsSLo install-docker.sh https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh
sh install-docker.sh --analytics
```

Windows PowerShell (Docker Desktop with Linux containers), EMS only:

```powershell
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
irm https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.ps1 -OutFile install-docker.ps1
powershell -ExecutionPolicy Bypass -File .\install-docker.ps1
```

Add `-Analytics` for EMS + Analytics. `chmod +x` is not required on
Linux/macOS: run the script through `sh install-docker.sh`.

### Manual install (full control)

If you prefer to run each step yourself, see the manual path in
[docker.md](docker.md). It downloads `docker-compose.yml`, runs `config init`,
and starts the stack with the same commands the installer uses.

On first start, the container creates `config/config.json` from the built-in
template if the file does not exist yet. Existing `config/config.json` files
are not overwritten.

The generated Compose file uses service name `ems`, port mapping
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
Choose your grid meter in the guided setup assistant. For Zendure SmartMeter
D0, select "Zendure SmartMeter D0 via MQTT".

### Option B: Manual Editing

```bash
nano config/config.json
```

Set at least:

- `grid_meter.type`
- `grid_meter.ip` for HTTP meters, or `grid_meter.mqtt.host` and
  `grid_meter.mqtt.topic` for Zendure SmartMeter D0 / MQTT
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
- Analytics (bundled InfluxDB): [influxdb.md](influxdb.md)
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
