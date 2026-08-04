# EMS Dashboard screenshots

Documentation screenshots of the live dashboard (**EMS SolarFlow Control**),
embedded from the step-by-step guides in
[docs/user/dashboard/](../../../user/dashboard/).

All screenshots use **deterministic synthetic data** (fake devices, values,
backup names and image refs). They do **not** show a real installation and must
never contain real serial numbers, IP addresses, tokens, passwords or personal
hostnames.

Admin Console screenshots live in [`../admin/`](../admin/). The README-level
preview images (`docs/assets/preview-*.jpg`) are a separate, older set generated
by `scripts/capture_dashboard_previews.py`.

## Files

| File | View | Scenario |
| --- | --- | --- |
| `dashboard-overview.png` | Overview — tiles, Live Flow, Rules | `normal` |
| `dashboard-overview-narrow.png` | Overview at a narrow viewport | `normal` |
| `dashboard-devices.png` | Devices — per-inverter cards | `normal` |
| `dashboard-devices-offline.png` | Devices — one inverter offline | `offline-device` |
| `dashboard-devices-readonly.png` | Devices — unauthenticated read-only | `auth-readonly` |
| `dashboard-energy.png` | Energy Delivered | `normal` |
| `dashboard-analytics.png` | Analytics | `normal` |
| `dashboard-control.png` | Control pipeline + runtime settings | `write-mode` |
| `dashboard-control-readonly.png` | Control without a session | `auth-readonly` |
| `dashboard-diagnose.png` | Diagnose | `write-mode` |
| `dashboard-logs.png` | Logs | `write-mode` |
| `dashboard-maintenance.png` | Maintenance — backup / restore / config upgrade | `write-mode` |

## How to refresh

Generated from the **real** dashboard static UI (`dashboard/static/`) driven by
`scripts/serve_dashboard_preview.py` with the synthetic payloads in
`scripts/dashboard_preview_data.py`. No hardware, Docker, MQTT, InfluxDB,
`config.json` or runtime state is involved.

Requirements: `firefox` (headless) and ImageMagick `convert`.

Regenerate every screenshot (Admin and Dashboard):

```bash
./scripts/capture-docs-screenshots.sh
```

Dashboard only, or a subset:

```bash
python3 scripts/capture_dashboard_docs.py
python3 scripts/capture_dashboard_docs.py --screens overview control
```

Preview interactively (no capture) to tweak the synthetic data:

```bash
python3 scripts/serve_dashboard_preview.py --scenario write-mode
```

## Capture settings

- Browser: headless Firefox, device scale factor 1.
- Desktop window `1440x2600`; the narrow view uses `390x1800`.
- Output: PNG, content-trimmed with a small consistent gutter.
- Theme: default dashboard theme (dark).
- One preview server serves every scenario; the capture script switches
  `scenario_name` between screens rather than restarting it.
- Each screen is retried until its content height clears a floor, so a slow first
  frame is never kept.

## Editing the demo data

Edit the scenario builders in
[`scripts/dashboard_preview_data.py`](../../../../scripts/dashboard_preview_data.py)
and re-run the capture. Keep all values synthetic.
