# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Before planning, editing or committing, Claude Code **must** read and follow
[`docs/developer/agent-rules.md`](docs/developer/agent-rules.md). It is the
canonical project-wide rule set. The Claude-specific commands, architecture
orientation and tool instructions below supplement it and never replace it.

## What This Is

Local-first EMS (Energy Management System) controller for Zendure SolarFlow
battery/inverter systems. No YAML automation stack, no cloud dependency for
control decisions. It reads grid-meter load (Shelly / everHome EcoTracker /
Tasmota) and Zendure device telemetry, calculates a power target, and writes
`outputLimit` (and related state) back to Zendure devices via local HTTP API.

This software writes to real power hardware. Be conservative with changes to
control logic, write gates, and safety reconciliation — see "Safety Model"
below.

## Commands

Install deps:

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Compile check (run after any change to entry script / `ems/` / `emsctl.py` / `scripts/check_log_events.py`):

```bash
python3 -m py_compile ems-solarflow-api-control.py ems/*.py emsctl.py scripts/check_log_events.py
```

Self-test:

```bash
python3 -B ems-solarflow-api-control.py --self-test
```

Simulation (no hardware/network required):

```bash
python3 -B ems-solarflow-api-control.py --simulate --max-cycles 1
```

Replay a captured trace:

```bash
python3 -B ems-solarflow-api-control.py --replay /path/to/trace.jsonl --once
```

### Tests

Full suite:

```bash
pytest
```

Single file / single test:

```bash
pytest tests/test_pv_first_charge_balance.py
pytest tests/test_pv_first_charge_balance.py::test_some_case -v
```

Offline power-control regression tests (the required CI check, deterministic, no hardware/network):

```bash
pytest tests/ -m "simulation and power_control"
```

Targeted tiers — do not run the full suite for a localized change
(see [`docs/developer/testing.md`](docs/developer/testing.md)):

```bash
./scripts/test-fast.sh                # unit + contract, no Docker/browser/slow
./scripts/test-admin.sh authority     # one Admin functional area
./scripts/test-mqtt.sh
./scripts/test-pr.sh core             # one pull-request group
./scripts/test-pr.sh appliance        # the appliance group
./scripts/test-rc.sh --list           # the release-candidate gates
```

Markers (`pytest.ini`, `--strict-markers` is on) have two independent
dimensions. Execution level, exactly one per module: `unit`, `contract`,
`integration`, `e2e`; plus `docker`, `browser`, `slow`. Functional areas, any
number: `admin`, `setup`, `maintenance`, `workflow`, `authority`, `config`,
`mqtt`, `power_control`, `backup_restore`, `system_build`, `appliance`. `simulation`,
`regression` and `mqtt_release` remain for the existing gates. Classify a new
module with a module-level `pytestmark`; `tests/test_test_classification.py`
enforces the rules.

### Log validation

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

### emsctl (runtime-state CLI, safe to use during development)

```bash
python3 emsctl.py status
python3 emsctl.py diagnose
python3 emsctl.py diagnose --json
python3 emsctl.py diagnose --control
python3 emsctl.py diagnose --control-quality --sample-seconds 60
```

## Architecture

Operating model is intentionally minimal: **one start script, one static config.**

```bash
python3 ems-solarflow-api-control.py
```

- `config.json` — static installation config (versioned template: `config.template.json`).
- `data/runtime-state.json` — mutable runtime/operator state, created and
  updated by the EMS and by `emsctl.py`. The Admin console also mirrors the
  whitelisted overlapping keys it changed into it on maintenance apply (config →
  runtime convergence, one-directional, via the same `dashboard/runtime_write.py`
  whitelist). Not a second static config.

The entry script (`ems-solarflow-api-control.py`) only does bootstrap/coordination:
CLI parsing, config loading, logging setup, client construction, runtime-state
construction, controller startup, main loop. All real implementation lives in `ems/`.

### Module layout (`ems/`)

- `config.py` — config loading, safe parsing, runtime mode helpers
- `logging_utils.py` — structured `event=...` logging setup
- `models.py` — telemetry/capability dataclasses
- `clients.py` — HTTP, Zendure, Shelly, Home Assistant clients
- `runtime_state.py` — mutable runtime-state read/write
- `runtime_intents.py` — runtime AC mode intent (`ac_output`/`ac_input`) reconciliation
- `target_control.py` — capability detection and target/output calculation (core control math)
- `controller.py` — main EMS control loop, ties everything together (largest module)
- `state_store.py` — SQLite store backing battery full-charge assist (and dashboard stats)
- `simulation.py` — simulation, replay, preflight, self-test helpers
- `diagnostics.py` — read-only `diagnose` service layer (versioned contract); imported by both `emsctl.py` and the dashboard
- `paths.py` — shared project-path resolvers (`BASE_DIR`, `resolve_*_path`); import-side-effect-free

Edit the smallest relevant module rather than the entry script or `controller.py`
when the change is localized (e.g. target math → `target_control.py`, runtime
state shape → `runtime_state.py`).

### Raspberry Pi Appliance (`appliance/`)

A second product in this repository, and the largest new subsystem: a Debian
package plus systemd units that manage a Raspberry Pi host running the EMS in
Docker. It is not part of the EMS control loop and must not import from `ems/`.

- Two processes, one privilege boundary: an unprivileged web service
  (`web.py`, `static/app.js`) talks over a unix socket to a root agent
  (`agent.py`, `commands.py`) that executes an allowlisted set of operations.
  The allowlist is enforced on the agent side (`protocol.py`,
  `operation_schema.py`); reaching that socket as an allowed uid is an
  appliance-takeover capability.
- Fail-safe A/B OS updates own the `ab_*.py` modules plus `os_update.py`,
  `os_fetch.py` and `os_releases.py`. This is bricking-class code: a trial slot
  commits itself only after its own health gates pass, and nothing else commits
  it. Read `docs/appliance/ab-os-updates.md` and
  `docs/appliance/ab-persistence-contract.md` before touching it.
- Release trust (`release_trust.py`, `release_attestation.py`,
  `os_releases.py`) is fail-closed by design. Do not weaken a verification path
  to make a gate pass.
- The Appliance Manager updates itself as a signed `.deb`
  (`manager_update.py`, `manager_releases.py`, `manager_retention.py`,
  `manager_install.py`, `manager_verify.py`), never on a timer and always on an
  operator's button, with an older package installable as readily as a newer
  one. Three properties are not negotiable: `dpkg` runs from its own systemd
  unit rather than the agent's cgroup, every refusal happens before it runs,
  and the reverter is a copy taken out of the *outgoing* package. **Doing
  nothing commits an install here** — under A/B it reverted — so the deadline
  in `manager_verify.py` is the only thing standing in for that, and it is
  software rather than firmware. Read
  `docs/appliance/adr/manager-self-update.md` before touching any of it.
- Two image shapes, and not every board has both: `image_variants.py` and
  `rpi_image_gen.HARDWARE_PROFILES` are the one table. A Raspberry Pi 3 boots
  the single-slot image and cannot boot the A/B one.
- Not confirmed on physical hardware — no image of either shape has booted on a
  board, and no appliance has installed a manager package over HTTPS.
  `docs/appliance/ab-hardware-validation.md` is the authority on what has and
  has not been proven; never upgrade a claim there without the evidence it
  names.

Compile check and tests:

```bash
python3 -m py_compile appliance/*.py && node --check appliance/static/app.js
pytest tests/ -k appliance -m "not docker and not browser and not slow"
npx playwright test --config=playwright.appliance.config.ts
```

`emsctl.py` is a separate large CLI for safe runtime-state edits and diagnostics
(`status`, `device ...`, `system ...`, `winter`, `ha`, `diagnose ...`,
`dashboard set-password`). The `diagnose` service layer
(`run_install_diagnosis`, `run_deep_diagnosis`, `run_hardware_diagnosis`,
`run_control_diagnosis`, `run_control_quality_diagnosis` — all read-only, reuse
the same data path as the CLI) lives in `ems/diagnostics.py`; `emsctl.py` keeps
the thin CLI wrappers (`handle_diagnose_command`, arg parsing) and re-exports the
service functions. `ems/diagnostics.py` is import-side-effect-free and must never
import `emsctl` (so the dashboard can import it directly).

### Control pipeline (per loop, see `docs/technical/control-logic.md`)

1. Reload `runtime-state.json` if changed
2. Optionally sync HA helper values
3. Read Shelly/EcoTracker/Tasmota house load
4. Read Zendure telemetry
5. Detect runtime capabilities
6. Run state reconciliation when due
7. Detect strict night/minSoc idle
8. Stabilize total target (`commanded_total_w` + filtered load, deadbands, ramps, filtering)
9. Allocate target across devices (PV-first, pv_priority_factor)
10. Apply device ramp/limits, `min_output_limit`, deadband
11. Write `outputLimit` only behind safety gates

### Runtime AC mode intent

Each device gets a runtime AC role: `ac_output` (`acMode=2`, normal output
regulation) or `ac_input` (`acMode=1`, excluded from output regulation, may
carry `ac_charge_power_w` reconciled as `inputLimit`). `acMode`/`inputLimit`
writes for this are owned exclusively by the runtime intent reconciler in
`runtime_intents.py` — don't add a second writer. Legacy role names
(`normal_output`, `ac_input_charge`, `reserved`) are accepted defensively and
mapped to `ac_output`/`ac_input`.

### Battery full-charge assist

Optional controller lifecycle feature using `ems/state_store.py` (own SQLite
DB, configured via `battery_full_charge_assist.state_database_path`,
independent of the dashboard DB). Tracks `socLimit == 1` Max-SoC events;
completion requires firmware-reported `socLimit == 1` exactly (SOC % and
configured `max_soc` are not completion thresholds). Assist/restore reuse the
normal safe write helpers and the runtime AC intent reconciler.

## Safety Model (see `docs/user/safety.md`)

Runtime `outputLimit` writes share the precondition `dry_run=false`,
`simulation_mode=false`, not replay, then require the named gate for the device's
transport: API/local-HTTP → `allow_hardware_writes=true`; local MQTT broker →
`allow_mqtt_local_control_writes=true`; Zendure cloud MQTT →
`allow_mqtt_zendure_control_writes=true`. All three gates default on
(`RELEASE_WRITE_GATE_DEFAULTS`): the template, config upgrade and normal
runtime loading resolve missing gate keys to the same release defaults, while
the simulation/replay safe config and template placeholder safety force every
gate off. Whether a transport actually writes is decided by configuration
presence: per-device `write_output_limit` opt-in, broker host, API key. Some
hardware generations are not yet validated on physical hardware (see
`docs/user/supported-setups.md`). Writes dispatch through `dev.write_output_limit()` and
`cfg.control_writes_allowed(dev.control_gate)`; MQTT control lives in
`ems/zendure_mqtt/` (`control.py`, `device_client.py`, `control_runtime.py`,
`write_topics.py`).

State reconciliation writes (`minSoc`, `socSet`, `smartMode`, `gridOffMode`,
winter `inputLimit`, full-charge-assist `socSet`/`acMode`/`inputLimit`)
additionally require `allow_state_reconciliation_writes=true` and are API-only
(MQTT control devices are output-only, `supports_state_reconciliation=False`).

The EMS must not run in parallel with another controller writing Zendure
`outputLimit`.

## Diagnose Contract (see `docs/developer/developer.md`)

`emsctl.py diagnose --json` is a versioned public contract
(`schema_version`, `diagnosis.{status,sections,metrics,root_causes,warnings,errors}`).
Root causes use a stable shape: `{code, severity, title, message, suggested_next_check}`
with `severity` in `info|warning|error` and lowercase underscore-separated `code`.
Support bundle (`diagnose --support-bundle`) has a fixed file list
(`diagnosis.json`, `control-diagnostics.json`, `control-quality.json`,
`redacted-config.json`, `runtime-state.json`, `bundle-metadata.json`, plus
`.txt` variants). Bump `schema_version`/`bundle_version` and add/update
contract tests for any incompatible change to these.

## Dashboard UI Work

When touching dashboard UI, read `docs/developer/dashboard-style-guide.md` first.
State whether the change uses:

- Aggregate / Device style
- Control / Energy stage style

Do not introduce a new dashboard visual system.

## Documentation Index

Docs are split by audience under `docs/` (see `docs/README.md` for the full
map): user docs in `docs/user/`, technical reference in `docs/technical/`,
developer docs in `docs/developer/`. Notably `technical/architecture.md`,
`technical/control-logic.md`, `technical/control-flow.md`,
`technical/runtime-state.md`, `technical/configuration.md`, `user/safety.md`,
`winter-mode.md`, `dashboard.md`, `cli.md`, `developer/development.md`,
`developer/developer.md`. Admin docs: `user/admin-console.md` (overview),
`user/admin-setup.md` (new-system flow), `user/admin-maintenance.md`
(manage-existing-system flow), `user/admin-backup-restore.md` (Admin
backup/restore workflow), `technical/admin-architecture.md` (architecture
rules), and `technical/admin-discovery.md` (full Admin reference).
Update the relevant doc when changing behavior described there.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ems-solarflow-api-control** (35470 symbols, 83822 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ems-solarflow-api-control/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ems-solarflow-api-control/clusters` | All functional areas |
| `gitnexus://repo/ems-solarflow-api-control/processes` | All execution flows |
| `gitnexus://repo/ems-solarflow-api-control/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

# Serena — Language-Server Intelligence

Serena runs a Python language server over this repo. It is registered globally in
`~/.claude.json` as `serena start-mcp-server --context=claude-code-pragmatic
--project-from-cwd`, so it activates itself from the working directory — no
`activate_project` call per session, and no project pinned across repos. Its state
lives in `.serena/`, git-excluded alongside `.gitnexus/` via `.git/info/exclude`.

Serena and GitNexus overlap on symbol lookup but are not interchangeable:

- **GitNexus answers "what does this touch"** — call graph, execution flows, blast
  radius, risk. It reads a snapshot written by the last `analyze` run, so it lags
  behind edits made in the current session, silently.
- **Serena answers "what is this right now"** — the language server always reflects
  the working tree, including edits made a minute ago.

## Which tool for what

| Question | Tool |
|---|---|
| Blast radius before an edit, risk level | GitNexus `impact` — mandatory, see above |
| Execution flows, call chains, "how does X work" | GitNexus `query` / `context` |
| Scope check before committing | GitNexus `detect_changes` |
| Taint / source→sink findings | GitNexus `explain` |
| Current body or signature of a symbol | Serena `find_symbol` with `include_body` |
| Structure of a file not yet read | Serena `get_symbols_overview` |
| Callers/usages, resolved through the type system | Serena `find_referencing_symbols` |
| Replace a whole function, method or class | Serena `replace_symbol_body` |
| Type/syntax errors after an edit | Serena `get_diagnostics_for_file` |
| Config, docs, JSON/YAML, a few lines at a known path | built-in Read / Grep / Edit |

## Rules that follow from the split

- The GitNexus `impact`-before-edit mandate stands unchanged. Serena does not
  replace it — a language server has no notion of blast radius.
- After editing a symbol in this session, trust Serena over GitNexus for that
  symbol's contents until the index has been re-analyzed. When the two disagree
  about what the code says, the language server is newer.
- Do not read a whole module to find one function. `get_symbols_overview`, then
  `find_symbol`. `ems/controller.py` and `emsctl.py` are large enough that this
  is the difference between a page and a wall of context.
- Never rename by find-and-replace. Serena `rename_symbol` is language-server-exact;
  GitNexus `rename` is call-graph-aware. Run `impact` first either way.
- Both toolsets are read-mostly and safe to use freely. Neither is a substitute for
  the safety rules in "Safety Model" — no tool output authorizes a write-gate change.
