# EMS SolarFlow Documentation

This is the documentation map. The root [README.md](../README.md) is the short
router that points you at one of three setup paths; this page lists everything.

Documentation is split by audience:

- **[docs/user/](user/index.md)** — start here for setup, everyday operation and help.
- **[docs/technical/](technical/)** — architecture, internals and reference.
- **[docs/developer/](developer/)** — source checkout, tests, CI and design notes.

Normal users should start with **User documentation**.
Use **Technical reference** only when you need deeper behavior or implementation
details.
Use **Developer documentation** only for source checkout, tests, local builds and
contributing.

## Operating models

Admin Console and Docker Bootstrap are the two user setup paths; both converge on
the same standard `config/config.json` layout. Developer Setup is a
source-checkout path for development and contributing, not a normal user setup.

| Path | Audience | Start here |
| --- | --- | --- |
| Admin Console | Most users | [Admin Console](user/admin-console.md) |
| Docker Bootstrap | Shell-only Docker users | [Docker Bootstrap](user/docker-bootstrap.md) |
| Developer Setup | Developers and contributors only | [Developer Setup](developer/developer-setup.md) |

## User documentation

Setup, everyday operation and help. Normal users only need this section.
[user/index.md](user/index.md) is the "Start here" landing page.

### Step-by-step guides

Screenshot-led walkthroughs. Each states what you see, what to select, what it
changes, and what to do when the result differs.

| Area | Document | Use |
|---|---|---|
| Admin Console | [user/admin/index.md](user/admin/index.md) | First start, Guided Setup, Guided Upgrade, Maintenance, devices, MQTT, backups, recovery. |
| EMS Dashboard | [user/dashboard/index.md](user/dashboard/index.md) | Overview, device cards, energy, control pipeline, runtime settings, diagnostics. |

### Setup paths

Normal users choose one of these two; both converge on the same standard
`config/config.json` layout.

| Model | Document | Use |
|---|---|---|
| Admin Console | [user/admin-console.md](user/admin-console.md) | Recommended browser-guided setup, discovery, maintenance, updates and backups. |
| Docker Bootstrap | [user/docker-bootstrap.md](user/docker-bootstrap.md) | Shell-only Docker install without the browser wizard. |

### Admin Console guides

| Topic | Document | Use |
|---|---|---|
| Admin setup | [user/admin-setup.md](user/admin-setup.md) | "Set up a new system" flow: discovery, config generation and apply. |
| Admin maintenance | [user/admin-maintenance.md](user/admin-maintenance.md) | "Manage my existing system" flow: guided upgrade, overview, config editor, backup. |
| Backup and restore | [user/admin-backup-restore.md](user/admin-backup-restore.md) | Preview-first backup and restore from the Admin Console. |

### Everyday use and help

| Topic | Document | Use |
|---|---|---|
| Quickstart | [quickstart.md](quickstart.md) | Docker-first beginner setup from install check to dashboard. |
| First-run checklist | [first-run-checklist.md](first-run-checklist.md) | Safe validation sequence after the first config edit. |
| Common commands | [common-commands.md](common-commands.md) | Daily Docker-first command sheet with native equivalents. |
| Config layout | [user/config-layout.md](user/config-layout.md) | Standard `config/config.json` layout and legacy migration. |
| Supported setups | [user/supported-setups.md](user/supported-setups.md) | Whether your grid meter and devices fit EMS. |
| Connection types | [user/connection-types.md](user/connection-types.md) | Local API, Local MQTT and Zendure cloud MQTT — which hardware fits which. |
| FAQ | [user/faq.md](user/faq.md) | Short answers for Admin, Docker, config, dashboard, backups and updates. |
| Troubleshooting | [user/troubleshooting.md](user/troubleshooting.md) | Short, Admin-first guide for common problems. |
| Safety | [user/safety.md](user/safety.md) | Simple pre-live checklist for hardware writes. |

### Install and features

| Topic | Document | Use |
|---|---|---|
| Install Docker | [install-docker.md](install-docker.md) | Docker Engine and Compose plugin install help. |
| Docker reference | [docker.md](docker.md) | Compose reference, first-run bootstrap and persisted data. |
| Native Python / advanced | [native-python.md](native-python.md) | Manual venv setup with local config and dry-run checks. |
| Standalone dashboard | [dashboard.md](dashboard.md) | Read-only live dashboard, Control Explain view and history. |
| Home Assistant | [home-assistant.md](home-assistant.md) | Optional HA publishing, helpers and sensors. |
| Winter mode | [winter-mode.md](winter-mode.md) | Optional winter minSoc ramp and reconciliation. |
| Battery full-charge assist | [battery-full-charge-assist.md](battery-full-charge-assist.md) | Optional EMS-managed full-charge assist. |
| Quality and maintenance | [quality-and-maintenance.md](quality-and-maintenance.md) | How the project is tested, packaged and maintained. |

## Technical reference

Architecture, internals and reference. You do not need this for a normal setup.

| Topic | Document | Use |
|---|---|---|
| Architecture | [technical/architecture.md](technical/architecture.md) | Project structure and runtime component boundaries. |
| Admin architecture | [technical/admin-architecture.md](technical/admin-architecture.md) | Admin Console = UI/orchestration, Docker Bootstrap layout, EMS/Core as source of truth. |
| Admin discovery | [technical/admin-discovery.md](technical/admin-discovery.md) | Full Admin Console internals: wizard, release/build identity, discovery, Docker setup, security. |
| System-build pairing | [technical/system-build-pairing.md](technical/system-build-pairing.md) | Admin and EMS as one paired system build: pair identity, alignment, embedded resources, known-good. |
| Admin workflow state | [technical/admin-workflow-state.md](technical/admin-workflow-state.md) | Persisted workflow-state inventory, config write paths, transition matrix and abandonment invariants. |
| Configuration | [technical/configuration.md](technical/configuration.md) | Static `config.json` keys, safety flags, devices, grid meters and winter settings. |
| Configuration examples | [configuration-examples.md](configuration-examples.md) | Copy/paste starting points for standalone, HA, dry-run and live writes. |
| Control logic | [technical/control-logic.md](technical/control-logic.md) | Target calculation, filtering, allocation and write suppression. |
| Control flow | [technical/control-flow.md](technical/control-flow.md) | Visual map of where config values affect one control cycle. |
| Runtime state | [technical/runtime-state.md](technical/runtime-state.md) | Mutable operator state and fields changed by CLI or HA helpers. |
| Safety model | [technical/safety-model.md](technical/safety-model.md) | Write gates, runtime write types and the Zendure fields EMS writes. |
| Troubleshooting reference | [technical/troubleshooting-reference.md](technical/troubleshooting-reference.md) | Command-level diagnostics, log events and deeper failure analysis. |
| Backup/restore internals | [technical/backup-restore.md](technical/backup-restore.md) | CLI backup, dry-run restore checks, encrypted backups and full restore. |
| Analytics / InfluxDB | [technical/influxdb.md](technical/influxdb.md) | Optional long-range analytics with bundled or external InfluxDB. |
| CLI reference | [cli.md](cli.md) | Full `emsctl.py` reference for runtime-state, diagnostics, config and backups. |
| Observed firmware behavior | [observed-firmware-no-energy-path.md](observed-firmware-no-energy-path.md) | Observed Zendure behavior when no energy path is available. |

## Developer documentation

For contributors and maintainers. Git clone and build-from-source belong here.

| Topic | Document | Use |
|---|---|---|
| Agent rules | [developer/agent-rules.md](developer/agent-rules.md) | Canonical project-wide rules for coding agents and maintainers. |
| Developer setup | [developer/developer-setup.md](developer/developer-setup.md) | Source checkout, venv, local config and dry-run validation. |
| Development notes | [developer/development.md](developer/development.md) | Module layout and developer workflow. |
| Developer notes | [developer/developer.md](developer/developer.md) | Additional development and maintenance context. |
| Testing | [developer/testing.md](developer/testing.md) | Compile checks, self-test, simulation and the pytest suite. |
| MQTT write-latency probe | [developer/mqtt-write-latency-probe.md](developer/mqtt-write-latency-probe.md) | On-hardware tool measuring how fast an MQTT `outputLimit` write reaches the inverter. |
| CI / release | [developer/ci-release.md](developer/ci-release.md) | Continuous integration, image publishing and release archives. |
| Dashboard style guide | [developer/dashboard-style-guide.md](developer/dashboard-style-guide.md) | Dashboard UI style conventions. |
| Design notes | [developer/design-notes/](developer/design-notes/) | Development tools and deeper design notes. |
