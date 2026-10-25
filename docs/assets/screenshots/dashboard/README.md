# EMS Dashboard screenshots

Documentation screenshots of the live dashboard (**EMS SolarFlow**), embedded
from the step-by-step guides in [docs/user/dashboard/](../../../user/dashboard/).
Every one shows the current (Modern) dashboard; the deprecated Classic dashboard
at `/classic/` is not documented with screenshots any more.

All screenshots use **deterministic synthetic data** (fake devices, values,
backup names and image refs). They do **not** show a real installation and must
never contain real serial numbers, IP addresses, tokens, passwords or personal
hostnames.

Admin Console screenshots live in [`../admin/`](../admin/).

## Files

| File | View | Scenario |
| --- | --- | --- |
| `dashboard-overview.png` | Overview — status tiles, energy flow (System), Storage, Active rules | `normal` |
| `dashboard-overview-today.png` | Overview scrolled down — the four today tiles and Power today | `normal` |
| `dashboard-overview-per-device.png` | Overview in Midnight, energy flow Per device | `normal` |
| `dashboard-overview-narrow.png` | Overview on a phone (390×844), with the bar at the bottom | `normal` |
| `dashboard-phone-more.png` | The More sheet on a phone: other pages, Quick settings, Classic switch | `normal` |
| `dashboard-devices.png` | Devices — the two inverter cards, the grid meter, the flow per device | `normal` |
| `dashboard-devices-details.png` | Devices — WR1 with Firmware status and Device settings opened | `normal` |
| `dashboard-devices-offline.png` | Devices — WR2 offline, the grid meter importing | `offline-device` |
| `dashboard-energy.png` | Energy — Basic: periods, context strip, months, years and lifetime | `normal` |
| `dashboard-energy-expert.png` | Energy — Expert with September's month detail | `normal` |
| `dashboard-analytics.png` | Analytics — Overview view with the Grid overlay, KPI cards | `normal` |
| `dashboard-control.png` | Control signed in — What the EMS is doing now, Runtime settings | `write-mode` |
| `dashboard-control-pipeline.png` | Control — How the target is made, Decision per device | `write-mode` |
| `dashboard-control-device.png` | Control — Decision per device with WR1's five stages opened | `write-mode` |
| `dashboard-control-readonly.png` | Control signed out with a password set — the runtime card asks for a sign-in | `auth-readonly` |
| `dashboard-sign-in.png` | The Sign in to the dashboard dialog | `auth-readonly` |
| `dashboard-schedule.png` | Schedule — one saved rule and the next seven days | `write-mode` |
| `dashboard-diagnose.png` | Diagnose — the five diagnosis cards and a Hardware report | `write-mode` |
| `dashboard-logs.png` | Logs — level filter, Follow, Service level, the log lines | `write-mode` |
| `dashboard-maintenance.png` | Maintenance — status, safety notes, backups | `write-mode` |
| `dashboard-appearance-menu.png` | Overview with the Appearance menu open | `normal` |
| `dashboard-appearance-themes.png` | The same Overview region in Graphite and in Midnight, side by side | `normal` |
| `dashboard-appearance-density.png` | The same Devices region at Normal and at Compact density, side by side | `normal` |

The README-level previews `docs/assets/preview-*.jpg`, embedded from
[README.md](../../../../README.md) and [docs/dashboard.md](../../../dashboard.md),
come from the same spec as whole pages rather than a 1440×900 window:

| File | View | Scenario |
| --- | --- | --- |
| `preview-aggregated.jpg` | Overview, the whole page | `normal` |
| `preview-devices.jpg` | Devices, the whole page with the power chart | `normal` |
| `preview-energy.jpg` | Energy in Basic with the month table open | `normal` |
| `preview-analytics.jpg` | Analytics, Grid view over 24 h | `normal` |
| `preview-control.jpg` | Control signed in, the whole page | `write-mode` |
| `preview-diagnose.jpg` | Diagnose, a Hardware report with an offline device | `offline-device`, signed in |
| `preview-logs.jpg` | Logs with the offline device's error and warning | `offline-device`, signed in |
| `preview-maintenance.jpg` | Maintenance with a checked config upgrade | `write-mode` |

## How to refresh

Every image is written by one Playwright spec,
[`tests/e2e-dashboard/capture-docs.spec.ts`](../../../../tests/e2e-dashboard/capture-docs.spec.ts),
in Chromium, against `scripts/serve_dashboard_preview.py` with the synthetic
payloads in `scripts/dashboard_preview_data.py`. No hardware, Docker, MQTT,
InfluxDB, `config.json` or runtime state is involved; the Schedule capture saves
a rule into the preview's own scratch copy and deletes it again.

```bash
EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
  --config=playwright.dashboard.config.ts --project=chromium capture-docs
```

or, together with the Admin Console's screenshots:

```bash
./scripts/capture-docs-screenshots.sh            # Admin + Dashboard
./scripts/capture-docs-screenshots.sh dashboard  # Dashboard only
```

One capture, by its file name:

```bash
EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
  --config=playwright.dashboard.config.ts --project=chromium capture-docs \
  -g dashboard-energy-expert
```

Preview interactively (no capture) to tweak the synthetic data:

```bash
python3 scripts/serve_dashboard_preview.py --scenario write-mode
```

## Capture settings

- Browser: Chromium, device scale factor 1, English locale.
- Guide PNGs: the 1440×900 window, or 390×844 for a phone. A subject further
  down the page is scrolled to just under the top bar.
- Preview JPGs: the whole page at 1440 wide, quality 85.
- Appearance: English, Graphite, Normal density, flow **System**, Energy
  **Basic**, unless the file is about another choice.
- Scenario: the server runs `normal`; a capture that needs another one sets the
  preview's `ems-preview-scenario` cookie for its own browser context.
- The day is pinned to 18:30 today (`tests/e2e-dashboard/pin-the-day.ts`), so
  the today tiles and Power today never show an empty morning.
- The appearance strips photograph the same 714×560 region once per value and
  lay the columns side by side on a blank page, 12 px apart.

Add an image by adding a test named after the file to the spec, embedding it in
a guide and listing it above — `tests/test_docs_user_guides.py` checks that the
spec, the committed files, the embeds and this README agree.

## Editing the demo data

Edit the scenario builders in
[`scripts/dashboard_preview_data.py`](../../../../scripts/dashboard_preview_data.py)
and re-run the capture. Keep all values synthetic.
