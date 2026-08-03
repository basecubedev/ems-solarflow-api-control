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

## Test selection

The suite is large — over 8000 Python tests and 47 Playwright specs — and the
full non-Docker run takes about 16 minutes on a developer machine against about
two for the fast tier. Run the tier that matches your change; the full suite is
a release gate, not a per-edit loop.

### Two marker dimensions

Every test module carries exactly one **execution level** and any number of
**functional areas**. The dimensions are independent and overlap on purpose.

| Level | Meaning |
|---|---|
| `unit` | isolated deterministic unit tests |
| `contract` | API, serialization, schema and frontend source contracts |
| `integration` | multiple production components without a real browser journey |
| `e2e` | complete user or service journeys |

| Execution property | Meaning |
|---|---|
| `docker` | requires a Docker daemon or Docker-first environment |
| `browser` | requires Playwright or a browser runtime |
| `slow` | exceeds the normal pull-request runtime budget |

| Functional area | Meaning |
|---|---|
| `admin` | Admin Server and Admin UI behavior |
| `setup` | Guided Setup lifecycle and installation flow |
| `maintenance` | Maintenance workflows |
| `workflow` | workflow ownership and lifecycle |
| `authority` | Device Plan, Preview, Apply and mutation authority |
| `config` | configuration generation, validation and mutation |
| `mqtt` | local MQTT, Zendure MQTT and broker workflows |
| `power_control` | power allocation and output safety |
| `backup_restore` | backup, restore and recovery workflows |
| `system_build` | System Build and deployment transitions |

`simulation`, `regression` and `mqtt_release` stay registered for the existing
gates. Every marker lives in `pytest.ini` and `--strict-markers` is enabled, so
a typo fails the run instead of silently selecting nothing.

### Fast developer tier

```bash
./scripts/test-fast.sh
# pytest -q -m "(unit or contract) and not docker and not browser and not slow" --maxfail=1
```

### Targeted functional tiers

```bash
./scripts/test-admin.sh authority
./scripts/test-admin.sh setup
./scripts/test-admin.sh maintenance
./scripts/test-admin.sh workflow
./scripts/test-mqtt.sh
./scripts/test-mqtt.sh integration
```

The same selections without the wrappers:

```bash
pytest -q -m "admin and authority and not slow"
pytest -q -m "admin and setup and not slow"
pytest -q -m "mqtt and not docker and not slow"
pytest -q -m "power_control and not slow"
pytest -q -m "system_build and not docker and not slow"
pytest -q -m "backup_restore"
pytest -q -m "docker"
```

### Pull-request groups

Each group runs independently and mirrors one CI job:

```bash
./scripts/test-pr.sh core
./scripts/test-pr.sh admin
./scripts/test-pr.sh mqtt
./scripts/test-pr.sh power-control
./scripts/test-pr.sh docker
./scripts/test-pr.sh chromium-critical
./scripts/test-pr.sh firefox-smoke
```

`core` is deliberately the *complement* of the functional groups
(`not docker and not admin and not mqtt and not power_control`), so a module
that carries no functional marker still runs in exactly one group. The union of
the five Python groups equals the full collection, and
`tests/test_test_classification.py` enforces that property.

### Nightly

`.github/workflows/nightly-full-suite.yml` runs the full non-Docker suite on
both supported Python versions, the strict deprecation check, the complete
Chromium and Firefox Admin suites, the Docker-first tier, the System Build tier
and the Admin upgrade/recovery journey.

### Release candidate

```bash
./scripts/test-rc.sh          # every gate, in order
./scripts/test-rc.sh --list   # print the gate list
```

Gates: static checks, the full non-Docker Python suite, the
`simulation and power_control` gate, the authority regressions, the security
regressions, the System Build tier, the Docker-first tier, the full Chromium
and Firefox Admin suites, the Admin replacement/recovery suite, the generated
config template and a clean-working-tree check. The RC tier never deselects a
known failure.

### Playwright groups

The specs carry Playwright tags, so one configuration serves every group:

```bash
npx playwright test --project=chromium --grep @smoke
npx playwright test --project=chromium --grep @authority
npx playwright test --project=firefox --grep @smoke
npx playwright test --project=chromium --grep "@setup|@authority"
npx playwright test --project=chromium               # full Admin suite
```

Tags: `@smoke` (fast critical journeys), `@setup`, `@maintenance`,
`@authority`, `@workflow`, `@system-build`.

### Prerequisites

- Python tiers need only the project virtualenv.
- `docker` tiers need a reachable Docker daemon (`docker info`). Without one the
  Docker suites skip with a precise reason instead of failing.
- Playwright tiers need `npm ci` and
  `npx playwright install chromium firefox`. No tier installs dependencies.

## Classifying a new test

Declare the markers at module level — that declaration is the authority, not
the file name:

```python
pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.integration,
]
```

Rules:

- Exactly one level marker per module (`unit`, `contract`, `integration`,
  `e2e`). `tests/test_test_classification.py` fails when a module declares none
  or several.
- Add every functional area that applies; overlap is intended.
- Use per-test markers only when one module genuinely mixes categories.
- Use `slow` when a test exceeds the pull-request budget, so the fast tier and
  the PR groups stay usable.
- The `mqtt_release` allowlist in `tests/conftest.py` is the one remaining
  name-based bridge and is guarded by a contract test. Classify anything new
  with markers instead.

## Required regression check

The offline power-control regression tests are the deterministic CI gate. They
must stay green:

```bash
pytest tests/ -m "simulation and power_control"
```

## Debugging selection

```bash
pytest --markers                                    # every registered marker
pytest --collect-only -q -m "admin and authority"
pytest --collect-only -q tests/test_write_gates.py
pytest -k "stale_device_plan"                       # by test-name substring
pytest tests/test_write_gates.py::test_gate_blocks_write
```

An empty selection exits with code 5. The tier scripts turn that into an
explicit error instead of a silent pass.

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
- `tests/test_test_classification.py` — the marker registry, the documented
  tier selections and the pull-request group partition.
- `tests/test_ci_workflow_docker_split.py` — how the CI groups are split.

When you move or rename docs, update these tests (or the redirect stubs) so the
links stay honest.

## What tests do not cover

Automated tests reduce risk but do not replace real hardware validation: dry-run
checks, watching the first live run, and per-installation review of power and
SOC limits are still required. See [../user/safety.md](../user/safety.md).

For an on-hardware measurement (not part of the offline suite) of how fast an
MQTT `outputLimit` write reaches the inverter, see
[mqtt-write-latency-probe.md](mqtt-write-latency-probe.md).
