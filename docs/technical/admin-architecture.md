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
