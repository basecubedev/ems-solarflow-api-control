# Admin Console screenshots

Documentation screenshots of the Admin Console (**EMS SolarFlow Admin**),
embedded from the step-by-step guides in
[docs/user/admin/](../../../user/admin/), from
[docs/user/admin-console.md](../../../user/admin-console.md), the other
`docs/user/admin-*.md` pages and [README.md](../../../../README.md). Every one
shows the current (Modern) console; the deprecated Classic console at
`/classic/` is not documented with screenshots any more.

Dashboard screenshots live in [`../dashboard/`](../dashboard/) and are captured
by `tests/e2e-dashboard/capture-docs.spec.ts`. Regenerate both sets with
[`scripts/capture-docs-screenshots.sh`](../../../../scripts/capture-docs-screenshots.sh).

All screenshots use **deterministic demo data** (fake devices, IPs, serials,
image refs and backup names). They do **not** show a real installation and must
never contain real serial numbers, IP addresses, tokens, passwords or personal
hostnames.

## Files

| File | View | Scenario |
| --- | --- | --- |
| `admin-first-start-password.png` | First start — create the shared password | `first-start` |
| `admin-login.png` | Sign in | `signed-out` |
| `admin-assistant-fresh.png` | Assistant on a host with nothing installed — Install EMS to do, the rest later | `fresh` |
| `admin-assistant.png` | Assistant on an installed system — the first backup is the next step, the tasks below | `no-backups` |
| `admin-overview.png` | Overview — everything in order, the four tiles, Control & safety, Versions | `installed` |
| `admin-overview-phone.png` | Overview on a phone (390×844), with the bar at the bottom | `installed` |
| `admin-phone-more.png` | The More sheet on a phone: other pages, Quick settings, Classic switch | `installed` |
| `admin-guided-setup-begin.png` | Guided setup — the start card with the five steps | `fresh` |
| `admin-guided-setup-start.png` | Guided setup — 01 System Build, verified and ready | `setup` |
| `admin-discovery-preview.png` | Guided setup — 02 Devices: search sources, progress, devices found | `setup` |
| `admin-guided-setup-config-preview.png` | Guided setup — 03 Settings: the check says Ready, the hardware in the draft | `setup` |
| `admin-setup-deployment.png` | Guided setup — 04 Prepare: the deployment is ready | `setup` |
| `admin-setup-start.png` | Guided setup — 05 Start EMS: EMS runs, Open the dashboard | `setup` |
| `admin-updates.png` | Updates — what runs now, the guided upgrade, the available versions | `installed` |
| `admin-upgrade-verifying.png` | Updates — a version chosen and being prepared: images downloaded and checked | `installed` |
| `admin-guided-upgrade-plan.png` | Updates — a version chosen and checked: options and what happens | `installed` |
| `admin-upgrade-running.png` | Updates — an update running, EMS being installed | `upgrade-running` |
| `admin-admin-update-reconnect.png` | Updates — the Admin console restarting, the page reconnecting | `upgrade-reconnect` |
| `admin-upgrade-completed.png` | Updates — Update complete | `upgrade-done` |
| `admin-settings-features.png` | Settings & devices — Features, Winter mode opened | `installed` |
| `admin-settings-safety.png` | Settings & devices — Safety | `installed` |
| `admin-mqtt-broker.png` | Settings & devices — Add-ons: the built-in MQTT broker running, its address and two accounts | `installed` |
| `admin-mqtt-mapping.png` | Settings & devices — Devices: the MQTT mapping with a mapped balcony inverter and the live topics grouped and labelled | `installed` |
| `admin-mqtt-mapping-editor.png` | Settings & devices — Devices: editing the mapped balcony inverter, its source options and the server's preview | `installed` |
| `admin-settings-devices.png` | Settings & devices — Devices: Find devices, What the search may use, the MQTT mapping, the devices | `installed` |
| `admin-settings-device.png` | Settings & devices — Devices, Inverter 1 opened | `installed` |
| `admin-settings-search.png` | Settings & devices — Devices after Search my network: the saved devices in the settings, a new inverter found | `installed` |
| `admin-settings-review.png` | Settings & devices — one change checked, ready for Save and apply | `installed` |
| `admin-services.png` | Services — saved settings EMS has not read yet, Restart EMS now | `settings-saved` |
| `admin-backups.png` | Backups — create, keep a copy elsewhere, your backups | `installed` |
| `admin-backup-restore.png` | Backups — the restore preview of a settings backup | `installed` |
| `admin-diagnostics.png` | Diagnostics — the checks run, all passed | `installed` |
| `admin-system.png` | System — files, device telemetry over MQTT, guided setup | `mqtt` |
| `admin-system-recovery.png` | System — Unfinished setup or update, a guided setup stuck | `recovery` |
| `admin-appearance-menu.png` | Overview with the Appearance menu open | `installed` |
| `admin-appearance-themes.png` | The same Overview region in Graphite and in Midnight, side by side | `installed` |
| `admin-appearance-density.png` | The same Backups region at Normal and Compact density, side by side | `installed` |

## How to refresh

Every file is written by one script,
[`scripts/capture_admin_docs.mjs`](../../../../scripts/capture_admin_docs.mjs),
with Playwright Chromium:

```bash
node scripts/capture_admin_docs.mjs                      # every file
node scripts/capture_admin_docs.mjs admin-overview.png   # some of them
```

It starts [`scripts/serve_admin_docs_preview.py`](../../../../scripts/serve_admin_docs_preview.py)
on a free loopback port. The preview serves the **real** Modern build
(`admin/static/modern/`) at its root with deterministic demo API answers from
[`tests/fixtures/admin_docs/`](../../../../tests/fixtures/admin_docs). The
settings page, its check, the telemetry view and the guided setup's device
search and device plan come from the shipped Admin builders, over the demo
installation's `config.json` (`demo_installation.json` laid over the shipped
`config/config.template.json`). No hardware, Docker, discovery, MQTT,
`config.json` or password is involved, and nothing is written to
config/runtime state.

Preview interactively in a browser (no capture) to tweak the demo data:

```bash
python3 scripts/serve_admin_docs_preview.py
# then open http://127.0.0.1:8092/#/overview
# or        http://127.0.0.1:8092/?scenario=fresh#/assistant
```

## Capture settings

- Browser: Chromium, device scale factor 1, English locale, Europe/Berlin, the
  clock fixed to 5 July 2026, 15:30.
- Guide PNGs: the 1440×900 window, or 390×844 for a phone. A page shown whole
  grows the window to its own height, so the menu stays beside it; a subject
  further down the page is scrolled to just under the top bar.
- Appearance: English, Graphite, Normal density, motion off, unless the file is
  about another choice.
- Scenario: the page's own `?scenario=` names the installation it shows, and
  the preview answers that page's API calls for it. `installed` is the default.
- The appearance strips photograph the same 714×560 region once per value and
  lay the columns side by side on a blank page, 12 px apart.

Add an image by adding an entry named after the file to `SHOTS` in the script,
embedding it in a guide and listing it above — `tests/test_docs_user_guides.py`
checks that the script, the committed files, the embeds and this README agree.

## Editing the demo data

To change what a screen shows (device names, versions, backup list, …), edit
the matching fixture in `tests/fixtures/admin_docs/` and re-run the capture; to
add a state, add a scenario to `build_scenarios()` in the preview. Keep all
values fake.
