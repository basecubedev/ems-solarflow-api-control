# Testing

This project writes to real power hardware, so tests are the main guard against
regressions in control logic, write gates and safety reconciliation. All the
checks below run offline — no Home Assistant, Shelly, Zendure devices, InfluxDB,
secrets, or network access are required.

See also [developer-setup.md](developer-setup.md) for the source checkout and
environment, and [ci-release.md](ci-release.md) for how these run in CI.

## Development approach

This project develops complex and risky features **contract-first** with
test-driven development: for control logic, write gates, safety reconciliation,
config validation and the Admin apply/credential paths, a failing contract test
is written and shown to fail before the production change is made. Bugs get a
reproducing test before the fix. Small UI tweaks and exploratory prototypes may
stay pragmatic and gain tests as they settle. This is a working policy for the
areas that can write to real hardware, not a claim that every historical line of
the project was produced under strict test-driven development. The canonical
project requirement and scope are defined in [agent-rules.md](agent-rules.md).

## Quick local loop

Compile check (run after any change to the entry script, `ems/`, `emsctl.py` or
`scripts/check_log_events.py`):

```bash
python3 -m py_compile ems-solarflow-api-control.py ems/*.py emsctl.py scripts/check_log_events.py
```

Self-test:

```bash
python3 -B ems-solarflow-api-control.py --self-test
```

Simulation (no hardware or network):

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Replay a captured trace:

```bash
python3 -B ems-solarflow-api-control.py --replay /path/to/trace.jsonl --once
```

## Pytest

Full suite:

```bash
pytest
```

Single file or single test:

```bash
pytest tests/test_pv_first_charge_balance.py
pytest tests/test_pv_first_charge_balance.py::test_some_case -v
```

### Required regression check

The offline power-control regression tests are the deterministic CI check. They
must stay green:

```bash
pytest tests/ -m "simulation and power_control"
```

Markers are defined in `pytest.ini`: `simulation`, `power_control`,
`regression`.

## Log validation

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

## Documentation and contract tests

Several tests protect docs and public contracts rather than runtime behavior:

- `tests/test_docs_user_structure.py` — the user / technical / developer
  documentation split and README routing.
- `tests/test_agent_rules_contract.py` — the canonical rule set and supported
  agent entry-point links.
- `tests/test_docker_docs_contract.py`, `tests/test_docker_first_setup.py` —
  the Docker Bootstrap installer/compose/docs promise.
- `tests/test_issue_templates.py` — issue-template documentation links.

When you move or rename docs, update these tests (or the redirect stubs) so the
links stay honest.

## What tests do not cover

Automated tests reduce risk but do not replace real hardware validation: dry-run
checks, watching the first live run, and per-installation review of power and
SOC limits are still required. See [../user/safety.md](../user/safety.md).

For an on-hardware measurement (not part of the offline suite) of how fast an
MQTT `outputLimit` write reaches the inverter, see
[mqtt-write-latency-probe.md](mqtt-write-latency-probe.md).
