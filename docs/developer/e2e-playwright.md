# Playwright end-to-end tests

A focused browser test suite for the Admin Console. It complements — never
replaces — the Python unit/service/API tests, the JavaScript contract tests, and
the Docker/Mosquitto suites. Playwright covers the things only a real browser can
catch: contradictory UI states, disabled-action deadlocks, button gating,
lost/resumed Setup state, and frontend/backend contract mismatches across
browsers.

## Layout

```text
playwright.config.ts        Chromium + Firefox (+ on-demand WebKit), webServer, artifacts
tests/e2e/run-admin.sh      Launch the test-mode Admin on an isolated temp state root
tests/e2e/fixtures/admin.ts Auth + per-test reset fixture
tests/e2e/pages/            Login / Setup page objects
tests/e2e/helpers/          Reusable assertions (ready/action invariant)
tests/e2e/*.spec.ts         Workflow tests
admin/test_support.py       Deterministic EMS_ADMIN_TEST_MODE runtime
```

## Test environment

The suite runs against a real Admin server started in `EMS_ADMIN_TEST_MODE=1`
(`admin/test_support.py`). Only *external effects* are faked; everything the tests
assert on stays real:

| Faked (external boundary)              | Kept real                              |
| -------------------------------------- | ------------------------------------- |
| Docker image inspect/pull              | Authentication + session cookie       |
| GitHub release metadata + archive      | CSRF enforcement                      |
| Embedded resource bundle location      | `SystemAlignmentService` state machine|
| Admin-replacement launcher             | Resource-strategy decisions           |
| Network scan (every host checked, none contacted; devices come from the seeded mDNS path) | Scan registry, progress and result routes |
| —                                      | Transition persistence                |
| —                                      | Discovery authorization               |
| —                                      | Server-driven button gating           |

State is isolated per run (temporary `EMS_ADMIN_DATA_DIR` / `EMS_INSTALL_DIR`,
see `run-admin.sh`) and reset to a known first-run state before every test via
the gated `POST /api/admin/test/reset` route. That route exists only when
`EMS_ADMIN_TEST_MODE` is set (the hook is `None` and the route 404s in a normal
deployment) and still requires a valid session + CSRF token.

The deterministic catalog offers four installable System Builds:

- `v9.9.9` — modern, aligned with the running Admin (embedded strategy).
- `v9.9.10` — modern, requires an Admin update.
- `v0.7.0` — legacy release installable by the modern Admin (release archive).
- `v0.6.9` — legacy whose historical archive cannot be verified.

## Local commands

```bash
npm install
npx playwright install --with-deps chromium firefox   # one-time browser install
npm run test:e2e                                       # all projects
npm run test:e2e:chromium
npm run test:e2e:firefox
npm run test:e2e:ui                                    # interactive UI mode
npx playwright test fresh-install                      # a single workflow
npx playwright show-trace test-results/**/trace.zip    # inspect a failure trace
```

`run-admin.sh` prefers the project virtualenv (`.venv/bin/python`) and falls back
to the system `python3`; the Python runtime dependencies (`requirements.txt`)
must be installed for the Admin server to start.

## CI

`.github/workflows/playwright-e2e.yml` runs `playwright-chromium-critical`
(`--grep "@smoke|@authority"`) and `playwright-firefox-smoke` (`--grep @smoke`)
on every relevant PR (path-filtered to `admin/**`, shared EMS config modules,
`deploy/admin/**`, `tests/e2e/**` and the package files). Reports, traces and
screenshots are uploaded on failure. WebKit runs the smoke group on demand
(`workflow_dispatch`) so a WebKit-only flake never blocks a PR.

The complete Chromium and Firefox suites run in the `Repo browser <browser>
full` jobs of `.github/workflows/playwright-e2e.yml`, which are skipped on a pull
request and run on the push to `main`, and in `./scripts/test-rc.sh`.
The former `playwright-chromium-smoke` job is now
`playwright-chromium-critical`; branch protection referring to the old name has
to be repointed.

Every CI job installs its browsers with
`scripts/ci-install-playwright-browsers.sh <browser>...`, never with
`npx playwright install` directly (`tests/test_ci_playwright_install.py`). The
system packages come from the runner's apt mirror, which stalls now and then:
the script makes apt give up on a stalled transfer and wait for the dpkg lock,
and before a retry it ends the apt-get a killed attempt left holding that lock.

## Groups

Specs carry Playwright tags, so one configuration serves every group:

```bash
npx playwright test --project=chromium --grep @smoke
npx playwright test --project=chromium --grep @authority
npx playwright test --project=firefox --grep @smoke
npx playwright test --project=chromium --grep "@setup|@authority"
```

Tags: `@smoke` (fast critical journeys), `@setup`, `@maintenance`,
`@authority`, `@workflow`, `@system-build`. Add the tag on the top-level
`test.describe(...)` where a spec has one, otherwise on each top-level
`test(...)`.

## Writing tests

- Drive the browser (click, select, reload, read visible state, wait for network
  responses). Do not call internal JS functions directly. API helpers may set up
  initial state, but the workflow itself stays browser-driven.
- Prefer role/label, then `data-testid`, then CSS. Add a `data-testid` only when a
  stable hook is missing.
- No arbitrary sleeps: wait for a response, a locator state, or an attribute.
- Do not click on a page that is still moving. Playwright checks the target
  only at the first pointer event, so when a section appears above the pointer
  between the press and the release, the browser sends the click to the element
  both events share and nothing happens -- no error, only a later step that
  fails. The Maintenance status page renders one read at a time and says so
  with `aria-busy="true"` while the reads that build it are out: the overview
  with its follow-ups, a recheck or container sync, the read-back after an MQTT
  migration. Open it with `MaintenancePage.openStatus`, which waits for that,
  and its cards with `openStatusCard`, which also checks that the card opened.
  A read a control on the page starts by itself -- diagnostics, a review
  refresh, a recovery action -- is not marked; wait for its result before the
  next click. The Firefox failure of the MQTT migration spec was this, and it
  had been written off as a flake.

- The console's address opens the console; the Classic console has been
  removed, and `/classic/` leads to the address.

## Covered lifecycle workflows

The browser-level specs drive the console's own pages
(`tests/e2e/admin-modern*.spec.ts`), the one app with the dashboard's pages
forwarded to a preview EMS (`tests/e2e/admin-one-app.spec.ts`) and the packaged
System Build gate (`tests/e2e/system-build-*packaged.spec.ts`). The specs that
drove the removed Classic console -- Admin update reconnect, MQTT migration,
Guided Upgrade steps, mixed transports, identity display, setup recovery and
workflow switching among them -- went with it; their server side stays covered
by the Python contract tests, and the console's pages by the specs above. The
real-container replacement canary (`tests/e2e/admin-replacement-canary.spec.ts`,
see [`ci-release.md`](ci-release.md)) still drives Classic and has to be
rewritten for the console.

When extending coverage, grow the deterministic test-mode — extend the
catalog and the `admin/test_support.py` adapters rather than faking frontend-only
state.
