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
| `dashboard-energy.png` | Energy Delivered — Basic | `normal` |
| `dashboard-energy-expert.png` | Energy Delivered — Expert, with the channel rows | `normal` |
| `dashboard-analytics.png` | Analytics | `normal` |
| `dashboard-control.png` | Control pipeline + runtime settings | `write-mode` |
| `dashboard-control-readonly.png` | Control without a session | `auth-readonly` |
| `dashboard-diagnose.png` | Diagnose | `write-mode` |
| `dashboard-logs.png` | Logs | `write-mode` |
| `dashboard-maintenance.png` | Maintenance — backup / restore / config upgrade | `write-mode` |
| `dashboard-appearance-palettes.png` | Appearance — the same cockpit in Signal, Void and Copper | `appearance gallery` |
| `dashboard-appearance-styles.png` | Appearance — the same cockpit in Glass, Console and Brutal | `appearance gallery` |
| `dashboard-appearance-density.png` | Appearance — the same cockpit at Compact, Normal and Roomy | `appearance gallery` |
| `dashboard-modern-overview.png` | Modern — Overview in Graphite | `normal` |
| `dashboard-modern-midnight-devices.png` | Modern — Overview in Midnight and German, energy flow per device | `normal` |
| `dashboard-modern-phone.png` | Modern — Overview on a phone, with the bottom bar | `normal` |

The three `*-appearance-*` strips are written by
[`scripts/capture_appearance_gallery.py`](../../../../scripts/capture_appearance_gallery.py)
rather than by the capture script above: each is one page photographed three
times with a single appearance axis changed, which is the only way to document
a choice whose entire effect is how something looks.

The three `dashboard-modern-*` images are written by the Playwright spec
[`tests/e2e-dashboard/capture-docs.spec.ts`](../../../../tests/e2e-dashboard/capture-docs.spec.ts)
against the same preview server, in Chromium. Modern renders its history after
the load event, so its capture waits for the chart instead of retrying a
height:

```bash
EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
  --config=playwright.dashboard.config.ts --project=chromium capture-docs
```

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
