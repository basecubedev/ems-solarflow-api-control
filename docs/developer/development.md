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

Default startup policy for the release template:

- Home Assistant disabled by default.
- Normal Zendure `outputLimit` writes enabled after local configuration.
- State reconciliation writes enabled for the full regulation profile.
- `dry_run=true` remains available as a manual no-write validation mode.

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

Offline power-control regression tests:

```bash
pytest tests/ -m "simulation and power_control"
```

These tests are deterministic simulated checks for pull requests. They do not
require Home Assistant, Shelly, Zendure devices, InfluxDB, secrets, or network
access, and they do not replace longer runtime tests, InfluxDB analysis, or
real hardware validation.

The GitHub Actions job `Simulated power-control regression tests` can be used
as a required status check for `main` in branch protection or repository
rulesets.

## GitNexus Indexing

GitNexus skips files larger than 512 KB by default. This repository sets the
supported `maxFileSize` analyze option to 2048 KB in `.gitnexusrc`, which is the
single source of truth for the project threshold. The limit includes
`admin/static/admin.js` with growth headroom without admitting arbitrarily large
generated or binary files.

Use the project launcher for manual and automated analysis:

```bash
gitnexus-project analyze --force
gitnexus-project status
scripts/check_gitnexus_index.sh
```

`gitnexus-project` can be a symlink to `scripts/gitnexus-project` in a directory
on `PATH`. MCP configurations should invoke the same launcher with the `mcp`
argument, so MCP and analyzer subprocesses inherit the threshold from
`.gitnexusrc`. The launcher preserves all arguments and exit statuses.

Analyze commands hold an exclusive project writer `flock`. MCP and read-only CLI
commands do not take that lock because a long-lived MCP process would otherwise
block every later analysis. Analyzers separately refuse to start while an MCP or
CLI reader has the LadybugDB file open; set
`GITNEXUS_DB_WAIT_TIMEOUT` to a bounded number of seconds when a caller should
wait for GitNexus's read-only connection pool to become idle. Exit status 75
means another analyzer owns the writer lock or a reader still has LadybugDB
open. Read-only queries never take the analyzer lock. Stop or restart only the
MCP server that still holds LadybugDB before a planned rebuild when its
connection does not become idle within the bounded wait.
The presence of `.gitnexus/analyze-writer.lock` does not indicate ownership;
`flock` ownership exists only while a live process holds the file descriptor.
Never delete the file to clear a suspected lock.

GitNexus 1.6.9 can leave this index's LadybugDB/FTS state inconsistent after an
incremental update even when every changed source file is valid UTF-8. Analyze
paths therefore use the supported `--force` option until that upstream failure
is resolved. The post-commit hook also uses a zero-second analyzer-lock timeout
and a short, bounded database wait. A skipped or failed run remains visible in
`.gitnexus/post-commit-analyze.log`; failures are never treated as a successful
refresh. Do not run a direct `gitnexus analyze` concurrently with the project
launcher.

## Third-Party Assets

When adding new dashboard icon, font, image, chart, UI asset, or frontend
package dependencies, update `THIRD_PARTY_LICENSES.md` and preserve the
upstream copyright and license notice.

Log event checks:

```bash
python3 scripts/check_log_events.py /tmp/ems-sim.log \
  --require startup \
  --require target_calculation
```

## Release / Review Archives

Build source archives with `git archive` so only tracked files are included.
This excludes local runtime data and secrets (`.venv/`, `__pycache__/`,
`data/*.sqlite`, `data/influxdb/` (bundled InfluxDB database state),
`deploy/docker/influxdb.env`, `develop/influxdb/.env`, `develop/influxdb/data/`,
…), which are all gitignored:

```bash
git archive --format=tar.gz -o ../ems-solarflow-api-control-clean.tar.gz HEAD
```

Do not hand-roll archives with `tar`/`zip` from the working tree — those pull in
ignored runtime/build artifacts and may leak local InfluxDB tokens.
