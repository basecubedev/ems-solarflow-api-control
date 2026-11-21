// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { defineConfig, devices } from "@playwright/test";
import { DOCS_SHOT_TIME, DOCS_ZONE } from "./tests/e2e-dashboard/pin-the-day";

// The EMS Dashboard, the third surface and the last one without a browser
// suite. It runs against scripts/serve_dashboard_preview.py, which serves the
// real dashboard/static/ assets -- the real stylesheet, the real theme.js, the
// real app.js -- with deterministic synthetic API responses: no hardware, no
// MQTT, no SQLite history, no secrets, no running EMS loop. That is what makes
// a cockpit browser test possible at all, and it already existed for the
// documentation captures.
//
// Two traps, each paid for once:
//
// The cockpit holds an SSE stream open on /api/events, so the network is never
// idle and `waitUntil: "networkidle"` runs into its timeout on every
// navigation. Wait for an element instead.
//
// And the port has to be one nothing else in this repository uses. With
// `reuseExistingServer`, Playwright adopts whatever is already listening
// rather than failing: a development server left running on the chosen port
// silently becomes the system under test, and sixteen tests then report that
// the theme menu is not visible -- which is true, on that page. Taken:
// 8123 Admin, 8124 Appliance Manager and packaged Admin, 8125 remote packaged
// Admin, 8126 Admin replacement canary, 8129 MQTT end to end, 8130 the Admin
// suite's EMS preview; this suite has 8127, its documentation captures 8128,
// and the console those captures are taken in, 8131: the dashboard's pages
// are part of the one app, so their images show them there, with the console's
// docs preview reading the EMS
// from the dashboard preview (scripts/serve_admin_docs_preview.py
// --ems-preview). The EMS's own Diagnose and Maintenance pages, which only its
// own port has, are taken from the dashboard preview itself.
//
// The documentation captures show one fixed day in one zone: the preview
// server, these workers and the browser all read DOCS_ZONE, and the server
// pins its clock to the instant the captures pin the page to (pin-the-day.ts).
// That holds only for a server this run starts, so a capture has a port of its
// own, 8128, and never adopts one already listening: a suite's real-clock
// server would silently document today instead of the pinned day.
const CAPTURE_DOCS = Boolean(process.env.EMS_DASHBOARD_CAPTURE_DOCS);
const SUITE_PORT = Number(process.env.EMS_DASHBOARD_E2E_PORT ?? 8127);
const CAPTURE_PORT = Number(process.env.EMS_DASHBOARD_CAPTURE_PORT ?? 8128);
const PORT = CAPTURE_DOCS ? CAPTURE_PORT : SUITE_PORT;
const BASE_URL = `http://127.0.0.1:${PORT}`;
const CONSOLE_PORT = Number(process.env.EMS_DASHBOARD_CAPTURE_CONSOLE_PORT ?? 8131);
const CONSOLE_URL = `http://127.0.0.1:${CONSOLE_PORT}`;
if (CAPTURE_DOCS) process.env.EMS_DASHBOARD_PREVIEW_URL = BASE_URL;
if (CAPTURE_DOCS) process.env.TZ = DOCS_ZONE;
const PINNED_CLOCK = CAPTURE_DOCS ? ` --now ${DOCS_SHOT_TIME}` : "";

export default defineConfig({
  testDir: "./tests/e2e-dashboard",
  // The Modern docs captures write into the tracked PNGs under
  // docs/assets/screenshots/dashboard/, so a default run must not collect them.
  testIgnore: CAPTURE_DOCS ? [] : ["capture-docs.spec.ts"],
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: CAPTURE_DOCS ? CONSOLE_URL : BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
  ],
  webServer: [
    {
      // /api/auth/status is the cheapest endpoint the preview answers, and it
      // answers it only once the scenario is built -- so it is a readiness probe
      // rather than a liveness one.
      command: `python3 scripts/serve_dashboard_preview.py --host 127.0.0.1 --port ${PORT}${PINNED_CLOCK}`,
      url: `${BASE_URL}/api/auth/status`,
      reuseExistingServer: !process.env.CI && !CAPTURE_DOCS,
      timeout: 60_000,
    },
    ...(CAPTURE_DOCS
      ? [{
          command: `python3 scripts/serve_admin_docs_preview.py --host 127.0.0.1 --port ${CONSOLE_PORT} --ems-preview ${BASE_URL}`,
          url: `${CONSOLE_URL}/api/admin/auth/status`,
          reuseExistingServer: false,
          timeout: 60_000,
          gracefulShutdown: { signal: "SIGINT" as const, timeout: 5000 },
        }]
      : []),
  ],
});
