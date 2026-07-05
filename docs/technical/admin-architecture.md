# Admin architecture

This page describes how the Admin Console, the Docker Bootstrap deployment
layout, and EMS/Core relate to each other. It is the architecture reference for
the Admin path. For the full Admin internals (wizard, release/build-identity
gating, network discovery, Docker setup and security) see
[admin-discovery.md](admin-discovery.md). For the user-facing guide see
[../user/admin-console.md](../user/admin-console.md).

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

Admin update is a two-phase flow. The running Admin process writes a pending
state file, starts an updater outside the current HTTP request, returns a
reconnect response, and the replacement Admin resumes from `data/admin/state/`.

Admin image update decisions are made by digest/build identity, not by tag name
alone.

Concretely:

- **Server-side target.** The browser only ever sends a release *tag*. The target
  Admin image is derived server-side as
  `ghcr.io/basecubedev/ems-solarflow-admin:<tag>` (`admin/admin_update.py`); an
  arbitrary image reference, path, or shell metacharacter in the "tag" is
  rejected. The browser never supplies an image ref, a compose path, or a
  command.
- **Digest decision.** `decide_admin_update` compares the running Admin image
  identity (`EMS_ADMIN_CONTAINER_NAME` / `EMS_ADMIN_IMAGE`+`EMS_ADMIN_TAG`, plus
  `docker image inspect`) against the target by digest. Equal digests mean the
  release only retagged an unchanged Admin image (no update). Unknown digests are
  treated as uncertain and require explicit confirmation.
- **Pending state.** `data/admin/state/pending-admin-update.json` is written
  atomically (temp file + fsync + rename), tolerates a missing file, and surfaces
  a clear recovery error for corrupt JSON instead of crashing. It holds no
  passwords or secrets.
- **Out-of-request updater.** `POST …/admin-update/execute` writes the pending
  state, launches `admin/update_apply.py` (a local delayed worker thread, or the
  `python -m admin.update_apply --plan-id <id>` sidecar), and returns
  `admin_update_started` with a reconnect hint *before* any pull or recreate. The
  updater pulls the target image, repoints the Admin compose/env tag, and runs
  `docker compose … up -d --no-deps --force-recreate` on the Admin service only.
- **Resume.** After the restart (which drops the in-memory Admin session), the new
  Admin proves success by observing that its own running image now matches the
  plan target, moves the pending state to `admin_update_succeeded`, and the
  browser resumes the EMS upgrade via `…/admin-update/resume`.

The updater only touches the Admin image, the Admin compose/env tag, and the
Admin service. It never pulls or recreates the EMS/InfluxDB containers and never
touches EMS config or data — those changes belong to the Guided EMS Upgrade,
after user confirmation. All Admin update APIs require a valid Admin session and,
for POST, the `X-CSRF-Token`.

## Why this split matters

- A single source of truth (EMS/Core) means the Admin Console, the CLI and the
  Docker Bootstrap path all produce and validate the same `config/config.json`.
- Orchestration bugs in the Admin Console cannot silently change control
  semantics, because those live in EMS/Core.
- Backups and restores behave identically whether triggered from the Admin
  Console or the CLI, because both use the EMS backup/restore implementation.

## Related references

- [admin-discovery.md](admin-discovery.md) — full Admin Console technical
  reference (wizard, release/build identity, discovery, Docker setup, security).
- [architecture.md](architecture.md) — EMS project structure and runtime
  component boundaries.
- [backup-restore.md](backup-restore.md) — EMS backup/restore internals.
- [configuration.md](configuration.md) — EMS config schema and safety flags.
