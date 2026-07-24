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

`.github/workflows/playwright-e2e.yml` runs `playwright-chromium-smoke` and
`playwright-firefox-smoke` on every relevant PR (path-filtered to `admin/**`,
shared EMS config modules, `deploy/admin/**`, `tests/e2e/**` and the package
files). Reports, traces and screenshots are uploaded on failure. WebKit runs the
same set on demand (`workflow_dispatch`) so a WebKit-only flake never blocks a
PR.

## Writing tests

- Drive the browser (click, select, reload, read visible state, wait for network
  responses). Do not call internal JS functions directly. API helpers may set up
  initial state, but the workflow itself stays browser-driven.
- Prefer role/label, then `data-testid`, then CSS. Add a `data-testid` only when a
  stable hook is missing.
- No arbitrary sleeps: wait for a response, a locator state, or an attribute.

## Covered lifecycle workflows

The workflows that originally shipped as backend/contract-only coverage now have
browser-level specs:

- Admin update reconnect → Continue completion, and a failed replacement —
  `tests/e2e/admin-update.spec.ts` (plus the real-container replacement canary
  in `tests/e2e/admin-replacement-canary.spec.ts`).
- MQTT migration review/apply and stale-fingerprint rejection —
  `tests/e2e/mqtt-migration.spec.ts`.
- Guided Upgrade preflight and live step progression —
  `tests/e2e/guided-upgrade.spec.ts`.
- Mixed API / local-MQTT / cloud-MQTT config round trip —
  `tests/e2e/mixed-transports.spec.ts` (config-draft persistence:
  `tests/e2e/setup-reload.spec.ts`).
- Maintenance Admin/EMS identity display —
  `tests/e2e/component-identities.spec.ts`.

When extending coverage, grow the deterministic test-mode — extend the
catalog and the `admin/test_support.py` adapters rather than faking frontend-only
state.
