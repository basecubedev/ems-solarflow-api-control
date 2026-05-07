# ems-solarflow-api-control

Simple and lightweight EMS (Energy Management System) for Zendure Solarflow systems.

No YAML. No complex Home Assistant setups.
Just Python + local API control.

---

# 💡 Why this project?

Most Solarflow integrations are:

* complex
* YAML-heavy
* hard to debug
* tightly coupled to Home Assistant

This project is different:

* direct local API control
* easy to understand
* easy to modify
* runs standalone
* Home Assistant optional
* dynamic entities
* no hidden automation logic

---

# 🚀 What it does

The EMS:

* reads house consumption (Shelly)
* reads Solarflow data (local API)
* calculates optimal output power
* distributes load across multiple devices
* prioritizes solar power usage
* dynamically balances charge/discharge across multiple batteries
* prevents PV curtailment on full batteries
* protects low SOC batteries during discharge
* automatically balances uneven battery states
* optionally integrates with Home Assistant

---

# ⚙️ Features

* ⚡ real-time control loop
* 🔌 multi-device support
* 🧠 intelligent SOC-aware balancing
* 🔋 intelligent multi-battery balancing
* ☀️ PV curtailment avoidance
* 🪫 low battery protection
* ⚖️ automatic SOC equalization
* 🔄 mixed battery / non-battery system support
* 🏠 optional Home Assistant integration
* 🧩 JSON-based configuration
* 🚫 no YAML required
* ♻️ dynamic Home Assistant entities
* 🔄 automatic entity cleanup
* 🐍 pure Python
* 🧰 standalone operation possible

---

# 🔋 Intelligent Battery Balancing

The EMS dynamically balances power distribution across all connected devices.

Features:

* fuller batteries feed more directly into household load
* batteries with remaining capacity keep more solar energy for charging
* low SOC batteries are protected during discharge
* battery SOC levels naturally converge over time
* mixed systems with and without batteries are supported automatically

This improves:

* PV utilization
* battery lifetime
* charge/discharge symmetry
* multi-device efficiency
* overall energy balancing

---

# 🖥️ Dashboard

Ready-to-use Home Assistant dashboard included:

```text
homeassistant-dashboard/dashboard.yaml
```

<p align="center">
  <img src="./homeassistant-dashboard/dashboard-preview.jpg" width="900">
</p>

---

# 🔌 Zendure API Basics

Zendure Solarflow devices provide a local HTTP API.

## 📥 Read device data

```bash
curl http://DEVICE_IP/properties/report
```

Important fields:

| Field             | Meaning                     |
| ----------------- | --------------------------- |
| `electricLevel`   | battery SOC (%)             |
| `solarInputPower` | solar input power           |
| `outputHomePower` | AC output to home           |
| `outputPackPower` | power sent TO battery       |
| `packInputPower`  | power received FROM battery |

---

## 🔋 Battery Power Semantics

Zendure reports battery values from the controller/inverter perspective.

This means:

| API Field         | Battery Meaning     |
| ----------------- | ------------------- |
| `outputPackPower` | battery charging    |
| `packInputPower`  | battery discharging |

The EMS converts this into a battery-centric model:

| EMS Value              | Meaning     |
| ---------------------- | ----------- |
| positive battery power | charging    |
| negative battery power | discharging |

This applies to all `battery_power` sensors.

---

## 📤 Write output limit

```bash
curl -X POST http://DEVICE_IP/properties/write \
  -H "Content-Type: application/json" \
  -d '{
    "sn": "YOUR_SN",
    "properties": {
      "acMode": 2,
      "outputLimit": 300,
      "smartMode": 0
    }
  }'
```

---

# ⚠️ Important behavior

* values are not persistent
* control works like RAM (temporary state)
* your script must run continuously
* if the script stops unexpectedly, the last configured output limit remains active
* always use conservative power limits
* additional failsafe mechanisms are recommended

---

# 🔍 How to get your device serial number (SN)

The serial number (SN) is required to send write commands.

## Option 1: via API (recommended)

```bash
curl http://DEVICE_IP/properties/report
```

Look for:

```text
"sn": "EOD1XXXXXXXXXXXX"
```

## Option 2: device label

* printed on device
* visible in Zendure app

---

# 🔘 Standalone Enable / Disable

The EMS can run fully standalone without Home Assistant.

Enable/disable via:

```json
"system": {
  "enabled": true
}
```

---

# 🏠 Home Assistant Integration

Home Assistant support is fully optional.

The EMS can run:

* standalone
* without HA
* only via local Zendure APIs

Enable/disable HA:

```json
"ha": {
  "enabled": true
}
```

The EMS uses the Home Assistant REST API:

```text
GET  /api/states/<entity_id>
POST /api/states/<entity_id>
```

Used for:

* enable/disable control
* max power setting
* loop interval
* telemetry publishing
* dashboard integration

---

# ♻️ Dynamic Home Assistant Entities

Entities are created dynamically via the REST API.

Behavior:

* entities appear automatically while EMS is running
* removed sensors disappear automatically
* no YAML sensor definitions required
* no manual cleanup necessary
* entities are runtime-driven

This keeps the Home Assistant setup minimal and clean.

---

# 🏠 Home Assistant Helpers

Optional HA helper entities.

## 🔘 Enable / Disable EMS

```yaml
input_boolean:
  ems_solarflow_enable:
    name: EMS Solarflow Enable
    icon: mdi:solar-power-variant
```

---

## ⚡ Max Total Power

```yaml
input_number:
  ems_solarflow_max_power:
    name: EMS Max Power
    min: 0
    max: 800
    step: 10
    unit_of_measurement: W
    mode: slider
```

---

## ⏱️ Control Loop Interval

```yaml
input_number:
  ems_solarflow_interval:
    name: EMS Loop Interval
    min: 1
    max: 10
    step: 1
    unit_of_measurement: s
    mode: slider
```

---

# 📊 Global Sensors

Automatically created by the EMS.

| Entity                               | Meaning                      |
| ------------------------------------ | ---------------------------- |
| `sensor.ems_solarflow_load`          | current household load       |
| `sensor.ems_solarflow_target_total`  | calculated EMS output target |
| `sensor.ems_solarflow_solar_total`   | total solar generation       |
| `sensor.ems_solarflow_battery_power` | signed battery power         |
| `sensor.ems_solarflow_home`          | estimated home power         |
| `sensor.ems_solarflow_soc_avg`       | average battery SOC          |

---

# 🔋 Battery Power Convention

All `battery_power` sensors use signed values:

| Value    | Meaning     |
| -------- | ----------- |
| positive | charging    |
| negative | discharging |
| zero     | idle        |

Example:

```text
+250 W  -> charging
-180 W  -> discharging
```

---

# 🔌 Per Device Sensors

For each device:

Example:

```text
WR1
WR2
```

The EMS creates:

## Core Sensors

```text
sensor.ems_solarflow_wr1_soc
sensor.ems_solarflow_wr1_min_soc
sensor.ems_solarflow_wr1_max_soc
sensor.ems_solarflow_wr1_solar
sensor.ems_solarflow_wr1_output
sensor.ems_solarflow_wr1_target
sensor.ems_solarflow_wr1_output_limit
sensor.ems_solarflow_wr1_battery_power
```

---

## Battery / Electrical

```text
sensor.ems_solarflow_wr1_voltage
sensor.ems_solarflow_wr1_remaining_minutes
```

---

## Thermal / Signal

```text
sensor.ems_solarflow_wr1_temp
sensor.ems_solarflow_wr1_rssi
```

---

## Solar Panel Inputs

```text
sensor.ems_solarflow_wr1_panel1
sensor.ems_solarflow_wr1_panel2
sensor.ems_solarflow_wr1_panel3
sensor.ems_solarflow_wr1_panel4
```

---

## Binary Sensors

```text
binary_sensor.wr1_fault
binary_sensor.wr1_ac_active
binary_sensor.wr1_dc_active
binary_sensor.wr1_grid_online
```

---

# 📁 Project Structure

```text
ems-solarflow-api-control/
│
├── homeassistant-dashboard/
├── ems-solarflow-api-control.py
├── config.json
├── config.template.json
├── ems-solarflow.service.template
├── README.md
└── .gitignore
```

---

# 📄 File Overview

## `ems-solarflow-api-control.py`

Main EMS control loop:

* device polling
* power calculation
* intelligent SOC balancing
* PV-aware load distribution
* battery headroom management
* API communication
* Home Assistant integration

---

## `config.json`

User configuration:

* device IPs
* serial numbers
* Shelly IP
* Home Assistant token
* EMS settings

---

## `config.template.json`

Template configuration:

```bash
cp config.template.json config.json
```

---

## `ems-solarflow.service.template`

Systemd service template:

* auto start
* restart on crash
* background execution

---

# ⚙️ Installation

## 1. Clone repository

```bash
git clone https://github.com/basecubedev/ems-solarflow-api-control
cd ems-solarflow-api-control
```

---

## 2. Create config

```bash
cp config.template.json config.json
```

Edit:

* device IPs
* serial numbers
* Home Assistant token
* system limits

---

## 3. Start EMS

```bash
python3 ems-solarflow-api-control.py
```

---

# 🔧 systemd (recommended)

## Install service

```bash
sudo cp ems-solarflow.service.template \
  /etc/systemd/system/ems-solarflow.service
```

---

## Edit service

```bash
sudo nano /etc/systemd/system/ems-solarflow.service
```

---

## Enable + start

```bash
sudo systemctl daemon-reload
sudo systemctl enable ems-solarflow
sudo systemctl start ems-solarflow
```

---

## Logs

```bash
journalctl -u ems-solarflow -f
```

---

# 🧠 Control Logic

1. read house consumption (Shelly)
2. read Zendure solar + battery state
3. calculate required total power
4. prioritize direct solar usage
5. detect battery charge/discharge headroom
6. avoid PV curtailment on full batteries
7. protect low SOC batteries
8. dynamically balance battery usage
9. update inverter output limits
10. publish telemetry to Home Assistant

---

# ⚡ Design Philosophy

```text
simple > complex
```

Principles:

* one script
* one config
* local control
* minimal dependencies
* no frameworks
* no hidden logic
* transparent behavior

---

# 📦 Dependencies

## pip

```bash
pip install -r requirements.txt
```

---

## Debian / Ubuntu

```bash
sudo apt install python3-requests
```

---

# 🐍 Requirements

* Python 3.10+
* Linux recommended
* tested on Debian / Ubuntu

---

# 🚧 Experimental Software

This project is experimental software intended for:

* self-hosting
* development
* experimentation
* private energy systems

Do not use this project in safety-critical environments.

---

# ⚠️ Disclaimer

This project is an unofficial community project and is not affiliated with, endorsed by, or supported by Zendure.

Use this software at your own risk.

The software directly controls power output behavior of connected energy devices.

No guarantee is provided regarding:

* safety
* stability
* reliability
* regulatory compliance
* protection against incorrect device behavior

The author is not responsible for:

* hardware damage
* battery damage
* energy losses
* grid violations
* legal or regulatory issues
* data loss
* direct or indirect damages

Always verify local electrical and grid regulations before using this software.

This project is intended for technically experienced users only.

---

# 📜 License

Licensed under the Apache License 2.0.

See the `LICENSE` file for details.

---

# 🛠️ Roadmap

* serial / parallel inverter control for VDE AR-N 4105:2026 F 1.2
* watchdog / failsafe
* improved dashboard visualization
* historical telemetry -> publish to InfluxDB
