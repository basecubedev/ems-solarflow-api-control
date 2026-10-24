import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.EMS_ADMIN_REMOTE_E2E_PORT ?? 8125);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "system-build-remote-packaged.spec.ts",
  globalTeardown: "./tests/e2e/remote-packaged-global-teardown.ts",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "remote-packaged-chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "bash tests/e2e/run-remote-packaged-admin.sh",
    url: BASE_URL,
    reuseExistingServer: false,
    timeout: 120_000,
    env: { EMS_ADMIN_REMOTE_E2E_PORT: String(PORT) },
  },
});
