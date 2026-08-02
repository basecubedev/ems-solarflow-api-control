import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Two discovered inverters that only *display* the same masked serial must stay
// two devices in the browser. Before the fix the discovery Map was keyed on
// `api_family + ":" + serial_number`, so both observations produced the key
// "zendure:••••", the second silently overwrote the first, and that same key
// also drove DOM identity, dismissal and selection.
//
// The browser now keys on the server-issued `observation_id` only. Discovery is
// deterministically mocked; the two observations differ solely in the address
// they answered on, exactly as a redacted view would present them.

const MASKED_SERIAL = "••••";
const OBSERVATION_A = "obs:v1:E2EMASKEDAAA";
const OBSERVATION_B = "obs:v1:E2EMASKEDBBB";

function maskedInverter(observationId: string, ip: string) {
  return {
    observation_id: observationId,
    // The backend could not physically identify either device, so neither gets
    // a physical_device_id — but both are still uniquely addressable.
    physical_device_id: null,
    identity_status: "unresolved",
    serial_number: MASKED_SERIAL,
    role_suggestion: "inverter",
    ip,
    port: 80,
    api_family: "zendure",
    device_type: "zendure_solarflow_800_pro_2",
    display_name: "SolarFlow 800 Pro 2",
    model: "SolarFlow 800 Pro 2",
    verified: true,
    usable_for_config: true,
    config_ready: true,
  };
}

const SHELLY = {
  observation_id: "obs:v1:E2ESHELLY",
  physical_device_id: "opaque:v1:E2ESHELLYPHYS",
  identity_status: "confirmed",
  serial_number: "SHELLYE2E",
  role_suggestion: "grid_meter",
  ip: "192.168.100.93",
  port: 80,
  api_family: "shelly_gen2",
  device_type: "shelly_pro_3em",
  display_name: "Shelly Pro 3EM",
  model: "Shelly Pro 3EM",
  verified: true,
  usable_for_config: true,
  config_ready: true,
};

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockDiscovery(page: Page) {
  const devices = [
    maskedInverter(OBSERVATION_A, "192.168.100.11"),
    maskedInverter(OBSERVATION_B, "192.168.100.12"),
    SHELLY,
  ];
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices", (route) =>
    json(route, { devices, ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/status", (route) =>
    json(route, { state: "enabled", message: "", devices_found: devices.length }),
  );
  await page.route("**/api/discovery/mdns/refresh", (route) =>
    json(route, { state: "enabled" }),
  );
  await page.route("**/api/discovery/networks", (route) =>
    json(route, { networks: [] }),
  );
  await page.route("**/api/discovery/mqtt-brokers", (route) =>
    json(route, { candidates: [] }),
  );
  await page.route("**/api/discovery/mqtt-brokers/**", (route) =>
    json(route, { candidates: [] }),
  );
  await page.route("**/api/discovery/mqtt-proposals", (route) =>
    json(route, { proposals: [] }),
  );
  await page.route("**/api/discovery/preparation", (route) =>
    json(route, {
      discovery_priority: ["local_api", "local_mqtt", "zendure_mqtt"],
      sources: {
        local_api: { enabled: true },
        local_mqtt: { enabled: false },
        zendure_mqtt: { enabled: false },
      },
    }),
  );
  await page.route("**/api/discovery/run", (route) =>
    json(route, {
      priority: ["local_api", "local_mqtt", "zendure_mqtt"],
      sources: {
        local_api: { enabled: true },
        local_mqtt: { enabled: false },
        zendure_mqtt: { enabled: false },
      },
      devices: [],
      details: {},
      refresh: true,
    }),
  );
}

function draftInverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

async function reachConfig(page: Page) {
  await mockDiscovery(page);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
  await page.locator('[data-setup-step="config"]').click();
}

test("Fresh Setup: two masked-serial observations stay two devices", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachConfig(page);

  // The collision: one entry would mean the second observation overwrote the
  // first. Each card must carry its own server-issued observation id.
  await expect(draftInverterCards(page)).toHaveCount(2);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${OBSERVATION_A}"]`),
  ).toHaveCount(1);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${OBSERVATION_B}"]`),
  ).toHaveCount(1);
});

test("Fresh Setup: dismissing one masked observation keeps the other", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachConfig(page);
  await expect(draftInverterCards(page)).toHaveCount(2);

  const first = page.locator(
    `#config-draft-list [data-source-id="${OBSERVATION_A}"]`,
  );
  await first.locator(".config-draft-remove").click();

  // Dismissal is scoped to one observation id: the sibling that merely renders
  // the same masked serial must survive.
  await expect(
    page.locator(`#config-draft-list [data-source-id="${OBSERVATION_A}"]`),
  ).toHaveCount(0);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${OBSERVATION_B}"]`),
  ).toHaveCount(1);
  await expect(draftInverterCards(page)).toHaveCount(1);
});

test("Fresh Setup: a masked serial never becomes a browser collection key", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachConfig(page);
  await expect(draftInverterCards(page)).toHaveCount(2);

  const sourceIds = await page
    .locator("#config-draft-list [data-source-id]")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-source-id") || ""),
    );

  expect(new Set(sourceIds).size).toBe(sourceIds.length);
  for (const id of sourceIds) {
    expect(id).not.toContain(MASKED_SERIAL);
  }
});
