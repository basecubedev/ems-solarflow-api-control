# Development

## Module Layout

The code is split by responsibility:

- `ems/config.py`: config loading, safe parsing and runtime mode helpers
- `ems/logging_utils.py`: structured event logging and logging setup
- `ems/models.py`: telemetry and capability dataclasses
- `ems/clients.py`: HTTP, Zendure, Shelly and Home Assistant clients
- `ems/runtime_state.py`: mutable runtime-state handling
- `ems/target_control.py`: capability detection and target calculation
- `ems/controller.py`: EMS control loop
- `ems/simulation.py`: simulation, replay, preflight and self-test helpers

Development should edit the smallest relevant module instead of the entry
script whenever possible.

The user-facing model remains unchanged:

```bash
python3 ems-solarflow-api-control.py
```

`config.json` remains the central static config. `runtime-state.json` remains
mutable runtime state.

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

Log event checks:

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```
