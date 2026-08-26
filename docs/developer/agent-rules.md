# Canonical Project Agent Rules

This document is the single authoritative, agent-independent rule set for work
on `ems-solarflow-api-control`. `AGENTS.md`, `CLAUDE.md`, Copilot instructions
and tool-specific guidance are entry points or supplements; they MUST NOT carry
a second complete copy of these rules.

These rules apply before planning, editing, reviewing, validating, committing
or reporting project work.

## 1. Instruction precedence and task intent

- Agents MUST follow the current user task and its explicit scope.
- These repository rules are mandatory defaults.
- Agents MUST NOT silently reinterpret or broaden a requested task.
- When requirements conflict, agents MUST identify the conflict before making a
  risky change.
- Safety, security, data preservation and hardware-write protection MUST NOT be
  weakened merely to satisfy a test or simplify an implementation.

Agents MUST distinguish a requested production change, its required regression
tests, necessary documentation, and unrelated cleanup or refactoring. Unrelated
improvements belong in a separate task unless essential to correctness.

## 2. Project-wide Single Source of Truth

Every authoritative fact has exactly one owner. Other layers may read, project,
cache or orchestrate it, but MUST NOT create a second independent authority. A
cache, UI model, marker, preview, test fixture or derived state MUST be
rebuildable from its authoritative source or explicitly treated as
non-authoritative.

| Concern | Authoritative source | Non-authoritative projections |
|---|---|---|
| Static EMS configuration and device activation | `config/config.json`, validated by EMS/Core | Admin forms, browser draft, preview JSON |
| Mutable EMS runtime/operator state | `data/runtime-state.json` through EMS-owned validated writers | Dashboard/Admin display state |
| Config schema and semantics | EMS/Core config modules and generated template contracts | Admin serializer guesses |
| Running containers, image identities and health | Docker daemon/runtime inspection | Cached Admin release selections |
| Desired deployment layout | Standard `docker-compose.yml` plus canonical install paths | Temporary staging compose files |
| EMS control calculations and write eligibility | EMS/Core control and safety modules | Admin UI labels or browser decisions |
| Backup/restore semantics | EMS/Core / `emsctl` backup and restore implementation | Admin orchestration and progress UI |
| Guided workflow identity and lifecycle | Validated durable workflow/transition record owned by Admin | Browser state, pollers, URL, local storage |
| Workflow artifact cleanup-scope ownership | Validated durable artifact claim; exact owner/workflow identity embedded in the artifact or sidecar; or a canonical workflow-scoped path derived from the exact workflow ID | File existence, file name or known global location |
| Permission to delete an in-scope artifact | Exact ownership proof and canonical-path validation | Cleanup-scope membership alone |
| System Build identity | Validated paired Admin/EMS build metadata and digests | Selected tag text |
| Diagnostics contract | EMS diagnostics service and versioned schema | CLI or UI formatting |
| Authentication secret | Shared validated password/auth store | Session/UI state |

### Device activation invariant

Device enabled/disabled state belongs to the logical device configuration, not
to its selected transport. A Maintenance transport switch API ↔ Local MQTT ↔
Zendure MQTT MUST preserve the device's current enabled state. A new device
added through Guided Setup MUST be enabled by default. A device becomes disabled
only through an explicit operator/config action, never as an accidental side
effect of changing transport.

Activation authority MUST NOT be duplicated in transport-specific objects.
Transport adapters describe connectivity and capabilities, not whether the
logical device is enabled.

### Workflow ownership invariant

An artifact may enter a workflow cleanup scope only through one of these
authoritative ownership proofs:

- a durable artifact claim in the validated workflow record;
- exact owner and workflow identity embedded in the artifact or its sidecar;
- a canonical workflow-scoped path whose identity is derived from the exact
  workflow ID.

File existence, file name or a known global location are never ownership proof.
Browser state MUST NEVER authorize cleanup. Cleanup-scope membership does not
authorize deletion: deletion still requires exact ownership proof and
canonical-path validation.

## 3. Architecture boundaries

### EMS/Core

EMS/Core owns the control loop, target calculation, device-capability
interpretation, write gates, config schema and validation, runtime-state
semantics, diagnostics, backup/restore formats, and hardware-write execution.

### Admin Console

The Admin Console owns the authenticated UI, guided orchestration, workflow
presentation, preview and explicit confirmation, calls to EMS/Core tools,
Docker-operation coordination, and durable Admin workflow records.

Admin MUST NOT reimplement EMS control logic, invent a second config schema or
backup format, infer hardware-write authority from UI state, or treat browser
state as workflow truth.

### Docker/Bootstrap

Docker/Bootstrap owns the standard deployment shape, host-valid bind paths, and
container/compose lifecycle. Every installation path MUST converge on:

```text
config/config.json
data/
docker-compose.yml
```

### Frontend

Frontend state is presentation state only. It may render, collect input,
request previews, show progress and poll authoritative backend state. It MUST
NOT decide workflow ownership, transition authority, deletion permission,
write-gate permission, or whether an operation succeeded.

## 4. Safety and fail-closed behavior

This project controls real electrical hardware. Treat `outputLimit` writes,
`acMode`/`inputLimit` writes, SoC reconciliation, write-gate defaults, device
activation, transport selection, container replacement, config/compose writes,
backup/restore, workflow cleanup, authentication and CSRF as high risk.

When authority or safety cannot be proven, fail closed: do not mutate. Preserve
or return the established error or recovery state.

Agents MUST NOT make a test pass by loosening a write gate, defaulting an
unknown state to enabled, assuming reconnect means success, deleting an
unproven file, bypassing exact operation identity, forcing a frontend button
enabled, or silently accepting malformed state.

Simulation, replay and test configurations MUST remain hardware-safe. Normal
tests MUST NOT contact or control real LAN devices. Any hardware integration
test MUST be explicit, isolated and clearly marked.

## 5. One owner for each mutation path

Before adding a writer, launcher, reconciler or cleanup path, identify the
existing owner. Current examples include:

| Mutation | Owner |
|---|---|
| Runtime AC intent writes | Runtime intent reconciler |
| Admin replacement launch | One `SystemAlignmentService` gateway |
| Runtime-state edits | EMS-owned validated runtime writer |
| Config apply | Validated config/Core path |
| Workflow cleanup | One backend lifecycle/cleanup service |

A second direct mutation path MUST NOT be added for convenience. A new caller
MUST reuse the owner, extract a shared service, or route through the existing
gateway. Tests SHOULD assert important single-call-site or single-owner
invariants where practical.

## 6. Contract-first bug fixing and feature development

Safety-relevant work and bugs follow this contract-first sequence:

1. Reproduce the behavior with a failing test.
2. Prove that it fails for the reported reason.
3. Implement the smallest correct production change.
4. Keep the regression test.
5. Run focused and broader validation.

The pre-fix test MUST fail for the intended reason, not merely match a proposed
implementation. Prefer public or service-level behavior. Synthetic stores and
fakes are appropriate for precise concurrency interleavings, with route or
integration coverage when the real boundary matters.

Contract-first coverage is required for control math, write gates, device
activation, transport switching, workflow state, container dispatch, config
apply, backup/restore, auth/CSRF, cleanup ownership, and diagnostics schemas.

## 7. Concurrency and lifecycle rules

Concurrency regressions MUST use deterministic synchronization such as
`threading.Event`, `Barrier`, `Condition`, explicit fake launchers/stores, or
request/response coordination. Arbitrary sleeps MUST NOT create ordering. Every
concurrency test MUST state the protected interleaving.

For in-process coordination, durable state remains authoritative, coordinator
memory only serializes callers, and process-local claims are not durable truth.
Completed coordinator entries SHOULD be retired safely rather than retained
forever to mask a race.

Process-crash windows MUST be documented honestly. Exactly-once behavior MUST
NOT be claimed beyond what the implementation and tests prove.

## 8. Code design and scope discipline

Prefer small focused functions, clear names, immutable authority/identity value
objects, one responsibility per service, existing validators/helpers, and
explicit error contracts.

Avoid large nested condition trees, duplicated state machines or serializers,
path-existence heuristics, browser-authoritative logic, broad refactors inside
a bug fix, and copied test helpers.

Edit the smallest relevant module. Do not move focused logic into the large
entry script or `ems/controller.py` when a dedicated module owns it. Do not add
a one-use abstraction unless it clarifies an important authority boundary.

## 9. Code comments and docstrings

- Comments MUST explain why, not what.
- Keep inline comments minimal and prefer self-explaining code and focused
  helpers.
- Use comments for non-obvious safety or architecture decisions only.
- Keep docstrings short and factual.
- Put detailed behavior matrices in tests and technical documentation.

Do not add control-flow narration, comments that repeat code, implementation
essays inside functions, or comments on every new block. Correct or remove
misleading and obsolete nearby comments when touching their code.

## 10. Language and naming

Agent communication and final reports to the project owner MUST be in German.
Source code, identifiers, comments, docstrings, commit messages, and technical
API/error identifiers MUST be in English.

Public error codes MUST remain lowercase, underscore-separated and stable when
already contractual. Public JSON fields, CLI options, events and error codes
MUST NOT be renamed casually.

## 11. Frontend and UI rules

Use established Admin and Dashboard patterns. Before Dashboard UI work, read
[`dashboard-style-guide.md`](dashboard-style-guide.md), declare the selected
style family (`Aggregate / Device` or `Control / Energy stage`), reuse existing
tokens/classes, and do not create a new visual system.

Keep semantic labels, inputs, selects and buttons, keyboard accessibility,
escaped dynamic text, and authentication/CSRF gates. Prefer `textContent` and
safe DOM APIs. Raw dynamic values MUST NOT be written to `innerHTML`.

Frontend controls reflect backend authority. Tests MUST NOT manually enable
disabled buttons, force-click past gates, or inject fake authoritative state
into the DOM.

Frontend extraction tests SHOULD use one shared helper-dependency registry, a
dependency-aware resolver, or public browser-level behavior rather than
per-test transitive `admin.js` dependency lists. Do not mix that refactor into
unrelated work; record it as technical debt when encountered.

## 12. Test isolation and resource discipline

Normal automated tests MUST be deterministic, isolated and independent of real
Zendure devices, LAN mDNS, unprovisioned MQTT brokers, developer config,
browser-session leftovers, test order, or prior test state. Setup/reset MUST
leave no cross-test state. Use exact bytes or hashes when preservation matters.

### Playwright

- Do not use arbitrary `waitForTimeout`; use response, event or locator
  authority.
- Hold previews and temporary resources in `try/finally`.
- Use `Promise.all` for an action plus its expected response where needed.
- Run stress cases sequentially and resource-consciously, using `--workers=1`
  when isolation is the purpose.
- Do not run multiple full browser and stress suites concurrently.
- Check available RAM, swap pressure, load and stray browser/test runners before
  or during long stress runs.

If exhaustion produces broad timeout or teardown failures, stop the invalid
run, report an environment/resource failure, free resources and repeat under
controlled conditions. Do not hide resource failures by globally increasing
timeouts without evidence. Remove generated reports, traces, screenshots and
videos before committing unless requested.

## 13. Tooling rules and fallbacks

When GitNexus is available and indexed, use impact analysis before editing
important symbols, query/context for unfamiliar flows, and `detect_changes`
before committing. Tool-managed GitNexus blocks MUST NOT be edited manually.
Repository-local GitNexus CLI and MCP invocations MUST use
`scripts/gitnexus-project` so `.gitnexusrc` and the analyzer writer lock apply.
Repository-local analyze invocations MUST pass `--force` until the documented
GitNexus 1.6.9 incremental LadybugDB/FTS failure is resolved.

A stale index is an agent's to fix, not to work around: re-running the analysis
through `scripts/gitnexus-project` is pre-authorized and needs no separate
instruction. Report that it was refreshed and against which revision.

If GitNexus is unavailable, stale or fails, do not pretend it was used. Perform
a manual blast-radius review with symbol/reference searches; inspect callers,
routes, tests and docs; report the limitation; and continue only when safe.

When Serena is available, use it for current symbol bodies, references, precise
edits and diagnostics. Otherwise use repository search, language tooling and
direct inspection. GitNexus and Serena complement one another; neither replaces
tests or architecture reasoning.

Agents MUST NEVER claim a tool, CI run, browser, hardware test or command
succeeded unless it actually completed.

## 14. Security and secrets

Never commit or expose passwords, API keys, MQTT credentials, private tokens,
unnecessary real serial numbers or private IPs, production configs, session
cookies, or backup passwords. Use anonymized fixtures.

Authentication, CSRF, path canonicalization, symlink/traversal protection,
strict tag/image validation and secret redaction MUST NOT be weakened. Validate
shell and Docker inputs and pass them without unsafe interpolation. The browser
MUST NOT provide arbitrary image references, compose paths or shell commands
when the server can derive them.

## 15. Filesystem and artifact ownership

Use canonical paths and validate their boundaries. Ownership MUST NOT be
inferred from location, file name, existence, mtime or a browser claim.

For workflow artifacts, use only the three authoritative cleanup-scope proofs
defined in the workflow ownership invariant above. Cleanup-scope inclusion is
not deletion authority. Deletion still requires exact ownership proof and
canonical-path validation.

Unproven files remain untouched and produce a recoverable/review state. Cleanup
MUST be idempotent. Every temporary artifact MUST have an owner and lifecycle.

## 16. Documentation and public contracts

Behavior changes MUST update their owning documentation. Do not duplicate the
same normative rule across documents; link to its canonical owner.

Contract changes require tests for relevant JSON schemas, diagnostics fields,
config-template generation, CLI behavior, HTTP responses, workflow stages,
error codes and documentation links. Incompatible changes MUST bump the
relevant schema/version and document migration or compatibility. Documentation
MUST NOT claim a stronger safety guarantee than the implementation proves.

## 17. Git discipline

At task start, record:

```bash
git branch --show-current
git rev-parse HEAD
git status --short
git stash list
```

Preserve unrelated work. Never use `git reset --hard`, `git clean -fd`, a force
checkout over user changes, unrelated stash mutation, history rewriting, or a
push without explicit instruction.

Before commit, run `git diff --check`, `git status --short`, and
`git diff --stat`, then inspect every changed file. Commits MUST be small,
logical, in English, and have no Co-Author trailers. Use test-before-fix commits
when explicitly required.

Do not commit generated reports, local state, traces, screenshots, temporary
archives, secrets or unintended scratch files. After commit, run
`git status --short` and `git log --oneline -5`. The tree SHOULD be clean unless
the task explicitly requires otherwise. There is no push without explicit
instruction.

Source and review archives MUST be produced with
`scripts/appliance-create-source-bundle.sh`. It archives the git object tree
rather than the working directory, so file modes and symlinks survive, and it
verifies the result against that tree before handing it over — an archive that
does not round-trip is deleted rather than delivered. Two previous review
archives arrived with the six `local-fs.target.wants` persistence symlinks
flattened into regular files, which produces a tree that still builds, generates
six mount units, activates none of them, and loses every write to the shared
paths at the next slot switch.

## 18. Validation and release claims

Validation MUST match the changed risk. Use the applicable baseline:

```bash
ruff check .
python -m compileall -q admin ems dashboard scripts tests emsctl.py ems-solarflow-api-control.py
node --check admin/static/admin.js
python tools/build_config_template.py --check
git diff --check
```

EMS/control changes additionally require focused tests, simulation and the
`power_control` gate, self-test, and the full non-Docker suite. Admin/frontend
changes require focused backend contracts, frontend extraction/contracts,
focused Chromium and Firefox, and full browser suites when release-relevant.
Docker changes require Docker-first tests where required env files exist.

Keep local and CI evidence separate. Report passed, failed, skipped,
deselected, timed out, not run and environment-blocked results accurately.
Never convert “in progress”, “likely green”, “passed previously” or “could not
run” into success. An RC or stable recommendation requires completed relevant
gates and no known blocker.

## 19. Final-report requirements

Implementation reports MUST include, as relevant: starting and final branch and
HEAD; working-tree status; root cause; authority/source-of-truth decision;
production files changed; tests added; focused, full and browser results; CI
status; commits; push status; and remaining limitations. Disclose skipped tests,
environment failures, accepted limitations and untested hardware behavior.

## 20. Prohibited anti-patterns

- A second independent source of truth.
- A duplicate writer, launcher or reconciler.
- Browser state treated as backend authority.
- File existence treated as ownership.
- A transport switch accidentally changing enabled state.
- Deleting unproven files.
- Weakening safety gates to satisfy tests.
- Sleeps for deterministic concurrency.
- Force-clicking disabled UI controls.
- Raw dynamic `innerHTML`.
- A broad unrelated refactor in a bug fix.
- Long narrative code comments.
- Fabricated tool or test results.
- Commit or push outside the requested scope; no push without explicit
  instruction.
