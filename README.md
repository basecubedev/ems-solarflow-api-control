# ems-solarflow-api-control

[![Continuous Integration](https://github.com/basecubedev/ems-solarflow-api-control/actions/workflows/simulated-regression-tests.yml/badge.svg)](https://github.com/basecubedev/ems-solarflow-api-control/actions/workflows/simulated-regression-tests.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.14-blue)
![automated tests](https://img.shields.io/badge/automated%20tests-1000%2B-blue)

Local-first EMS (Energy Management System) control for Zendure SolarFlow
systems. It reads your local grid meter and Zendure device telemetry, controls
inverter output to reduce grid import and export, and provides a local dashboard
without requiring Home Assistant.

> This software controls real power hardware.
> Untouched template configs run in safe mode and cannot write to hardware.
> Replace all placeholders, run diagnose, and monitor the first live run.

## Choose your path

Most users should start with the **Admin Console**. Pick one path and follow its
guide — all three converge on the same standard `config/config.json` layout, so
you can switch later.

| Path | Best for | What you get |
| --- | --- | --- |
| Admin Console | Most users | Browser setup, discovery, maintenance, updates and backups |
| Docker Bootstrap | Shell users | Copy/paste Docker install without the browser wizard |
| Developer Setup | Contributors | Local development and debugging from a Git checkout |

## Recommended: Admin Console

The Admin Console (product name **EMS SolarFlow Admin**) runs locally in your
browser. It guides setup, device discovery, config generation, updates, backups
and maintenance. EMS still runs the control loop — the Admin Console is UI and
orchestration only.

No Git checkout is required. The installer downloads the published image and
starts the Admin Console in the current folder:

```bash
mkdir -p ems-solarflow-api-control
cd ems-solarflow-api-control
curl -fsSLO https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/deploy/admin/install-admin-console.sh
sh install-admin-console.sh
```

Then open:

```text
http://127.0.0.1:8090
```

The default uses host networking for reliable LAN device discovery, so the UI is
also reachable from another device on your LAN at `http://<host-ip>:8090`. Use
`--bridge` if you need Docker bridge networking instead:

```bash
sh install-admin-console.sh --bridge
```

The Admin Console is designed for a trusted local EMS host or trusted LAN. Run it
only there — never expose it to the internet. Overview:
[docs/admin.md](docs/admin.md). Full guide:
[docs/setup/admin-setup.md](docs/setup/admin-setup.md).

## Docker Bootstrap

For shell users who want a copy/paste Docker install without the browser wizard.
The installer sets up everything in the current folder — no repository clone is
required. It writes `docker-compose.yml`, creates `config/` and `data/`, and
starts EMS.

**Linux/macOS — EMS only**

```bash
mkdir -p ems-solarflow-api-control && cd ems-solarflow-api-control
curl -fsSLo install-docker.sh https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh
sh install-docker.sh
```

**Linux/macOS — EMS + Analytics**

```bash
sh install-docker.sh --analytics
```

**Windows PowerShell — EMS only**

```powershell
mkdir ems-solarflow-api-control
cd ems-solarflow-api-control
irm https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.ps1 -OutFile install-docker.ps1
powershell -ExecutionPolicy Bypass -File .\install-docker.ps1
```

Add `-Analytics` on Windows for the bundled InfluxDB analytics feature.

The generated `docker-compose.yml` uses service name `ems`, publishes the
dashboard as `8080:8080`, mounts `./config:/app/config`, and mounts
`./data:/app/data`. Existing `config/config.json` files are not overwritten.

Full guide: [docs/setup/docker-bootstrap.md](docs/setup/docker-bootstrap.md) ·
[docs/quickstart.md](docs/quickstart.md) · [docs/docker.md](docs/docker.md).

## Developer Setup

Only for development, debugging and contributing from a Git checkout with a local
Python environment. This is **not** the normal user path — for a home install,
use the Admin Console or Docker Bootstrap above.

Start here: [docs/setup/developer-setup.md](docs/setup/developer-setup.md).

### Build Admin Console from source (Developer Setup only)

Contributors can build and run the Admin Console image from a Git checkout
instead of the published image. Normal users should use the
`install-admin-console.sh` installer above.

```bash
git clone https://github.com/basecubedev/ems-solarflow-api-control.git
cd ems-solarflow-api-control
deploy/admin/start-admin-setup.sh
```

## Configure EMS

You need a grid meter type and endpoint, a Zendure IP and serial per device, and
suitable power and SOC limits. Home Assistant details are only needed if you use
Home Assistant.

The Admin Console can generate this config for you. From the CLI, the guided
assistant helps too:

```bash
docker compose exec ems python3 emsctl.py config init
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

Template placeholders such as example IPs or `YOUR_SN` force EMS into safe mode:
control is disabled, dry-run is enabled, and hardware writes are blocked until
you replace them.

Config help: [configuration guide](docs/configuration.md) ·
[examples](docs/configuration-examples.md) ·
[first-run checklist](docs/first-run-checklist.md).

## Where your files live

```text
config/config.json   your setup
data/                runtime state, dashboard history, optional analytics data
data/backups/        EMS backup archives
data/admin/          Admin Console state, release cache, staging and logs
```

Older native checkouts may still use a root `./config.json`. It is kept only for
legacy compatibility; new setups should use `config/config.json`. See
[docs/setup/config-layout.md](docs/setup/config-layout.md).

## Open the dashboard

The live monitoring dashboard is separate from the Admin Console:

```text
http://<host-ip>:8080
```

Use `http://127.0.0.1:8080` if the browser runs on the same machine. More:
[docs/dashboard.md](docs/dashboard.md).

## First checks

```bash
docker compose ps
docker compose logs -f
docker compose exec ems python3 emsctl.py diagnose
```

Hardware checks are read-only. Use them only when you are ready to probe the
configured devices and meter:

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

## Updating safely

Back up first, then update. The Admin Console **Guided upgrade** does this in a
conservative, guided way — see
[docs/setup/admin-maintenance.md](docs/setup/admin-maintenance.md).

From the CLI:

```bash
docker compose exec ems python3 emsctl.py backup create --type config --password
docker compose pull
docker compose up -d
docker compose exec ems python3 emsctl.py config upgrade --yes --backup
docker compose exec ems python3 emsctl.py diagnose
```

For stable deployments, pin a release tag in `docker-compose.yml` instead of
`latest`. Full sequence: [docs/common-commands.md](docs/common-commands.md).

## Backups

Backups live in `data/backups/` by default. Docker users see that folder on the
host via the `./data:/app/data` mount; no separate backup volume is needed.

The Admin Console can create and restore backups. Password-protected backups are
recommended for config archives because they can contain secrets. Without the
password, an encrypted backup cannot be restored.

Details: [docs/backup-restore.md](docs/backup-restore.md) and
[docs/setup/admin-backup-restore.md](docs/setup/admin-backup-restore.md).

## Optional Analytics

The dashboard works with local SQLite history by default. **Analytics**
(long-range charts and history) is backed by a bundled InfluxDB. The simplest way
to enable it is the Docker Bootstrap installer:

```bash
sh install-docker.sh --analytics
```

See [docs/influxdb.md](docs/influxdb.md) for the Docker-first, manual, and
external InfluxDB setups.

## FAQ

Short answers, including Admin Console questions, are in
[docs/faq.md](docs/faq.md).

## Documentation

Start with the user guides. Technical reference is deeper detail you only need
when a task calls for it.

**User guides**

| Topic | Link |
| --- | --- |
| Admin Console overview | [docs/admin.md](docs/admin.md) |
| Admin Console: set up a new system | [docs/setup/admin-setup.md](docs/setup/admin-setup.md) |
| Admin Console: manage my existing system | [docs/setup/admin-maintenance.md](docs/setup/admin-maintenance.md) |
| Admin Console backup / restore | [docs/setup/admin-backup-restore.md](docs/setup/admin-backup-restore.md) |
| Beginner quickstart | [docs/quickstart.md](docs/quickstart.md) |
| First-run checklist | [docs/first-run-checklist.md](docs/first-run-checklist.md) |
| Common commands | [docs/common-commands.md](docs/common-commands.md) |
| Backup and restore | [docs/backup-restore.md](docs/backup-restore.md) |
| FAQ | [docs/faq.md](docs/faq.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Safety model | [docs/safety.md](docs/safety.md) |

**Technical reference**

| Topic | Link |
| --- | --- |
| Configuration reference | [docs/configuration.md](docs/configuration.md) |
| Control logic and flow | [docs/control-logic.md](docs/control-logic.md) |
| Runtime state | [docs/runtime-state.md](docs/runtime-state.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Admin Console technical reference | [docs/admin-discovery.md](docs/admin-discovery.md) |

The full documentation map, including developer and maintainer docs, is in
[docs/README.md](docs/README.md).

## Safety and responsibility

This project is intended for stable EMS control use, but every installation is
different. Battery size, PV layout, inverter limits, wiring, meter behavior,
firmware versions, network stability, and local requirements can affect the
result.

Users are responsible for reviewing configuration, validating hardware setup,
setting suitable power and SOC limits, and monitoring operation. Do not run EMS
in parallel with another controller that writes Zendure `outputLimit`.

For a no-write validation run, set `system.dry_run=true`. Detailed safety model:
[docs/safety.md](docs/safety.md).

## Screenshots

<table>
  <tr>
    <th>Aggregated Flow</th>
    <th>Device Flow</th>
    <th>Energy Statistics</th>
  </tr>
  <tr>
    <td width="33%"><img src="docs/assets/preview-aggregated.jpg" alt="EMS SolarFlow dashboard aggregated energy flow preview" width="100%"></td>
    <td width="33%"><img src="docs/assets/preview-devices.jpg" alt="EMS SolarFlow dashboard per-device energy flow preview" width="100%"></td>
    <td width="33%"><img src="docs/assets/preview-energy.jpg" alt="EMS SolarFlow dashboard energy statistics preview" width="100%"></td>
  </tr>
</table>

<table>
  <tr>
    <th>Analytics (InfluxDB)</th>
    <th>Diagnose</th>
    <th>Logs</th>
  </tr>
  <tr>
    <td width="33%"><img src="docs/assets/preview-analytics.jpg" alt="Dashboard Analytics tab backed by InfluxDB" width="100%"></td>
    <td width="33%"><img src="docs/assets/preview-diagnose.jpg" alt="Dashboard Diagnose tab" width="100%"></td>
    <td width="33%"><img src="docs/assets/preview-logs.jpg" alt="Dashboard Logs tab" width="100%"></td>
  </tr>
</table>

#### Control Center

<img src="docs/assets/preview-control.jpg" alt="EMS SolarFlow dashboard control center preview" width="100%">

## Getting help

Run diagnostics first:

```bash
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

Then see [docs/troubleshooting.md](docs/troubleshooting.md). Hardware feedback is
welcome through GitHub issues, especially for unsupported payloads, incorrect
readings, or setup-specific behavior.
