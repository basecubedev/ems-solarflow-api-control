# Option A: Guided Admin Setup

Best for normal users who want a browser-based setup with device discovery and
later maintenance. Admin is orchestration/UI only; the EMS core stays the source
of truth. Admin Setup is a Docker-only path.

## Layout

```text
Config:  ./config/config.json
Data:    ./data/
Admin:   ./data/admin/
Compose: ./docker-compose.yml
```

`./data/admin/` holds only Admin state, releases, staging, backups and logs. It
is not a live EMS runtime layout.

## Two Admin flows

The Admin UI opens on a start/router screen that detects the current install
state and recommends the safest of exactly **two** flows:

- **Set up a new system** — for first-time setup or a clean reinstall.
- **Manage my existing system** — for updates, backups, diagnostics, changing
  settings, and migrating an existing config.

The Docker bootstrap and developer/manual paths are documentation-level
alternatives ([docker-bootstrap.md](docker-bootstrap.md),
[developer-setup.md](developer-setup.md)); they are not selectable flows inside
the Admin UI.

The router recommends and preselects a flow but never acts silently:

- A fresh install root is recommended for **Set up a new system**.
- Any existing, legacy, partial or Admin-prepared install is recommended for
  **Manage my existing system** and does not auto-start the setup wizard.
- Choosing **Set up a new system** while an install already exists requires an
  explicit confirmation before any replace/reset behavior.
- A legacy root `config.json` routes to Maintenance, which offers to migrate it
  to `config/config.json` first (see [config-layout.md](config-layout.md)).

## Steps (Set up a new system)

1. Start the Admin server (see [../admin-discovery.md](../admin-discovery.md)).
2. Open the Admin UI, pick **Set up a new system**, and work through
   **01 Release**, **02 Devices**, **03 Config**.
3. Apply the generated config. Admin writes it to the standard
   `config/config.json` and backs up any existing config first.
4. Start EMS and run `emsctl.py diagnose`.

If a legacy root `config.json` is present, Admin can use it as source data, but
the applied target is always `config/config.json`.

Full detail: [../admin-discovery.md](../admin-discovery.md). Layout and legacy
migration: [config-layout.md](config-layout.md).
