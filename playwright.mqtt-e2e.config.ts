import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// The MQTT mapping end to end: a real Mosquitto, a played Shelly plug, the
// test-mode Admin and, started by the test, the real EMS -- all from
// tests/e2e-mqtt/harness.py on an isolated installation. Needs Docker and the
// pinned Mosquitto image; tests/test_mqtt_mapping_browser_end_to_end.py runs it.
const PORT = Number(process.env.EMS_MQTT_E2E_PORT ?? 8129);
const STATE = process.env.EMS_MQTT_E2E_STATE ?? join(process.env.TMPDIR || tmpdir(), `ems-mqtt-e2e-state-${PORT}.json`);
process.env.EMS_MQTT_E2E_STATE = STATE;

export default defineConfig({
  testDir: "./tests/e2e-mqtt",
  globalTeardown: "./tests/e2e-mqtt/global-teardown.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 180_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
  ],
  webServer: {
    command: `"${process.env.EMS_MQTT_E2E_PYTHON || ".venv/bin/python"}" tests/e2e-mqtt/harness.py`,
    url: `http://127.0.0.1:${PORT}/api/admin/auth/status`,
    reuseExistingServer: false,
    timeout: 90_000,
    env: { EMS_MQTT_E2E_PORT: String(PORT), EMS_MQTT_E2E_STATE: STATE },
  },
});
