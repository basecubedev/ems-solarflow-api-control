# EMS SolarFlow

## Smarter local control for Zendure solar and battery systems

EMS SolarFlow brings your grid meter, Zendure inverters, batteries and solar production together in one energy management system.

It continuously measures whether your home is importing electricity from the grid or exporting surplus solar power. It then adjusts the connected inverters and batteries to match your household demand as closely as possible.

The goal is simple:

> Use more of your own solar energy, reduce unnecessary grid export and manage several devices as one coordinated system.

---

## What EMS SolarFlow does

EMS SolarFlow can automatically:

- follow the current power demand of your home
- reduce unwanted grid export
- supply more power when household demand rises
- reduce inverter output when demand falls
- coordinate several inverters and battery systems
- distribute the required power across available devices
- respect total system and individual device limits
- protect configured minimum and maximum battery levels
- react safely when a device or measurement becomes unavailable

The control loop runs continuously. Once the system is configured, normal operation does not require manual adjustment.

---

## One system for several devices

A single EMS installation can manage one inverter or a larger setup with multiple Zendure devices and battery packs.

Power is not divided blindly. The system can consider:

- current battery level
- available solar power
- battery capacity
- installed PV capacity
- individual device limits
- configurable device priorities
- whether a device is online and ready

Unused capacity can be reassigned to other devices. This helps the complete installation work as one coordinated energy system instead of several independent units.

---

## Smooth and responsive power control

Household loads can change within seconds. EMS SolarFlow includes control logic designed to react quickly without constantly sending unnecessary changes to the hardware.

It provides:

- smoothing of noisy grid-meter readings
- fast reaction to significant changes between grid import and export
- controlled increases and reductions in output
- suppression of very small, unnecessary adjustments
- safe handling of old or missing telemetry
- automatic clamping to system and device limits
- redistribution when one device reaches its limit

The current control decision can be followed step by step in the dashboard.

---

## Flexible device connections

Zendure devices can be connected in three ways:

### Local API

The preferred option for newer SolarFlow and ZenSDK-compatible devices. Communication stays inside the local network and provides the fastest response as well as full device-state coordination.

### Local MQTT

Zendure devices can communicate through your own local MQTT broker. This provides low latency and avoids a cloud dependency after the device has been moved to the local broker.

### Zendure MQTT

Devices can also be reached through Zendure's MQTT infrastructure using a Zendure API key. This is especially useful for devices that do not provide a compatible local API.

### True mixed operation

All connection types can be used together in the same installation.

For example:

| Device | Connection |
|---|---|
| Inverter 1 | Local API |
| Inverter 2 | Local MQTT |
| Inverter 3 | Zendure MQTT |

All devices still participate in the same control loop, power allocation and safety checks. Multiple MQTT brokers and separate credentials are supported as well.

---

## Supported grid measurements

EMS SolarFlow uses a compatible grid meter to determine the current household demand and grid flow.

Supported options include:

- Shelly Pro meters
- Shelly Plus, Gen2 and Gen3 meters
- Shelly 3EM Gen1
- Zendure Smart Meter 3CT
- Zendure Smart Meter D0
- everHome EcoTracker
- Tasmota-based meters
- generic MQTT power meters
- selected Home Assistant entities

Support differs by model and connection type. The current compatibility overview is available in [Supported Setups](supported-setups.md).

---

## Battery management

EMS SolarFlow does more than change inverter output. It also helps keep battery operation consistent with the configured energy strategy.

Available functions include:

- minimum and maximum battery level limits
- battery-aware power distribution
- coordinated charging and discharging across several devices
- battery-capacity-based allocation
- operating-mode and battery-limit reconciliation
- optional AC charging control
- configurable off-grid socket modes

### Winter mode

Winter mode can gradually raise the minimum battery reserve during selected months. Outside the winter period, it returns the system to the configured summer reserve.

This protects a larger reserve during low-production months without changing the normal household power-control logic.

### Battery full-charge assist

The optional full-charge assistant helps battery-backed devices periodically reach the full state reported by their firmware.

It can schedule an assist window, temporarily prepare the required charging conditions and restore the normal battery settings afterwards. The process survives restarts and is visible in diagnostics and the device dashboard.

---

## Built-in dashboard

The local dashboard provides a clear view of the complete energy system.

### Overview

See the current flow between:

- solar production
- batteries
- inverters
- household consumption
- the public grid

### Devices

View each device separately, including power, battery level, connection state, firmware-reported operating states and full-charge-assist status.

### Control

Follow the current EMS decision from the grid measurement through filtering, target calculation and device allocation to the final output target.

### Energy

See delivered energy totals and estimated savings based on a configurable electricity price.

Available periods include daily, weekly, monthly, yearly and lifetime statistics.

### History

Short-term history is stored locally and works without any external database.

### Analytics

Optional InfluxDB integration adds long-term analysis with:

- extended time ranges
- custom date selection
- device filters
- zooming
- grid, battery, PV and device views
- comparison overlays for battery level, grid power and EMS targets

### Diagnose

Run guided installation, hardware, control and quality checks directly from the browser. A redacted support bundle can be downloaded for troubleshooting.

### Logs

View recent EMS messages, filter by severity and temporarily adjust the service log level while diagnosing a problem.

### Maintenance

Authenticated operators can create backups, preview restores and review configuration upgrades from the dashboard.

### Everyday runtime controls

The normal installation remains configuration-based, but selected operating values can be changed without rebuilding the setup. Authenticated dashboard controls, the command-line tool and optional Home Assistant helpers can manage values such as:

- enabling or pausing EMS control
- total and per-device power limits
- control interval and minimum output
- device availability and PV priority
- winter mode
- off-grid socket mode
- selected AC charging roles and charge power

These changes use the same local runtime state and remain subject to the configured safety limits.

---

## Guided setup and maintenance

The Admin Console is the recommended installation and management interface.

### Guided setup

It can:

- prepare a new installation
- discover devices on the local network
- discover local MQTT brokers and connected devices
- discover Zendure MQTT devices using an API key
- collect optional broker credentials
- identify possible grid meters and inverters
- generate a configuration proposal
- validate settings before applying them
- prepare and start the required services

Detected devices are presented for review before they are added. Unknown or conflicting hardware remains telemetry-only until the exact model is confirmed.

### Existing-system maintenance

The Admin Console can also manage an existing installation:

- show installation and container status
- run EMS diagnostics
- add, edit or remove hardware
- rediscover devices and changed addresses
- manage MQTT connections and credentials
- preview configuration changes
- back up the current configuration before applying changes
- migrate older MQTT configuration safely

### Guided upgrades

Guided Upgrade provides a visible, step-by-step update process:

- select and verify the target System Build
- ensure the Admin Console and EMS belong to the same build
- run preflight checks
- create a backup
- review required configuration changes
- update the deployment
- recreate the EMS service
- run health checks and diagnostics

The exact verified software image is used, and the system keeps a last-known-good record of successful installations.

---

## Backup and restore

EMS SolarFlow includes backup and restore tools for:

- configuration and credentials
- local history and state databases
- bundled InfluxDB analytics data

Restore is preview-first. The system shows what will be replaced before anything is changed.

Before a restore starts, an automatic rollback backup is created. Archives are checked for integrity and compatibility. Password-protected backups are supported through the command-line tools.

The system does not silently downgrade the EMS software when older data is restored.

---

## Optional Home Assistant integration

Home Assistant is not required. EMS SolarFlow can run completely on its own.

When enabled, Home Assistant can receive:

- household load
- solar and battery power
- EMS targets
- battery levels
- device availability
- device operating states
- winter-mode status

Optional helpers can also change selected runtime settings, such as:

- enabling or disabling EMS control
- total and per-device power limits
- minimum output level
- control interval
- winter mode
- device priority
- off-grid socket mode

Home Assistant remains an optional interface. The local EMS state and safety rules remain authoritative if Home Assistant is unavailable.

---

## Safe testing and operation

EMS SolarFlow controls real power hardware, so write access is deliberately protected.

Safety features include:

- dry-run operation without hardware writes
- separate permissions for Local API, Local MQTT and Zendure MQTT control
- exact-model checks before MQTT output control is allowed
- telemetry-only fallback for unknown or conflicting devices
- configurable system and device power limits
- stale-data and offline-device handling
- protected state-changing dashboard actions
- automatic backups before risky changes
- previews and explicit confirmation before configuration or restore operations
- redaction of passwords, API keys and tokens from diagnostics

A new setup can therefore be observed and validated before live control is enabled.

Only one controller should change the Zendure output limit at a time. Zendure HEMS, Home Assistant automations or other MQTT writers must not compete with EMS SolarFlow for the same control value.

---

## Local-first and independent

The EMS control loop, dashboard, configuration, short-term history and operational state all run on your own system.

Home Assistant, InfluxDB and Zendure MQTT are optional. Newer supported devices can operate entirely through the local network using the Local API or Local MQTT.

The built-in web interfaces support password-protected operator access. The dashboard can also provide local HTTPS with a self-signed or user-provided certificate. Public internet exposure is not recommended; remote access should use a VPN or a properly secured reverse proxy.

Typical installations run with Docker on a small home server, NAS or Raspberry Pi. A manual Python installation and a comprehensive command-line tool are also available for advanced users.

A dedicated **appliance image** is also available for a Raspberry Pi that should run EMS and nothing else: a prepared card with the operating system, the containers and a small management console. It comes in a two-slot shape that rolls a failed operating-system update back by itself (Raspberry Pi 4 and 5) and a single-slot shape patched by `apt` (Raspberry Pi 3, 3B+, 4 and 5). Neither has been confirmed on a physical board yet.

---

## Current hardware coverage

| Hardware family | Available connection | Current confidence |
|---|---|---|
| SolarFlow 800 Pro 2 | Local API and Zendure MQTT | Validated on real hardware |
| SolarFlow 800 / 800 Plus / 800 Pro | Local API and Zendure MQTT | Family-supported |
| SolarFlow 1600 AC+ | Local API and Zendure MQTT | Family-supported |
| SolarFlow 2400 AC / AC+ / Pro | Local API and Zendure MQTT | Family-supported |
| SolarFlow 4000 AC+ | Local API and Zendure MQTT | Family-supported |
| Hub 1200 / Hub 2000 / Hyper 2000 / AIO 2400 | Local MQTT or Zendure MQTT | Reverse-engineered; testers wanted |
| Ace 1500 / SuperBase V | MQTT | Telemetry-only |
| Shelly Pro grid meter | Local HTTP | Validated on real hardware |
| Zendure Smart Meter 3CT / D0 | HTTP or MQTT | Implemented; testers wanted |
| Other supported grid meters | HTTP, MQTT or Home Assistant | Model-dependent |
| Appliance image (Raspberry Pi 3 / 4 / 5) | — | Reverse-engineered; no board has booted one |

**Validated** means confirmed on real hardware.  
**Family-supported** means the model shares a known supported protocol but has not yet been individually confirmed.  
**Reverse-engineered** means support was developed from vendor information and community implementations and still needs wider physical-hardware testing.

---

## Who is it for?

EMS SolarFlow is designed for people who want to:

- use more of their own solar energy
- reduce unnecessary grid export
- coordinate several Zendure systems
- combine newer and older hardware generations
- keep energy management local where possible
- understand how their system is behaving
- avoid dependence on one vendor application for day-to-day control and analysis

It can support a simple installation with one inverter and one battery, as well as a mixed setup with several inverters, batteries, brokers and connection types.

---

## Project status

EMS SolarFlow is a young, actively developed open-source project.

The core control system, mixed Local API/MQTT operation, dashboard, guided setup, diagnostics, updates, backup and restore are implemented. Hardware support continues to grow as more device generations and firmware versions are tested by the community.

Reports from both successful and unsuccessful hardware tests are valuable. They help move devices from expected or reverse-engineered support to confirmed compatibility.

---

## In one sentence

**EMS SolarFlow turns supported grid meters, Zendure inverters and batteries into one coordinated, locally managed energy system that follows household demand, reduces grid export and makes the complete installation visible and maintainable from the browser.**

## Learn more

**Start here:** [User documentation](index.md) routes you by situation — new
installation, existing installation, upgrade, problem diagnosis, or a device
compatibility report.

Step-by-step, screenshot-led guides:

- [Admin Console guides](admin/index.md) —
  [Guided Setup](admin/guided-setup.md) ·
  [Guided Upgrade](admin/guided-upgrade.md) ·
  [Maintenance](admin/maintenance.md) ·
  [MQTT](admin/mqtt.md) ·
  [Backup and restore](admin/backup-restore.md) ·
  [Diagnostics and recovery](admin/diagnostics-recovery.md)
- [EMS Dashboard guides](dashboard/index.md) —
  [Overview](dashboard/overview.md) ·
  [Devices](dashboard/devices.md) ·
  [Energy](dashboard/energy.md) ·
  [Control pipeline](dashboard/control.md) ·
  [Runtime settings](dashboard/runtime-settings.md)

Reference:

- [Supported setups](supported-setups.md)
- [Connection types](connection-types.md)
- [Admin Console](admin-console.md)
- [Safety guide](safety.md)
- [Full documentation](../README.md)
