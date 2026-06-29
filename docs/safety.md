# Safety Model

This software can write to real power hardware.

The template profile is intended for normal standalone live control after real
local values are configured and installation limits are reviewed. If required
placeholders are still present, EMS forces safe mode: control disabled,
dry-run enabled, and hardware writes blocked. Set `dry_run=true` manually when
you want a no-write validation run.

## Operator Responsibility

The EMS can write to real power hardware. The documented defaults are intended
for normal standalone operation after local configuration, but every
installation must be reviewed by the operator.

Before unattended operation, verify device serial numbers, IP addresses, Shelly
readings, maximum power limits, minimum and maximum SOC limits, battery sizes,
PV factors, and any local grid or electrical requirements.

Zendure Local API must be available and enabled for local EMS control. Do not
run Zendure HEMS, Home Assistant automations, MQTT writers, or any other
controller in parallel if they write Zendure `outputLimit`. EMS assumes
exclusive write control over `outputLimit` while active. Monitor the first live
run and every run after relevant configuration changes.

Use [first-run-checklist.md](first-run-checklist.md) before unattended
operation.

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
socSet=1000 / configured socSet restore during battery full-charge assist
acMode/inputLimit during battery full-charge assist only through runtime intent
```

Offgrid socket intent uses the Zendure `gridOffMode` tri-state mapping:

```text
off      -> 2
eco      -> 1
standard -> 0
```

Runtime AC mode intent is evaluated during the control loop. Normal output
devices target `acMode=2`; runtime AC input/charge reservations target
`acMode=1` and are excluded from normal output regulation.

The controller writes `acMode` only when telemetry differs from the desired
runtime target. Automatic startup reconciliation remains conservative: unknown
`acMode` values are not forced back to normal output automatically, and
firmware recovery/charge blockers are still honored before startup returns a
device to `acMode=2`. An explicit runtime output command, such as
`emsctl device WR1 ac-mode output`, may still write `acMode=2` from reported
`acMode=0` or from `acMode=1` with active AC charge telemetry.

## Preflight

Docker first checks:

```bash
docker compose exec ems python3 emsctl.py diagnose
docker compose exec ems python3 emsctl.py diagnose --hardware
```

Native Python preflight:

```bash
python3 -B ems-solarflow-api-control.py --preflight
```

Preflight reads live telemetry and checks prerequisites without dispatching
control writes.

## Bounded Runs

Use bounded runs for native Python live tests:

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
