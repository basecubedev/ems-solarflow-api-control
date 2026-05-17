# EMS SolarFlow Documentation

This directory contains the public project documentation.

Start here:

| Topic | Document | Use |
|---|---|---|
| Quickstart live control | [quickstart.md](quickstart.md) | First setup path from config copy to dry-run and bounded live runs. |
| Configuration reference | [configuration.md](configuration.md) | Static `config.json` keys, safety flags, output control, devices, Shelly, HA, and winter settings. |
| Configuration examples | [configuration-examples.md](configuration-examples.md) | Copy/paste starting points for standalone, HA, dry-run, live writes, runtime state, and winter mode. |
| Power/control flow map | [control-flow.md](control-flow.md) | Visual map of where config values affect one EMS control cycle. |
| Control logic details | [control-logic.md](control-logic.md) | Target calculation, filtering, allocation, minSoc idle, and write suppression behavior. |
| Runtime state | [runtime-state.md](runtime-state.md) | Mutable operator state and the fields changed by CLI or Home Assistant helpers. |
| CLI tool | [cli.md](cli.md) | Safe `emsctl.py` commands for runtime-state changes. |
| Home Assistant integration | [home-assistant.md](home-assistant.md) | Optional HA publishing, helpers, sensors, dashboard files, and control relationship. |
| Winter mode | [winter-mode.md](winter-mode.md) | Optional winter minSoc ramp and reconciliation behavior. |
| Safety model | [safety.md](safety.md) | Hardware-write gates, dry-run behavior, and staged validation. |
| Troubleshooting | [troubleshooting.md](troubleshooting.md) | Common symptoms, relevant events, and links back to detail pages. |
| Architecture | [architecture.md](architecture.md) | Project structure and runtime component boundaries. |
| Development notes | [development.md](development.md) | Developer workflow and validation notes. |
| InfluxDB telemetry capture | [develop-tool-influxdb-telemetry.md](develop-tool-influxdb-telemetry.md) | Development tool for recording EMS runtime telemetry into InfluxDB. |
| InfluxDB state-transition analysis | [develop-tool-influxdb-state-transition-analysis.md](develop-tool-influxdb-state-transition-analysis.md) | Development tool for analyzing runtime state transitions from InfluxDB data. |
| Observed firmware behavior | [observed-firmware-no-energy-path.md](observed-firmware-no-energy-path.md) | Observed Zendure behavior when no energy path is available. |

The root [README.md](../README.md) is intentionally short and focuses on
project overview, quick start, and links into these detailed topic documents.
