# Appliance Manager documentation screenshots

Documentation screenshots of the Appliance Manager, embedded from the
step-by-step guides in [docs/user/appliance/](../../../user/appliance/). Every
one shows the current (Modern) console at the appliance's address; the
deprecated Classic console at `/classic/` is not documented with screenshots
any more.

Generated, never hand-edited. Each image is written by one Playwright spec,
`tests/e2e-appliance/capture-docs.spec.ts`, against the deterministic test
server, so no capture contains a real host name, address, serial or key.

## Regenerating

```bash
EMS_APPLIANCE_CAPTURE_DOCS=1 npx playwright test \
    --config=playwright.appliance.config.ts capture-docs --project=chromium
```

or `./scripts/capture-docs-screenshots.sh appliance`. The captures are
excluded from the config unless that variable is set, so the normal appliance
suite -- which runs in CI on every change to `appliance/**`, and in the RC tier
ahead of a clean-tree check -- can never overwrite them.

## Capture IDs

| ID | Page | Used in |
| --- | --- | --- |
| `appliance-first-start-password` | First start — Create the appliance password, both fields | [first-start.md](../../../user/appliance/first-start.md) |
| `appliance-login` | Sign in on an appliance that has a password, one field | [first-start.md](../../../user/appliance/first-start.md) |
| `appliance-assistant-fresh` | Assistant on a freshly flashed card — Install EMS Admin to do, Set up the EMS later | [assistant.md](../../../user/appliance/assistant.md) |
| `appliance-assistant` | Assistant on an installed appliance — Set up the EMS next with Open EMS Admin, the seven tasks | [assistant.md](../../../user/appliance/assistant.md) |
| `appliance-overview` | Overview — the verdict with what needs attention, the six tiles, the quick actions | [overview.md](../../../user/appliance/overview.md) |
| `appliance-overview-phone` | Overview on a phone (390×844), with the bar at the bottom | [appearance.md](../../../user/appliance/appearance.md) |
| `appliance-phone-more` | The More sheet on a phone: other pages, Quick settings, Open EMS Admin, Classic switch | [appearance.md](../../../user/appliance/appearance.md) |
| `appliance-admin-install` | EMS Admin on a fresh card — Install EMS Admin, the version choice | [first-start.md](../../../user/appliance/first-start.md) |
| `appliance-updates` | Updates — the three cards, the Manager package, EMS Admin versions, the operating system | [updates.md](../../../user/appliance/updates.md) |
| `appliance-update-plan` | Updates — the plan for the security updates, waiting for Install | [updates.md](../../../user/appliance/updates.md) |
| `appliance-update-running` | Updates — an OS update running, the banner with its stage and steps | [updates.md](../../../user/appliance/updates.md) |
| `appliance-recovery` | EMS Admin — installed version, known-good versions, Start/Stop/Restart, Preview repair, Roll back | [recovery.md](../../../user/appliance/recovery.md) |
| `appliance-network-wifi` | Network — the interfaces, Change the WLAN after a scan, hostname and time zone | [network.md](../../../user/appliance/network.md) |
| `appliance-backup-access` | SSH & backup access — SSH, rescue account, keys, backup account, export, paths, commands | [backup.md](../../../user/appliance/backup.md) |
| `appliance-diagnostics` | Diagnostics — host health, services, the support archive, the logs | [recovery.md](../../../user/appliance/recovery.md) |
| `appliance-settings` | Settings — ports, sessions, update policy, audit, detail level, Appliance password | [first-start.md](../../../user/appliance/first-start.md) |
| `appliance-appearance-menu` | Overview with the Appearance menu open | [appearance.md](../../../user/appliance/appearance.md) |
| `appliance-appearance-themes` | The same Overview region in Graphite and in Midnight, side by side | [appearance.md](../../../user/appliance/appearance.md) |
| `appliance-appearance-density` | The same Overview region at Normal and Compact density, side by side | [appearance.md](../../../user/appliance/appearance.md) |

## Capture settings

- Browser: Chromium, device scale factor 1, English locale, Europe/Berlin, the
  browser clock fixed to 5 July 2026, 15:30.
- Guide PNGs: the 1440×900 window, or 390×844 for a phone. A page shown whole
  grows the window to its own height, so the menu stays beside it.
- Appearance: English, Graphite, Normal density, motion off, unless the file is
  about another choice.
- State: each capture resets the test host with `/api/test/reset` into the
  state it shows — `never_installed` for a fresh card, `manager_index` and
  `manager_package_kept` for a package index and a kept package,
  `operation_running` for an update the agent is still carrying out,
  `expire_sessions` for the sign-in page.
- Host paths: the test host keeps its files under a temporary directory with a
  random name; the spec reads the status answers with that directory taken off,
  so a path reads as it does on an appliance (`/srv/ems-appliance-export`,
  `/opt/ems-solarflow/data`).
- The appearance strips photograph the same 714×560 region once per value and
  lay the columns side by side on a blank page, 12 px apart.

Add an image by adding a test titled with the file's name to the spec,
embedding it in a guide and listing it above —
`tests/test_appliance_screenshot_index.py` and `tests/test_docs_user_guides.py`
check that the spec, the committed files, the embeds and this README agree.

## Why these are deterministic

The server behind them is the same `EMS_APPLIANCE_TEST_MODE` fixture the
browser suite uses: a scripted host with fixed versions, a fixed hostname and a
fixed set of containers. Re-running the capture produces the same pages, so a
diff in an image means the UI changed and not that the data moved.
