# Quickstart

## What This Guide Does

This guide gets a fresh checkout to live Zendure `outputLimit` control with the
fewest required steps.

It does not require Home Assistant. For details and safety background, see
[configuration.md](configuration.md), [safety.md](safety.md), and
[troubleshooting.md](troubleshooting.md).

## Requirements

- Python 3
- Network access from the EMS host to the grid meter
- Network access from the EMS host to each Zendure device
- Zendure device IP address and serial number
- Grid meter IP address

The EMS should not run in parallel with another controller that writes Zendure
`outputLimit`.

## 1. Install Dependencies

Modern Linux distributions often protect the system Python environment. Use a
virtual environment by default.

Install Python, venv support, and pip first if needed.

Debian / Ubuntu / Raspberry Pi OS:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

openSUSE:

```bash
sudo zypper install python3 python3-pip python3-virtualenv
```

Create and use a local virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 2. Create `config.json`

```bash
cp config.template.json config.json
```

Edit only your local `config.json`. Prefer `config.template.json` for shared
examples. Do not commit real Home Assistant tokens, Zendure serial numbers, or
local IP addresses if the repository is public.

The template default is standalone-first: Home Assistant is disabled, live
Zendure `outputLimit` control is enabled, and required state reconciliation is
enabled for the normal regulation profile after local configuration. These
defaults are a starting point, not a universal safety profile. Set
`system.dry_run=true` if you want a no-write validation run before the first
normal start.

## 3. Configure Devices And Grid Meter

Set the grid meter type and IP. Supported types are `shelly`, `ecotracker`,
and `tasmota_http`. Shelly uses total power by default:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50"
  }
}
```

Use `channels` when selected Shelly clamps should be summed. A single item list
such as `["c"]` reads only clamp C:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50",
    "channels": ["c"]
  }
}
```

Multiple items such as `["a", "c"]` sum only those selected clamps:

```json
{
  "grid_meter": {
    "type": "shelly",
    "ip": "192.168.1.50",
    "channels": ["a", "c"]
  }
}
```

For everHome EcoTracker, use `"type": "ecotracker"` and the EcoTracker IP.
For Tasmota HTTP smart meter readers, configure the Tasmota IP and the JSON
field path that contains current power:

```json
{
  "grid_meter": {
    "type": "tasmota_http",
    "ip": "192.168.1.70",
    "power_path": "StatusSNS.SML.Power_curr"
  }
}
```

Legacy configs with only `shelly.ip` still work.

Set each Zendure device:

```json
{
  "name": "WR1",
  "ip": "192.168.1.100",
  "sn": "YOUR_SN",
  "max_power": 800,
  "pv_kwp": 1.0,
  "pv_priority_factor": 1.0,
  "battery_kwh": 1.0,
  "min_soc": 15,
  "max_soc": 100
}
```

Use your real values only in local `config.json`.

Review the installation-specific limits before unattended operation:

- `max_power` and `system.max_total_power`
- `min_soc` and `max_soc`
- `pv_kwp` and `pv_priority_factor`
- `battery_kwh`
- Grid meter direction and readings

## 4. Home Assistant Default

Home Assistant is optional and disabled by default. Keep this for standalone
operation:

```json
{
  "ha": {
    "enabled": false,
    "control_enabled": false,
    "url": "",
    "token": ""
  }
}
```

## 5. First Live Target Configuration

Before the first live run, make this path explicit:

1. Copy the template to `config.json`.
2. Enter real grid meter and Zendure IP addresses and serial numbers.
3. Review power, SOC, battery, and PV limits for the installation.
4. Optionally run simulation, preflight, and dry-run checks.
5. Monitor the first bounded live run.

These checks are optional but recommended:

```bash
python3 -B ems-solarflow-api-control.py --self-test
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
python3 -B ems-solarflow-api-control.py --preflight
```

The template already uses this live-control-ready system policy:

```json
{
  "system": {
    "enabled": true,
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": true,
    "reconcile_ac_mode_on_start": true,
    "reconcile_smart_mode": true,
    "max_total_power": 800,
    "loop_interval": 3,
    "min_output_limit": 35
  }
}
```

This means the main regulation features are enabled after local configuration.
It does not remove the need to review device limits, SOC limits, grid meter
readings, and installation-specific constraints.

- `dry_run=false` enables real mode instead of calculation-only mode.
- `allow_hardware_writes=true` allows runtime Zendure `outputLimit` writes.
- `allow_state_reconciliation_writes=true` allows required SOC/mode/runtime
  state reconciliation for normal regulation.
- `reconcile_ac_mode_on_start=true` is a startup reconciliation helper, not
  permanent cyclic forcing of `acMode`.
- `reconcile_smart_mode=true` keeps Zendure runtime/RAM mode aligned.

## 6. Start EMS Live

Run a bounded live test first:

```bash
python3 -B ems-solarflow-api-control.py --duration 120
```

Then start the normal live loop:

```bash
python3 -B ems-solarflow-api-control.py
```

## 7. Verify It Is Controlling

Expected live-control log events:

```text
event=startup dry_run=False allow_hardware_writes=True
event=target_calculation
event=write_output_limit
```

This event means no live output write happened:

```text
event=dry_run_output_limit
```

If there is no `write_output_limit`, see
[troubleshooting.md](troubleshooting.md#no-power-changes).

If Home Assistant helper changes are ignored, see
[troubleshooting.md](troubleshooting.md#home-assistant-helpers-are-ignored).

If regulation is too slow, see
[troubleshooting.md](troubleshooting.md#regulation-is-too-slow).

If a device does not output power, see
[troubleshooting.md](troubleshooting.md#device-is-online-but-does-not-deliver-power).

## 8. Run As A Service

After a successful bounded live run, run the same command under your preferred
service manager. Keep the working directory set to the repository root, or pass
an explicit config path:

```bash
python3 -B ems-solarflow-api-control.py --config /path/to/config.json
```

For systemd, this repository includes a starting template:

```text
ems-solarflow.service.template
```

Copy it to a local service file, adjust `User`, `WorkingDirectory`, and
`ExecStart` for your installation path, then install it with your normal systemd
workflow.

## If Something Does Not Work

If live control does not behave as expected, see
[troubleshooting.md](troubleshooting.md).
