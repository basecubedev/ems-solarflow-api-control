# Admin architecture

This page describes how the Admin Console, the Docker Bootstrap deployment
layout, and EMS/Core relate to each other. It is the architecture reference for
the Admin path. For the full Admin internals (wizard, release/build-identity
gating, network discovery, Docker setup and security) see
[admin-discovery.md](admin-discovery.md). For the user-facing guide see
[../user/admin-console.md](../user/admin-console.md). For how Admin and EMS are
resolved, aligned and installed as one paired **system build**, see
[system-build-pairing.md](system-build-pairing.md).

## Roles and boundaries

There are three layers, with clear ownership:

- **Admin Console = UI / orchestration.** The Admin Console (product name
  *EMS SolarFlow Admin*) is a local browser application that guides setup,
  device discovery, config generation, diagnostics, updates, backups and
  restore. It orchestrates EMS containers and calls EMS/Core tools. It does not
  run the control loop and it does not own config or backup semantics.
- **Docker Bootstrap = standard deployment layout.** The Docker Bootstrap path
  owns the standard on-disk deployment layout (compose file, `config/`, `data/`)
  that every path converges on. The Admin Console deploys onto this same layout.
- **EMS / Core = source of truth.** EMS remains the source of truth. EMS owns
  the control loop, config schema and semantics, diagnostics, and backup/restore
  behavior. Every Admin Console backup is a normal EMS backup archive, and every
  config apply is validated by EMS/Core.

The key rule:

> EMS remains the source of truth. The Admin Console is UI and orchestration
> only. The Docker Bootstrap layout is the standard deployment shape both paths
> share.

Because EMS/Core owns config, diagnostics and backup/restore semantics, the
Admin Console never invents its own config format or its own backup format — it
calls the same EMS tools a shell user would run directly.

The Admin Console is config-authoritative and does not reconcile the
runtime-state device lifecycle. The one exception is config → runtime
convergence: on maintenance Apply it mirrors the whitelisted overlapping scalar
keys it changed into `runtime-state.json`, but only through the EMS-owned
runtime-write whitelist (`dashboard/runtime_write.py`) — the same validated
writer the Dashboard uses — so it introduces no second runtime format and stays
inside the whitelist safety property. It already writes `runtime-state.json`
wholesale during restore.

## Standard deployment layout

All three operating models (Admin Console, Docker Bootstrap, Developer Setup)
converge on the same standard layout, so an installation can move between them:

```text
standard layout:
  config/config.json     EMS static config (source of truth)
  data/                  EMS runtime data (runtime state, history, analytics)
  docker-compose.yml     deployment definition
```

Additional EMS-owned paths under `data/`:

```text
  data/backups/          EMS backup archives
```

## Admin state

The Admin Console keeps its own orchestration state separate from EMS config and
runtime data, under a dedicated directory:

```text
admin state:
  data/admin/            Admin Console state, release cache, staging and logs
```

`data/admin/` holds Admin-only data (release cache, staging areas, Admin logs
and UI state). It is not EMS config and not part of the EMS control path.
Removing it does not change EMS behavior; it only resets Admin Console state.

## Authentication

The Admin Console is protected by the shared EMS/Dashboard password — there is
exactly one local password per host, and no Admin-only password store.

- **Shared password file.** Admin auth uses the shared dashboard auth file. The
  path is resolved from the EMS install context (`EMS_INSTALL_DIR` / install
  root). A fresh install without `config/config.json` uses
  `config/dashboard-auth.json`; when a config exists and sets
  `dashboard.auth_file`, that path is honoured (relative paths resolve against
  the install root).
- **First visitor creates it.** When no password exists, the first browser user
  creates the initial password. Creation is atomic (`O_EXCL`) so a second
  concurrent visitor gets a clean `409` instead of overwriting it. Until the
  password is set, every setup/maintenance/discovery API stays blocked.
- **Malformed file is a recovery state, not setup.** If the shared file exists
  but cannot be parsed, `auth/status` reports `recovery_required` (with
  `auth_configured: true`, `requires_initial_password: false`); the UI shows a
  repair panel instead of first-password setup. `auth/setup` and `auth/login`
  return `409 auth_file_invalid` and the file is never auto-overwritten or
  auto-deleted — the operator repairs or removes it on the EMS host.
- **Shared password, separate sessions.** Admin and the Dashboard share the
  password but not the browser session: Admin uses its own `ems_admin_session`
  cookie, so a Dashboard login does not grant Admin access and vice versa.
- **Standard web hardening.** Authenticated mutating endpoints require the
  session CSRF token (`X-CSRF-Token`); read-only endpoints require a valid
  session. Logins are rate limited. The password is never logged or returned to
  the UI. No running EMS container is required for any of this.

The password file lives under the mounted install layout
(`config/dashboard-auth.json`), never only inside the Admin container image, so
EMS reuses the same password later.

### Web hardening headers

The Admin Console sends browser hardening headers on every UI and API response
(including auth failures and JSON errors):

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- a self-only Content Security Policy with `object-src 'none'`
- `Permissions-Policy` disabling geolocation, microphone, camera, payment and USB

HTTPS is not forced by default because the Admin Console is designed for trusted
local networks and self-signed certificates are confusing for many users. HTTPS
is available as an optional parallel listener (below), never a forced redirect,
and no HSTS or `upgrade-insecure-requests` is added.

### Admin HTTPS listener

Admin HTTPS reuses the shared `dashboard/https.py` helper; certificate
generation and the `SSLContext` build live there and are never duplicated in
`admin/`. `admin/https.py` is a thin wrapper that only adds Admin default paths
and install-root resolution.

The default Admin certificate paths are:

- `config/admin.crt`
- `config/admin.key`

They are resolved relative to the EMS install root, not the Admin container
working directory. If no mounted install root is available, Admin refuses to
generate a certificate rather than writing one into the read-only image.

When enabled (`--https` / `EMS_ADMIN_HTTPS_ENABLED`), Admin starts HTTP on 8090
and HTTPS on 8091 against the **same** `AdminRuntime` object, so auth sessions,
rate limiters, discovery/scan state, the mDNS provider, MQTT discovery and the
upgrade/backup job registries are shared and not duplicated across transports.
Only the per-listener transport flag differs: the HTTPS listener wraps its
socket with the `SSLContext` and sets `https_active=True`, which adds the
`Secure` attribute to Admin session cookies.

## Admin container update

Admin and EMS are managed as one strict paired system build through
`SystemAlignmentService` (`admin/system_alignment.py`). The running Admin process
writes a staged transition, starts an updater outside the current HTTP request,
returns a reconnect response, and the replacement Admin resumes from
`data/admin/state/`. Reconnect proves only the Admin stage; it does not complete
the EMS operation or write known-good state.

Admin image update decisions are made by digest/build identity, not by tag name
alone.

Concretely:

- **Server-side target.** The browser only ever sends a release *tag*. The target
  Admin image is derived server-side as
  `ghcr.io/basecubedev/ems-solarflow-admin:<tag>` (`admin/admin_update.py`). The
  tag must match the strict release pattern (`admin.releases.TAG_PATTERN`), so an
  image reference, path, or shell metacharacter — even without whitespace
  (`v0.7.0;rm`, `v0.7.0$(x)`, `../v0.7.0`) — is rejected. When a release catalogue
  is available the tag must also be a known, selectable release. The browser never
  supplies an image ref, a compose path, or a command.
- **Digest decision.** `decide_admin_update` compares the running Admin image
  identity (`EMS_ADMIN_CONTAINER_NAME` / `EMS_ADMIN_IMAGE`+`EMS_ADMIN_TAG`, plus
  `docker image inspect`) against the target by digest. Equal digests mean the
  release only retagged an unchanged Admin image (no update). Unknown digests are
  treated as uncertain and require explicit confirmation.
- **Strict EMS-upgrade gate.** Fresh Setup, Automated Setup and Guided Upgrade
  resolve the same Admin/EMS pair and call `SystemAlignmentService`. Config and
  EMS mutations remain blocked until the transition reaches
  `resources_verified`; an uncertain or mismatched Admin identity is a hard
  alignment failure, not a compatibility warning.
- **Pending state.** `data/admin/state/pending-admin-update.json` is written
  atomically (temp file + fsync + rename), tolerates a missing file, and surfaces
  a clear recovery error for corrupt JSON instead of crashing. It holds no
  passwords or secrets. `execute` refuses a no-op plan (`update_required=false`).
- **Out-of-request updater.** `POST …/admin-update/execute` writes the pending
  state, launches the updater, and returns `admin_update_started` with a reconnect
  hint *before* any pull or recreate. The default launcher (`AdminUpdateLauncher`)
  runs a detached `docker run --rm` sidecar built from the pending target image so
  the process running `docker compose up` is never the container being replaced;
  the same-process thread worker is opt-in via `EMS_ADMIN_UPDATE_LOCAL_WORKER=1`
  (dev/tests). The updater pulls the target image, keeps the Admin image and tag
  metadata in sync (`image:`, compose `EMS_ADMIN_TAG`, `.env.admin`), and runs
  `docker compose … up -d --no-deps --force-recreate` on the Admin **compose
  service** (`EMS_ADMIN_COMPOSE_SERVICE`, distinct from the inspect-only
  `EMS_ADMIN_CONTAINER_NAME`).
- **Resume.** After the restart (which drops the in-memory Admin session), the new
  Admin proves success by observing that its own running image now matches the
  plan target, moves the pending state to `admin_update_succeeded`, and the
  browser resumes the EMS upgrade via `…/admin-update/resume` (selecting the
  resumed release so a fresh plan cannot overwrite it).

The updater only touches the Admin image, the Admin compose/env tag, and the
Admin service. It never pulls or recreates the EMS/InfluxDB containers and never
touches EMS config or data — those changes belong to the Guided EMS Upgrade,
after user confirmation. All Admin update APIs require a valid Admin session and,
for POST, the `X-CSRF-Token`.

## System Build compatibility modes

A resolved System Build has one compatibility mode
(`admin/system_build.py: system_build_compatibility`), decided purely by its
build-id kind, and one resource strategy derived from it
(`system_build_resource_strategy`):

- **`modern_paired` → `embedded`.** A modern release/RC/latest/dev build ships a
  verified embedded resource bundle inside the running Admin image. The Admin is
  aligned to the selected build; Step 1 readiness requires the embedded bundle to
  verify.
- **`local` → `embedded`.** A local checkout bakes its own bundle and verifies it
  the same way.
- **`legacy_release` → `release_archive`.** A pre-contract CI build id
  (`<run>-<attempt>`, e.g. `123456789-1`) predates the embedded bundle and the
  modern transition/resume protocol. The running **modern Admin is kept** as the
  orchestration layer (never downgraded to the historical Admin image), and the
  selected EMS image's resources are prepared from the **exact historical
  tag/revision** through `ReleaseManager.prepare` (`ReleaseArchiveResources`) —
  never the running Admin's embedded bundle and never `main`.

`validate()` exposes `resource_strategy`, `embedded_resources_applicable` and an
`embedded_resources_valid` that is `null` (not `false`) when embedded resources
do not apply, so a legacy release is never blocked by a match it cannot satisfy.
Step 1 readiness gates on the selected strategy, so selecting a legacy release
never leaves both *Update Admin Server* and *Continue* disabled while reporting
ready.

**Identity separation.** For a legacy release the durable transition and the
known-good record store the running **orchestrator Admin** identity (modern)
separately from the **selected EMS build** (historical): the transition's
`orchestrator_admin` block and the known-good `admin_*` fields hold the modern
Admin, while the flat/`ems_*`/`selected_ems_build` fields hold the historical
EMS. Setup discovery authorization and resume compare the running Admin to the
orchestrator identity, not the selected build id. The legacy CI build id is
accepted by the transition parser on its validated format alone (the modern
revision-embedding integrity check still applies to modern build ids). Recovery
never downgrades the Admin to the historical release.

## Why this split matters

- A single source of truth (EMS/Core) means the Admin Console, the CLI and the
  Docker Bootstrap path all produce and validate the same `config/config.json`.
- Orchestration bugs in the Admin Console cannot silently change control
  semantics, because those live in EMS/Core.
- Backups and restores behave identically whether triggered from the Admin
  Console or the CLI, because both use the EMS backup/restore implementation.
  Bundled InfluxDB restore is the clearest case: the Admin Console orchestrates
  the existing EMS CLI restore flow (`emsctl.py backup restore`) instead of
  implementing a separate InfluxDB restore engine, and never pushes an InfluxDB
  archive through the generic file restore path.

## Related references

- [admin-discovery.md](admin-discovery.md) — full Admin Console technical
  reference (wizard, release/build identity, discovery, Docker setup, security).
- [architecture.md](architecture.md) — EMS project structure and runtime
  component boundaries.
- [backup-restore.md](backup-restore.md) — EMS backup/restore internals.
- [configuration.md](configuration.md) — EMS config schema and safety flags.
