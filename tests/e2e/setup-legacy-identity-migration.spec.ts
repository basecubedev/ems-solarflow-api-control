import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Guided Setup state written by an earlier release carries no issued identity:
// the draft's source id is the old `<api_family>:<serial>` key, dismissals are
// bare serials, and nothing holds an opaque token. The browser can no longer
// relate such an entry to a current observation — it compares issued ids only —
// so POST /api/setup/device-plan resolves it from the fields it already
// persists and hands back typed ids.
//
// These journeys are the operator-visible consequences: a configured inverter
// must not reappear as a second one, a bare-serial removal must not spread to an
// unrelated device that merely shows the same name, and an entry the backend
// cannot identify must stay distinct rather than merge.

const LEGACY_SERIAL = "E2ELEGACY001";
const OTHER_SERIAL = "E2ELEGACY002";
const MASKED_SERIAL = "••••";
const SHARED_DISPLAY = "SolarFlow 800 Pro 2";

function inverter(overrides: Record<string, unknown>) {
  return {
    role_suggestion: "inverter",
    port: 80,
    api_family: "zendure",
    device_type: "zendure_solarflow_800_pro_2",
    display_name: SHARED_DISPLAY,
    model: SHARED_DISPLAY,
    verified: true,
    usable_for_config: true,
    config_ready: true,
    ...overrides,
  };
}

const SHELLY = {
  observation_id: "obs:v1:E2ELEGACYSHELLY",
  physical_device_id: "opaque:v1:E2ELEGACYSHELLYPHYS",
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

async function mockDiscovery(page: Page, devices: unknown[]) {
  const preparation = {
    discovery_priority: ["local_api", "local_mqtt", "zendure_mqtt"],
    sources: {
      local_api: { enabled: true },
      local_mqtt: { enabled: false },
      zendure_mqtt: { enabled: false },
    },
  };
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
    json(route, preparation),
  );
  await page.route("**/api/discovery/run", (route) =>
    json(route, { ...preparation, devices: [], details: {}, refresh: true }),
  );
}

// Seed the stores exactly as the previous release wrote them: an array of draft
// items with no form id and a serial-derived source id, and a dismissal store of
// bare serials.
async function seedLegacyState(
  page: Page,
  { draft = [] as unknown[], dismissedSerials = [] as string[] } = {},
) {
  await page.addInitScript(
    ([items, serials]) => {
      const marker = "__ems_legacy_seeded";
      if (window.sessionStorage.getItem(marker)) return;
      window.sessionStorage.setItem(marker, "1");
      window.localStorage.setItem("ems-admin-config-draft", JSON.stringify(items));
      if ((serials as string[]).length) {
        window.localStorage.setItem(
          "ems-admin-config-dismissed-serials",
          JSON.stringify(serials),
        );
      }
    },
    [draft, dismissedSerials] as const,
  );
}

async function reachConfig(page: Page) {
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

function draftInverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

test("Fresh Setup: a legacy draft is rehydrated instead of duplicated", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await mockDiscovery(page, [
    inverter({
      observation_id: "obs:v1:E2ELEGACYONE",
      physical_device_id: "opaque:v1:E2ELEGACYONEPHYS",
      identity_status: "confirmed",
      serial_number: LEGACY_SERIAL,
      ip: "192.168.100.21",
    }),
    SHELLY,
  ]);
  await seedLegacyState(page, {
    draft: [
      {
        // The pre-identity shape: no draft_item_id, no tokens, and the old
        // `<api_family>:<serial>` collection key.
        source_id: "zendure:" + LEGACY_SERIAL,
        role: "inverter",
        config_name: "INV_LEGACY",
        display_name: SHARED_DISPLAY,
        enabled: true,
        serial_number: LEGACY_SERIAL,
        ip: "192.168.100.21",
        port: 80,
        api_family: "zendure",
        device_type: "zendure_solarflow_800_pro_2",
      },
    ],
  });
  await reachConfig(page);

  // One inverter, still under the operator's name: the backend recognized the
  // stored entry as the device discovery is currently reporting.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(page.locator("#config-draft-list")).toContainText("INV_LEGACY");
});

test("Fresh Setup: a legacy bare-serial removal spares a same-name device", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await mockDiscovery(page, [
    inverter({
      observation_id: "obs:v1:E2ELEGACYDISMISSED",
      physical_device_id: "opaque:v1:E2ELEGACYDISMISSEDPHYS",
      identity_status: "confirmed",
      serial_number: LEGACY_SERIAL,
      ip: "192.168.100.21",
    }),
    inverter({
      observation_id: "obs:v1:E2ELEGACYKEPT",
      physical_device_id: "opaque:v1:E2ELEGACYKEPTPHYS",
      identity_status: "confirmed",
      serial_number: OTHER_SERIAL,
      ip: "192.168.100.22",
    }),
    SHELLY,
  ]);
  await seedLegacyState(page, { dismissedSerials: [LEGACY_SERIAL] });
  await reachConfig(page);

  // The dismissal resolves to one issued physical identity. The second inverter
  // shows the same model name and is a different device, so it is adopted.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(
    page.locator('#config-draft-list [data-source-id="obs:v1:E2ELEGACYKEPT"]'),
  ).toHaveCount(1);
  await expect(
    page.locator('#config-draft-list [data-source-id="obs:v1:E2ELEGACYDISMISSED"]'),
  ).toHaveCount(0);
});

test("Fresh Setup: a masked legacy entry stays distinct and unresolved", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await mockDiscovery(page, [
    inverter({
      observation_id: "obs:v1:E2ELEGACYMASKEDA",
      physical_device_id: null,
      identity_status: "unresolved",
      serial_number: MASKED_SERIAL,
      ip: "192.168.100.31",
    }),
    SHELLY,
  ]);
  await seedLegacyState(page, {
    draft: [
      {
        source_id: "zendure:" + MASKED_SERIAL,
        role: "inverter",
        config_name: "INV_MASKED",
        display_name: SHARED_DISPLAY,
        enabled: true,
        serial_number: MASKED_SERIAL,
        ip: "192.168.100.32",
        port: 80,
        api_family: "zendure",
        device_type: "zendure_solarflow_800_pro_2",
      },
    ],
  });
  await reachConfig(page);

  // Neither entry proves which hardware it is, and they answer on different
  // endpoints: the stored one and the discovered one stay two rows. A shared
  // placeholder must never merge them, and never dismiss one through the other.
  await expect(draftInverterCards(page)).toHaveCount(2);
  const sourceIds = await page
    .locator("#config-draft-list [data-source-id]")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-source-id") || ""),
    );
  expect(new Set(sourceIds).size).toBe(sourceIds.length);
});
