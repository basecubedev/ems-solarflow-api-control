import { defineConfig, devices } from "@playwright/test";

// The deterministic test-mode Admin server (EMS_ADMIN_TEST_MODE) runs on an
// isolated temporary state root — see tests/e2e/run-admin.sh. Per-test state is
// reset through the gated /api/admin/test/reset endpoint (see fixtures).
const PORT = Number(process.env.EMS_ADMIN_E2E_PORT ?? 8123);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testIgnore: [
    "system-build-packaged.spec.ts",
    "system-build-remote-packaged.spec.ts",
    "admin-replacement-canary.spec.ts",
  ],
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: process.env.CI ? "retain-on-failure" : "off",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    // WebKit is not installed for the default PR run; the pre-release workflow
    // installs it and selects this project explicitly.
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  webServer: {
    command: "bash tests/e2e/run-admin.sh",
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: { EMS_ADMIN_E2E_PORT: String(PORT) },
  },
});
