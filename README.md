# ems-solarflow-api-control

[![Continuous Integration](https://github.com/basecubedev/ems-solarflow-api-control/actions/workflows/simulated-regression-tests.yml/badge.svg)](https://github.com/basecubedev/ems-solarflow-api-control/actions/workflows/simulated-regression-tests.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.14-blue)
![automated tests](https://img.shields.io/badge/automated%20tests-3700%2B-blue)
[![Test-Driven Development](https://img.shields.io/badge/Test--Driven%20Development-contract--first-blue)](docs/developer/testing.md#development-approach)

Local-first EMS control for Zendure SolarFlow systems.
It reads local grid meter and Zendure telemetry, controls inverter output,
and provides a local dashboard.

> **New to EMS SolarFlow?**  
> Read the [Project Overview](docs/user/project-overview.md) for a non-technical introduction to the main features, supported setups, dashboards, energy management, and system administration.

> EMS controls real power hardware.
> Read the [safety guide](docs/user/safety.md) before enabling live writes.

## Supported hardware at a glance

Each device carries one status — **Validated**, **Family-supported**,
**Reverse-engineered** or **User-reported**. The maintainer validates on a
**SolarFlow 800 Pro 2** and a **Shelly Pro**; wider coverage needs community
reports. Definitions and the full matrix live in
[docs/user/supported-setups.md](docs/user/supported-setups.md).

| Hardware / integration | Connection | Status |
| --- | --- | --- |
| SolarFlow ZenSDK inverters — 800 Pro 2, plus 800 / 800 Plus / 800 Pro / 1600 AC+ / 2400 AC / 2400 AC+ / **SolarFlow 2400 Pro** / 4000 AC+ | Local API (ZenSDK) + Zendure cloud MQTT | 800 Pro 2 Validated; rest Family-supported |
| Older Hub / Hyper / AIO / Ace MQTT devices — Hub 1200/2000, **Hyper 2000**, AIO 2400, Ace 1500 | Local or Zendure cloud MQTT | Reverse-engineered |
| Any Zendure device via API key | Zendure cloud MQTT | Telemetry for any device; control needs an exact supported model |
| Zendure & Shelly grid meters — **Shelly Pro**, Zendure **Smart Meter 3CT** / **Smart Meter D0** (Local API) | HTTP | Shelly Pro Validated; Zendure meters Reverse-engineered |
| Other HTTP / MQTT grid meters — Shelly Plus/Gen2/Gen3, Shelly 3EM Gen1, everHome EcoTracker, Tasmota, generic MQTT, D0 over local MQTT | HTTP / MQTT | Family-supported / Reverse-engineered |
| Home Assistant entity as a load signal | HA API | Legacy; not recommended for new setups |

**MQTT control is an implemented EMS transport**, not a future feature: a
supported inverter joins the same control loop, target calculation and safety
gates as the Local API, over a local broker or Zendure cloud MQTT. ZenSDK cloud
control is **Validated** on the SolarFlow 800 Pro 2; the older legacy-JSON write
path is **Reverse-engineered** and still needs broader hardware validation — the
**Roadmap** is confirming it on more device generations, not building the
feature. Every write still requires an exact supported model, a verified write
protocol, the per-device control capability and the transport write gate.
[Device compatibility reports](https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml)
(working *or* broken) are very welcome.

## Choose your path

Most users should start with the **Admin Console**. All three paths converge on
the same standard `config/config.json` layout, so you can switch later.

| Path | Choose this if | Continue |
| --- | --- | --- |
| **Admin Console** | You want browser-guided setup, discovery, updates, backups and maintenance | [docs/user/admin-console.md](docs/user/admin-console.md) |
| **Docker Bootstrap** | You want shell-only Docker setup | [docs/user/docker-bootstrap.md](docs/user/docker-bootstrap.md) |
| **Developer Setup** | You want to develop, debug or build from source | [docs/developer/developer-setup.md](docs/developer/developer-setup.md) |

## Choose your connection

EMS reaches your Zendure devices over three connection types; any one supported
connection is enough.

| Connection | Works with | Continue |
| --- | --- | --- |
| **Local API (ZenSDK)** | Newer SolarFlow / ZenSDK models on your LAN — fastest, fully local control | [connection-types.md](docs/user/connection-types.md#local-api-zensdk) |
| **Local MQTT** | Devices re-pointed to your own local broker — low latency, no cloud | [connection-types.md](docs/user/connection-types.md#local-mqtt) |
| **Zendure MQTT (cloud)** | Any Zendure device via your Zendure API key; higher latency over the internet | [connection-types.md](docs/user/connection-types.md#zendure-mqtt-cloud) |

## Recommended: Admin Console

Install and start the Admin Console in a local EMS folder:

```bash
mkdir -p ems-solarflow-api-control
cd ems-solarflow-api-control
curl -fsSLO https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/deploy/admin/install-admin-console.sh
sh install-admin-console.sh
```

Then open `http://127.0.0.1:8090`. On first start, create the
shared EMS/Admin password in the browser.

Default mode uses host networking for reliable local discovery.
Use `--bridge` only if you need Docker bridge networking.

It provides guided setup, maintenance, backup/restore and guided upgrades. The
[Admin Console user guide](docs/user/admin-console.md#what-the-admin-console-looks-like)
has demo videos of a fresh install with hardware discovery and a guided software
update.

![Admin Console start page](docs/assets/screenshots/admin/admin-landing.png)

Full guide: [docs/user/admin-console.md](docs/user/admin-console.md)

## Documentation

- [User documentation](docs/user/)
- [Technical reference](docs/technical/)
- [Developer documentation](docs/developer/)
- [Full documentation map](docs/README.md)

## Getting help

- [FAQ](docs/user/faq.md)
- [Troubleshooting](docs/user/troubleshooting.md)
- [GitHub issues](https://github.com/basecubedev/ems-solarflow-api-control/issues)
