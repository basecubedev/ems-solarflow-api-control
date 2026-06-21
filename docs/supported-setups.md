# Supported Setups

Use this page to check whether your setup is likely to fit EMS before you start
editing config.

## Recommended Setup

- Docker Compose
- local network access to Zendure devices
- one supported grid meter
- EMS is the only controller writing Zendure `outputLimit`
- dashboard reachable only on your trusted local network

## Supported Grid Meters

| Type | Config value | Notes |
|---|---|---|
| Shelly Pro / Plus / Gen2 / Gen3 | `shelly` | Uses the RPC status endpoint. |
| Shelly 3EM Gen1 | `shelly_3em_gen1` | Uses the classic `/status` endpoint. |
| EcoTracker | `ecotracker` | Uses the local API path implemented by EMS. |
| Tasmota HTTP / SmartMeter reader | `tasmota_http` | Requires URL/IP and `power_path`. |

## Zendure Devices

Each configured Zendure device needs:

- local IP address
- serial number
- max power
- battery size
- PV size / priority metadata
- min/max SOC limits

Avoid assuming support for a model only from the name. Use tested/known setups
where possible, and open an issue with diagnostics if your device payloads look
different from the supported local API behavior.

## Optional Integrations

- Home Assistant is optional.
- InfluxDB analytics is optional.
- Native Python is supported for advanced/manual installs.

## Not Recommended

- running EMS alongside another controller that writes Zendure `outputLimit`
- using placeholder IPs or `YOUR_SN`
- starting unattended before diagnose and first-run validation
- assuming InfluxDB is required for the dashboard
- exposing the dashboard publicly without a reverse proxy/auth design

Next step: [quickstart.md](quickstart.md) or
[first-run-checklist.md](first-run-checklist.md).
