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
| Connection keep/replace/add/block | `admin/connection_planner.py` | Maintenance switching, Setup switching and batch planning; browser renders the returned action and reason |
| Setup batch planning and legacy-state rehydration | `admin/setup_planner.py` (`build_setup_plan`, `plan_setup_connection_switch`) | `POST /api/setup/device-plan`; `admin.js` applies the returned typed operations |
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
| Setup selection/adoption rule | `admin/setup_planner.py` | `tests/test_admin_setup_batch_planner.py` |
| persisted Setup identity shape | `admin/setup_planner.py` (`IDENTITY_SCHEMA_VERSION`) | `tests/test_admin_setup_identity_migration.py` |
| grid-meter field | `ems/config_catalog.py::GRID_METER_VARIANTS` | `tests/test_admin_shared_config_normalization.py` |
| catalog field for an editor | `ems/config_catalog.py` (scope/level/editable/risk) | `tests/test_config_field_index.py` |

### Device identity and connection planning

The browser never decides physical equivalence. The chain is:

```text
ems/device_identity.py             physical identity, evidence policy, placeholders
  -> admin/observation_identity.py    the only issuer of public ids
  -> admin/connection_planner.py      keep / replace / add / block, for one pair
       -> admin/setup_planner.py         Setup adapter: batch planning + rehydration
       -> admin/maintenance_config.py    Maintenance adapter
  -> admin.js                         renders ids, actions and reasons
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
  replacement rules — both call `plan_connection_change`. Setup's *batch*
  question (one connection per device across all three sources) is orchestration
  only: `admin/setup_planner.py` owns source priority, manual-versus-automatic
  origin and the operation list, and asks the pairwise planner for every identity,
  conflict and replacement verdict — including the grouping itself.
- When the backend issues no ids, the browser fails closed: it still renders,
  but destructive actions gate on `hasObservationIdentity`.

#### Persisted Setup state and rehydration

Guided Setup keeps its work in `localStorage`. Each store answers a different
question and is therefore keyed differently:

| Store | Scope | Key |
|---|---|---|
| `ems-admin-config-draft` | one editable form row | local `draft_item_id` |
| `ems-admin-config-mqtt-preview` | one selected connection | proposal `id` |
| `ems-admin-config-dismissed` | one discovered observation | `observation_id` |
| `ems-admin-config-dismissed-devices` | one physical inverter | `physical_device_id` |
| `ems-admin-config-mqtt-manual-devices` | manual entry | local index |
| `transportInverterNames` (memory) | display name | issued identity, else local handle |

No schema-version field was added. The stores are distinguished by *shape*
rather than by a version marker, because the legacy shapes are unambiguous and a
version field would have to be invented for stores that never had one:

- a legacy draft item has no `draft_item_id`, and its `source_id` is the old
  `<api_family>:<serial>` / `<source>:<ip>:<port>` key rather than `obs:v1:…`;
- a legacy dismissal store holds bare serials under
  `ems-admin-config-dismissed-serials`, which the typed store replaces;
- a legacy MQTT selection carries no `physical_identity_token`.

`SETUP_IDENTITY_SCHEMA_VERSION` in `admin.js` and `IDENTITY_SCHEMA_VERSION` in
`admin/setup_planner.py` name the contract the two sides speak, so a future
incompatible change has a place to declare itself.

Two kinds of reference travel in that plan and must not be confused. Issued ids
(`observation_id`, `connection_id`, `physical_device_id`) say *what* something
is and are minted server-side. Local handles — `draft_item_id` for a form row,
`observation_ref` for a discovered card — say *which* entry an operation is
about; the browser supplies them, they carry no evidence, and the plan echoes
them so its operations always name something the caller can resolve, even when a
payload arrived without an issued id.

Rehydration is one request. The browser posts what it persisted to
`POST /api/setup/device-plan`; the backend resolves every entry from the fields
it already carries, issues the typed ids, and returns explicit mappings plus the
plan. The browser then replaces its identity-keyed stores with the returned ids.
It is idempotent (re-planning migrated state returns the same `plan_id`) and
fail-closed: an entry the backend cannot place stays unresolved, is preserved
with its original values, and produces a warning rather than a merge or a drop.
A response whose ticket is not the newest is discarded, and operations are
applied only when the plan's `plan_id` is still the current one.

Apply does not trust that. `/api/setup/config-preview` re-resolves every MQTT
selection against current trusted proposals and re-runs
`find_duplicate_zendure_device_identities` over the submitted draft, so a draft
that two physical devices would collapse into — however the browser got there —
is rejected as `zendure_device_identity_duplicate` before anything is written.
The plan is advisory for the browser's own draft; the invariant is enforced
where the config is produced.

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
