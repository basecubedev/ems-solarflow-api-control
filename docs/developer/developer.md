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
| Output-control eligibility | `ems/mqtt_control/power_capability.py` (model, write route, broker source), composed with route completeness by `ems/zendure_mqtt/capability.py` | `admin/zendure_mqtt_config_draft.py` (enforce/project), `admin/connection_capability.py` (discovered connections, for Setup planning), runtime `device_client`, migration, diagnostics, `admin.js` (labels only) |
| Physical inverter identity | `ems/device_identity.py` (`resolve_physical_identity`, `compare_physical_identity`, `is_masked_identity_value`, `EVIDENCE_PRECEDENCE`) | `admin/maintenance_config.py`, `admin/zendure_mqtt_config_proposals.py`, `admin/zendure_mqtt_config_draft.py`; browser compares server-issued `opaque:v1:` tokens |
| Connection keep/replace/add/block | `admin/connection_planner.py` | Maintenance switching, Setup switching and batch planning; browser renders the returned action and reason |
| Setup batch planning and legacy-state rehydration | `admin/setup_planner.py` (`build_setup_plan`, `plan_setup_connection_switch`, `setup_candidate_generation`) | `POST /api/setup/device-plan`; `admin.js` applies the returned executable operations |
| Device-plan mutation authority | `admin/device_plan_registry.py` plus the durable preview record in `admin/guided_setup_workflow.py` | Setup config preview, write and apply |
| Browser-facing device ids | `admin/observation_identity.py` (`observation_id`, `physical_device_id`, `connection_id`) | discovery run, mDNS and scan responses; `admin.js` keys its collections on them |
| MQTT TLS/broker semantics | `ems/config.py` (`normalize_mqtt_tls_mode`, `resolve_mqtt_tls_metadata`, `canonical_mqtt_tls_mode`, `mqtt_tls_mode_name`, `MQTT_TLS_OBSERVED_MODES`) | `admin/zendure_mqtt_broker_profiles.py`, `discovery_connections.py`, `mqtt_topic_discovery.py`, `zendure_mqtt_config_proposals.py` |
| Secret classification | `admin/secret_policy.py` (catalog metadata via `ems/config_catalog.py::is_secret_catalog_field`) | draft strip, browser redaction, workflow fingerprint |
| Catalog fields and editability | `ems/config_catalog.py` (`config_field_index`, `is_editable_catalog_field`, `grid_meter_variant_field_spec`) | `setup_config.py`, `maintenance_config.py`, `device_common_fields.py` |
| Config mutation semantics | `ems/config_mutation.py` (`coerce_catalog_value`, `resolve_change`, `apply_config_changes`, `apply_grid_meter_changes`, `strip_incompatible_grid_meter_fields`, `mutation_diff`) | `admin/setup_config.py` and `admin/config_preview.py` (Setup adapter), `admin/maintenance_config.py` (Maintenance adapter), `admin/device_common_fields.py` (device value set) |

### Shared config mutation

A field means the same thing on every screen. The workflows differ in *policy*,
not in interpretation, so both hand the change to one core:

```text
base config + typed changes + explicit policy
  -> ems/config_mutation.py
       -> normalized config
       -> issues
       -> deterministic, secret-safe mutation record
```

`ConfigChange(path, value, operation)` carries an intent rather than a bare
value, and `MutationPolicy` carries the workflow half:

| Policy field | Setup | Maintenance |
|---|---|---|
| `scope` | `setup` | `maintenance` |
| `allow_secret` | yes (a new install enters its own credentials) | no (secrets never round-trip through the editor) |
| `preserve_legacy_representations` | no — a generated config gets the canonical nested shape | yes — an existing flat MQTT grid meter is edited where it lives |
| `allow_remove` | yes | yes |

The `set`/`clear`/`keep` rules the core owns:

- an explicit operation always wins;
- an empty answer **clears** the key, so an emptied number never becomes `""`
  in a config EMS Core has to parse;
- a **secret** inverts that default: an empty credential box means "not
  retyped" and keeps the stored value, so only `clear_password` removes one.
  `CredentialIntent.from_draft()` is the one reader of that fragment;
- unknown paths are not writable, and existing unknown config keys an operator
  added by hand are never touched.

Grid-meter mutation is one function. It applies the type first, then the
top-level values, *then* the variant cleanup — a draft still holds the values of
the variant it was loaded from, so a cleanup that ran first would have stale
keys written back in behind it. MQTT values are narrowed to what the target
variant may carry and land in the representation the block already uses
(Maintenance) or in the canonical nested block (Setup).

`mutation_diff` flattens both configs to sorted leaf paths (`devices[0].max_power`),
never renders a secret on either side, and is stable against dict ordering — so
it is safe both for the browser and as preview input. Admin supplies which keys
count as secret (`admin/secret_policy.py`) and how far a value is bounded.

The canonical semantics are versioned: `CONFIG_MUTATION_CONTRACT_VERSION` enters
`setup_mutation_fingerprint` (version 3), so a preview issued under older
mutation rules cannot be applied by a process that would now write something
else from the same answers.

Domain matrices live in `tests/test_config_mutation.py`; the Setup/Maintenance
equivalence cases in `tests/test_admin_setup_maintenance_mutation_parity.py`;
the ownership pins in `tests/test_admin_authority_ownership.py` and
`tests/test_admin_shared_config_normalization.py`.

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
| persisted Setup identity shape | `admin/setup_planner.py` (`IDENTITY_SCHEMA_VERSION`), `admin.js` (`setupStoreEnvelope`) | `tests/test_admin_setup_identity_migration.py` |
| device-plan request field | `admin/server.py` (`_setup_plan_references`) | `tests/test_admin_setup_trust_boundary.py` |
| device-plan conflict code | `admin/server.py` (`DEVICE_PLAN_*`) | `tests/test_admin_setup_plan_binding.py` |
| grid-meter field | `ems/config_catalog.py::GRID_METER_VARIANTS` | `tests/test_admin_shared_config_normalization.py` |
| catalog field for an editor | `ems/config_catalog.py` (scope/level/editable/risk) | `tests/test_config_field_index.py` |
| config-mutation rule (coercion, clear/keep, grid-meter normalization) | `ems/config_mutation.py` | `tests/test_config_mutation.py`, `tests/test_admin_setup_maintenance_mutation_parity.py` |
| `ems/` module Admin imports | the module **and** a `COPY` line in `deploy/admin/Dockerfile` (the Admin image ships an explicit file list, not all of `ems/`) | `tests/test_admin_docker_image_contract.py` |

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

Each store is a versioned envelope written by `setupStoreEnvelope()` in
`admin.js`:

```json
{"schema_version": 1, "items": []}
```

`readSetupStore()` also accepts the bare array an older release wrote, so a
store written before the envelope existed reads as the unversioned contract and
is rewritten in envelope form on its first save. The migration is therefore
idempotent and needs no separate pass. `SETUP_IDENTITY_SCHEMA_VERSION` in
`admin.js` and `IDENTITY_SCHEMA_VERSION` in `admin/setup_planner.py` name the
same contract, and the legacy shapes stay recognizable on their own terms:

- a legacy draft item has no `draft_item_id`, and its `source_id` is the old
  `<api_family>:<serial>` / `<source>:<ip>:<port>` key rather than `obs:v1:…`;
- a legacy dismissal store holds bare serials under
  `ems-admin-config-dismissed-serials`, which the typed store replaces;
- a legacy MQTT selection carries no `physical_identity_token`.

#### Trusted candidates, handles and hints

Three kinds of value travel between the browser and the planner, and conflating
them is exactly the failure this boundary exists to prevent.

| Kind | Examples | What it may decide |
|---|---|---|
| Trusted candidate | a current discovery observation, a current MQTT proposal | everything: identity, grouping, capability, verdict |
| Handle | `observation_id`, proposal `id`, `draft_item_id`, `observation_ref`, a confirmation token | *which* server-owned record or local row is meant |
| Hint | a persisted `serial_number`, `ip`, `mqtt.device_id`, a bare-serial dismissal | *where to look* — never what was found |

`POST /api/setup/device-plan` accepts handles and hints, never candidates. Its
candidate set is the server's own discovery view (`mdns_provider.devices()`
stamped by `admin/observation_identity.py`, plus `_trusted_mqtt_proposals()`);
source priority comes from the preparation store rather than the request. A
submitted observation handle may attach the caller's `observation_ref` to a
record that is still offered, and nothing else; a handle that no longer resolves
appears in `unresolved_references` and contributes no operation. Client trust
booleans (`verified`, `usable_for_config`, `identity_status`,
`physical_device_id`) are ignored wherever they appear.

Rehydration then treats every persisted entry as a hint. It is compared against
the trusted candidates through the same canonical planner that grouping uses,
and:

| Match | Result |
|---|---|
| exactly one physical identity, one connection | `legacy_match: "matched"` — the candidate's issued ids are copied over |
| none | `legacy_match: "unmatched"` — no issued ids, entry preserved, `legacy_state_unresolved` warning |
| several | `legacy_match: "ambiguous"` — no issued ids, entry preserved, `legacy_state_ambiguous` warning |

A dismissal resolves on physical identity alone (two routes to one device are
still one dismissal); an already-issued `opaque:v1:` token is the browser's
migrated store rather than a hint and stands on its own. A masked or placeholder
value never resolves. Rehydration is idempotent: re-planning migrated state
returns the same `plan_id`.

#### Verdict-to-operation enforcement

`build_setup_plan` derives its operations from the final canonical verdict, not
from the selected source:

| Verdict | `operations` | `proposed_operations` | `confirmations` |
|---|---|---|---|
| `use_candidate`, `add_as_new_device` | replacement emitted | — | — |
| `keep_current` (candidate *is* the current connection) | id refresh only | — | — |
| `keep_current` (any other reason) | — | — | — |
| `replace_with_confirmation` | — | full switch | one token |
| `block_identity_conflict`, `block_unresolved_identity`, `block_capability_loss` | — | — | — |

A group whose recorded source already *is* the selected one is not switching;
its operations only remove the duplicates an earlier switch left behind. Setup's
output-control verdicts come from `admin/connection_capability.py`, which adapts
the canonical resolver — so a local MQTT scalar or an unresolved write route is
`control_continuity: "lost"`/`"unknown"` and can never be auto-selected over a
controllable connection.

A confirmation token binds the candidate generation, the physical identity, both
connection ids and both entry references. The browser sends it back in
`confirmed_switches` (the backend re-plans with `operator_confirmed=True` and
returns executable operations) or `declined_switches` (the group keeps its
current connection). A token minted under a different generation authorizes
nothing.

#### Device plan → config preview → apply

Planning only matters if the plan is what reaches `config/config.json`, so the
three steps are one authority chain:

```text
device_plan_id  (keyed token, recorded with its candidate generation)
  -> POST /api/setup/config-preview   verifies plan, generation, confirmations
       -> config_preview_id           fingerprint includes device_plan_id
            -> POST /api/setup/config/{write,apply}
```

`admin/device_plan_registry.py` records the plans this process issued together
with their candidate generation and whether a confirmation is still outstanding.
Preview refuses a request that presents none (`device_plan_required`), one this
process did not issue or whose generation has moved on (`stale_device_plan`), or
one still awaiting an answer (`device_plan_confirmation_required`). The accepted
plan id enters `setup_mutation_fingerprint` (version 2) and, with its generation,
the durable preview record — so mutation authority survives an Admin restart
while a discovery change between review and apply is still a `stale_device_plan`
conflict. The registry itself is transient on purpose: a lost entry makes the
browser re-plan, which is the safe direction.

Apply does not rely on the plan alone. `/api/setup/config-preview` re-resolves
every MQTT selection against current trusted proposals and re-runs
`find_duplicate_zendure_device_identities` over the submitted draft, so a draft
that two physical devices would collapse into — however the browser got there —
is rejected as `zendure_device_identity_duplicate` before anything is written.
The plan governs what the browser may do to its own draft; the invariant is
enforced where the config is produced.

On the browser side, a response whose ticket is not the newest is discarded, and
operations are applied only when the plan's `plan_id` *and* `generation` are
still the current ones.

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
