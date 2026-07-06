# Admin Console screenshots

Documentation screenshots of the Admin Console (**EMS SolarFlow Admin**). They
are embedded from [docs/user/admin-console.md](../../../user/admin-console.md)
and [README.md](../../../../README.md).

All screenshots use **deterministic demo data** (fake devices, IPs, serials,
image refs and backup names). They do **not** show a real installation and must
never contain real serial numbers, IP addresses, tokens, passwords or personal
hostnames.

## Files

| File | Screen |
| --- | --- |
| `admin-landing.png` | Start page (Setup vs. Maintenance) |
| `admin-guided-setup-start.png` | Guided Setup — release step |
| `admin-discovery-preview.png` | Guided Setup — device discovery |
| `admin-guided-setup-config-preview.png` | Guided Setup — generated config preview |
| `admin-maintenance-overview.png` | Maintenance — read-only overview |
| `admin-backup-restore.png` | Backup / restore |
| `admin-guided-upgrade-plan.png` | Guided upgrade plan |
| `admin-admin-update-reconnect.png` | Admin Console self-update reconnect overlay |

## How to refresh

The screenshots are generated from the **real** Admin static UI
(`admin/static/`) driven by a local docs-preview server that serves
deterministic demo API responses from
[`tests/fixtures/admin_docs/`](../../../../tests/fixtures/admin_docs). No
hardware, Docker, discovery, MQTT, `config.json` or password is involved, and
nothing is written to config/runtime state.

Requirements: `firefox` (headless) and ImageMagick `convert` — the same tools
used by `scripts/capture_dashboard_previews.py`.

Regenerate every screenshot:

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

Screens: `landing`, `guided-setup-start`, `discovery`, `config-preview`,
`maintenance-overview`, `backup-restore`, `guided-upgrade`,
`admin-update-reconnect`.

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
