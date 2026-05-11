# Safety Model

This software can write to real power hardware.

Start with simulation, replay, dry-run, and preflight checks. Do not enable live
writes until the logs match the behavior you expect.

## Write Gates

Runtime output writes require:

```text
dry_run=false
simulation_mode=false
not replay
allow_hardware_writes=true
```

State reconciliation writes additionally require:

```text
allow_state_reconciliation_writes=true
```

## Write Types

Runtime control may write:

```text
outputLimit
```

State reconciliation may write:

```text
minSoc
socSet
smartMode
gridOffMode for explicit offgrid socket intent
inputLimit only during winter recovery reconciliation
```

Offgrid socket intent uses the Zendure `gridOffMode` tri-state mapping:

```text
off      -> 2
eco      -> 1
standard -> 0
```

Startup may initialize `acMode=2` once when the device appears idle and no
firmware recovery/charge condition is active.

## Preflight

```bash
python3 -B ems-solarflow-api-control.py --preflight
```

Preflight reads live telemetry and checks prerequisites without dispatching
control writes.

## Bounded Runs

Use bounded runs for live tests:

```bash
python3 -B ems-solarflow-api-control.py --duration 60
python3 -B ems-solarflow-api-control.py --max-cycles 5
```

## Simulation And Replay

Simulation and replay never contact hardware:

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
python3 -B ems-solarflow-api-control.py --replay /path/to/trace.jsonl --once
```
