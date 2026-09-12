import { defineConfig, devices } from "@playwright/test";

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
// Admin, 8126 Admin replacement canary.
const PORT = Number(process.env.EMS_DASHBOARD_E2E_PORT ?? 8127);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e-dashboard",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
  ],
  webServer: {
    // /api/auth/status is the cheapest endpoint the preview answers, and it
    // answers it only once the scenario is built -- so it is a readiness probe
    // rather than a liveness one.
    command: `python3 scripts/serve_dashboard_preview.py --host 127.0.0.1 --port ${PORT}`,
    url: `${BASE_URL}/api/auth/status`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
