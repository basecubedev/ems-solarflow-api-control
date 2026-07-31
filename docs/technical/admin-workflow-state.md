# Admin workflow state — inventory, write paths, transition matrix, invariants

Every persisted state source the Admin console's workflows share, the paths that
can write configuration, and the invariants those must satisfy. Companion to
`admin-architecture.md` (architecture rules) and `system-build-pairing.md`
(transition lifecycle).

Scope: Guided Setup, Maintenance, Guided Upgrade, Recovery, authentication loss,
Admin restart.

**How to read this.** Sections 1–3, 5 and 7 describe the system **as it is now**,
after the server-owned workflow-authority hardening. Section 4 is the original
audit that motivated the first hardening pass and is deliberately kept in the
past tense — it records what was broken, not what is. Section 7 is the unified
cross-workflow lifecycle: which guided workflow owns the Admin, how a switch
between them runs, and how a stranded one is recovered.

## 0. Authority layers

Five authorities, none duplicated into another:

| Authority | Record | Owns |
|---|---|---|
| **System Alignment transition** | `state/pending-transition.json` | operation ID, transition mode, transition stage, worker state |
| **Guided Setup workflow** | `state/guided-setup-workflow.json` (`admin/guided_setup_workflow.py`) | the durable `workflow_id`, its lifecycle (`active` → `abandoned`/`superseded`/`completed`), the **exact preview authority** and which artifacts it owns; links the transition only by `operation_id` |
| **Exact preview** | the workflow record's `preview` slot | one opaque `config_preview_id` bound to a fingerprint of the full mutation input, the live-config baseline it was reviewed against and the prepared payload hash |
| **Installed system** | Docker state + live `config/config.json` + `state/known-good-system-build.json` | what is actually running; known-good is durable installed-system state, **never** a workflow artifact |
| **Release cache** | `releases/<tag>/…`, `state/selected-release.json` | downloaded resources; a workflow's selected target is recorded in its own record (`selected_system_tag`), not inferred from the cache |

A raw, browser-held `config_revision` proves **nothing** about which draft was
reviewed; it survives in preview responses for explanation only and is never
mutation authority.

Beside these five sit three **arbiters**, not authorities. None stores durable
state — a restart holds no claims — and none duplicates transition stage or
worker liveness:

* `SetupLifecycleCoordinator` (`admin/setup_lifecycle.py`) decides *who may act
  on the Guided Setup workflow right now* (§5.4);
* `ReplacementDispatchCoordinator` (`admin/replacement_dispatch.py`) decides
  *which concurrent caller performs a dispatch attempt's one Admin replacement
  launch* — one attempt per normal dispatch, one more per accepted explicit
  retry (§5.5);
* `AdminWorkflowLifecycleService` (`admin/workflow_lifecycle.py`) decides *which
  guided workflow owns the Admin, whether it may be switched away from, and
  whether a recovery is safe* (§7). It reads the five authorities together,
  creates none of its own, and delegates every mutation back to the owner.

---

## 1. Persisted state inventory

`<install>` is the install root; `<admin_data>` is the Admin data dir
(`<install>/admin` by default, `EMS_ADMIN_DATA_DIR` otherwise).

### 1.1 Backend, durable (survives refresh, logout and Admin restart)

| # | Artifact | Creator | Readers | Writers | Delete path | Owner | Affects deployment |
|---|---|---|---|---|---|---|---|
| B0 | `<admin_data>/state/guided-setup-workflow.json` (`format_version: 2`) | `GuidedSetupWorkflowStore.ensure_active` (start-path) | every Setup mutation route, `DeploymentService`, `GET /api/setup/workflow` | `GuidedSetupWorkflowStore` only | never deleted — rewritten terminal (`abandoned`/`superseded`/`completed`) plus a validated `cleanup` state | **Guided Setup workflow record** | Yes — mutation and deploy authority, and cleanup ownership |
| B1 | `<admin_data>/state/pending-transition.json` | `PendingTransitionStore.begin` | `SystemAlignmentService.status/resume/...`, every alignment gate | `PendingTransitionStore` only | `clear()` (no HTTP caller); `cancel()` rewrites to terminal `cancelled` | System Build transition | Yes — gates Setup/Upgrade |
| B2 | `<admin_data>/workflows/guided-setup/<workflow_id>/generated/config.json` | `ConfigExportService.write` (target resolved from B0) | `DeploymentService._generated_config_state` → `plan`/`prepare` | `ConfigExportService.write` | `SetupWorkflowArtifacts.clear` via `POST /api/setup/abandon` / supersede | **Guided Setup workflow** | Yes — copied over live config, gated by B2a |
| B2a | `…/generated/config.meta.json` (beside B2) | `SetupWorkflowArtifacts.record_generated` | `read_generated_metadata` → `DeploymentService._generated_config_rejection` | same | same as B2 | **Guided Setup workflow** | Yes — carries `workflow_id`, `preview_id`, `draft_fingerprint`, `base_config_revision`, `prepared_config_sha256` |
| B3 | `<admin_data>/state/.admin-deployment.json` (path fixed by the install-state contract) | `DeploymentService._write_marker` | `_prepared_marker`, `detect_install_state` | `_write_marker` (stamps `workflow_id`/`preview_id`) | `SetupWorkflowArtifacts.clear` via `POST /api/setup/abandon` / supersede | **Guided Setup workflow** (ownership in content + B0) | Yes — start refuses a marker from another workflow |
| B4 | `<admin_data>/state/selected-release.json` | `ReleaseManager` / `embedded_resources` | `_selected_release_tag` | same | **none** on abandon (cache; the workflow's target lives in B0) | release cache | Yes |
| B5 | `<admin_data>/state/known-good-system-build.json` | `KnownGoodStore.record` | `SystemAlignmentService` | `KnownGoodStore` | none — **durable installed-system state**, survives every workflow cancellation | installed system | Yes |
| B6 | `<admin_data>/state/guided-upgrade-context.json` | `GuidedUpgradeContextStore.save` | `.load(operation_id, target_system_tag)` | same | `clear_for_operation(operation_id)` on Cancel upgrade and on completed upgrade (after the durable success); kept on `failed_recoverable` | Guided Upgrade | Yes |
| B7 | `<admin_data>/state/pending-admin-update.json` | `PendingAdminUpdateStore.write` | `AdminUpdateService.status/resume` | same | `clear()` | Admin self-update | Yes |
| B8 | `<admin_data>/releases/<tag>/…` | `ReleaseManager` | deployment/upgrade | same | `test_reset` only | Setup/Upgrade | Yes |
| B9 | `<admin_data>/backups/config/*.json` | `ConfigApplyService._backup` | restore flows | same | Backup UI | Backup/restore | No |
| B10 | `<install>/config/config.json` | installer / Admin | EMS runtime, Maintenance, Setup preview | `ConfigApplyService.apply_prepared`, `DeploymentService._write_config` | n/a | **live config** | n/a |
| B11 | `<install>/data/runtime-state.json` | EMS | EMS, Admin runtime convergence | EMS + `dashboard/runtime_write.py` | n/a | EMS runtime | n/a |

### 1.2 Backend, in-process (does **not** survive Admin restart)

| # | State | Owner | Restart behavior |
|---|---|---|---|
| P1 | `OperationCoordinator._active` / `._abandoned` | worker liveness | Lost — a restart makes every worker claim look inactive, which is what keeps an expired orphan escapable (§5.3); the claim is held by Guided Upgrade/deployment workers **and** by every resource import |
| P2 | `DeploymentJobRegistry` / `StartJob` registries | deployment jobs | Lost — job IDs 404 after restart |
| P3 | `SetupIntentStore` (in-memory, TTL 20 min) | Fresh Setup confirmation | Lost — re-confirmation required |
| P4 | `DeploymentService._active_job` / `_active_start_job` | deployment serialization | Lost |
| P5 | `SetupLifecycleCoordinator._claims` / `._terminalized` | Setup mutation vs. terminal exclusion (§5.4) | Lost — **fail closed by design**: a restart holds no claims, so nothing pretends a pre-restart worker is still live; every commit stays gated by B0 and by the durable marker checks |
| P6 | `ReplacementDispatchCoordinator._entries` | Admin replacement dispatch exclusion per operation id, one entry per dispatch attempt (§5.5) | Lost — by design: a restart holds no claim and no published dispatch, which is exactly why the crash window between a durable `admin_reconnect_pending` and the launch stays a documented limitation (§5.5, §6.5) |

### 1.3 Browser (`localStorage`; survives refresh, survives logout, survives Admin restart)

| # | Key | Cleared by `Start over` |
|---|---|---|
| L1 | `ems-admin-config-draft` | yes |
| L2 | `ems-admin-config-dismissed` | yes |
| L3 | `ems-admin-config-dismissed-serials` | yes |
| L4 | `ems-admin-config-features` | yes (`clearFeatureValues`) |
| L5 | `ems-admin-config-mqtt-preview` | yes (`clearMqttSelection`) |
| L6 | `ems-admin-config-mqtt-manual-devices` | yes |
| L7 | `ems-admin-config-mqtt-broker` | yes |
| L8 | `ems-admin-setup-step` | overwritten by `setActiveStep("release")` |
| L9 | `ems-admin-setup-workflow` (`{workflow_id, preview_id}` — render-only copies of B0's identity) | replaced by the fresh identity Restart setup mints |

Browser memory additionally holds `setupState.{devices,config,deployment,start}`,
`upgradeState`, discovery caches and the setup operation context.

**No `sessionStorage` is used.** Logout therefore does not invalidate any local
planning state; L1–L8 outlive a session.

---

## 2. Config write paths

| Path | Entry point | Owner | Source | Target | Freshness check | Backup | Cleanup |
|---|---|---|---|---|---|---|---|
| W1 Setup apply | `POST /api/setup/config/apply` → `_setup_config_transaction` → `ConfigApplyService.apply` | Guided Setup | browser draft | live `config/config.json` | **workflow + exact-preview authority (§2.3) checked before staging**, plus the commit-time guard | yes | n/a |
| W2 Setup export | `POST /api/setup/config/write` → `ConfigExportService.write` | Guided Setup | browser draft | workflow-owned B2 | **same authority as W1**; binds workflow/preview/fingerprint/baseline/payload-hash into B2a for W3 | no | B2/B2a via abandon/supersede |
| W3 Setup deploy | `POST …/deployment/prepare` → `_run_prepare` → `_write_config` | Guided Setup | **B2 on disk** | live `config/config.json` | **workflow ownership + `base_config_revision` from B2a** (§2.2), checked before any workspace write and not bypassed by `overwrite` | yes (`_backup_existing_runtime`) | B2/B2a/B3 via abandon/supersede |
| W4 Maintenance apply | `POST /api/admin/maintenance/config/apply` → `prepare_maintenance_config_apply` | Maintenance | browser draft + live config merge | live `config/config.json` | **client-supplied `expected_revision`, captured when the draft was loaded** | optional | n/a |
| W5 MQTT migration apply | `prepare_migration_apply(expected_revision)` | Maintenance | live config | live `config/config.json` | client `expected_revision` | yes | n/a |
| W6 Guided Upgrade migration | `admin/guided_upgrade.py:926` | Guided Upgrade | migration output | live `config/config.json` | `migration["revision"]` | yes | n/a |
| W7 Backup restore | `admin/backup_restore.py` | Backup/restore | backup set | live `config/config.json` | set-scoped | yes | n/a |
| W8 Legacy migration | `migrate_legacy_root_config` | Install-state | legacy root config | standard config | layout-state guarded | yes | n/a |
| W9 Runtime convergence | `dashboard/runtime_write.py` whitelist | Maintenance apply | live config | `data/runtime-state.json` | n/a | no | n/a |

### 2.1 The traced scenario

```
01 Start Guided Setup
02 Generate staged configuration      → W2 writes B2
03 Leave Setup without deployment     → B2 persists, nothing owns it
04 Open Maintenance
05 Change and save live configuration → W4 writes config/config.json
06 Return to Setup / resume
07 Execute deployment                 → W3 copies B2 over config/config.json
```

**Step 07 is now rejected** with `stale_generated_config` (HTTP 409) before any
workspace write. It used to overwrite, because the two pre-existing gates cannot
see live-config drift:

- `_existing_install_conflict` returns `None` when `_marker_matches_workspace(_prepared_marker())`,
  so an install with a matching B3 is treated as "already owned by this Admin,
  updateable" and no confirmation is requested.
- `_workspace_conflict` compares `marker["config_sha256"]` against the
  **generated** config's hash. That hash tracks B2, never the live config, so a
  Maintenance edit leaves both sides equal and the guard passes.

`_stale_generated_config` closes it by comparing the live config against the
`base_config_revision` recorded in B2a when B2 was written.

### 2.2 W3 deploy-authority contract

Ownership first, then freshness. `DeploymentService._generated_config_rejection`
refuses with `generated_config_review_required` (409) unless **all** of:

- B0 holds an **active** workflow, and B2a's `workflow_id` names it;
- B2a's `preview_id` and `prepared_config_sha256` equal the active workflow's
  stored preview;
- the generated bytes hash to exactly `prepared_config_sha256`.

A sidecar-less legacy artifact, an Archive-98 sidecar without workflow identity,
a tampered file, or an artifact from an abandoned/superseded workflow all land
in the review-required path: the UI returns to Config Preview and the user
regenerates under the active workflow. Unknown legacy files are reported, never
silently deleted or silently deployed. Then freshness — presence and absence
are both part of the revision state (`base_config_revision` is the
`{expected_revision, expect_absent}` pair the preview was issued against):

| `base_config_revision` | Live config | Result |
|---|---|---|
| `expect_absent: true` | absent | allowed (fresh install) |
| `expect_absent: true` | equals B2 | allowed (redeploy) |
| `expect_absent: true` | differs from B2 | **rejected** `stale_generated_config` |
| `expected_revision: <sha>` | absent | **rejected** (deletion is a change) |
| `expected_revision: <sha>` | equals base | allowed |
| `expected_revision: <sha>` | equals B2 | allowed (redeploy) |
| `expected_revision: <sha>` | differs from both | **rejected** |

`overwrite=true` does not bypass either check: it confirms replacing an
install, not discarding a change the operator was never shown. Deployment
**start** additionally refuses a marker (B3) whose `workflow_id` is not the
active workflow's.

### 2.3 W1/W2 exact-preview mutation contract

Every Setup mutation presents `setup_workflow_id` + `config_preview_id`.
`POST /api/setup/config-preview` requires the active workflow ID, validates and
resolves the draft, computes a deterministic fingerprint of the full mutation
input (devices draft, grid-meter count, features, the **resolved** trusted MQTT
proposals, manual broker fields with secret values reduced to digests, manual
MQTT devices — never `overwrite`, the IDs, or the legacy `config_revision`),
captures the live baseline and the prepared payload hash, and persists one new
opaque `config_preview_id` into B0. A not-ready preview revokes the stored
preview instead of issuing one. The read-only alias
`POST /api/setup/config-preview/validate` issues no authority.

Write/apply verify, inside the shared apply transaction and **before any
credential is staged**:

| # | Check | Failure |
|---|---|---|
| 01 | workflow exists and is active | 409 `setup_workflow_required` / `setup_workflow_not_active` |
| 02 | a preview is stored and an ID was submitted | 409 `setup_preview_required` |
| 03 | submitted ID and recomputed fingerprint match the stored preview | 409 `setup_preview_mismatch` |
| 04 | live config still matches the previewed baseline | 409 `stale_setup_config`, **revokes the preview** |
| 05 | exact serialization reproduces `prepared_config_sha256` | 409 `setup_preview_mismatch`, revokes the preview |
| 06 | credential staging | 400, byte-exact rollback |
| 07 | commit-time revision check | 409, rollback, revokes the preview |
| 08 | commit | — |

Consumption: a successful direct Apply consumes the preview; a successful Write
binds it into B2a and keeps it (deployment validates against it); a newer
preview invalidates the older one; abandonment/supersede invalidates every
preview. A rejected mutation never consumes the preview, stages nothing and
leaves the live config and credential store byte-exact.

**A preview for draft A can never authorize draft B** — the fingerprint, not
the live revision, is what binds the mutation to the reviewed request. Requests
carrying only the legacy `config_revision` are refused with
`setup_workflow_required` before anything changes.

**W4 has true optimistic locking**: the client sends the `revision` it received
when the Maintenance draft was loaded, and `prepare_maintenance_config_apply`
refuses on mismatch with `status: "conflict"`.

### 2.4 Setup route authority matrix

Every mutating Setup route and exactly what authority it demands. Enforced by
`tests/test_admin_setup_route_contracts.py`, which also fails when a new POST
route under `/api/setup/` is added without a row here.

Four terms, four distinct jobs — none of them substitutes for another:

| Term | What it is | Lifetime |
|---|---|---|
| `setup_intent_id` | a **workflow-bound one-shot user confirmation**: this session confirmed Fresh Setup, for this workflow, against this installation state | consumed by one mutation; retired with its workflow |
| `setup_workflow_id` | the **durable owner** of the Setup state (B0) | until the workflow is terminal and its cleanup converged |
| transition `operation_id` | the **exact System Alignment transition reference** the workflow owns | the transition's own lifecycle |
| lifecycle claim | **mutual exclusion** while a mutation's irreversible work — including transition creation — can still commit | one request (or one worker) |

| Route | `setup_workflow_id` | `setup_intent_id` | `config_preview_id` | bound generated artifact | transition `operation_id` | lifecycle claim |
|---|---|---|---|---|---|---|
| `POST /api/setup/config/write` | required, exact | — | required, exact | writes it | — | `config_write` (mutation) |
| `POST /api/setup/config/apply` | required, exact | — | required, exact | — | — | `config_apply` (mutation) |
| `POST /api/setup/deployment/prepare` | required, exact | — | — | required (B2a proves workflow + preview + hash + baseline) | mirrors the current one, never advances it | `deployment_prepare` (mutation, held for the whole worker) |
| `POST /api/setup/deployment/start` | required, exact | — | — | via B3, whose `owner`/`workflow_id` must match | claims the EMS operation | `deployment_start` (mutation, held for the whole worker) |
| `POST /api/setup/deployment/repair-permissions` | required, exact | — | — | via B3 owner match | — | `permission_repair` (mutation) |
| `POST /api/setup/deployment/resolve-container-conflict` | required, exact | — | — | via B3 owner match | may acknowledge its own conflict recovery | `container_conflict_resolution` (mutation) |
| `POST /api/setup/abandon` | **required whenever B0 exists**; also the retry-cleanup entry point | — | — | — | cancels only the **exact** `operation_id` it stores (§5.3) | `abandon` / `cleanup_retry` (terminal) |
| `POST /api/setup/system-build/supersede` | required, exact | — | — | — | same | `supersede` (terminal) |
| workflow completion (deployment worker's terminal callback) | the worker's carried id | — | — | — | the completed operation | `complete` (terminal) |
| `POST /api/setup/system-build/update-admin` | required, exact | required, bound to that workflow | — | — | **creates and owns it before it exists** (§5.5) | `system_build_update_admin` (mutation) |
| `POST /api/setup/system-build/confirm` | required, exact | required, bound to that workflow | — | — | same | `system_build_confirm` (mutation) |
| `POST /api/setup/releases/prepare` | required, exact | required, bound to that workflow | — | — | same | `setup_release_prepare` (mutation) |
| `POST /api/setup/automated/releases/prepare` | required, exact | required, bound to that workflow | — | — | same (`automated_setup` mode) | `setup_release_prepare` (mutation) |

The four System Build routes are in the matrix with the *same* authority as every
other Setup mutation. They used to be exempt on the argument that they only
*create* the transition a workflow later links to — but creating a transition is
exactly what makes a workflow the owner of an irreversible operation, and a
workflow-less create could be raced by an abandon, adopt an existing transition it
never started, or be linked afterwards on a best-effort basis. Both release-prepare
routes share one handler and differ only in the transition mode they open, so they
share one claim name. Release preparation still fills the shared release cache
(B8/B4) — installed-system state that outlives every workflow — but it may only do
so on behalf of the workflow that owns the transition it advances.

Read-only routes and `POST /api/setup/config/download` (serializes a draft to the
browser, touches no durable state) are outside the matrix.

---

## 3. Transition matrix

`T` = `pending-transition.json` (B1). "Artifacts" refers to B2–B6.

| # | Transition | Allowed | Owner → next | T before → after | Artifacts | Live config | Recovery | UI view |
|---|---|---|---|---|---|---|---|---|
| 1 | Setup → Maintenance | **reads yes, writes blocked** | Setup → Maintenance | unchanged | **all retained** | untouched | available | Maintenance, config apply returns 409 |
| 2 | Maintenance → Setup | yes | Maintenance → Setup | unchanged | retained | untouched | available | Setup |
| 3 | Setup → Guided Upgrade | **only through the Setup owner** — upgrade validate *and* execute return 409 `setup_abandon_required` until the Setup is terminated, and 409 `setup_cleanup_required` while a terminal workflow's cleanup has not converged. The console resolves it with one previewed lifecycle switch (§7.3), which runs that same owner | Setup → Upgrade | `fresh_install`/`automated_setup` → `cancelled` by the termination | **removed with it**; unfinished cleanup keeps both Upgrade phases blocked and offers Retry cleanup | untouched | available | Upgrade |
| 4 | Guided Upgrade → Maintenance | yes | Upgrade → Maintenance | unchanged | retained | untouched | available | Maintenance |
| 5 | Maintenance → Guided Upgrade | yes | Maintenance → Upgrade | unchanged | retained | untouched | available | Upgrade |
| 5a | Guided Upgrade → Guided Setup | yes, **one previewed lifecycle switch** (§7.3) when the transition is cancellable; refused `workflow_operation_in_progress` otherwise | Upgrade → Setup | `guided_upgrade` → `cancelled` | Setup artifacts unaffected; the upgrade's own B6 cleared, operation-bound | untouched | available | Setup step 1, fresh workflow + intent |
| 5b | task selection reached by navigation alone | yes | unchanged | unchanged | retained | untouched | available | the workflow resumes when reopened |
| 6 | Setup → session loss | yes | Setup → none | unchanged | retained | untouched | available | Login |
| 7 | Maintenance → session loss | yes | — | unchanged | retained | untouched | available | Login |
| 8 | Upgrade → session loss | yes | — | unchanged | retained | untouched | available | Login |
| 9 | Setup → refresh | yes | Setup → Setup | unchanged | retained | untouched | available | Setup, rehydrated from L1–L8 |
| 10 | Maintenance → refresh | yes | — | unchanged | retained | untouched | available | Maintenance, draft reloaded |
| 11 | Upgrade → refresh | yes | — | unchanged | retained | untouched | available | Upgrade |
| 12 | Setup → Admin restart | yes | — | unchanged | retained | untouched | available | Setup; P1–P4 lost |
| 13 | Maintenance → Admin restart | yes | — | unchanged | retained | untouched | available | Maintenance |
| 14 | Upgrade → Admin restart | yes | — | unchanged | retained | untouched | available | Upgrade; job IDs 404 |
| 15 | active → **Restart setup** (abandon) | yes | Setup → none | non-terminal → `cancelled` | **workflow dir + B3 removed**; B0 → `abandoned`; a fresh workflow + intent is minted | untouched | available | Setup step 1 |
| 16 | Setup-owned recovery → **Discard setup** | yes; a submitted `setup_workflow_id` must match B0 | Setup → none | non-terminal → `cancelled` | **workflow dir + B3 removed** via `/api/setup/abandon`; B0 → `abandoned` | untouched | available | choices |
| 16a | Guided Upgrade recovery → **Cancel upgrade** | yes | Upgrade → none | non-terminal → `cancelled` | Setup artifacts untouched — not its to remove; **its own B6 cleared** (operation-bound); B5 preserved | untouched | available | choices |
| 16b | unknown transition owner | **no destructive action offered**; the primitive also refuses (`transition_cancel_unsupported`) | — | unchanged | unchanged | untouched | available | recovery panel without a discard/cancel button |
| 16c | Setup-owned transition → `POST /api/admin/system-alignment/cancel` | **rejected**, 409 `setup_abandon_required` | Setup | **unchanged** | **unchanged** | untouched | available | caller is directed to Discard setup |
| 16d | selected build changed after a Setup transition exists | yes, **one backend operation** `POST /api/setup/system-build/supersede` | Setup → Setup (new workflow) | non-terminal → `cancelled` | old workflow dir + B3 removed; B0 old → `superseded`, replacement `active` with the new tag; fresh intent issued | untouched | available | Setup step 1, new identity |
| 17 | active → `failed_recoverable` | yes | → Recovery | → `failed_recoverable` | retained (B6 kept for upgrade recovery) | untouched | available | recovery panel |
| 18 | `failed_recoverable` → Recovery/abandon | yes | Recovery | → `resume_stage` or `cancelled` | abandon removes the workflow dir + B3 | untouched | n/a | recovery panel |
| 18a | **Setup**-owned `failed_recoverable` → Return to running build | **refused**, 409 `setup_return_unsupported` (§5.6) | Setup | **unchanged** | **unchanged** | untouched | available | the action is not offered; Resume and Discard setup are |
| 18b | Guided Upgrade / align-existing `failed_recoverable` → Return to running build | yes, unchanged | Recovery → align-existing | old op `cancelled`, new `align_existing_install` committed | B6 retained for the old operation | untouched | n/a | reconnect overlay |
| 18c | Setup-owned transition mid-resource-import → Discard setup | **refused**, 409 `mutation_in_progress` (§5.3) | Setup | **unchanged** (`admin_aligned`, claimed) | **unchanged** | untouched | available | wait for the import, then discard |
| 19 | `failed_recoverable` → unrelated Maintenance action | writes blocked until terminal | Maintenance | unchanged | retained | may change | available | Maintenance |
| 20 | Setup abandoned → Maintenance write | yes | Maintenance | terminal | already removed | may change | available | Maintenance, apply allowed |
| 21 | Maintenance changed live config → stale Setup deploy | **rejected** | Setup | unchanged | retained | **untouched** | available | deployment error, `stale_generated_config` |
| 21a | legacy/unowned generated config → deploy | **rejected before any workspace write** | Setup | unchanged | retained (reported, never silently deleted) | **untouched** | available | 409 `generated_config_review_required`, back to Config Preview |
| 24 | live config changed → stale Setup apply/write | **rejected; the stored preview is revoked** | Setup | unchanged | retained | **untouched** | available | conflict panel, `stale_setup_config`, then re-review |
| 25 | mutation without workflow/preview authority (incl. legacy `config_revision`-only requests) | **refused** | Setup | unchanged | retained | untouched | available | 409 `setup_workflow_required` / `setup_preview_required` |
| 25a | old tab from workflow A → mutation against workflow B | **refused**; the tab stops polling/mutating | Setup | unchanged | unchanged | untouched | available | workflow-conflict panel: `Open current setup` / `Discard local draft` |
| 22 | Admin restart (any workflow) | yes | unchanged | unchanged | unchanged (B0 and its preview read back identically) | untouched | available | same interpretation; P1–P4 lost |
| 22a | upgrade completes (durable known-good + `completed`) | yes | Upgrade → none | `completed` | **B6 cleared** (operation-bound), B5 written | untouched | n/a | done |
| 23 | abandon with a failed artifact removal | partial | Setup → none | `cancelled` | some removed, rest reported `failed`; B0 stays terminal **under the same `workflow_id`** with `cleanup.state = pending`; a replacement Setup and both Upgrade phases stay blocked | untouched | available | error, Retry cleanup under the same id converges |
| 23a | abandon finds a **claimed** artifact it cannot prove it owns | partial | Setup → none | `cancelled` | the artifact is **kept**; B0 terminal with `cleanup.state = review_required`; a retry does not convert this to clean | untouched | available | 409 `setup_artifact_review_required`, review-required copy, **no** Retry cleanup button, **Recheck setup cleanup** offered |
| 23b | abandon of a workflow that claimed nothing, with installed-system files present | yes | Setup → none | `cancelled` | the files are **not inspected**; B0 terminal with `cleanup.state = complete` | untouched | available | 200, no review copy, Setup and Guided Upgrade stay available |
| 26 | mutation still running → abandon/supersede | **refused**, 409 `setup_operation_in_progress` (with the operation kind) | Setup | **unchanged** | **unchanged** | untouched | available | the workflow stays open; nothing claims to have been discarded |
| 26a | terminalization begun → apply/write | **refused**, 409 `setup_workflow_not_active` before any credential is staged | Setup | unchanged | unchanged | **untouched** | available | workflow-conflict panel |
| 26b | prepare/start worker live → abandon/supersede | **refused**, 409 `setup_operation_in_progress` until the worker settles | Setup | unchanged | unchanged | untouched | available | wait, then discard |
| 26c | workflow superseded while its prepare worker runs | the worker **fails** at its next ownership check; no config, compose, marker or container state is committed | Setup | unchanged | replacement workflow's artifacts unaffected | untouched | available | job failed, `setup_workflow_not_active` |

Rows 16/16a/16b/16c are the ownership rule: the recovery action is chosen by
`transition.mode`, never by the panel, and the public
`system-alignment/cancel` primitive now **refuses** Setup-owned modes instead of
offering the artifact-less bypass (internal Setup abandonment still calls
`SystemAlignmentService.cancel` after owner and worker checks). Row 21 protects
any historical state where a transition ended without its artifacts.

Rows 3, 5a and 5b are covered by §7's tests. Rows 15, 16–16d, 18, 20–26c — see
`tests/test_admin_setup_lifecycle_exclusion.py`,
`tests/test_admin_setup_cleanup_ownership.py`,
`tests/test_admin_setup_cleanup_recovery.py`,
`tests/test_admin_setup_deployment_binding.py`,
`tests/test_admin_setup_route_contracts.py`,
`tests/test_admin_workflow_abandon.py`, `tests/test_admin_setup_config_revision.py`,
`tests/test_admin_setup_preview_authority.py`,
`tests/test_admin_setup_workflow_identity.py`,
`tests/test_admin_setup_cancellation_ownership.py`,
`tests/test_admin_setup_artifact_ownership.py`,
`tests/test_admin_guided_upgrade_context_lifecycle.py`,
`tests/e2e/workflow-abandon.spec.ts`, `tests/e2e/setup-stale-config.spec.ts`,
`tests/e2e/setup-stale-apply.spec.ts`, `tests/e2e/setup-recovery-ownership.spec.ts`
and `tests/e2e/admin-restart.spec.ts`.

### 3.1 Gating reach

Two independent gates read the transition:

- `_require_alignment_resources` gates the Setup endpoints (config write/apply
  — after the workflow-identity check — plus deployment prepare/start). Guided
  Upgrade validation is additionally gated server-side by
  `_setup_owned_conflict` (409 `setup_abandon_required`), so resolving an
  unresolved Setup is the backend's demand, not a client-side workaround.
- `_reject_unrelated_transition_write` (`server.py:1749`) gates
  `/api/admin/maintenance/config/apply`,
  `/api/admin/maintenance/zendure-mqtt/migration-apply` and
  `/api/admin/config/migrate-legacy` on `is_transition_pending()`, which is true
  for **every** non-terminal transition regardless of mode or expiry.

The second gate is the user-visible failure mode: a Guided Setup that is no
longer in use leaves its transition at a non-terminal stage, so **Maintenance
can no longer save a config change**. Every escape is now a supported backend
operation — Restart setup, Discard setup, the recovery panel, a lifecycle switch
(§7.3) or Maintenance → Workflow recovery (§7.4) — and each cancels the exact
operation its owner named. Deleting `pending-transition.json` by hand is not a
supported recovery path.

`failed_recoverable` **is** in `CANCELLABLE_TRANSITION_STAGES`, and
`OperationCoordinator.abandon` is the atomic prove-inactive-then-cancel. Recovery
is therefore reachable at the store level; the remaining exposure is that
abandoning it leaves B2–B6 behind.

---

## 4. Risk verification (original audit, pre-hardening)

Historical record of what the audit found. Section 5 states which of these are
now closed; nothing here describes current behaviour.

| Risk | Verdict at audit time | Evidence |
|---|---|---|
| 1 `Start over` leaves backend state | **CONFIRMED** | `startGuidedSetupOver()` (`admin.js:8865`) issues no network request |
| 2 Pending transition gates unrelated functions | **CONFIRMED (severe)** | `_reject_unrelated_transition_write` blocks Maintenance config apply, MQTT migration apply and legacy migration for *any* non-terminal transition; Guided Upgrade is blocked separately by `_require_alignment_resources` |
| 3 Generated config survives abandoned Setup | **CONFIRMED** | B2 has no delete path anywhere |
| 4 Maintenance updates live config while stale B2 persists | **CONFIRMED** | W4 and B2 are wholly independent |
| 5 Later Setup deployment overwrites newer live config | **CONFIRMED** | W3 `_write_config`; both guards blind to live-config drift |
| 6 Recovery blocked by the state it must repair | **NOT CONFIRMED** | `failed_recoverable` is cancellable; coordinator abandon is atomic |
| 7 Divergent interpretation of persisted state | **CONFIRMED** | `detect_install_state` (marker), `_alignment_resources_allowed` (stage) and `_workspace_conflict` (generated hash) each answer "is a workflow in progress" differently |
| 8 Browser / backend / filesystem disagree | **CONFIRMED** | L1–L8 outlive logout and restart; no `sessionStorage`; B2/B3 outlive the browser |
| 9 No single owner for temporary artifacts | **CONFIRMED** | B2–B6 have creators and readers but no owner and no lifecycle |

---

## 5. Target invariants

- **I1 Workflow ownership** — every persisted temporary artifact (B2–B6) has
  exactly one workflow owner recorded alongside it.
- **I2 Safe abandonment** — abandoning a workflow removes or invalidates every
  artifact owned only by it; the running EMS and the live config are untouched.
- **I3 Config freshness** — a staged config must not replace a live config that
  changed after the staged config was created.
- **I4 Recovery independence** — recovery stays reachable when workflow state is
  invalid, incomplete or `failed_recoverable`.
- **I5 Refresh consistency** — effective workflow state is equivalent before and
  after a browser refresh.
- **I6 Restart consistency** — an Admin restart does not create a new
  interpretation of the persisted workflow.
- **I7 Single authority** — backend persisted state is authoritative for
  transition ownership and lifecycle; browser state may render it but must not
  create a conflicting workflow model.

### 5.1 Status

Scoped deliberately: the hardening covers Guided Setup, not every workflow.

| Invariant | Before hardening | After hardening | Remaining gap |
|---|---|---|---|
| I1 Workflow ownership | violated | **held** — B0 records the workflow identity; the transition `operation_id` is persisted **before** the transition is committed (§5.5); B2/B2a live in the workflow's own directory; B3 is ownership-stamped; B6 clears on its operation's terminal events; B5 is reclassified as installed-system state; the workflow's selected tag lives in B0 | B4 (`selected-release.json`) stays a shared cache by design |
| I2 Safe abandonment | violated | **held** — abandon/supersede name their workflow exactly, hold an exclusive terminal claim (§5.4), cancel only the **exact** `operation_id` they store, remove only provably owned artifacts and keep the same `workflow_id` owning whatever remains (§5.3); an unprovable owner cancels and cleans nothing; a claimed resource verification is refused as `mutation_in_progress` so a successful abandon never precedes a resource-cache write (§5.3); the public cancel primitive refuses Setup-owned modes | a failed removal is reported and blocks follow-ups, not rolled back (see 5.3 and 6.5) |
| I3 Config freshness | violated | **held for W1, W2 and W3** — the exact preview (workflow ID + preview ID + fingerprint + baseline + payload hash, §2.3) blocks a stale or cross-draft apply/write, and workflow-owned B2a blocks a stale or foreign deploy (§2.2) | — |
| I4 Recovery independence | held | held — `failed_recoverable` stays cancellable and abandonable, and every recovery action offered has a durable owner: the Setup return path is refused rather than left unowned (§5.6) | Setup recovery is Resume or Discard setup; no Setup→align-existing handoff exists yet (§6.5) |
| I5 Refresh consistency | held | held | — |
| I6 Restart consistency | held | held — B0, its preview and its `cleanup` state persist across a real process restart, tested | in-process state (P1–P5) is still lost by design, and fails closed |
| I7 Single authority | violated | **held** — mutation authority is server-issued and server-verified; the browser renders identity (L9) but cannot mint it; Restart setup / Discard / supersede clear browser state only after backend success | — |

### 5.2 User-visible workflow actions

Each action names exactly what it does, and appears only where it is accurate.

| Action | Where | Endpoint | Removes | Leaves untouched |
|---|---|---|---|---|
| **Restart setup** | Guided Setup header | `POST /api/setup/abandon` (workflow-verified), then a fresh workflow via start-path | setup draft, generated config, deployment plan, setup progress; returns to step 1 | installed EMS, live config, runtime data, containers, volumes, backups |
| **Discard setup** | recovery panel, `fresh_install`/`automated_setup`; the stale-conflict panel; the Setup-to-Upgrade conflict dialog | `POST /api/setup/abandon` (workflow-verified) | same as above | same as above |
| **Cancel upgrade** | recovery panel, `guided_upgrade` | `POST /api/admin/system-alignment/cancel` | the transition and **its own operation's upgrade context (B6)** | running EMS build, live config, backups, known-good (B5), **and Setup artifacts — it does not own them** |
| **Return to running build** | recovery panel, **non-Setup modes only** | `POST /api/admin/system-alignment/return-to-running-build` | the failed transition; starts an `align_existing_install` one for the known-good build | live config, backups, known-good (B5). **Not offered for `fresh_install`/`automated_setup`** — the new operation would have no durable owner (§5.6) |
| **Review current configuration** | preview-conflict panel | `POST /api/setup/config-preview` | nothing | the draft; earns a new exact preview |
| **Open current setup** | workflow-conflict panel (old tab) | `GET /api/setup/workflow` | nothing | the local draft; adopts the current workflow identity |
| **Discard local draft** | workflow-conflict panel (old tab) | none (local) + `GET /api/setup/workflow` | only this tab's local draft | the newer session's server-side setup |
| **Retry cleanup** | after `abandon_cleanup_incomplete` (also after a reload, while `cleanup.state` is `pending`) | `POST /api/setup/abandon` with the **terminal workflow's exact id** | the owned artifacts that remain | live config and running EMS |
| *(no action)* | after `setup_artifact_review_required` | — | nothing — the artifacts are kept for operator review | everything |

An unknown transition owner is offered **no** destructive action, and the
public cancel primitive refuses it (`transition_cancel_unsupported`).

### 5.3 Terminal and cleanup ownership contract

**Terminalization requires the exact workflow.** `POST /api/setup/abandon` needs
`setup_workflow_id` whenever B0 exists; a missing id is 409
`setup_workflow_required` and changes nothing. There is no "abandon whatever is
stored" shortcut. The legacy no-id path exists only for installs that predate B0,
and it can neither adopt nor delete a workflow-owned artifact.

**Successful terminalization does not imply cleanup completion.** The two are
separate facts and are reported separately: B0 goes terminal (mutation and
preview authority revoked immediately) while its `cleanup` records what is still
on disk.

**A workflow cancels only the exact operation ID it stores.**
`transition_ownership(record, transition)` (`admin/setup_workflow.py`) is the one
verdict, and it is strict:

| Situation | Verdict | Abandon / cleanup-retry result |
|---|---|---|
| no transition, or its stage is `completed`/`cancelled` | `none` | nothing to cancel; **cleanup proceeds** |
| Setup-mode transition, B0's `operation_id` **is** its operation id | `owned` | cancel, then clean up |
| Setup-mode transition, B0 names **no** `operation_id` | `unproven` | 409 `setup_transition_owner_unproven` — **nothing cancelled, nothing cleaned** |
| Setup-mode transition, B0 names a **different** `operation_id` | `mismatch` | 409 `setup_transition_context_mismatch` — nothing cancelled, nothing cleaned |
| the active transition is **not** Setup-owned (e.g. `guided_upgrade`) | `unproven` | 409 — a Setup owner never terminates or cleans around a foreign transition |

> **A Setup-owned mode is never proof of transition ownership.** The mode only
> *classifies* a transition. Only the exact `operation_id` identifies which
> workflow started it.

An unproven or mismatched owner fails **closed on both halves**: a workflow that
cannot name the transition also cannot know which artifacts belong to it, so it
must not delete any. The one exception is the documented pre-workflow path: with
**no** B0 record at all (and therefore no submitted id) there is no workflow that
could be named and no newer workflow whose state could be lost, so a Setup-mode
transition is still cancellable. That is also the operator escape hatch if a
pre-hardening install ever presents an unlinked workflow beside an active
transition: remove `state/guided-setup-workflow.json`, then discard.

**An owned cancel can still be refused while its operation is mutating.**
Ownership decides *whether* this workflow may cancel; the transition store
decides *whether now is a safe moment*. `PendingTransitionStore.cancel()` refuses
a non-expired transition outside `CANCELLABLE_TRANSITION_STAGES`, and — the case
the visible stage hides — also while a **resource verification is claimed**:

| Stage | `resources_claimed_at` | Cancellable | Why |
|---|---|---|---|
| `admin_aligned` | absent | yes | nothing external is running |
| `admin_aligned` | present | **no** — `mutation_in_progress` | the claim is taken *before* `import_into_cache` and the stage advances only *after* it returns, so the shared resource cache is being written under a stage that still reads `admin_aligned` |
| `resources_verified` | present | yes | the import finished; the claim is history |
| `failed_recoverable` | present | yes | the attempt is over and a retry clears the claim |
| any non-terminal stage, **expired** | any | yes *by the durable store* — but see the worker rule below | expiry closes every forward path; without this the record would wedge the store permanently |

`SystemAlignmentService.status()` reports `cancel_available: false` for that
window, so the console does not offer an action the store will refuse, and
`POST /api/setup/abandon` returns the same actionable 409 `mutation_in_progress`
and **cleans nothing** — B0 stays `active` and its artifacts stay on disk. The
claimed verification then finishes or fails under its own claim, and the abandon
succeeds on retry. Both resource strategies (embedded bundle and release
archive) take the same claim, so both are covered by the same gate.

**Expiry is not a worker verdict.** The durable store deliberately bypasses the
`resources_claimed_at` gate once the TTL passes: an orphaned record left behind by
a crashed Admin must stay escapable, and the marker alone cannot distinguish
"crashed mid-import" from "still importing". So the *live* worker is proven
separately, through the same `OperationCoordinator` (P1) that already gates
abandonment. Every execution of `verify_resources` holds that operation's claim
for its whole duration — taken before the durable claim, released in `finally` on
success and on failure alike — and a claim refused because abandonment already
won means the importer never starts at all:

| Expired | Live coordinator worker | Recovery cancellation |
|---|---|---|
| no | any | as the table above (`mutation_in_progress` while claimed) |
| yes | **yes** | **refused** — 409 `transition_worker_active`, nothing cancelled and nothing cleaned |
| yes | no | allowed — the recovery escape |
| yes | unreadable liveness | refused — `transition_worker_status_unavailable` (fails closed) |

The claim is bound to the service once, at construction, rather than passed by
each caller, so it covers every path into the importer: the shared
`verify-resources` route, `confirm_setup_build` / `prepare_setup_resources`,
`_start_resolved_system_alignment`, the resume helper and the Guided Upgrade
reconnect advance. `import_into_cache` has exactly one caller and it is inside the
claim, so there is no route-specific way to reach the importer unregistered.

**After an Admin restart the escape returns.** Coordinator state is in-process
(P1), so a restarted Admin holds no claim: an expired transition with a *stale*
durable `resources_claimed_at` reports `worker_active: false` and stays
abandonable. That is the intended asymmetry — a claim can only be missing when the
process that held it is gone, and it can only be present while a mutation is
genuinely live.

A successful abandon can therefore never precede a resource-cache mutation from
the operation it abandoned, expired or not.

**Cleanup is best-effort per owned artifact; ownership never is.**

Ownership is two independent decisions, and they must not be conflated:

| Stage | Source of truth | Question |
|---|---|---|
| Claim authority | the durable workflow record's `artifacts` map | is this artifact this workflow's responsibility at all? |
| Deletion proof | sidecar metadata, marker content, canonical path identity | is removing it safe? |

**Known path existence alone never creates workflow ownership.** A workflow with
no artifact claims ignores the installed system's pre-existing files: they are
not read as leftovers, not deleted, and never reported as unresolved. Before
this split, an abandoned workflow that had created nothing still inspected
`<admin_data>/generated/config.json` and `<admin_data>/state/.admin-deployment.json`,
could not prove it owned them (correctly — it did not), and left a permanent
`review_required` that blocked every later Setup and Guided Upgrade.

Two artifacts are in scope without a record claim, and only because nothing else
can own them:

- `workflows/guided-setup/<id>/` — namespaced by workflow id and proven by path
  identity, so it can hold nothing but this workflow's files, including one
  written in the crash window before its claim was persisted;
- an artifact whose own sidecar or marker content names *this exact workflow* —
  content that specific is itself the claim, and it is exactly what a foreign or
  installed-system file never carries.

`review_required` is reserved for an artifact the workflow claimed (or proved it
owns) but cannot prove safe to delete.

| Artifact | Removed only when | Otherwise |
|---|---|---|
| `workflows/guided-setup/<id>/` | its realpath **is** `<admin_data>/workflows/guided-setup/<matching id>` — traversal-shaped ids, symlinks and foreign directories are rejected | kept, `review_required` (`setup_artifact_owner_mismatch`) |
| `<admin_data>/state/.admin-deployment.json` | the validated marker content carries `owner: "guided_setup"` **and** the matching `workflow_id` | kept, `review_required` — a malformed, unowned, foreign or pre-ownership marker stays on disk |
| `<admin_data>/generated/config.json` (+ sidecar) | its sidecar validates and names this exact workflow as owner | kept, `review_required` (`generated_config_review_required`) — a sidecar-less or malformed legacy config is **never** deleted and **never** auto-adopted |

Each entry is reported as `removed`, `absent`, `failed` or `review_required`, and
the attempt continues after a failure. The two outcomes are distinct because the
right next action differs:

| Outcome | `cleanup.state` | HTTP | Error | Retry helps |
|---|---|---|---|---|
| a removal this workflow owned failed | `pending` | 500 | `abandon_cleanup_incomplete` | yes — converges |
| an artifact's owner could not be proven | `review_required` | 409 | `setup_artifact_review_required` | no — an operator decides |
| everything owned is gone | `complete` | 200 | — | n/a |

**Unfinished cleanup keeps the workflow the owner.** While `cleanup.state` is
`pending` or `review_required`:

- B0 keeps the **same `workflow_id`** — it is the only ownership record for the
  files left behind, so `ensure_active()` refuses to mint a replacement;
- `POST /api/admin/start-path` (`setup_new`) returns 409 `setup_cleanup_required`
  and issues **no** setup intent and **no** workflow;
- Guided Upgrade **validation and execution** both return 409
  `setup_cleanup_required`;
- retry cleanup must present that exact `workflow_id`; a second retry is
  idempotent;
- the state survives an Admin restart (B0 is durable) and a browser reload (the
  browser keeps the id for the retry but drops the preview);
- `GET /api/setup/workflow` exposes a redacted summary — state, counts and
  per-artifact `{kind, status}` only. No absolute paths and no OS error strings
  ever enter B0 or that view; server logs keep the technical detail.

The frontend treats `ok !== true` as a failed reset: it keeps the local draft and
never shows the completed-reset message.

**Recovering records stranded by path-inferred ownership.** A terminal record
whose `cleanup.state` is `review_required` while it claims *no* artifact, and
whose unresolved entries name only the global locations a workflow cannot own
without a claim (`legacy_generated_config`, `legacy_generated_metadata`,
`deployment_marker`), is stale bookkeeping from the pre-claim cleanup. Every
authoritative read — the workflow view, the Setup/Upgrade conflict check and
workflow creation — reconciles it to `complete`. The reconciliation touches no
file: it changes the record and nothing else, so the installed system's generated
config and deployment marker (and with it `install_state: admin_prepared_install`)
stay byte-exact.

A review state that names a claimed artifact, or this workflow's own directory,
is a genuine ownership question and is never reconciled. For those the UI offers
**Recheck setup cleanup**, which re-runs the same exact-id abandon route: the
backend re-evaluates ownership under the claim-aware plan and either converges or
keeps the review. It never overrides ownership and never deletes anything an
owner could not be proven for.

### 5.4 Mutation / termination exclusion

`SetupLifecycleCoordinator` (`admin/setup_lifecycle.py`) is the one arbiter, shared
by the HTTP and HTTPS listeners through `AdminRuntime`. Exactly one of these two
statements is true of a Guided Setup mutation at any moment:

> it owns the workflow until its irreversible work has finished, **or** the
> workflow was abandoned/superseded before it started.

- Claims are non-overlapping per workflow: `system_build_update_admin`,
  `system_build_confirm`, `setup_release_prepare`, `config_write`, `config_apply`,
  `deployment_prepare`, `deployment_start`, `permission_repair`,
  `container_conflict_resolution` (mutations) and `abandon`, `supersede`,
  `cleanup_retry`, `complete` (terminal).
- A terminal claim cannot start while a mutation claim is held → 409
  `setup_operation_in_progress`, carrying only the operation kind. Nothing is
  cancelled and nothing is removed.
- A mutation claim cannot start once terminalization has begun → 409
  `setup_workflow_not_active`, *before* the config is serialized and before any
  credential is staged. A successful abandon is therefore a hard barrier: no
  config write, credential promotion, artifact binding, deployment preparation,
  compose write, marker write, permission repair or container action can commit
  under that workflow afterwards.
- A rejected claim modifies no durable state. Claims release on success and on
  exception alike; a terminal claim that raised rolls its barrier back, because a
  termination that changed nothing must leave the workflow mutable.
- **Asynchronous work is inside the claim.** `deployment/prepare` and
  `deployment/start` establish the claim before the handler answers 202 and hand
  it to the worker, which releases it when it settles (success *or* exception).
  So abandon/supersede are refused while a worker can still write, and the worker
  carries an **immutable** workflow identity: it re-checks that identity before the
  workspace write and again before the marker write, and the marker is stamped
  from that carried identity — never from a fresh "which workflow is active now"
  lookup.
- **Restart fails closed.** Claims are in-process (P5). After a restart nothing
  is claimed, so no phantom worker blocks recovery; the durable checks (B0 status,
  B2a ownership, B3 `owner`/`workflow_id`) are what still refuse orphaned work.

### 5.5 Setup transition creation and ownership

A Setup System Build transition may be **created, resumed or changed only by the
exact active Guided Setup workflow that owns the authorizing setup intent**, and
it must never become active after that workflow was abandoned or superseded.

The four creation routes therefore run one fixed order:

```text
parse request
→ require the exact active workflow (setup_workflow_id, never "whichever is stored")
→ acquire the lifecycle mutation claim for that workflow
→ prove ownership of any already-active transition (§5.3 verdicts)
→ claim the workflow-bound one-shot setup intent
→ resolve/validate the System Build
→ pre-commit: persist operation_id + mode + selected tag into B0
→ commit the transition / launch the Admin replacement
→ release the claim
```

The claim comes **before** the intent on purpose: a refused claim or an unprovable
transition owner then leaves the user's one-shot confirmation unspent, so the
retry after the blocking operation finishes needs no re-confirmation. The intent
is still consumed atomically, exactly once, the moment the request is authorized.

**Operation linking is not best-effort.** The link runs inside
`SystemAlignmentService.start_resolved(..., pre_launch=…)` — and, for
`confirm_setup_build` / `prepare_setup_resources`, the same callback threaded
through to the same primitive. That boundary guarantees:

```text
operation id minted
→ workflow link persisted
→ transition committed
→ optional Admin launcher invoked
```

That sequence crosses **two** durable boundaries, and they are classified
separately — neither the pair nor the whole operation is atomic:

| Boundary | Durable write | Decides |
|---|---|---|
| **B0 link → B1 transition creation** | `PendingTransitionStore.begin` writes B1 | whether the `pre_launch` undo may compensate B0 |
| **B1 `admin_update_pending` → B1 `admin_reconnect_pending`** | `PendingTransitionStore.advance` rewrites B1 | whether the Admin replacement may be launched |

Both use the same rule — the durable record, never the exception, is the
outcome — and the same operation-identity proof (below). The second boundary is
not reached at all when the Admin is already aligned: that transition is
committed straight to `admin_aligned` and no launcher exists for it.

The boundary has two failure sides, and both end with B0 and B1 agreeing.

**Link failure before the commit.** A persistence failure raises
`SetupTransitionLinkError` inside the boundary, so **no transition is committed
and no Admin replacement is launched**. The route answers 500
`setup_transition_link_failed`, B0 stays `active` with `operation_id: null`, and
a retry starts cleanly once the Admin data directory is writable again. An
unownable transition must never come into existence — that state is precisely
what §5.3 has to refuse afterwards.

**Commit failure after the link.** The link is written first, so a failure of
the transition commit that follows it is the one window where B0 could name an
operation that never reached B1. `pre_launch` therefore returns an **undo** — but
the exception alone never authorizes it. `begin()` writes through `os.replace`
and then still has work to do (leaving its file lock, returning through any
instrumented wrapper), so an exception can be raised with the exact operation
*already durable*. Compensating there would delete the owner of a live
transition. `_start_resolved` therefore classifies the exact operation against
the durable state before it decides:

```text
PendingTransitionStore.begin raises
→ read B1 once (SystemAlignmentService._commit_outcome)
   not_committed  the exact operation is absent from B1
                  → undo: compare-and-restore the exact previous B0 reference
                  → raise the store error (400 transition_state_write_failed)
   committed      B1 holds the exact operation id and its transition identity
                  → no undo; B0 keeps ownership
                  → continue the normal post-commit path exactly once
   unprovable     B1 cannot be read, or names the operation with a different
                  identity
                  → no undo, nothing launched
                  → 500 transition_commit_unprovable
```

**The exact transition identity.** "The same operation" is the operation id
*plus* the canonical immutable projection of the record —
`transition_identity()` over `TRANSITION_IDENTITY_FIELDS`
(`admin/admin_update.py`). That projection is derived by **exclusion**: every
field of `TransitionRecord` except the eight the store actually mutates
(`TRANSITION_MUTABLE_FIELDS`: `stage`, `updated_at`, `admin_update_claimed_at`,
`resources_claimed_at` and the four recoverable-failure fields `failed_stage`,
`resume_stage`, `error_code`, `error_message`). So identity is not "the
selected build": it also covers `request_fingerprint`, `admin_alignment_required`,
`compatibility_mode`, `resource_strategy`, the orchestrator Admin identity, the
development acknowledgement and its tag, and `created_at` / `expires_at` /
`next_step` / `resume_path` — all fixed by `make_transition_record` and never
rewritten afterwards. A field added to the record is therefore immutable
identity unless it is explicitly declared mutable, which fails closed rather
than open.

That matters beyond bookkeeping: a durable record that shared the operation id
but carried a different `resource_strategy` would have the transition prepare
resources through a different provider than the one the caller resolved and
linked. Any such mismatch is `unprovable` — never blindly compensated, never
continued.

`committed` is a real outcome, not a retry: the transition exists, so the
post-commit path (stage advance and, when alignment requires it, the single
Admin launcher invocation) continues and any later failure keeps the committed
link like every other post-commit failure. A retry then *resumes* that exact
operation instead of minting a second one.

**Any** operational failure of that commit is classified, not only an
already-normalized `TransitionStateError`: the real writer reaches `os.replace`,
so a full or read-only disk surfaces as a raw `OSError` straight out of `begin()`.
`_start_resolved` catches normal exceptions there (never `BaseException`). For a
proven non-commit it invokes the undo exactly once and reports through the same
stable contract — a store error keeps its own `reason`, an `OSError` becomes
`transition_state_write_failed`. The raw filesystem error never reaches the route
or the browser. An exception that is neither is not an ordinary state-write
failure: the compensation still runs (the durable proof is what authorizes it),
but the error keeps its own type rather than being reported as one.

**Operator recovery for `transition_commit_unprovable`.** The durable transition
state could not be read back, so the server cannot say whether the operation was
started — and refuses to guess in either direction: nothing is launched, no
workflow ownership is removed, no artifact is cleaned. B0 keeps naming the
operation, which is what keeps §5.3 able to refuse an adoption afterwards. The
recovery is manual and read-first: check the Admin data directory
(`state/pending-transition.json` and its lock file — permissions, free space,
mount state), then read that file to decide. If it names the operation the
workflow names, the transition is real: reconnect or resume it, or Discard setup
to cancel it as one owned unit. If it is absent or corrupt, discard the setup and
start it again; the workflow record is never rewritten by this failure, so it
still identifies whatever is on disk.

**Reconnect-stage failure after the commit.** When alignment is required, the
committed transition still has one durable write before anything is launched:
`advance` moves B1 from `admin_update_pending` to `admin_reconnect_pending`. The
order is deliberate — the sidecar may begin immediately and must always find a
state it can fail recoverably — and it has the same post-commit window as
`begin()`. Treating the exception as failure skipped the launcher while
`admin_reconnect_pending` was already durable, so B1 claimed a replacement Admin
was expected that had never been started, and a retry read that stage and
reported it without launching. `_start_admin_replacement` classifies it the same
way:

```text
PendingTransitionStore.advance raises
→ read B1 once (SystemAlignmentService._stage_commit_outcome)
   committed      B1 is the exact operation at admin_reconnect_pending
                  → continue the post-stage path: launch exactly once
                  → normal result, or the unchanged launcher-failure contract
   not_committed  B1 is the exact operation still at admin_update_pending
                  → launch nothing; B0 keeps ownership
                  → the stable store failure (400 transition_state_write_failed)
   unprovable     B1 cannot be read, holds another operation, has a different
                  identity, or a stage that is neither
                  → launch nothing, remove no ownership, start no second
                    transition
                  → 500 transition_stage_commit_unprovable
```

`not_committed` stays retryable *because* the launcher runs strictly after the
stage write: a transition parked at `admin_update_pending` provably launched
nothing. `start_resolved` therefore resumes such a record — same operation id,
same owner, no second transition — performs the missing stage write and the one
launch. Once B1 reads `admin_reconnect_pending` the launch is considered done
and a *later* retry only reports it. The worker side holds the last guard:
`claim_admin_update` accepts one claim, and only at `admin_reconnect_pending`.

**One dispatch per operation, including under overlap.** Classifying the stage
serializes *sequential* retries; it does not serialize two that overlap. Both
read `admin_update_pending`, both enter the replacement start, one advances the
stage and the other sees the advance already done and returns idempotently — and
then both invoked the launcher. The second dispatch is the real Docker sidecar
name collision, so it raised, and the losing request marked the transition
`failed_recoverable` with `admin_update_launch_failed` while the first
replacement was already running. Guided Setup routes hold a lifecycle mutation
claim (§5.4) and were already serialized; Guided Upgrade runs on
`ThreadingHTTPServer` with no equivalent claim, so two browser tabs, a replayed
request or a network retry reached it.

Three ownerships now guard the replacement, and they answer different questions:

| Ownership | Where | Survives restart | Decides |
|---|---|---|---|
| durable stage | B1 `admin_update_pending` → `admin_reconnect_pending` | yes | whether a replacement is expected at all |
| transient dispatch | `ReplacementDispatchCoordinator` (P6, `admin/replacement_dispatch.py`) | no | which concurrent caller performs a dispatch attempt's one launcher call |
| worker-side Admin update | `claim_admin_update()` in the sidecar | yes | which sidecar may act on the transition |

`_start_admin_replacement` is the **only** start path to the launcher: `start`,
`start_resolved`, `confirm_setup_build`, `prepare_setup_resources` and
`return_to_running_build` all reach it through `_start_resolved`. `retry()` is the
second entry, `_retry_dispatch_attempt`, and both reach the launcher through the
same `_commit_reconnect_and_launch`, so there is exactly one `self._launcher(...)`
call site in the service. Each holds an exclusive dispatch claim, keyed by
operation id, around the whole sequence — re-read B1, decide, advance the
reconnect stage, invoke the launcher, publish the result. A concurrent caller
blocks (it must answer the authoritative transition, not "try again"), then:

```text
dispatch already completed in this process
   launch succeeded  → re-read B1 under the claim and report that transition
                       (never failed_recoverable, never a second launcher call)
   launch failed     → report the dispatching caller's exact
                       admin_update_launch_failed, attempt nothing
no dispatch completed
   → re-read B1 under the claim; only the durable stage authorizes a launch
     admin_update_pending       → this caller owns it: advance/classify the
                                  stage, launch exactly once
     admin_reconnect_pending    → already handed off → report that transition,
                                  launch nothing
     failed_recoverable         → raise its durable error_code, launch nothing
                                  (explicit retry stays the way forward)
     any later stage            → report that transition, launch nothing
     missing/unreadable/foreign → fail closed, launch nothing
                                  → 500 transition_stage_commit_unprovable
```

**Why the claim alone is not enough.** The claim is transient and is dropped as
soon as its last live caller leaves it. A request that read `admin_update_pending`
and was then descheduled *before* entering the coordinator arrives at an **empty**
claim: nothing in this process still records that the single dispatch happened,
and `advance()` answers the already-committed edge idempotently rather than
refusing — so that request launched a second replacement, with the same operation
id, and the Docker sidecar name collision then marked a `failed_recoverable` over
a replacement that was already running. Only B1, re-read under the claim, still
knows. The claim serializes the callers that overlap in time; the durable stage
covers the ones that do not.

**An explicit retry is a new dispatch attempt.** A claim covers one *attempt*,
and the outcome an accepted retry owes the operator is a **new** launcher call —
so it may never be answered from the attempt whose failure it is recovering
from. That attempt's entry is alive for as long as it still has waiters, and the
first fix reopened `admin_update_pending` durably *before* entering the claim,
walked into that live entry, was handed its published
`admin_update_launch_failed`, launched nothing, and left the operation stranded
at a reopened `admin_update_pending`: accepted retry + reopened durable state +
no dispatch.

The recovery edge therefore belongs **inside** the retry's own attempt:

```text
own a retry attempt (owned_retry)
→ re-read B1 under it and prove the exact immutable operation identity
→ take the atomic recovery edge (failed_recoverable → resume_stage), which is
   what decides that this stage may be reopened at all
→ admin_update_pending? advance to admin_reconnect_pending and launch once
   anything else?         report that transition, launch nothing
→ publish this attempt's outcome
```

`owned_retry` **detaches** a settled attempt instead of answering from it, and
detaches rather than clears:

| Caller | Attempt | Receives |
|---|---|---|
| the old owner and its waiters | the settled one | the old attempt's published outcome, unchanged |
| the retry that detached it | a new one | the new launcher call's outcome |
| a second retry, or a start/resume caller that reads the reopened stage | the same new one | the retry attempt's outcome — it launches nothing itself |

Detaching happens once, under the registry guard, so two simultaneous retries
share one new attempt and one launch. The old entry is not retained: it
disappears with its own last waiter, exactly as before, so nothing accumulates
per operation and a retry that really fails is recorded `failed_recoverable` and
stays explicitly retryable — the next retry detaches *that* settled attempt in
turn. A missing, unreadable or foreign B1 fails the retry closed under its claim
with `transition_stage_commit_unprovable`; a stage that may not be reopened is
refused by the store's own edge (`invalid_transition`, `not_resumable`,
`expired`). Nothing is reopened and nothing is launched in either case.

The stage is authoritative in **one direction only**: it may withdraw a launch,
never demand one. `admin_reconnect_pending` is still not proof that a dispatch
happened — the stage can become durable without one (the post-commit case, and
the crash window below) — which is why nothing ever relaunches from it. The one
caller that may launch on an already-committed reconnect stage is the caller that
committed it *itself*, inside its own claim, and caught the exception after the
write; that is `_stage_commit_outcome`'s classification, and it happens between
the authority read and the launcher call.

The claim records the outcome of the **launcher call**, not of the stage write.
A failure raised before the launcher — a proven non-commit, an unprovable stage
— dispatched nothing, publishes nothing and leaves the next caller free to
perform the single dispatch, so both remain exactly as retryable and as
fail-closed as above.

The claim never holds the transition-store file lock while Docker runs, and it
is released on success and on exception alike.

**Not crash-atomic (known limitation).** Classification covers a failing
`advance` and the dispatch claim covers concurrent callers in one process —
neither covers a dying process. The claim is in-process state (P6) and is gone
after a restart, by design: nothing may pretend a pre-restart dispatch is still
owned. If the Admin is killed after
`admin_reconnect_pending` is durable but before the launcher has started the
detached sidecar, nothing relaunches it: the Admin has no startup transition
reconciler, `retry()` only relaunches from `failed_recoverable`, and the resume
route verifies the *replacement* Admin identity, which the un-replaced Admin
fails. `admin_reconnect_pending` is not in `CANCELLABLE_TRANSITION_STAGES`, so
the escape is expiry: after `DEFAULT_TRANSITION_TTL_SECONDS` (1 h) the record may
be cancelled from any non-terminal stage, and the detached sidecar holds no local
worker claim, so an expired orphan reports inactive and Discard setup / abandon
succeeds. Until then the operation waits. Closing this would need a durable
"launcher dispatched" marker plus a restart-time reconciler — see §6.5.

The restore is `GuidedSetupWorkflowStore.restore_transition_link`, a
compare-and-restore that writes only while B0 still names the operation this
attempt wrote. The replaced value is read under the same store lock as the write
(`link_transition`), so it can never be sampled stale, and a compensation that
lost the race to a newer successful link changes nothing. For the normal first
creation the restored value is `null`; B0 keeps its selected tag and mode
(selection metadata, not ownership), stays `active` and stays retryable, and no
Admin launcher ran.

**The compensation reads strictly.** Ordinary authority reads (`load()`,
`active()`, `require_active()`) fail closed: an unreadable, oversized, malformed or
foreign-shaped record reads as `None`, because refusing a mutation is the safe
answer there. For a compensation that same `None` would be a *wrong* answer — it is
indistinguishable from a genuinely stale compare-and-restore, but the conclusion is
the opposite. `restore_transition_link` therefore uses an internal strict read,
under the same store lock:

| Durable B0 state during compensation | Result |
|---|---|
| names a different operation | harmless stale no-op — nothing is written |
| belongs to a different workflow | harmless stale no-op — nothing is written |
| absent | no-op — no record exists that could name a stale operation |
| unreadable / oversized / malformed / invalid shape | `GuidedSetupWorkflowReadError` → 500 `setup_transition_link_unreconciled` |

A record that is present but unusable is never cleared, replaced or reconstructed —
it is the only ownership proof for whatever that workflow left on disk.

The undo runs **only** for a proven non-commit. Once the transition is durable —
whether `begin()` returned or failed after committing — the link is correct, so a
failing Admin launcher, a failing response or a failing reconnect all keep the
committed `operation_id` — the workflow must stay able to resume or abandon the operation
that does exist. If the compensating write or its strict read fails, the route
answers 500 `setup_transition_link_unreconciled` and launches nothing: nothing was
started, but B0 and B1 genuinely disagree, so the operator is told to discard and
restart the setup rather than being shown a "nothing happened" that is not quite
true.

**A resume is not an adoption.** When a non-terminal transition already exists,
`_start_resolved` reuses it instead of creating one, so the creation routes verify
ownership *before* calling System Alignment: `unproven` and `mismatch` are refused
with 409 and the transition is never advanced (no resource verification, no stage
change). Mode and build context are still matched by System Alignment itself
(`transition_active` / `transition_context_mismatch`).

**Setup intents are workflow-bound.** `SetupIntent` carries
`(intent_id, session_id, workflow_id, action, install_state_fingerprint)`. `issue()`
requires the active workflow id; `validate()` and `claim()` require the same one and
answer `setup_intent_workflow_mismatch` otherwise. Abandon, supersede and
completion invalidate **every remaining intent for that workflow in every
session** — so two browsers in one Fresh Setup can each hold a confirmation, but
neither confirmation survives the workflow it was issued for. Issuing a second
intent for the same workflow in another session grants neither session authority
over a later workflow.

A retired intent leaves a short-lived tombstone, so a stale copy is answered
`setup_intent_workflow_mismatch` rather than a bare unknown id. Re-confirming in
the same session clears that session's tombstones (pre-existing behavior), after
which the same stale copy reads as `setup_intent_required`. All three refusals are
409, authorize nothing, and land in the same browser recovery surface — which of
them a caller sees depends on whose session terminalized the workflow, never on
what it may change.

### 5.6 Setup return-to-running contract

`POST /api/admin/system-alignment/return-to-running-build` is **refused for a
Setup-mode transition** with 409 `setup_return_unsupported`. Guided Upgrade,
align-existing and every other mode keep the unchanged operation-id-only
contract.

The primitive is two durable steps:

```text
cancel the failed operation   (B1 → cancelled)
start a new align_existing    (B1 → a new operation id)
```

Nothing carries the Guided Setup workflow across that gap. Held open, an
abandon terminalizes B0 between the two steps and **both** sides report success,
leaving an `align_existing_install` transition whose owner no longer exists: B0
still points at the cancelled operation, no record names the new one, and no
workflow can complete, resume or abandon it. Even without the race, the
successful path has no defined answer for who owns the new operation id, when
the Setup artifacts are cleaned, when its intents are invalidated, or how any of
that resumes after an Admin restart.

The alternative would be an explicit durable Setup→align-existing handoff. For
this RC the unsafe path is disabled instead, because a handoff without a durable
owner is worse than no handoff: a Setup-owned recovery already has two actions
that *do* have an owner —

- **Resume** — the transition's own retry path (`resume_stage`), owned by B1;
- **Discard setup** — `POST /api/setup/abandon`, owned by B0, which also removes
  the workflow's temporary files.

The refusal lives in `SystemAlignmentService.return_to_running_build`, before the
first durable step, so it covers every caller rather than one HTTP route.
`status()` stops reporting `return_available` for Setup modes and the recovery
panel hides the action entirely — including when a stale payload still claims
`return_available`, and in the click handler itself, so a refused action can
never be sent from a rendered console.

**Audited alongside it** (`resume`, `verify-resources`, the return primitive):
after a successful `POST /api/setup/abandon` the record is `cancelled`, which is
terminal, so every forward edge — `retry`, `resume_after_admin_reconnect`,
`claim_resource_verification`, EMS recovery — and the return refusal already fail
closed on the durable state alone. None of these routes needs an added workflow
claim; the transition store's own terminal check and CAS stage gates provide the
exclusion centrally.

---

## 6. Implemented hardening and deferred work

### 6.1 Release hardening (implemented)

Smallest coherent change scoped to Guided Setup, without a second workflow
architecture. Per-invariant status is in section 5.1.

1. **One owner for Setup's temporary artifacts** — `admin/setup_workflow.py`
   introduces `SetupWorkflowArtifacts`, the only module that resolves, inspects
   and removes B2 (generated config + its new metadata sidecar) and B3
   (deployment marker).
2. **One backend abandon operation** — `abandon_setup_workflow(...)` plus
   `POST /api/setup/abandon`. Idempotent. Cancels the transition **only** when it
   is setup-owned (`fresh_install` / `automated_setup`) and non-terminal, so a
   Guided Upgrade transition is never adopted. Removes the owned artifacts, never
   touches the live config, the EMS or shared release resources, and returns the
   resulting authoritative state. A live worker still blocks the cancel through
   the existing `OperationCoordinator`, and nothing is deleted when the cancel is
   refused.
3. **Stale generated-config protection** — `ConfigExportService.write` records
   `base_config_revision` into B2a; `DeploymentService.prepare` refuses with
   `stale_generated_config` (409) before any workspace write. The full
   presence/absence contract is in section 2.2.
3a. **Stale Setup-mutation protection** — the preview returns the live revision
   state, the browser stores it beside its draft, and W1/W2 refuse a mutation
   whose baseline no longer matches (section 2.3), before any credential is
   staged.
3b. **Owner-routed recovery** — the recovery panel picks its action from
   `transition.mode`, so a Setup-owned recovery discards its artifacts and an
   upgrade cancellation keeps the narrower semantics it actually provides
   (section 5.2).
4. **Consistent frontend reset** — `startGuidedSetupOver()` is async, calls the
   abandon endpoint first, and clears browser state only after the backend
   confirms `ok: true`. A refusal keeps the draft and active step, shows the
   error, and calls `resumeGuidedSetupLifecycle()` to re-read the authoritative
   status and restart the polling the active step needs — so a refused reset
   cannot leave the wizard frozen. Both job pollers clear their own timer and
   capture the current generation, so recovery cannot duplicate a timer or
   revive a superseded request.

### 6.2 Server-owned workflow authority (implemented)

The follow-up architecture the first pass deferred:

1. **Durable workflow identity** — `admin/guided_setup_workflow.py` persists one
   Guided Setup workflow record (B0): opaque server-generated `workflow_id`,
   lifecycle status, transition link (`operation_id`, mode, selected tag), the
   exact preview authority and the owned artifact paths. Atomic
   fsync-and-replace writes; fail-closed validation on read; no drafts, secrets
   or transition stage inside. `POST /api/admin/start-path` (`setup_new`)
   returns the active `setup_workflow_id`; `GET /api/setup/workflow` exposes a
   redacted view.
2. **Exact preview authority** — section 2.3. The browser-held `config_revision`
   is no longer trusted from the browser; a preview for A cannot authorize B,
   credential-affecting changes invalidate the preview, and rejected mutations
   stage nothing.
3. **Workflow-owned artifacts** — section 2.2. Generated config + metadata live
   under `workflows/guided-setup/<workflow_id>/`; the deployment marker keeps
   its install-state-contract path but is ownership-stamped and start-verified.
   Legacy sidecar-less artifacts require regeneration
   (`generated_config_review_required`) — a one-time consequence after
   upgrading mid-Setup, never a silent deploy and never a silent delete.
4. **One lifecycle owner for Setup termination** — abandon (restart/discard,
   workflow-verified), `POST /api/setup/system-build/supersede` (build change),
   and the Setup-to-Upgrade conflict all run through the backend owner;
   `POST /api/admin/system-alignment/cancel` refuses Setup-owned modes with
   `setup_abandon_required` and unknown modes with
   `transition_cancel_unsupported`. No frontend Setup path calls the primitive.
5. **Guided Upgrade context lifecycle** — `clear_for_operation` binds cleanup to
   the operation: Cancel upgrade and a completed upgrade (after the durable
   known-good + `completed` stage) clear exactly their own context;
   `failed_recoverable` and refused cancels keep it. Known-good (B5) survives
   every workflow cancellation.

### 6.3 Terminal and cleanup ownership (implemented)

What 6.2 left open: authority was *verified* once but never *held*, and a
terminalized workflow stopped owning what it had left behind.

1. **One lifecycle coordinator** — `admin/setup_lifecycle.py`, shared by both
   listeners through `AdminRuntime`. Mutation and terminal claims are mutually
   exclusive per workflow, so an Apply that passed verification keeps the workflow
   until its commit is done, and a successful abandon is a hard barrier against
   every later commit — including credential staging. Full semantics and the
   operation set: section 5.4.
2. **Exact identity for every destructive action** — `POST /api/setup/abandon`
   requires `setup_workflow_id` whenever B0 exists (409
   `setup_workflow_required`); the same is true of supersede, deployment
   prepare/start, permission repair and container-conflict resolution. Nothing
   falls back to "whichever workflow is stored". The per-route matrix is section
   2.4.
3. **Workers bound to immutable identity** — prepare/start resolve their workflow
   once, at submission, and carry it. The worker re-checks it before the workspace
   write and again before the marker write, and `_write_marker` stamps that
   carried identity plus `owner: "guided_setup"`. A workflow superseded mid-prepare
   therefore fails its worker instead of having its replacement's identity written
   into B3.
4. **Cleanup is ownership-proving** — `SetupWorkflowArtifacts.clear()` no longer
   unlinks the global legacy paths unconditionally. A workflow directory is removed
   only when its realpath is that workflow's own directory; a global artifact only
   when its validated content names that workflow as owner. Everything else is kept
   and reported `review_required`. Details and the outcome table: section 5.3.
5. **Durable cleanup-pending ownership** — B0 gained a validated `cleanup` state
   (`not_required` / `pending` / `complete` / `review_required`, `format_version: 2`,
   fail-closed on read, no paths and no OS errors inside). An unfinished cleanup
   keeps the same `workflow_id`, blocks a replacement Fresh Setup *and* both Guided
   Upgrade phases, survives an Admin restart, and is retried under that exact id.
6. **Truthful recovery in the browser** — a workflow conflict now also invalidates
   every in-flight preview generation and drops the preview timer, so a response
   already on the wire cannot repaint a superseded tab or re-enable Apply/Write. A
   reload keeps a cleanup-pending id (the retry needs it) while dropping its
   preview, `setup_operation_in_progress` keeps the workflow open instead of
   claiming a discard, and a review-required outcome shows why a retry would not
   help.

### 6.4 Exact Setup transition authority (implemented)

What 6.3 left open: the routes that *create* the Setup System Build transition
held no claim, took no workflow id, and linked the transition afterwards on a
best-effort basis — so an unlinked workflow could cancel a transition it never
started, an abandon could win against an in-flight create (both returning
success), and a setup intent outlived the workflow it confirmed.

1. **Workflow-bound setup intents** — `SetupIntent` carries its `workflow_id`;
   `issue`/`validate`/`claim` require it, and terminalizing a workflow retires every
   remaining intent for it in every session. An intent from a superseded workflow
   cannot authorize its replacement, not even from the session that issued it.
2. **Lifecycle claims for transition creation** — `system_build_update_admin`,
   `system_build_confirm` and `setup_release_prepare` join the mutation claim set,
   so abandon/supersede and transition creation can never both succeed for one
   workflow. Order and rationale: section 5.5.
3. **Atomic ownership at the pre-commit boundary** — the workflow link is persisted
   inside `start_resolved(..., pre_launch=…)`; a persistence failure commits no
   transition and launches no Admin replacement (500
   `setup_transition_link_failed`). `confirm_setup_build` /
   `prepare_setup_resources` route through the same primitive.
4. **Strict cancellation proof** — `transition_ownership()` replaces the mode-only
   fallback with `none` / `owned` / `unproven` / `mismatch`, and the two unprovable
   verdicts fail closed without cancelling *or* cleaning. Table: section 5.3.
5. **Resume is owner-only** — an already-active transition is verified against B0's
   `operation_id` before System Alignment is called, so a replacement workflow can
   never adopt or advance work it did not start.
6. **Browser parity** — the four System Build callers submit the server-issued
   `setup_workflow_id`, and the workflow-conflict panel now also covers
   `setup_intent_workflow_mismatch`, `setup_transition_owner_unproven` and
   `setup_transition_context_mismatch`, dropping the local intent and preview
   authority with it.

### 6.4a Setup continuation ownership (implemented)

What 6.4 left open: three narrow gaps at the boundary between B0 and B1, each of
which let an operation outlive or contradict the workflow it was started for.

1. **Compensated transition linking** — the link is written before the transition
   commit, so a commit failure used to leave B0 naming an operation that never
   reached B1. `pre_launch` now returns an undo that `_start_resolved` invokes for
   exactly that failure; it compare-and-restores the reference the attempt
   replaced and refuses if a newer link already won. A launcher failure *after* a
   durable commit keeps the committed link. A failing compensation is reported as
   500 `setup_transition_link_unreconciled`. Both sides: section 5.5.
2. **A claimed resource verification is externally mutating** — cancel and
   `cancel_available` now treat `admin_aligned` plus `resources_claimed_at` as
   running work, so a successful Discard setup can no longer precede a
   resource-cache write from the operation it abandoned. Table: section 5.3.
3. **No unowned Setup return handoff** — `return-to-running-build` is refused for
   Setup-mode transitions rather than shipping a two-step handoff with no durable
   owner for the new operation, so a return and an abandon can never both succeed.
   Rationale, alternatives and the audit of the other shared continuation routes:
   section 5.6.

### 6.4b Exact compensation and live-worker exclusion (implemented)

What 6.4a left open: three narrower gaps behind the same boundary.

1. **Every pre-commit failure compensates** — the undo was reached only for a
   normalized `TransitionStateError`, but the real writer reaches `os.replace`, so a
   raw `OSError` bypassed it and left B0 linked to an operation that never became
   durable. The commit boundary now catches normal operational exceptions, runs the
   exact undo once, and normalizes anything that is not a store error to
   `transition_state_write_failed`. Section 5.5.
2. **A compensation that cannot read is not a stale one** — `restore_transition_link`
   used the fail-closed `load()`, so an unreadable or corrupt B0 record read as
   "nothing to restore" and the route reported only the commit error while the stale
   link stayed on disk. It now reads strictly under the store lock; an absent record
   is the one defined no-op, everything else unusable answers 500
   `setup_transition_link_unreconciled` and is never rewritten. Table: section 5.5.
3. **Expiry does not outrank a live importer** — the durable store bypasses
   `resources_claimed_at` once the TTL passes so an orphan stays escapable, which
   also let an expired transition be abandoned mid-import. Every resource import now
   holds the operation's `OperationCoordinator` claim, so `cancel_available` stays
   false and abandon answers `transition_worker_active` while it runs, while a
   restarted Admin — which holds no claim — keeps the escape. Table: section 5.3.

### 6.4c Proven commit outcomes (implemented)

What 6.4b left open: the compensation was reached for *every* normal exception out
of `PendingTransitionStore.begin`, which silently assumed the transition had not
become durable.

1. **The exception is not the outcome** — a failure raised after the atomic replace
   (leaving the store lock, an instrumented wrapper) left a durable transition whose
   B0 owner was then removed by the undo: a Setup transition with no workflow owner,
   which §5.3 must afterwards refuse to adopt. `_start_resolved` now reads B1 once
   and classifies the exact operation as `not_committed` / `committed` /
   `unprovable`; only the first compensates. Section 5.5.
2. **A durable transition continues, it does not restart** — for `committed` the
   post-commit path runs exactly once (stage advance, at most one Admin launcher
   invocation), later failures keep the committed link, and a retry resumes that
   exact operation rather than minting a second one. Section 5.5.
3. **An unprovable outcome fails closed** — an unreadable B1, or one naming the
   operation with a different transition identity, launches nothing, removes no
   ownership and cleans nothing; the route answers 500
   `transition_commit_unprovable` with the operator recovery path. Section 5.5.

### 6.4d Proven reconnect-stage outcomes (implemented)

What 6.4c left open: only the *first* durable boundary was classified. The stage
write that immediately follows it — `admin_update_pending` →
`admin_reconnect_pending`, committed before the Admin replacement is launched —
still treated its exception as proof of failure, and the commit-identity proof
compared only the selected build fields.

1. **A committed stage must still launch** — a failure raised after `advance`
   replaced B1 (a lock-release error on the way out) skipped the launcher, so B1
   durably claimed a replacement Admin was expected while none had been started
   and a retry only reported that stage. `_start_admin_replacement` now classifies
   the stage against B1 and launches exactly once for a proven commit. Section 5.5.
2. **A proven non-commit stays retryable** — the launcher runs strictly after the
   stage write, so a transition parked at `admin_update_pending` provably launched
   nothing; `start_resolved` resumes that exact operation, performs the missing
   stage write and the single launch instead of minting a second transition.
   Section 5.5.
3. **An unprovable stage fails closed** — an unreadable B1, another operation, a
   different transition identity or a stage that is neither the expected old nor
   new one launches nothing, removes no workflow owner and starts no second
   transition; the route answers 500 `transition_stage_commit_unprovable`.
   Section 5.5.
4. **Identity is the whole immutable projection** — commit classification compared
   `mode`/tag/build/revision/images/digests only, so a durable record sharing the
   operation id but differing in `request_fingerprint`, `resource_strategy`,
   `compatibility_mode`, the Admin-alignment decision, the development
   acknowledgement, the orchestrator Admin identity, `expires_at` or `next_step`
   classified as *this* caller's commit. Both boundaries now compare
   `transition_identity()`, derived by excluding the eight mutable lifecycle
   fields. Section 5.5.

Deliberately **not** closed here: overlapping callers of one operation (6.4e),
and the process-restart window between the durable reconnect stage and the
launcher invocation. The latter is a documented limitation, not an implemented
guarantee — Section 5.5 and §6.5.

### 6.4e Single Admin replacement dispatch (implemented)

What 6.4d left open: it classified both durable boundaries for **one caller at a
time**. Two overlapping retries of the same `admin_update_pending` transition
both read that stage, both entered the replacement start, and both invoked the
launcher — one advancing the stage, the other seeing the advance already done and
returning idempotently. With the real Docker sidecar name collision the second
dispatch raised, so the durable transition claimed `failed_recoverable` /
`admin_update_launch_failed` while the first replacement was already dispatched.
Guided Setup was already serialized by its lifecycle mutation claim (§5.4);
Guided Upgrade, on `ThreadingHTTPServer`, had no equivalent claim.

1. **One dispatch per operation** — `ReplacementDispatchCoordinator` (P6) gives
   each operation id an exclusive, process-shared claim around the whole
   reconcile/advance/launch/publish sequence. Section 5.5.
2. **A waiting caller answers the authoritative transition** — after the claim is
   released it re-reads B1 and reports it; it never launches and never marks a
   successfully dispatched transition `failed_recoverable`. Section 5.5.
3. **The claim records the launcher outcome, not the stage** — a proven
   non-commit and an unprovable stage dispatched nothing, publish nothing and
   stay exactly as retryable and as fail-closed as 6.4d made them; a launcher
   that really failed is reported to every waiter instead of being attempted
   twice. Section 5.5.
4. **The sidecar guard is unchanged** — `claim_admin_update()` remains the
   durable worker-side owner inside the replacement; the dispatch claim exists
   because that guard runs too late to prevent a second launch. Section 5.5.

Deliberately **not** closed here: the process-crash window. The claim is
transient by design and holds nothing across a restart — §6.5.

### 6.5 Deferred follow-up architecture

Evaluated, deliberately **not** implemented:

- **Crash recovery between the reconnect stage and the launcher** — the two
  durable boundaries are each classified against B1 and concurrent callers in one
  process are serialized by the dispatch claim (§6.4e), but the sequence is still
  not crash-atomic: an Admin killed after `admin_reconnect_pending` is durable and
  before the sidecar starts leaves an operation that nothing relaunches until the
  transition expires (§5.5). The dispatch claim does not narrow this window — it
  is in-process state (P6) and holds nothing across a restart, deliberately.
  Closing it needs a durable "launcher dispatched"
  marker written before the launch and a restart-time reconciler that may
  relaunch only while that marker proves no sidecar was started — a third durable
  state and a new startup path, both of which have to be safe against a sidecar
  that *did* start and is mid-recreate.

- **A durable Setup → align-existing handoff** — would restore Return to running
  build for a Setup-owned recovery (§5.6). It needs a record that names the new
  operation's owner, defines when Setup artifacts are cleaned and intents
  invalidated, and resumes correctly after an Admin restart. Until that exists the
  path stays refused; Resume and Discard setup cover the recovery.

- **Transactional artifact cleanup** — removals stay best-effort **per owned
  artifact** and are reported per path (section 5.3). Ownership is never
  best-effort, and an unfinished cleanup now blocks every follow-up path, so the
  remaining gap is atomicity, not safety. A quarantine-then-commit scheme would
  make abandonment genuinely atomic, at the cost of a second on-disk state to
  reconcile.
- **An explicit quarantine action** — a `review_required` artifact currently needs
  an operator on the filesystem; a tested, explicit quarantine/adopt action would
  give it a UI path.
- **Automatic legacy adoption** — a sidecar-less generated config could be
  auto-adopted when every ownership fact is provable from authoritative state;
  today it always requires regeneration, which is safe and cheap.
- **Unified recovery lifecycle / rebase-and-diff** — presenting a stale generated
  config as a diff against the changed live config, instead of only refusing it.
- **A workflow record for Guided Upgrade** — its durable context (B6) now has an
  owned lifecycle, but Upgrade has no multi-artifact directory of its own;
  introducing a second record today would duplicate transition state.

---

## 7. Unified guided workflow lifecycle

What §6 left open: every authority had an owner, but *no* service read them
together. The start path, the upgrade conflict gate, the unrelated-transition
write gate and two browser helpers each answered "who owns the Admin right now"
for themselves, so a user could reach a state none of them could resolve — and
the documented escape was deleting a JSON file over SSH.

`AdminWorkflowLifecycleService` (`admin/workflow_lifecycle.py`) is that one
reading. It owns no durable state: B0, B1 and B6 stay authoritative for their own
concern, and every mutation is delegated to the service that owns it.

### 7.1 Owner and state

Ownership is decided by the durable records, never by the open UI, the selected
release or the browser URL:

```text
non-terminal transition?
  fresh_install / automated_setup -> guided_setup
  guided_upgrade                  -> guided_upgrade
  align_existing_install          -> a separate owner; not switchable here
  unknown mode                    -> unknown; fail closed
else
  B0 active, or its cleanup still blocking -> guided_setup
  otherwise                                -> none
```

An unreadable B0 or B1 makes the owner `unknown` and the state `malformed`.

| State | Meaning | Switchable |
|---|---|---|
| `idle` | nothing owns the console | yes |
| `active` | a guided workflow is in progress | yes |
| `operation_running` | a Setup mutation claim is held, or the transition is non-terminal and not cancellable | no |
| `cleanup_pending` | a terminal Setup's owned removal failed | no — safe recovery converges it |
| `review_required` | a terminal Setup kept an artifact whose owner it could not prove | no — an operator decides |
| `malformed` | a durable record could not be read | no — advanced release |

A running mutation outranks an unreadable record on purpose: recovery must stay
blocked while anything can still write, whatever else is corrupt.

`inspect()` normalizes one thing on the way — a review state that only ever named
installed-system files (§5.3) — which changes the record and no file. Everything
else is read-only.

### 7.2 The fingerprint

Every mutating switch or recovery must present the fingerprint of the state it
was decided on. It covers exactly the durable facts behind the verdict:

```text
B0  present, readable, workflow id, status, cleanup state, artifact claims, operation id
B1  present, readable, operation id, mode, stage
B6  present, readable, operation id, target tag
```

An **unreadable** file additionally contributes a SHA-256 of its bytes, because
identity fields cannot distinguish two different corrupt records and a preview
must bind the exact bytes it was shown for. In-process liveness is deliberately
out: a claim that comes and goes must not invalidate a preview the operator is
still reading, and the running-operation gate is re-evaluated at execution time
anyway. A mismatch is refused with 409 `workflow_lifecycle_changed`; nothing
changes.

### 7.3 Switching

`POST /api/admin/workflow-lifecycle/switch/preview` says what one switch would
do; `POST …/switch` performs it. They are separate endpoints so a confirmation is
always shown against the state it was computed for.

| Target | Current owner | Action | Delegated to |
|---|---|---|---|
| `guided_upgrade` | `guided_setup` | `discard_guided_setup` | `abandon_setup_workflow` (exact operation cancel + claim-aware cleanup + intent retirement) |
| `guided_setup` | `guided_upgrade` | `cancel_guided_upgrade` | `SystemAlignmentService.cancel` + `clear_for_operation`, then `ensure_active` + a fresh session intent |
| `guided_setup` | `guided_setup` (active) | `resume_guided_setup` | `ensure_active` |
| `guided_setup` | `none` | `start_guided_setup` | `ensure_active` |
| `none` | either | the owner's own termination | as above, without a replacement |
| any | already satisfied | `none` | — |

A blocked switch lists no reset scope: it promises nothing because it will do
nothing. What it always preserves is fixed and stated in the preview: live EMS
configuration, runtime data, deployment marker, containers, volumes, backups.

Refusals, all leaving every durable record untouched:

| Situation | Code |
|---|---|
| a Setup mutation claim is held | `setup_operation_in_progress` |
| the transition is non-terminal and not cancellable (reconnect pending, EMS operation running, healthcheck pending, claimed resource import, live worker) | `workflow_operation_in_progress` |
| a terminal Setup's cleanup is `pending` or `review_required` | `workflow_recovery_required` |
| the Setup cannot prove it owns the active transition | `workflow_switch_blocked` + the exact `setup_transition_*` detail |
| the transition mode is `align_existing_install` or unknown | `workflow_owner_unknown` |
| B0 or B1 is unreadable | `workflow_state_malformed` |
| the presented fingerprint is not current | `workflow_lifecycle_changed` |
| `confirm` was not `true` | `confirmation_required` (400) |

Concurrency: the arbiter serializes the decide-then-act sequence in process, and
the loser re-reads a changed state and is refused by the fingerprint. Two
simultaneous switches therefore perform exactly one termination, one
cancellation and one new target workflow — never a partial cross-owner state.

**Navigation is not termination.** Leaving the task selection, opening
Diagnostics or Backup keeps the durable workflow; only an explicit switch,
"Start over" or a workflow reset terminates it.

### 7.4 Recovery

`POST /api/admin/workflow-lifecycle/recovery/preview` and `POST …/recovery`,
same split, same fingerprint rule. Both modes are refused outright while
`state` is `operation_running`, with 409 `workflow_recovery_unsafe`.

**`safe`** uses nothing but normal domain operations, and deletes no state file:

```text
cancel a cancellable non-Setup transition through SystemAlignmentService
clear that operation's upgrade context
terminalize / retry cleanup of the Setup through abandon_setup_workflow
clear an orphaned upgrade context, bound to the operation the file names
```

An unreadable record is refused here (`workflow_recovery_unsafe`, detail
`workflow_state_malformed`): the normal operations cannot resolve what they
cannot read.

**`release_stale_state`** may quarantine durable Admin workflow metadata. It
requires a preview, the exact fingerprint, explicit confirmation and a reason,
and it refuses while a claim, a worker or a replacement may still be running —
the Docker probe additionally looks for that operation's replacement sidecar,
and a probe that raises fails closed. The allowlist is derived from the Admin's
own stores; a browser can never name a path:

```text
state/guided-setup-workflow.json
state/pending-transition.json
state/guided-upgrade-context.json
```

Only files that exist *and* cannot be read as valid state are in scope. Each
target's parent must resolve to the canonical Admin state directory, which must
not be a symlink; otherwise the release is refused. The order is fixed: back up
every file with its hash, write the manifest, then unlink. Never in scope:
`state/.admin-deployment.json`, `state/known-good-system-build.json`,
`config/config.json`, `docker-compose.yml`, `data/runtime-state.json`,
dashboard databases, InfluxDB data, backups and credential stores.

Backups land in `<admin_data>/state/workflow-recovery/<UTC timestamp>/` beside a
`recovery-manifest.json`:

```text
manifest_version, created_at, mode, reason, admin_revision,
lifecycle_fingerprint, files[{name, sha256, bytes}]
```

The manifest carries identity and hashes only — no file content, so no secret a
malformed record happened to contain reaches it. Backups are never pruned by the
Admin.

A second release finds nothing left in scope and answers `released: []`.

### 7.5 Route integration

| Route | Uses the arbiter for |
|---|---|
| `GET /api/admin/workflow-lifecycle` | the normalized view |
| `POST /api/admin/workflow-lifecycle/switch{,/preview}` | the switch decision and execution |
| `POST /api/admin/workflow-lifecycle/recovery{,/preview}` | both recovery modes |
| `_setup_owned_conflict()` (upgrade validate + execute) | the owner verdict; its public `setup_abandon_required` / `setup_cleanup_required` codes are unchanged |
| browser start path (`setup_new`) | consults the view first and switches when another workflow owns the console |

`POST /api/setup/abandon`, `POST /api/setup/system-build/supersede` and
`POST /api/admin/system-alignment/cancel` keep their existing narrow contracts:
they are the owners the arbiter delegates to, and remain reachable for the
Restart setup / recovery-panel actions that already name one owner.

`_reject_unrelated_transition_write` is deliberately **not** folded in: it is
operation-specific validation owned by System Alignment ("do not write config
while a build transition is pending"), not a cross-workflow ownership decision.

### 7.6 Accepted limitations

- The in-process serialization covers concurrent callers in one process. A dying
  process holds no claim, by design (P1/P5/P6); the durable records are what a
  restarted Admin re-reads.
- A crash between the backup write and the unlink of a released file leaves the
  backup plus the original — safe, and a second release re-quarantines whatever
  is still unreadable.
- The Docker replacement probe is a positive confirmation only. A daemon that
  cannot be reached answers "no replacement seen"; the durable stage is what
  actually blocks every replacement window.
- Coverage: `tests/test_admin_workflow_lifecycle.py`,
  `tests/test_admin_workflow_switching.py`,
  `tests/test_admin_workflow_recovery.py`,
  `tests/test_admin_workflow_recovery_routes.py`,
  `tests/test_admin_workflow_lifecycle_frontend.py`,
  `tests/e2e/workflow-switching.spec.ts` and
  `tests/e2e/workflow-recovery.spec.ts`.
