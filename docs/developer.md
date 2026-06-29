# Developer Notes

## Diagnose Contract

`emsctl.py diagnose --json` is the public diagnose contract for CLI users,
support bundles, and future dashboard/API integrations.

The current contract uses:

```json
{
  "schema_version": 1,
  "status": "ok|warning|error",
  "generated_at": "ISO-8601 timestamp",
  "diagnosis": {
    "version": 1,
    "timestamp": "ISO-8601 timestamp",
    "status": "ok|warning|error",
    "sections": [],
    "metrics": {},
    "root_causes": [],
    "warnings": [],
    "errors": []
  }
}
```

Existing detailed sections such as `checks`, `control`, and
`control_quality` remain available. New consumers should prefer the top-level
`schema_version`, `diagnosis`, `sections`, `metrics`, `root_causes`,
`warnings`, and `errors` fields for cross-mode handling.

`diagnose --hardware` additionally populates a top-level `hardware_health`
object (`{grid_meter, devices[]}`) with in-memory communication-health
snapshots per endpoint (status, success/failure/consecutive counters, latency
summary, staleness). It is additive and absent for other modes; consumers must
treat it as optional. The same snapshot shape is produced at runtime by
`EMSController.health_snapshot()` for future dashboard/InfluxDB export.

The diagnose API `schema_version` is the machine-readable JSON contract
version. It is independent from the README Diagnose Evolution labels, which
describe the feature rollout stages V1 through V6.

## Service Layer

The CLI entry point calls `run_diagnosis(args)`, which finalizes the raw
diagnostic data into the stable contract. Thin service functions are available
for future API and dashboard use:

- `run_install_diagnosis(args)`
- `run_deep_diagnosis(args)`
- `run_hardware_diagnosis(args)`
- `run_control_diagnosis(args)`
- `run_control_quality_diagnosis(args)`

These functions are read-only and reuse the same data path as the CLI. They do
not write Zendure devices, Home Assistant, MQTT, or runtime control state.

## Root Causes

All machine-readable root causes must use this shape:

```json
{
  "code": "minimum_soc_protection_active",
  "severity": "warning",
  "title": "Minimum SOC protection active",
  "message": "Minimum SOC protection active",
  "suggested_next_check": "Review the related diagnose section for details."
}
```

Allowed severities are `info`, `warning`, and `error`. Codes should be stable,
lowercase, and underscore-separated. If a human-readable legacy cause is
generated internally, finalization converts it into this object format before
JSON output or support bundle export.

## Support Bundle

`diagnose --support-bundle` writes a ZIP with this exact stable layout:

```text
diagnosis.json
diagnosis.txt
control-diagnostics.json
control-diagnostics.txt
control-quality.json
control-quality.txt
redacted-config.json
runtime-state.json
bundle-metadata.json
```

`bundle-metadata.json` includes:

```json
{
  "bundle_version": 1,
  "generated_at": "ISO-8601 timestamp",
  "ems_version": "0.6.0",
  "schema_version": 1
}
```

Secret redaction is applied to common token, password, dashboard auth, MQTT,
API, serial, and credential fields. The bundle intentionally excludes logs and
unstructured project metadata so external tooling can validate the expected
file list exactly.

## Contract Changes

Future incompatible JSON or bundle changes must increment the relevant version:

- `schema_version` for diagnose JSON changes
- `bundle_version` for support bundle layout changes

Add or update contract tests whenever a public field, root-cause shape, bundle
file name, or CLI diagnose variant changes.

## Local Dashboard Preview

`scripts/serve_dashboard_preview.py` starts a local preview server for the
dashboard with deterministic, synthetic, non-secret data. It serves the **real**
`dashboard/static/` assets (no parallel mock UI), so any dashboard change can be
inspected visually without real hardware, MQTT, Zendure/Shelly access, SQLite
history, passwords, or a running EMS loop.

```bash
python3 scripts/serve_dashboard_preview.py
python3 scripts/serve_dashboard_preview.py --scenario firmware-status
python3 scripts/serve_dashboard_preview.py --scenario write-mode
python3 scripts/serve_dashboard_preview.py --host 127.0.0.1 --port 8767
```

It binds to `127.0.0.1:8767` by default and prints the preview URLs. Start at the
landing page, which links every view for the current scenario:

```text
http://127.0.0.1:8767/preview
```

Each flow view also has a stable URL that opens the dashboard directly in that
view: `/preview/aggregated`, `/preview/devices`, `/preview/control`,
`/preview/energy`, `/preview/diagnose`, `/preview/logs`.

List the available scenarios or views without starting a server:

```bash
python3 scripts/serve_dashboard_preview.py --list-scenarios
python3 scripts/serve_dashboard_preview.py --list-views
```

Scenarios:

- `normal` — healthy two-device system (read-only).
- `firmware-status` — mixed `ac_mode` / `ac_status` / `soc_limit` / `pack_state`
  / `dc_status` / `grid_state` / `grid_off_mode` values across devices, including
  an unknown-value device, to visually test readable firmware-status labels.
- `offline-device` — one healthy device and one offline device with missing
  telemetry and warning states.
- `auth-readonly` — dashboard authentication configured but not logged in
  (login button visible, write controls locked).
- `write-mode` — authenticated operator preview; the runtime write UI renders.
  Write requests are preview-only and never touch disk, config, runtime state,
  devices, or the network. The Diagnose and Logs tabs only show content in this
  scenario because they are auth-gated.

The shared synthetic payloads live in `scripts/dashboard_preview_data.py` and are
reused by the screenshot helper so previews and screenshots stay in sync. Capture
mode defaults to the authenticated `write-mode` scenario (so the operator-only
Diagnose and Logs tabs render) unless you pass `--scenario` explicitly, and
`--views all` expands to every flow view:

```bash
# Screenshot helper (needs Firefox headless + ImageMagick `convert`):
python3 scripts/serve_dashboard_preview.py --capture                       # diagnose + logs, write-mode
python3 scripts/serve_dashboard_preview.py --capture --views all           # every view
python3 scripts/serve_dashboard_preview.py --capture --scenario firmware-status --views devices control
python3 scripts/capture_dashboard_previews.py                              # legacy helper (diagnose + logs JPGs)
python3 scripts/capture_dashboard_previews.py --serve-only
```
