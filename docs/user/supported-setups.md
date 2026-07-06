# Supported Setups

Use this page to check whether your setup is likely to fit EMS before you start
editing config. It is a user-facing compatibility overview, not the full config
reference. For exact keys and values see
[configuration.md](../technical/configuration.md) and, for Admin Console
discovery, [admin-discovery.md](../technical/admin-discovery.md).

## Recommended Setup

- Docker Compose (Admin Console is the recommended path)
- local network access to Zendure devices with Zendure Local API available and
  enabled
- one supported grid meter
- EMS is the only controller writing Zendure `outputLimit`
- dashboard reachable only on your trusted local network

## Supported Zendure Devices (Local API / ZenSDK)

EMS controls Zendure SolarFlow devices through the local Zendure API /
ZenSDK-compatible HTTP API. Known ZenSDK-compatible models:

| Model | Notes |
|---|---|
| SolarFlow 800 | ZenSDK-compatible local HTTP control. |
| SolarFlow 800 Plus | ZenSDK-compatible local HTTP control. |
| SolarFlow 800 Pro | ZenSDK-compatible local HTTP control. |
| SolarFlow 1600 AC+ | ZenSDK-compatible local HTTP control. |
| SolarFlow 2400 AC | ZenSDK-compatible local HTTP control. |
| SolarFlow 2400 AC+ | ZenSDK-compatible local HTTP control. |
| SolarFlow 2400 Pro | ZenSDK-compatible local HTTP control. |

Each configured Zendure device needs a local IP address, serial number, max
power, battery size, PV size / priority metadata, and min/max SOC limits.

Avoid assuming support for a model only from the name. Use tested/known setups
where possible, and open an issue with diagnostics if your device payloads look
different from the supported local API behavior.

## Supported Grid Meters

| Type | Config value | Notes |
|---|---|---|
| Shelly Pro / Plus / Gen2 / Gen3 | `shelly` | Uses the RPC status endpoint. |
| Shelly 3EM Gen1 | `shelly_3em_gen1` | Uses the classic `/status` endpoint. |
| EcoTracker | `ecotracker` | Uses the local API path implemented by EMS. |
| Zendure Smart Meter 3CT HTTP | `zendure_smartmeter_3ct_http` | Reads `total_power` from the local `/properties/report` REST endpoint; no MQTT required. |
| Tasmota HTTP / SmartMeter reader | `tasmota_http` | Requires URL/IP and `power_path`. |
| Zendure SmartMeter D0 (MQTT) | `zendure_smartmeter_d0` | Subscribes to an existing broker topic such as `Zendure/sensor/<SERIAL>/totalPower`; positive import, negative export. |
| Generic MQTT grid meter | `mqtt` | Requires an existing broker, one topic, and a numeric or JSON power payload. |

MQTT here is a **grid meter input** only. It is a read-only load signal; EMS
does not control Zendure devices or inverters over MQTT.

## Optional Integrations

- Home Assistant is optional.
- InfluxDB analytics is optional.
- Native Python is supported for advanced/manual installs.

Zendure Local API must be available and enabled for local EMS control. Do not
run Zendure HEMS, Home Assistant automations, MQTT writers, or any other
controller in parallel if they write Zendure `outputLimit`. EMS assumes
exclusive write control over `outputLimit` while active.

## Roadmap

- MQTT/ZHA-style Zendure device or inverter control is planned, but is **not**
  part of the current supported control path. Today MQTT is only supported as a
  grid meter input (see above).

## Not Supported / Not Yet Verified

- MQTT/ZHA-style device or inverter control (roadmap only).
- Older MQTT-only Zendure devices without the local Zendure API / ZenSDK.
- Any Zendure model not listed above, unless your own tested setup confirms the
  local API behavior.
- Running EMS alongside another controller that writes Zendure `outputLimit`.

## Not Recommended

- using placeholder IPs or `YOUR_SN`
- starting unattended before diagnose and first-run validation
- assuming InfluxDB is required for the dashboard
- exposing the dashboard publicly without a reverse proxy/auth design

## Next step

- Most users: [Admin Console](admin-console.md)
- Shell-only install: [Docker Bootstrap](docker-bootstrap.md)
- Development or source checkout: [Developer Setup](../developer/developer-setup.md)
