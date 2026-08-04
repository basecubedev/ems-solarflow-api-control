import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.EMS_ADMIN_PACKAGED_E2E_PORT ?? 8124);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "system-build-packaged.spec.ts",
  globalTeardown: "./tests/e2e/packaged-global-teardown.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: process.env.CI ? "retain-on-failure" : "off",
  },
  projects: [{ name: "packaged-chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "bash tests/e2e/run-packaged-admin.sh",
    url: BASE_URL,
    reuseExistingServer: false,
    timeout: 600_000,
    env: { EMS_ADMIN_PACKAGED_E2E_PORT: String(PORT) },
  },
});
