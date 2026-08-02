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
  "ems_version": null,
  "build_label": "v0.6.3-12-gabcdef-dirty",
  "schema_version": 1
}
```

`ems_version` is a real release tag (e.g. `"v0.6.3"`) only for official release
builds; it is `null` for `latest`/local builds. `build_label` is a best-effort
build/revision label (from `EMS_GIT_DESCRIBE` / `git describe` / commit / build
id) or `null`. Both are derived at runtime by `ems.build_info.collect_build_info`
from CI build env vars or the local Git checkout — never hardcoded.

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

## Authority map and test layering

Every fact below has exactly one owner. Other layers adapt inputs, project the
result or enforce it — they never recompute it. The structural contracts in
`tests/test_admin_authority_ownership.py` fail when a second owner appears.

| Concern | Owner | Adapters / projections |
|---|---|---|
| Output-control eligibility | `ems/mqtt_control/power_capability.py` (model, write route, broker source), composed with route completeness by `ems/zendure_mqtt/capability.py` | `admin/zendure_mqtt_config_draft.py` (enforce/project), runtime `device_client`, migration, diagnostics, `admin.js` (labels only) |
| Physical inverter identity | `ems/device_identity.py` (`resolve_physical_identity`, `compare_physical_identity`, `is_masked_identity_value`, `EVIDENCE_PRECEDENCE`) | `admin/maintenance_config.py`, `admin/zendure_mqtt_config_proposals.py`, `admin/zendure_mqtt_config_draft.py`; browser compares server-issued `opaque:v1:` tokens |
| Connection keep/replace/add/block | `admin/connection_planner.py` | Maintenance switching, Setup adoption; browser renders the returned action and reason |
| Browser-facing device ids | `admin/observation_identity.py` (`observation_id`, `physical_device_id`, `connection_id`) | discovery run, mDNS and scan responses; `admin.js` keys its collections on them |
| MQTT TLS/broker semantics | `ems/config.py` (`normalize_mqtt_tls_mode`, `resolve_mqtt_tls_metadata`, `canonical_mqtt_tls_mode`, `mqtt_tls_mode_name`, `MQTT_TLS_OBSERVED_MODES`) | `admin/zendure_mqtt_broker_profiles.py`, `discovery_connections.py`, `mqtt_topic_discovery.py`, `zendure_mqtt_config_proposals.py` |
| Secret classification | `admin/secret_policy.py` (catalog metadata via `ems/config_catalog.py::is_secret_catalog_field`) | draft strip, browser redaction, workflow fingerprint |
| Catalog fields and editability | `ems/config_catalog.py` (`config_field_index`, `is_editable_catalog_field`, `grid_meter_variant_field_spec`) | `setup_config.py`, `maintenance_config.py`, `device_common_fields.py` |

### Where a new thing is declared

| Adding a… | Declare it in | Tests that must change |
|---|---|---|
| hardware model / write profile | `ems/mqtt_control/zendure_profiles.py` | `tests/test_zendure_mqtt_write_capability_matrix.py` |
| broker source | `ems/mqtt_control/power_capability.py` (`KNOWN_BROKER_SOURCES`, `_BROKER_SOURCE_VERIFIED_FAMILIES`) | `tests/test_zendure_mqtt_broker_source_capability.py` |
| TLS mode alias | `ems/config.py` alias sets | `tests/test_mqtt_tls_and_bool_helpers.py` |
| secret field | `ems/config_catalog.py` field metadata, or a marker in `admin/secret_policy.py` | `tests/test_admin_secret_policy.py` |
| identity evidence kind | `ems/device_identity.py` (`IdentityKind`, `EVIDENCE_PRECEDENCE`, `PHYSICAL_EVIDENCE_KINDS`) | `tests/test_device_identity.py` |
| placeholder / mask form | `ems/device_identity.py::is_masked_identity_value` | `tests/test_device_identity.py` |
| connection plan action | `admin/connection_planner.py` | `tests/test_admin_connection_planner.py` |
| grid-meter field | `ems/config_catalog.py::GRID_METER_VARIANTS` | `tests/test_admin_shared_config_normalization.py` |
| catalog field for an editor | `ems/config_catalog.py` (scope/level/editable/risk) | `tests/test_config_field_index.py` |

### Device identity and connection planning

The browser never decides physical equivalence. The chain is:

```text
ems/device_identity.py        physical identity, evidence policy, placeholders
  -> admin/connection_planner.py   keep / replace / add / block
  -> admin/observation_identity.py stable public ids
  -> admin.js                      renders ids, actions and reasons
```

- Identity states are `confirmed`, `probable`, `unresolved`, `ambiguous` and
  `conflict`. Only `confirmed`/`probable` identify hardware; an unresolved
  observation never receives a `physical_device_id`.
- Evidence precedence, strongest first: verified physical serial, verified
  scoped MQTT device anchor, precise scoped MQTT route, local API endpoint. The
  endpoint is *route* evidence and never confirms hardware on its own.
- A masked, redacted or placeholder value is never positive evidence
  (`is_masked_identity_value`). Two observations that only display the same
  placeholder stay separate observations.
- Physical identity and write-route ambiguity are separate answers: one inverter
  seen on two precise product routes is still one inverter, and only its write
  address became ambiguous (`IdentityComparison.route_ambiguous`).
- The three public ids mean different things and are not interchangeable:
  `observation_id` (`obs:v1:`) is this device reached this way,
  `physical_device_id` (`opaque:v1:`) is the hardware, `connection_id`
  (`conn:v1:`) is one transport route. All are keyed tokens, so no host, serial
  or route segment is recoverable from them.
- Setup and Maintenance may differ in workflow, never in identity or
  replacement rules — both call `plan_connection_change`.
- When the backend issues no ids, the browser fails closed: it still renders,
  but destructive actions gate on `hasObservationIdentity`.

### Test layering

- **Core / shared authority tests** own the complete combinatorial matrix. The
  physical-identity evidence matrix lives in `tests/test_device_identity.py`;
  the plan action matrix in `tests/test_admin_connection_planner.py`.
- **Endpoint tests** cover auth, CSRF, request parsing, delegation, error
  mapping, preview/apply persistence and rollback — a representative case per
  domain, never the whole matrix.
- **Frontend tests** cover passive projection, event handling, DOM escaping and
  renderer purity. They must not restate Core's physical-equivalence rules.
- **Playwright** keeps a small set of critical journeys, not domain permutations.
- **Structural contracts** in `tests/test_admin_authority_ownership.py` fail when
  a second identity, planning or id-issuing owner appears.
