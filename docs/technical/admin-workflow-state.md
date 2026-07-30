# Admin workflow state — inventory, write paths, transition matrix, invariants

Every persisted state source the Admin console's workflows share, the paths that
can write configuration, and the invariants those must satisfy. Companion to
`admin-architecture.md` (architecture rules) and `system-build-pairing.md`
(transition lifecycle).

Scope: Guided Setup, Maintenance, Guided Upgrade, Recovery, authentication loss,
Admin restart.

**How to read this.** Sections 1–3 and 5 describe the system **as it is now**,
after the server-owned workflow-authority hardening. Section 4 is the original
audit that motivated the first hardening pass and is deliberately kept in the
past tense — it records what was broken, not what is.

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

Beside these five sits one **arbiter**, not an authority:
`SetupLifecycleCoordinator` (`admin/setup_lifecycle.py`) decides *who may act on
the Guided Setup workflow right now*. It stores no durable state — a restart
holds no claims — and duplicates neither transition stage nor worker liveness.
See §5.4.

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
| P1 | `OperationCoordinator._active` / `._abandoned` | worker liveness | Lost — a restart makes every worker claim look inactive |
| P2 | `DeploymentJobRegistry` / `StartJob` registries | deployment jobs | Lost — job IDs 404 after restart |
| P3 | `SetupIntentStore` (in-memory, TTL 20 min) | Fresh Setup confirmation | Lost — re-confirmation required |
| P4 | `DeploymentService._active_job` / `_active_start_job` | deployment serialization | Lost |
| P5 | `SetupLifecycleCoordinator._claims` / `._terminalized` | Setup mutation vs. terminal exclusion (§5.4) | Lost — **fail closed by design**: a restart holds no claims, so nothing pretends a pre-restart worker is still live; every commit stays gated by B0 and by the durable marker checks |

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

| Route | `setup_workflow_id` | `config_preview_id` | bound generated artifact | transition `operation_id` | lifecycle claim |
|---|---|---|---|---|---|
| `POST /api/setup/config/write` | required, exact | required, exact | writes it | — | `config_write` (mutation) |
| `POST /api/setup/config/apply` | required, exact | required, exact | — | — | `config_apply` (mutation) |
| `POST /api/setup/deployment/prepare` | required, exact | — | required (B2a proves workflow + preview + hash + baseline) | mirrors the current one, never advances it | `deployment_prepare` (mutation, held for the whole worker) |
| `POST /api/setup/deployment/start` | required, exact | — | via B3, whose `owner`/`workflow_id` must match | claims the EMS operation | `deployment_start` (mutation, held for the whole worker) |
| `POST /api/setup/deployment/repair-permissions` | required, exact | — | via B3 owner match | — | `permission_repair` (mutation) |
| `POST /api/setup/deployment/resolve-container-conflict` | required, exact | — | via B3 owner match | may acknowledge its own conflict recovery | `container_conflict_resolution` (mutation) |
| `POST /api/setup/abandon` | **required whenever B0 exists**; also the retry-cleanup entry point | — | — | cancels only its **own** `operation_id` (§5.3) | `abandon` / `cleanup_retry` (terminal) |
| `POST /api/setup/system-build/supersede` | required, exact | — | — | same | `supersede` (terminal) |
| workflow completion (deployment worker's terminal callback) | the worker's carried id | — | — | the completed operation | `complete` (terminal) |
| `POST /api/setup/system-build/confirm` | — (see below) | — | — | creates it | — |
| `POST /api/setup/system-build/update-admin` | — (see below) | — | — | creates it | — |
| `POST /api/setup/releases/prepare`, `…/automated/releases/prepare` | — | — | — | — | — |

`confirm` and `update-admin` deliberately do **not** take a workflow id: they
*create* the transition a workflow then links to, and remove, overwrite or
terminalize nothing. Their authority is the one-shot, session-bound
`setup_intent_id`, which an old tab does not hold, and the link they write targets
the single stored B0 record rather than a chosen candidate. Release preparation
fills the shared release cache (B8/B4) — installed-system state that outlives
every workflow.

Read-only routes and `POST /api/setup/config/download` (serializes a draft to the
browser, touches no durable state) are outside the matrix.

---

## 3. Transition matrix

`T` = `pending-transition.json` (B1). "Artifacts" refers to B2–B6.

| # | Transition | Allowed | Owner → next | T before → after | Artifacts | Live config | Recovery | UI view |
|---|---|---|---|---|---|---|---|---|
| 1 | Setup → Maintenance | **reads yes, writes blocked** | Setup → Maintenance | unchanged | **all retained** | untouched | available | Maintenance, config apply returns 409 |
| 2 | Maintenance → Setup | yes | Maintenance → Setup | unchanged | retained | untouched | available | Setup |
| 3 | Setup → Guided Upgrade | **only through the Setup owner** — upgrade validate *and* execute return 409 `setup_abandon_required` until an explicit Discard setup runs `POST /api/setup/abandon`, and 409 `setup_cleanup_required` while a terminal workflow's cleanup has not converged | Setup → Upgrade | `fresh_install`/`automated_setup` → `cancelled` by the abandon | **removed with the abandon**; unfinished cleanup keeps both Upgrade phases blocked and offers Retry cleanup | untouched | available | Upgrade |
| 4 | Guided Upgrade → Maintenance | yes | Upgrade → Maintenance | unchanged | retained | untouched | available | Maintenance |
| 5 | Maintenance → Guided Upgrade | yes | Maintenance → Upgrade | unchanged | retained | untouched | available | Upgrade |
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
| 23a | abandon finds an artifact it cannot prove it owns | partial | Setup → none | `cancelled` | the artifact is **kept**; B0 terminal with `cleanup.state = review_required`; a retry does not convert this to clean | untouched | available | 409 `setup_artifact_review_required`, review-required copy, **no** Retry cleanup button |
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

Rows 15, 16–16d, 18, 20–26c are covered by tests — see
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

The second gate is the user-visible failure mode: an abandoned Guided Setup
leaves its transition at a non-terminal stage, so **Maintenance can no longer
save a config change** even though Setup is no longer in use. Because
`startGuidedSetupOver()` never cancels the transition, the only escapes are the
System Build recovery panel's Abandon button or deleting
`pending-transition.json` by hand.

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
| I1 Workflow ownership | violated | **held** — B0 records the workflow identity; B2/B2a live in the workflow's own directory; B3 is ownership-stamped; B6 clears on its operation's terminal events; B5 is reclassified as installed-system state; the workflow's selected tag lives in B0 | B4 (`selected-release.json`) stays a shared cache by design |
| I2 Safe abandonment | violated | **held** — abandon/supersede name their workflow exactly, hold an exclusive terminal claim (§5.4), cancel only their **own** transition, remove only provably owned artifacts and keep the same `workflow_id` owning whatever remains (§5.3); the public cancel primitive refuses Setup-owned modes | a failed removal is reported and blocks follow-ups, not rolled back (see 5.3 and 6.4) |
| I3 Config freshness | violated | **held for W1, W2 and W3** — the exact preview (workflow ID + preview ID + fingerprint + baseline + payload hash, §2.3) blocks a stale or cross-draft apply/write, and workflow-owned B2a blocks a stale or foreign deploy (§2.2) | — |
| I4 Recovery independence | held | held — `failed_recoverable` stays cancellable and abandonable | — |
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

**A workflow cancels only its own transition.** A Setup-owned *mode* is not
ownership: once B0 names an `operation_id`, only that transition may be cancelled,
so one workflow can never terminate another's. A record that never linked a
transition falls back to the mode check — B0 holds a single workflow, so there is
no other candidate.

**Cleanup is best-effort per owned artifact; ownership never is.**

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

### 5.4 Mutation / termination exclusion

`SetupLifecycleCoordinator` (`admin/setup_lifecycle.py`) is the one arbiter, shared
by the HTTP and HTTPS listeners through `AdminRuntime`. Exactly one of these two
statements is true of a Guided Setup mutation at any moment:

> it owns the workflow until its irreversible work has finished, **or** the
> workflow was abandoned/superseded before it started.

- Claims are non-overlapping per workflow: `config_write`, `config_apply`,
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
   falls back to "whichever workflow is stored". The per-route matrix, including
   the two intent-gated routes that deliberately take no workflow id, is section
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

### 6.4 Deferred follow-up architecture

Evaluated, deliberately **not** implemented:

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
