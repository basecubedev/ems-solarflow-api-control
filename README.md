# ems-solarflow-api-control

Local-first EMS (Energy Management System) control for Zendure SolarFlow systems.

No YAML automation stack. No cloud dependency for control decisions. Just
Python, JSON configuration, local device telemetry, structured logs, and
transparent runtime control.

This project is designed for advanced users who want a deterministic,
inspectable, firmware-aware controller for Zendure SolarFlow devices.

---

## Status And Safety Warning

This software interacts with real power hardware.

It is:

- experimental
- under active development
- intended for testing and validation
- designed for advanced users
- not production-certified
- not guaranteed safe for unattended operation

Live hardware writes are disabled by default in the template.

Start with dry-run, simulation, replay, or preflight mode. Inspect the logs.
Only enable live writes after you understand the calculated targets and the
current firmware state of your devices.

The EMS should not run in parallel with another controller that writes Zendure
`outputLimit`.

Detailed safety model: [docs/safety.md](docs/safety.md).

---

## Why This Project?

Most SolarFlow control setups become hard to reason about at runtime.

This project favors:

```text
observable > magical
runtime truth > assumed state
simple > complex
```

Core goals:

- direct local Zendure API control
- Shelly-based household load tracking
- standalone operation without Home Assistant
- optional Home Assistant monitoring and runtime controls
- PV-first allocation with battery top-up
- stable fast output control for short loop intervals
- runtime-state file for mutable operator state
- conservative SOC/mode reconciliation
- winter minSoc ramp as state reconciliation
- structured `event=...` logs for validation

---

## Architecture

```mermaid
flowchart LR

    Shelly["Shelly Power Meter"]
    EMS["EMS Controller\nPython"]
    WR1["Zendure WR1"]
    WR2["Zendure WR2"]
    HA["Home Assistant\noptional"]

    Shelly -->|house load| EMS

    WR1 -->|telemetry| EMS
    WR2 -->|telemetry| EMS

    EMS -->|runtime outputLimit| WR1
    EMS -->|runtime outputLimit| WR2

    EMS -->|status sensors| HA
    HA -->|optional helper values| EMS
```

Home Assistant is optional and is not required for control decisions.

Control details: [docs/control-logic.md](docs/control-logic.md).
Visual control-flow map: [docs/control-flow.md](docs/control-flow.md).

---

## Project Structure

The EMS keeps a simple user-facing model:

```text
one start script, one static config
```

You still start the EMS with:

```bash
python3 ems-solarflow-api-control.py
```

and configure the installation through:

```text
config.json
```

The `ems/` package contains internal implementation modules only. This keeps
the main script small and makes future changes easier to review, while
preserving the same operating model.

`runtime-state.json` is not a second static config. It is local mutable runtime
state created and updated by the EMS.

More: [docs/architecture.md](docs/architecture.md).

---

## Quick Start

Detailed first live-control setup: [docs/quickstart.md](docs/quickstart.md).

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Install Python, venv support, and pip first if needed. Debian / Ubuntu /
Raspberry Pi OS:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

openSUSE:

```bash
sudo zypper install python3 python3-pip python3-virtualenv
```

Create local config:

```bash
cp config.template.json config.json
```

Edit:

- Zendure device IPs
- Zendure serial numbers
- Shelly IP
- Home Assistant URL and token if used
- power limits
- SOC limits
- safety flags

Run preflight:

```bash
python3 -B ems-solarflow-api-control.py --preflight
```

Run read-only dry-run:

```bash
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
```

Start EMS only after reviewing logs:

```bash
python3 -B ems-solarflow-api-control.py
```

Configuration details: [docs/configuration.md](docs/configuration.md).

---

## Configuration

Start with `config.template.json`.

Short comments inside the template explain the main sections. Detailed
explanations and copy/paste examples are in:

- [docs/configuration.md](docs/configuration.md)
- [docs/configuration-examples.md](docs/configuration-examples.md)

---

## Runtime State And CLI

Static installation data belongs in `config.json`.

Mutable operator state belongs in `runtime-state.json`:

```text
enabled
max_total_power
loop_interval
min_output_limit
per-device enabled
per-device max_power
per-device offgrid_socket_mode
```

Safe runtime-state edits:

```bash
python3 emsctl.py status
python3 emsctl.py system min-output-limit 30
python3 emsctl.py device WR1 offgrid eco
python3 emsctl.py winter enable
python3 emsctl.py ha disable
```

More:

- [docs/runtime-state.md](docs/runtime-state.md)
- [docs/cli.md](docs/cli.md)

---

## Home Assistant

Home Assistant can be used for:

- monitoring
- optional runtime-state helper controls
- dashboard visualization

Dashboard example:

```text
homeassistant-dashboard/dashboard.yaml
```

More: [docs/home-assistant.md](docs/home-assistant.md).

---

## Winter Mode

Winter mode raises `minSoc` gradually through state reconciliation.

It does not alter normal output target calculation.

It can also apply a conservative winter AC `inputLimit` during the winter
adjustment context only.

More: [docs/winter-mode.md](docs/winter-mode.md).

---

## Validation

Compile:

```bash
python3 -m py_compile ems-solarflow-api-control.py ems/*.py emsctl.py scripts/check_log_events.py
```

Self-test:

```bash
python3 -B ems-solarflow-api-control.py --self-test
```

Simulation:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Replay:

```bash
python3 -B ems-solarflow-api-control.py --replay /path/to/trace.jsonl --once
```

Log event checks:

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Documentation

| Topic | Document |
|---|---|
| Configuration | [docs/configuration.md](docs/configuration.md) |
| Configuration examples | [docs/configuration-examples.md](docs/configuration-examples.md) |
| Power control flow | [docs/control-flow.md](docs/control-flow.md) |
| Runtime state | [docs/runtime-state.md](docs/runtime-state.md) |
| CLI tool | [docs/cli.md](docs/cli.md) |
| Home Assistant | [docs/home-assistant.md](docs/home-assistant.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Development | [docs/development.md](docs/development.md) |
| Control logic | [docs/control-logic.md](docs/control-logic.md) |
| Winter mode | [docs/winter-mode.md](docs/winter-mode.md) |
| Release notes | [docs/release.md](docs/release.md) |
| Safety model | [docs/safety.md](docs/safety.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |

---

## Project Files

| Path | Purpose |
|---|---|
| `ems-solarflow-api-control.py` | Main EMS entry script |
| `ems/` | Internal EMS implementation modules |
| `emsctl.py` | Safe runtime-state CLI |
| `config.template.json` | Versioned config template |
| `config.json` | Local config, ignored by Git |
| `runtime-state.json` | Mutable runtime state, ignored by Git |
| `homeassistant-dashboard/dashboard.yaml` | HA dashboard example |
| `scripts/check_log_events.py` | Structured log validator |
| `docs/` | Public documentation |

---

## License

See [LICENSE](LICENSE).
