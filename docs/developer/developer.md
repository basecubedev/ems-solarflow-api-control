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
| Setup batch planning and legacy-state rehydration | `admin/setup_planner.py` (`build_setup_plan`, `plan_setup_connection_switch`, `setup_candidate_generation`, `setup_confirmation_fingerprint`, `setup_operations_fingerprint`, `setup_decision_fingerprint`) | `POST /api/setup/device-plan`; `admin.js` applies the returned executable operations |
| Setup draft field authority | `admin/setup_planner.py` (`DRAFT_FIELD_AUTHORITY`) | device plan, catalog mutation, exact preview — each field named once |
| Device-plan mutation authority | `admin/device_plan_registry.py` (contract + `device_plan_conflict`) plus the durable preview record and `workflow_authority_revision` in `admin/guided_setup_workflow.py` | Setup config preview, write and apply |
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
| device-plan conflict code | `admin/server.py` (`DEVICE_PLAN_*`), `admin.js` (`SETUP_DEVICE_PLAN_CONFLICT_ERRORS`) | `tests/test_admin_setup_plan_binding.py`, `tests/test_admin_setup_plan_draft_authority.py` |
| draft field a plan must authorize | `admin/setup_planner.py` (`_DRAFT_IDENTITY_FIELDS`), `admin.js` (`setupPlanStatePayload`) | `tests/test_admin_setup_plan_draft_authority.py` |
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
device_plan_id  (keyed token, recorded with the whole contract it was
                 issued under: run + ownership revision, candidate
                 authority, persisted state, planner verdict, executable
                 operations, outstanding confirmations, expected draft)
  -> POST /api/setup/config-preview   re-establishes every recomputable
                                      part of that contract, then compares
                                      the submitted draft
       -> config_preview_id           fingerprint includes device_plan_id
            -> POST /api/setup/config/{write,apply}
```

A plan id proves only that *some* plan was issued. `admin/device_plan_registry.py`
records the complete contract it was issued under, and Preview re-establishes
every recomputable part of it from current server state:

| Recorded | Answers | Conflict when it moves |
|---|---|---|
| `workflow_id` | which Setup run decided this | `stale_device_plan` |
| `workflow_revision` | whether that run is still an owner | `stale_device_plan` |
| `candidate_authority_fingerprint` | what it was planned over | `stale_device_plan` |
| `draft_revision` | the whole persisted state it read, dismissals included | (contract) |
| `decision_fingerprint` | what the planner decided per device | (contract) |
| `executable_operations_fingerprint` | what it authorized *doing* | (contract) |
| `confirmation_fingerprint` | what it is still waiting for | `device_plan_confirmation_required` |
| `expected_draft_fingerprint` | which draft it authorized | `device_plan_draft_mismatch` |

Two derived digests bind the parts together: `plan_fingerprint` over what the
planner decided, and `mutation_authority_fingerprint` over the whole contract.
Both are recomputed on every read, so an entry whose parts and digests disagree
is refused (`stale_device_plan`) rather than trusted in part. `record()` requires
every field — a caller that cannot name one has not established it, and a plan
recorded without it would validate against nothing. Preview also refuses a
request that presents no plan (`device_plan_required`).

**Confirmation is a fingerprint, not a flag.** A plan carries the digest of its
outstanding switch tokens; the settled value is the digest of the empty set. The
value therefore moves whenever confirmation-relevant state does — a switch
appearing, being answered, or being re-proposed under a different candidate
generation — while saying nothing about what is being confirmed. The tokens it
covers are themselves opaque and already bind the generation, the physical
identity and both connection ids.

**The live-config baseline deliberately stays out of the plan contract.** It is
owned by the exact preview record, which re-reads it under the apply transaction
and reports a moved baseline as `stale_setup_config` — a conflict with its own
operator action. Re-checking it here would revoke a plan without changing its
id, and a browser repairs a refused plan by re-planning: the same plan id would
come back and the tab could never earn a preview again. What the plan does
record is the state it read (`setup_state_revision`), which is what makes two
plans over the same candidates and different dismissals different authority.

**Workflow binding is ownership, not identity.** `workflow_authority_revision`
(in `admin/guided_setup_workflow.py`) digests the durable record's ownership
fields — format, id, type, status, creation. A run that was completed, abandoned
or superseded is no longer an owner and its revision moves with it, so a plan
issued inside it can never present itself as a decision of the current run — even
where the replacement carries the same identity. Ordinary progress (reviewing,
linking a transition, binding artifacts) deliberately leaves it alone, or every
preview would revoke the plan it was issued for and the wizard could never
converge.

**The candidate authority** (`setup_candidate_generation`) covers each trusted
candidate's issued `observation_id`, `connection_id`, `physical_device_id`,
identity status, transport, output-control verdict and discovery generation —
not the handles alone. An observation id is derived from the *route*, so
replacing the hardware at an address keeps the handle, and a broker that
re-answers with the same route in a later round is a new observation of it;
without the rest, a plan about different hardware would still look current.
`candidate_authority_of` is the entry point for a caller that holds discovery
state but has not run the planner, so Preview recomputes exactly what the
planner did.

**The draft binding** (`expected_draft_projections` → `setup_draft_fingerprint`)
is the draft the plan saw — devices and MQTT selections alike
(`expected_selection_projections`) — minus the entries it drops. Preview
canonicalizes the submitted device list and selections the same way and
compares.

The *additive* operations (`adopt_observations`, `select_mqtt_proposals`) are
deliberately not part of it. They are advice the browser may not have been able
to take up — the observation or proposal has to still be in the list it renders
— so a plan that predicted them would refuse the very draft it was computed
over. A *drop* is different: the plan has decided that entry is gone. Either way the browser re-plans after applying operations, so the
settled plan's state is the draft it presents — and a mismatch is repaired by
re-planning rather than by wedging the wizard. For that repair to converge, every
draft entry needs a `draft_item_id` (manual entries included), or an operation
naming it could never be applied.

What is compared is which hardware is configured and how it is reached (`role`,
`source_id`, `serial_number`, `physical_identity_token`, `ip`, `port`,
`api_family`, `device_type`, `grid_meter_type`, and each selection's
`id`/`broker_ref`). A name, an enabled flag or a catalog value is operator
intent about an already-authorized device and is owned elsewhere — so renaming a
device does not revoke a plan, and an unknown browser field never becomes
writable by appearing in the payload.

#### Setup draft field authority

That split is only safe while it is total: a field with two owners is one
neither can actually hold, and a field with none is a browser value that reaches
`config/config.json` without ever being authorized. Every field of a Setup draft
entry therefore names exactly one owner in
`admin/setup_planner.py::DRAFT_FIELD_AUTHORITY`:

| Authority | Fields | Enforced by |
|---|---|---|
| `device_plan` | `role`, `source_id`, `serial_number`, `physical_identity_token`, `ip`, `port`, `api_family`, `device_type`, `grid_meter_type`, plus the `draft_item_id` its operations address | `expected_draft_fingerprint` (the handle stays out — rekeying a card is not a change to what the plan decided) |
| `catalog_mutation` | `config_values` | `ems/config_mutation.py`, coerced against the central catalog |
| `exact_preview` | `config_name`, `display_name`, `enabled` | `setup_mutation_fingerprint`, which covers the whole submitted draft from review to apply |
| `presentation` | `auto_added`, `auto_selected`, `connection_type`, `discovery_source`, `manual` | nothing — asserted to be read nowhere the config is produced |

`zendure_mqtt_proposals[]` is deliberately not classified field by field,
because nothing the browser sends survives: a selection is a *lookup key*, and
`resolve_trusted_proposal` returns a copy of the trusted stored proposal with
only the validated `replace_grid_meter` decision taken from the request. That is
a stronger guarantee than a fingerprint — there is nothing for an unclassified
selection field to fall through into.

`tests/test_admin_setup_draft_field_authority.py` pins the classification from
both sides: every field the browser builds into a draft must be classified, and
every classified field must still be built.

The accepted plan id enters `setup_mutation_fingerprint` (version 3) and, with
its candidate generation, the durable preview record. Apply needs no second
registry lookup: that fingerprint already covers this exact draft and the plan it
was reviewed under, and Preview only issued it after proving the whole contract
held — which is what lets the binding survive the transient registry across an
Admin restart, while a discovery change between review and apply is still a
`stale_device_plan` conflict. Apply re-establishes the rest of the contract by
other means it already holds: `require_active` proves the run is still an owner
under the same identity, and the reviewed live baseline is re-read under the
apply transaction. The registry is transient on purpose: a lost entry makes the
browser re-plan, which is the safe direction, and a run that ends drops its
plans with it (`forget_workflow`).

Apply does not rely on the plan alone. `/api/setup/config-preview` re-resolves
every MQTT selection against current trusted proposals and re-runs
`find_duplicate_zendure_device_identities` over the submitted draft, so a draft
that two physical devices would collapse into — however the browser got there —
is rejected as `zendure_device_identity_duplicate` before anything is written.
The plan governs what the browser may do to its own draft; the invariant is
enforced where the config is produced.

On the browser side, a response whose ticket is not the newest is discarded, and
operations are applied only when the plan's `plan_id` *and* `generation` are
still the current ones. The plan request carries the browser's whole draft, not
only its inverters — an entry no plan saw is an entry no plan authorizes. Any of
the four device-plan conflicts clears the local preview id, disables Apply and
asks for a fresh plan; the mutation itself is never retried. The browser is
never told *which* fact moved — a plan from a replaced run and a plan from a
moved discovery state are the same repair — and a refusal answering an older
plan does not re-plan at all, or it would race the request already in flight.
`tests/test_admin_setup_plan_frontend.py` drives the shipped handler for all
four codes; it duplicates no backend rule.

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
  a second identity, planning or id-issuing owner appears;
  `tests/test_admin_setup_draft_field_authority.py` fails when an editable Setup
  field has no owner, or two.
