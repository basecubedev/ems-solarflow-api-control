# Native Python Setup

Docker is recommended for normal users. Native Python remains supported for
developers, manual installs, local debugging, and service-manager setups where
you want to control the Python environment yourself.

## Requirements

- Python 3.11 or newer
- network access from this host to the grid meter
- network access from this host to each Zendure device
- Zendure device IP address and serial number
- grid meter type and IP address

## Install Python Dependencies

Debian / Ubuntu / Raspberry Pi OS:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

openSUSE:

```bash
sudo zypper install python3 python3-pip python3-virtualenv
```

Create and use a virtual environment from the repository checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Create Config

New setups use the standard `config/config.json` layout:

```bash
mkdir -p config data
cp config/config.template.json config/config.json
```

Edit only your local `config/config.json`:

```bash
nano config/config.json
```

Set real grid meter and Zendure values, then review power, SOC, battery, and PV
limits. Template placeholder values force safe mode until replaced.

You can also run the optional setup assistant, which now writes
`config/config.json`:

```bash
python3 emsctl.py config init
```

Older checkouts may still use a root `config.json`. That legacy layout is still
read as a fallback, but new setups should use `config/config.json`. See
[Config Layout](user/config-layout.md).

## First Checks

Run local diagnostics:

```bash
python3 emsctl.py diagnose
```

Run a no-write validation if desired:

```bash
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
```

Run preflight:

```bash
python3 -B ems-solarflow-api-control.py --preflight
```

Use hardware diagnostics only when you are ready for read-only probes to the
configured local devices and meter:

```bash
python3 emsctl.py diagnose --hardware
```

## Start EMS

Run a bounded live test first:

```bash
python3 -B ems-solarflow-api-control.py --duration 120
```

Then start the normal loop:

```bash
python3 -B ems-solarflow-api-control.py
```

If you keep the config somewhere else, pass it explicitly:

```bash
python3 -B ems-solarflow-api-control.py --config /path/to/config.json
python3 emsctl.py --config /path/to/config.json diagnose
```

## Run As A Service

After a successful bounded live run, use your preferred service manager. Keep
the working directory set to the repository root, or pass `--config` with an
absolute path.

This repository includes a starting systemd template:

```text
ems-solarflow.service.template
```

Copy it to your local service location and adjust `User`, `WorkingDirectory`,
and `ExecStart` for your installation.

## Useful Native CLI Commands

```bash
python3 emsctl.py status
python3 emsctl.py interactive
python3 emsctl.py diagnose
python3 emsctl.py diagnose --deep
python3 emsctl.py diagnose --control
python3 emsctl.py diagnose --control-quality --sample-seconds 60
python3 emsctl.py backup create
python3 emsctl.py config upgrade --dry-run
python3 emsctl.py config upgrade
```

Native backups are stored in `data/backups/` by default.

More CLI details: [cli.md](cli.md).
