import { defineConfig, devices } from "@playwright/test";

// The Raspberry Pi Appliance Manager UI runs against a deterministic test
// server (EMS_APPLIANCE_TEST_MODE) backed by the scripted FakeHost — no Docker,
// no apt, no systemd. Per-test state is reset through the gated
// /api/test/reset endpoint, which does not exist outside test mode.
const PORT = Number(process.env.EMS_APPLIANCE_E2E_PORT ?? 8124);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e-appliance",
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
    command: "bash tests/e2e-appliance/run-appliance.sh",
    url: `${BASE_URL}/api/session`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: { EMS_APPLIANCE_E2E_PORT: String(PORT) },
  },
});
