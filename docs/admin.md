# EMS SolarFlow Admin Console

The Admin Console (product name **EMS SolarFlow Admin**) is the local browser UI
for setup and maintenance. It runs next to EMS, not inside the control loop. EMS
still owns the control logic; the Admin Console is UI and orchestration only.

The Admin Console is a Docker path. Run it only on a trusted local machine.

## Use it for

- first setup
- device discovery
- config generation
- diagnostics
- updates
- backups
- restore

## Two flows

The Admin Console start screen detects your install state and recommends one of
two flows. It never acts silently.

- **Set up a new system** — for a fresh install or a deliberate reinstall.
  See [setup/admin-setup.md](setup/admin-setup.md).
- **Manage my existing system** — for updates, config changes, diagnostics,
  backups and restore. See [setup/admin-maintenance.md](setup/admin-maintenance.md).

Mutating actions preview the change and ask for confirmation. Config apply,
guided upgrade and restore back up what they replace first.

## It does not replace EMS

- EMS still runs the control loop and remains the source of truth.
- EMS owns config semantics and backup/restore behavior — every backup is a
  normal EMS backup archive.
- Docker is the runtime. The Admin Console orchestrates EMS containers; it does
  not replace them.

## Files

| Path | Purpose |
| --- | --- |
| `config/config.json` | EMS config |
| `data/` | EMS runtime data (state, history, optional analytics) |
| `data/backups/` | EMS backup archives |
| `data/admin/` | Admin Console state, release cache, staging and logs |

## Start

```bash
deploy/admin/start-admin-setup.sh
```

Then open:

```text
http://127.0.0.1:8090
```

If device discovery cannot see your LAN, start it with host networking:

```bash
deploy/admin/start-admin-setup.sh --hostnet
```

## Safety

Do not expose the Admin Console to the internet. A deployment-capable Admin
container controls the host Docker engine, which is effectively root-equivalent.
Run it only on a trusted local network.

Full technical reference: [admin-discovery.md](admin-discovery.md).
