# Quickstart

## What This Guide Does

This guide gets a fresh checkout to live Zendure `outputLimit` control with the
fewest required steps.

It does not require Home Assistant. For details and safety background, see
[configuration.md](configuration.md), [safety.md](safety.md), and
[troubleshooting.md](troubleshooting.md).

## Requirements

- Python 3
- Network access from the EMS host to the Shelly meter
- Network access from the EMS host to each Zendure device
- Zendure device IP address and serial number
- Shelly IP address

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

## 3. Configure Devices And Shelly

Set the Shelly IP:

```json
{
  "shelly": {
    "ip": "192.168.1.50"
  }
}
```

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

## 4. Optional: Disable Home Assistant For First Start

Home Assistant is optional. For the simplest standalone first start:

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

Before the first live run, these checks are recommended but not strictly
required. Run them while the template safety flags are still active:

```bash
python3 -B ems-solarflow-api-control.py --self-test
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
python3 -B ems-solarflow-api-control.py --preflight
```

Use this target configuration for the first live `outputLimit` run:

```json
{
  "system": {
    "enabled": true,
    "dry_run": false,
    "allow_hardware_writes": true,
    "allow_state_reconciliation_writes": false,
    "max_total_power": 800,
    "loop_interval": 5,
    "min_output_limit": 0
  }
}
```

- `dry_run=false` enables real mode instead of calculation-only mode.
- `allow_hardware_writes=true` allows runtime Zendure `outputLimit` writes.
- `allow_state_reconciliation_writes=false` keeps SOC/mode reconciliation
  disabled for the first live run.
- Enable SOC/mode reconciliation later only if you want EMS to manage SOC and
  mode state too.

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
