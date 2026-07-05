# EMS SolarFlow Documentation

This directory contains the public project documentation. It is grouped so
normal users can find setup and everyday tasks first, and technical or developer
detail stays clearly separated.

The root [README.md](../README.md) is the short, user-first entry point. This
page is the full documentation map.

## For users

Everyday setup, operation and help.

| Topic | Document | Use |
|---|---|---|
| Admin Console overview | [admin.md](admin.md) | What the Admin Console is, its two paths, and where files live. |
| Set up a new system | [setup/admin-setup.md](setup/admin-setup.md) | Admin Console "Set up a new system" flow with device discovery, release selection, and config apply. |
| Manage my existing system | [setup/admin-maintenance.md](setup/admin-maintenance.md) | Admin Console "Manage my existing system" flow: guided upgrade, read-only overview, config editor, and backup. |
| Admin Console backup / restore | [setup/admin-backup-restore.md](setup/admin-backup-restore.md) | Preview-first backup and restore from the Admin Console, what it can restore, and what is blocked. |
| Quickstart | [quickstart.md](quickstart.md) | Docker-first beginner setup from install check to dashboard and diagnose. |
| First-run checklist | [first-run-checklist.md](first-run-checklist.md) | Safe validation sequence after the first config edit. |
| Common commands | [common-commands.md](common-commands.md) | Daily Docker-first command sheet with native Python equivalents. |
| Backup and restore | [backup-restore.md](backup-restore.md) | CLI backup before updates, dry-run restore checks, encrypted backups, and full local restore. |
| FAQ | [faq.md](faq.md) | Short answers for Admin, Docker, config, dashboard, backups, and updates. |
| Troubleshooting | [troubleshooting.md](troubleshooting.md) | Symptom index, beginner checks, diagnostics, and links back to detail pages. |
| Supported setups | [supported-setups.md](supported-setups.md) | Check whether your grid meter, Zendure devices, and setup style fit EMS. |
| Safety model | [safety.md](safety.md) | Hardware-write gates, dry-run behavior, and staged validation. |

## Operating models

There are three operating models. All converge on the same standard
`config/config.json` layout, so you can switch later.

| Model | Document | Use |
|---|---|---|
| Admin Console | [setup/admin-setup.md](setup/admin-setup.md) | Recommended browser-guided setup, maintenance, updates and backups for most users. |
| Docker Bootstrap | [setup/docker-bootstrap.md](setup/docker-bootstrap.md) | Copy/paste Docker install for shell users, without the browser wizard. |
| Developer Setup | [setup/developer-setup.md](setup/developer-setup.md) | Setup from a Git checkout for development, debugging and contributing. |

Supporting setup references:

| Topic | Document | Use |
|---|---|---|
| Native Python / advanced | [native-python.md](native-python.md) | Manual setup with venv, local config, dry-run checks, and service-manager notes. |
| Config layout | [setup/config-layout.md](setup/config-layout.md) | Standard `config/config.json` layout and legacy root-config migration. |
| Install Docker | [install-docker.md](install-docker.md) | Practical Docker Engine and Compose plugin install help. |
| Docker reference | [docker.md](docker.md) | Compose reference, first-run config bootstrap, persisted data, and permissions. |
| Quality and maintenance | [quality-and-maintenance.md](quality-and-maintenance.md) | How the project is tested, packaged, maintained, and where the limits remain. |

## Features

| Topic | Document | Use |
|---|---|---|
| Standalone dashboard | [dashboard.md](dashboard.md) | Read-only live dashboard, Control Explain view, local history, and telemetry endpoints. |
| InfluxDB analytics | [influxdb.md](influxdb.md) | Optional long-range analytics with bundled or external InfluxDB. |
| Home Assistant integration | [home-assistant.md](home-assistant.md) | Optional HA publishing, helpers, sensors, dashboard files, and control relationship. |
| Winter mode | [winter-mode.md](winter-mode.md) | Optional winter minSoc ramp and reconciliation behavior. |
| Battery full-charge assist | [battery-full-charge-assist.md](battery-full-charge-assist.md) | Optional EMS-managed full-charge assist based on firmware `socLimit`. |

## Technical reference

Deeper detail. You do not need this for a normal setup.

| Topic | Document | Use |
|---|---|---|
| Configuration reference | [configuration.md](configuration.md) | Static `config.json` keys, safety flags, output control, devices, grid meters, HA, and winter settings. |
| Configuration examples | [configuration-examples.md](configuration-examples.md) | Copy/paste starting points for standalone, HA, dry-run, live writes, runtime state, and winter mode. |
| Control flow | [control-flow.md](control-flow.md) | Visual map of where config values affect one EMS control cycle. |
| Control logic | [control-logic.md](control-logic.md) | Target calculation, filtering, allocation, minSoc idle, and write suppression behavior. |
| Runtime state | [runtime-state.md](runtime-state.md) | Mutable operator state and the fields changed by CLI or Home Assistant helpers. |
| CLI tool | [cli.md](cli.md) | Full `emsctl.py` reference for runtime-state, diagnostics, config, and backups. |
| Safety model | [safety.md](safety.md) | Hardware-write gates, dry-run behavior, and staged validation. |
| Admin Console technical reference | [admin-discovery.md](admin-discovery.md) | Full Admin Console internals: wizard, release/build-identity gating, network discovery, Docker setup, and security. |
| Architecture | [architecture.md](architecture.md) | Project structure and runtime component boundaries. |
| Observed firmware behavior | [observed-firmware-no-energy-path.md](observed-firmware-no-energy-path.md) | Observed Zendure behavior when no energy path is available. |

## Developer and maintainer docs

For contributors and maintainers.

| Topic | Document | Use |
|---|---|---|
| Development notes | [development.md](development.md) | Developer workflow and validation notes. |
| Developer notes | [developer.md](developer.md) | Additional development and maintenance context. |
| Dashboard style guide | [dashboard-style-guide.md](dashboard-style-guide.md) | Dashboard UI style conventions. |
| InfluxDB telemetry capture | [develop-tool-influxdb-telemetry.md](develop-tool-influxdb-telemetry.md) | Development tool for recording EMS runtime telemetry into InfluxDB. |
| InfluxDB state-transition analysis | [develop-tool-influxdb-state-transition-analysis.md](develop-tool-influxdb-state-transition-analysis.md) | Development tool for analyzing runtime state transitions from InfluxDB data. |
