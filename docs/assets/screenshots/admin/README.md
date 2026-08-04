# Admin Console screenshots

Documentation screenshots of the Admin Console (**EMS SolarFlow Admin**). They
are embedded from the step-by-step guides in
[docs/user/admin/](../../../user/admin/), from
[docs/user/admin-console.md](../../../user/admin-console.md) and from
[README.md](../../../../README.md).

Dashboard screenshots live in
[`../dashboard/`](../dashboard/) and are captured by
`scripts/capture_dashboard_docs.py`. Regenerate both sets with
[`scripts/capture-docs-screenshots.sh`](../../../../scripts/capture-docs-screenshots.sh).

All screenshots use **deterministic demo data** (fake devices, IPs, serials,
image refs and backup names). They do **not** show a real installation and must
never contain real serial numbers, IP addresses, tokens, passwords or personal
hostnames.

## Files

| File | Screen | Capture id |
| --- | --- | --- |
| `admin-first-start-password.png` | First start — create the shared password | `password-setup` |
| `admin-login.png` | Login | `login` |
| `admin-landing.png` | Start page (Setup vs. Maintenance) | `landing` |
| `admin-guided-setup-start.png` | Guided Setup — 01 Release | `guided-setup-start` |
| `admin-discovery-preview.png` | Guided Setup — 02 Devices | `discovery` |
| `admin-guided-setup-config-preview.png` | Guided Setup — 03 Config | `config-preview` |
| `admin-setup-deployment.png` | Guided Setup — 04 Prepare deployment | `setup-deployment` |
| `admin-setup-start.png` | Guided Setup — 05 Start EMS | `setup-start-done` |
| `admin-maintenance-hub.png` | Maintenance hub (three paths) | `maintenance-hub` |
| `admin-maintenance-overview.png` | Maintenance — read-only overview | `maintenance-overview` |
| `admin-maintenance-diagnostics.png` | Maintenance — EMS diagnostics card | `maintenance-diagnostics` |
| `admin-maintenance-config-hardware.png` | Maintenance — Configuration & hardware card | `maintenance-config-hardware` |
| `admin-maintenance-mqtt.png` | Maintenance — Zendure MQTT telemetry card | `maintenance-mqtt` |
| `admin-maintenance-recovery.png` | Maintenance — Workflow recovery card | `maintenance-recovery` |
| `admin-backup-restore.png` | Backup / restore | `backup-restore` |
| `admin-guided-upgrade-plan.png` | Guided upgrade plan | `guided-upgrade` |
| `admin-upgrade-running.png` | Guided upgrade — live validation | `upgrade-run-3` |
| `admin-upgrade-completed.png` | Guided upgrade — completed | `upgrade-done` |
| `admin-admin-update-reconnect.png` | Admin Console self-update reconnect overlay | `admin-update-reconnect` |

## How to refresh

The screenshots are generated from the **real** Admin static UI
(`admin/static/`) driven by a local docs-preview server that serves
deterministic demo API responses from
[`tests/fixtures/admin_docs/`](../../../../tests/fixtures/admin_docs). No
hardware, Docker, discovery, MQTT, `config.json` or password is involved, and
nothing is written to config/runtime state.

Requirements: `firefox` (headless) and ImageMagick `convert` — the same tools
used by `scripts/capture_dashboard_previews.py`.

Regenerate every screenshot (Admin and Dashboard):

```bash
./scripts/capture-docs-screenshots.sh
```

Admin only:

```bash
python3 scripts/capture_admin_docs.py
```

Regenerate a subset:

```bash
python3 scripts/capture_admin_docs.py --screens landing maintenance-overview
```

Preview interactively in a browser (no capture) to tweak the demo data:

```bash
python3 scripts/serve_admin_docs_preview.py
# then open http://127.0.0.1:8092/?screen=maintenance-overview
```

Screen ids are listed in the **Files** table above; `scripts/capture_admin_docs.py
--help` prints the authoritative set. The preview server also drives the
video-only screens `upgrade-run-1`, `upgrade-run-2` and `upgrade-run-4`.

Guided Setup steps 02–05 are authorized by a **server-confirmed setup
transition**, never by browser state, so the preview serves one from
`guided_setup_demo.json` (`system_alignment_status`). Without it every setup
screen silently falls back to step 01 — see the distinctness test in
`tests/test_docs_user_guides.py`.

## Capture settings

- Browser: headless Firefox, window `1440x1600`, device scale factor 1.
- Output: PNG, content-trimmed with a small consistent gutter.
- Theme: default Admin theme (dark).
- The capture script retries a screen until its content has rendered, so a slow
  first frame is never kept.

## Editing the demo data

To change what a screen shows (device names, versions, backup list, …), edit the
matching fixture in `tests/fixtures/admin_docs/` and re-run the capture. Keep all
values fake. To change how a screen is reached, edit the driver in
`scripts/admin_docs_preview.js`.
