# Docker Bootstrap

Best for shell users who want a copy/paste Docker install without the browser
wizard. For a browser-guided setup instead, use the
[Admin Console](admin-console.md).

## Layout

```text
Config:  ./config/config.json
Data:    ./data/
Compose: ./docker-compose.yml
```

The installer is self-contained: it writes `docker-compose.yml` from an embedded
template, so no repository clone is required.

## Install

### Linux/macOS — EMS only

```bash
mkdir -p ems-solarflow-api-control && cd ems-solarflow-api-control
curl -fsSLo install-docker.sh https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh
sh install-docker.sh
```

### Linux/macOS — EMS + Analytics

```bash
sh install-docker.sh --analytics
```

`--analytics` enables the bundled InfluxDB analytics feature. See
[../technical/influxdb.md](../technical/influxdb.md) for the full analytics setup.

### Windows PowerShell — EMS only

```powershell
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
irm https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.ps1 -OutFile install-docker.ps1
powershell -ExecutionPolicy Bypass -File .\install-docker.ps1
```

Add `-Analytics` on Windows for the bundled InfluxDB analytics feature.

## What the installer produces

The installer writes `docker-compose.yml`, creates `config/` and `data/`, and
starts EMS. The generated `docker-compose.yml` uses service name `ems`, publishes
the dashboard as `8080:8080`, mounts `./config:/app/config`, and mounts
`./data:/app/data`. Existing `config/config.json` files are not overwritten.

For EMS-only installs `config/config.json` is created on first container start;
with `--analytics` the installer creates it during setup by running
`config init --analytics`.

## Configure and verify

```bash
docker compose exec ems python3 emsctl.py config init
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

Template placeholders such as example IPs or `YOUR_SN` force EMS into safe mode
(control disabled, dry-run on, hardware writes blocked) until you replace them.

Full detail: [../quickstart.md](../quickstart.md) and [../docker.md](../docker.md).
Layout and legacy migration: [config-layout.md](config-layout.md). Configuration
reference: [../technical/configuration.md](../technical/configuration.md).
