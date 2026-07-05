# ems-solarflow-api-control

[![Continuous Integration](https://github.com/basecubedev/ems-solarflow-api-control/actions/workflows/simulated-regression-tests.yml/badge.svg)](https://github.com/basecubedev/ems-solarflow-api-control/actions/workflows/simulated-regression-tests.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.14-blue)
![automated tests](https://img.shields.io/badge/automated%20tests-2100%2B-blue)

Local-first EMS control for Zendure SolarFlow systems.
It reads local grid meter and Zendure telemetry, controls inverter output,
and provides a local dashboard.

> EMS controls real power hardware.
> Read the [safety guide](docs/user/safety.md) before enabling live writes.

## Supported hardware at a glance

**Zendure devices:** EMS controls Zendure SolarFlow devices through the local
Zendure API / ZenSDK-compatible HTTP API. Known ZenSDK-compatible models:
SolarFlow 800, SolarFlow 800 Plus, SolarFlow 800 Pro, SolarFlow 1600 AC+,
SolarFlow 2400 AC, SolarFlow 2400 AC+, SolarFlow 2400 Pro.

**Grid meters:** Shelly Pro/Plus Gen2/Gen3, Shelly 3EM Gen1, EcoTracker,
Zendure Smart Meter 3CT HTTP, Tasmota HTTP / SmartMeter, Zendure SmartMeter D0
via MQTT, and generic MQTT grid meters.

**Roadmap:** MQTT/ZHA-style Zendure device or inverter control is planned, but
is not part of the current supported control path.

Full list and notes:
[docs/user/supported-setups.md](docs/user/supported-setups.md)

## Choose your path

Most users should start with the **Admin Console**. All three paths converge on
the same standard `config/config.json` layout, so you can switch later.

| Path | Choose this if | Continue |
| --- | --- | --- |
| **Admin Console** | You want browser-guided setup, discovery, updates, backups and maintenance | [docs/user/admin-console.md](docs/user/admin-console.md) |
| **Docker Bootstrap** | You want shell-only Docker setup | [docs/user/docker-bootstrap.md](docs/user/docker-bootstrap.md) |
| **Developer Setup** | You want to develop, debug or build from source | [docs/developer/developer-setup.md](docs/developer/developer-setup.md) |

## Recommended: Admin Console

Install and start the Admin Console in a local EMS folder:

```bash
mkdir -p ems-solarflow-api-control
cd ems-solarflow-api-control
curl -fsSLO https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/deploy/admin/install-admin-console.sh
sh install-admin-console.sh
```

Open:

```text
http://127.0.0.1:8090
```

On first start, create the shared EMS/Admin password in the browser.

Default mode uses host networking for reliable local discovery.
Use `--bridge` only if you need Docker bridge networking.

Full guide: [docs/user/admin-console.md](docs/user/admin-console.md)

## Documentation

- [User documentation](docs/user/)
- [Technical reference](docs/technical/)
- [Developer documentation](docs/developer/)
- [Full documentation map](docs/README.md)

## Getting help

Start with:

- [FAQ](docs/user/faq.md)
- [Troubleshooting](docs/user/troubleshooting.md)
- [GitHub issues](https://github.com/basecubedev/ems-solarflow-api-control/issues)
