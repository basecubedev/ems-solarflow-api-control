# Admin Console: Set up a new system

Best for most users who want a browser-guided setup with device discovery and
later maintenance. The Admin Console is orchestration/UI only; the EMS core stays
the source of truth. It is a Docker-only path.

Use this for a fresh install or a deliberate reinstall. To update or change an
existing system, use [admin-maintenance.md](admin-maintenance.md) instead. Setup
writes only `config/config.json` (backing up any existing config first); it does
not touch `data/` or runtime databases.

## Start

```bash
deploy/admin/start-admin-setup.sh
```

Then open:

```text
http://127.0.0.1:8090
```

If device discovery cannot see your LAN, start it with host networking instead:

```bash
deploy/admin/start-admin-setup.sh --hostnet
```

Run the Admin Console only on a trusted local machine.

## Layout

```text
Config:  ./config/config.json
Data:    ./data/
Admin:   ./data/admin/
Compose: ./docker-compose.yml
```

`./data/admin/` holds only Admin Console state, releases, staging, backup-set
metadata and logs. It is not a live EMS runtime layout.

## Two Admin Console flows

The Admin Console opens on a start/router screen that detects the current install
state and recommends the safest of exactly **two** flows:

- **Set up a new system** — for first-time setup or a clean reinstall.
- **Manage my existing system** — for updates, backups, diagnostics, changing
  settings, and migrating an existing config (see
  [admin-maintenance.md](admin-maintenance.md)).

Docker Bootstrap and Developer Setup are documentation-level alternatives
([docker-bootstrap.md](docker-bootstrap.md),
[developer-setup.md](developer-setup.md)); they are not selectable flows inside
the Admin Console.

The router recommends and preselects a flow but never acts silently:

- A fresh install root is recommended for **Set up a new system**.
- Any existing, legacy, partial or Admin-prepared install is recommended for
  **Manage my existing system** and does not auto-start the setup wizard.
- Choosing **Set up a new system** while an install already exists requires an
  explicit confirmation before any replace/reset behavior.
- A legacy root `config.json` routes to Maintenance, which offers to migrate it
  to `config/config.json` first (see [config-layout.md](config-layout.md)).

## Steps (Set up a new system)

1. Start the Admin Console (see **Start** above) and open
   `http://127.0.0.1:8090`.
2. Pick **Set up a new system**, and work through **01 Release**, **02 Devices**,
   **03 Config**.
3. Apply the generated config. The Admin Console writes it to the standard
   `config/config.json` and backs up any existing config first.
4. Start EMS and run `emsctl.py diagnose`.

After setup, open the dashboard at `http://<host-ip>:8080`, work through the
[first-run checklist](../first-run-checklist.md), and use
[admin-maintenance.md](admin-maintenance.md) for later updates and backups.

If a legacy root `config.json` is present, the Admin Console can use it as source
data, but the applied target is always `config/config.json`.

Full detail: [../admin-discovery.md](../admin-discovery.md). Layout and legacy
migration: [config-layout.md](config-layout.md).
