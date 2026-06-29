# EMS SolarFlow Documentation

This directory contains the public project documentation.

## Start Here

| Topic | Document | Use |
|---|---|---|
| Quickstart | [quickstart.md](quickstart.md) | Docker-first beginner setup from install check to dashboard and diagnose. |
| Supported setups | [supported-setups.md](supported-setups.md) | Check whether your grid meter, Zendure devices, and setup style fit EMS. |
| First-run checklist | [first-run-checklist.md](first-run-checklist.md) | Safe validation sequence after the first config edit. |
| Quality and maintenance | [quality-and-maintenance.md](quality-and-maintenance.md) | How the project is tested, packaged, maintained, and where the limits remain. |
| FAQ | [faq.md](faq.md) | Short beginner answers for Docker, Home Assistant, config, dashboard, and updates. |

## Setup And Operation

| Topic | Document | Use |
|---|---|---|
| Install Docker | [install-docker.md](install-docker.md) | Practical Docker Engine and Compose plugin install help for Debian, Ubuntu, and Raspberry Pi OS. |
| Common commands | [common-commands.md](common-commands.md) | Daily Docker-first command sheet with native Python equivalents. |
| Docker | [docker.md](docker.md) | Compose reference, first-run config bootstrap, persisted data, permissions, and v0.6.0 release scope. |
| Native Python | [native-python.md](native-python.md) | Advanced/manual setup with venv, local config, dry-run checks, and service-manager notes. |
| CLI tool | [cli.md](cli.md) | Safe `emsctl.py` commands for runtime-state, diagnostics, config init/upgrade, and backups. |
| Backup and restore | [backup-restore.md](backup-restore.md) | Step-by-step guide: backup before updates, dry-run restore checks, encrypted backups, and full local restore. |
| Troubleshooting | [troubleshooting.md](troubleshooting.md) | Symptom index, beginner checks, diagnostics, and links back to detail pages. |

## Features

| Topic | Document | Use |
|---|---|---|
| Standalone dashboard | [dashboard.md](dashboard.md) | Read-only live dashboard, Control Explain view, local history, and telemetry endpoints. |
| InfluxDB analytics | [influxdb.md](influxdb.md) | Optional long-range analytics with bundled or external InfluxDB. |
| Home Assistant integration | [home-assistant.md](home-assistant.md) | Optional HA publishing, helpers, sensors, dashboard files, and control relationship. |
| Winter mode | [winter-mode.md](winter-mode.md) | Optional winter minSoc ramp and reconciliation behavior. |
| Battery full-charge assist | [battery-full-charge-assist.md](battery-full-charge-assist.md) | Optional EMS-managed full-charge assist based on firmware `socLimit`. |

## Advanced And Reference

| Topic | Document | Use |
|---|---|---|
| Configuration reference | [configuration.md](configuration.md) | Static `config.json` keys, safety flags, output control, devices, grid meters, HA, and winter settings. |
| Configuration examples | [configuration-examples.md](configuration-examples.md) | Copy/paste starting points for standalone, HA, dry-run, live writes, runtime state, and winter mode. |
| Power/control flow map | [control-flow.md](control-flow.md) | Visual map of where config values affect one EMS control cycle. |
| Control logic details | [control-logic.md](control-logic.md) | Target calculation, filtering, allocation, minSoc idle, and write suppression behavior. |
| Runtime state | [runtime-state.md](runtime-state.md) | Mutable operator state and the fields changed by CLI or Home Assistant helpers. |
| Safety model | [safety.md](safety.md) | Hardware-write gates, dry-run behavior, and staged validation. |
| Quality and maintenance | [quality-and-maintenance.md](quality-and-maintenance.md) | User-facing overview of automated checks, Docker rebuilds, dependency maintenance, runtime safety design, and limitations. |
| Architecture | [architecture.md](architecture.md) | Project structure and runtime component boundaries. |
| Observed firmware behavior | [observed-firmware-no-energy-path.md](observed-firmware-no-energy-path.md) | Observed Zendure behavior when no energy path is available. |

## Development And Maintainers

| Topic | Document | Use |
|---|---|---|
| Development notes | [development.md](development.md) | Developer workflow and validation notes. |
| Developer notes | [developer.md](developer.md) | Additional development and maintenance context. |
| Dashboard style guide | [dashboard-style-guide.md](dashboard-style-guide.md) | Dashboard UI style conventions. |
| InfluxDB telemetry capture | [develop-tool-influxdb-telemetry.md](develop-tool-influxdb-telemetry.md) | Development tool for recording EMS runtime telemetry into InfluxDB. |
| InfluxDB state-transition analysis | [develop-tool-influxdb-state-transition-analysis.md](develop-tool-influxdb-state-transition-analysis.md) | Development tool for analyzing runtime state transitions from InfluxDB data. |

The root [README.md](../README.md) is intentionally short and focuses on
project overview, Docker-first quick start, and links into these detailed topic
documents.
