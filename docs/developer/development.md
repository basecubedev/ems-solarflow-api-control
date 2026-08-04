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

Third-party license inventory:

```bash
python tools/check_third_party_licenses.py
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

## Third-Party Licenses

[`THIRD_PARTY_LICENSES.md`](../../THIRD_PARTY_LICENSES.md) is the authoritative
human-readable inventory of every third-party component: runtime dependencies,
development dependencies, vendored assets, container base images, optional
platform packages and generated assets. It records version, SPDX identifier,
purpose, upstream, and whether a component runs at runtime and whether this
project redistributes it.

Update the inventory in the same commit as the dependency change:

1. Add a row to the section that matches how the component reaches users
   (runtime, development, vendored, base image, optional platform).
2. Fill every required column: `Component`, `Version`, `License (SPDX)`,
   `Used for`, `Runtime`, `Distributed`, `Upstream`. Use `✅` / `❌` for the two
   flag columns.
3. Take the license from upstream package metadata, the upstream `LICENSE` file
   or the upstream repository — never guess. If it cannot be determined, say so
   in the row and in `License Notes` instead of inventing an identifier.
4. For a vendored asset, record its origin, upstream version and SHA-256, and
   add the upstream license text next to the files when the license requires the
   notice to travel with the code (as `dashboard/static/uPlot.LICENSE` does).
5. Runtime dependencies also need a `Runtime`/`Distributed` review of the
   transitive closure: everything pip installs into the images belongs in the
   transitive table.

Verify:

```bash
python tools/check_third_party_licenses.py
pytest -q -m documentation
```

The checker fails when a direct dependency from `requirements.txt`,
`requirements-dev.txt`, `deploy/admin/requirements.txt` or `package.json` has no
row, when an optional platform package in `package-lock.json` is undocumented,
when a static asset without the project license header is not listed as
vendored, when a documented entry no longer exists in any manifest, when a
component appears twice in one section, or when a table is missing a required
column. It runs in the CI `Static checks` job and through
`tests/test_third_party_licenses.py`. Transitive resolution is deliberately out
of scope — that table is maintained by hand.

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
