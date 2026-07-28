import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Reversible maintenance connection switching: one physical inverter moves back
// and forth between its discovered connections inside a single discovery
// session. The connection it no longer uses becomes selectable again straight
// away — no rescan, no reload, no duplicate device. Discovery is
// deterministically mocked; the maintenance draft runs against the real
// test-mode backend.

const SERIAL = "SWITCH-SERIAL";
const ROUTE_B1 = "SWITCH-ROUTE-B1";
const ROUTE_B2 = "SWITCH-ROUTE-B2";
const ROUTE_CLOUD = "SWITCH-ROUTE-CLOUD";

type DiscoveryState = {
  apiDevices: unknown[];
  proposals: unknown[];
};

function apiInverter(serial: string, ip: string) {
  return {
    serial_number: serial,
    role_suggestion: "inverter",
    ip,
    port: 8080,
    api_family: "zendure_local_http",
    device_type: "zendure_solarflow_800_pro",
    display_name: "SolarFlow 800 Pro 2",
    model: "SolarFlow 800 Pro 2",
    verified: true,
    usable_for_config: true,
    config_ready: true,
  };
}

// Proposals are mocked without an opaque server proposal id, matching the other
// maintenance specs: the resulting draft entries take the manual path through
// the real backend, so the trusted-proposal boundary stays enforced.
function mqttProposal(
  brokerRef: string,
  route: string,
  source = "local_mqtt",
  host = "192.168.60.10",
) {
  return {
    serial_number: SERIAL,
    device_id: route,
    target: "device",
    connection_source: source,
    broker_ref: brokerRef,
    broker_host: host,
    broker_port: source === "zendure_cloud_mqtt" ? 8883 : 1883,
    broker_tls: source === "zendure_cloud_mqtt",
    output_control_supported: true,
    display_name: "SolarFlow 800 Pro 2",
    hardware_model: "hyper_2000",
    topic_family: "legacy_zendure_json",
    hardware_generation: "hub_hyper_legacy",
    role_hint: "inverter",
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: SERIAL,
      enabled: true,
      mqtt: {
        broker_ref: brokerRef,
        source,
        topic_family: "legacy_zendure_json",
        device_id: route,
        product_key: "SWITCH-PK",
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: true },
    },
  };
}

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockDiscovery(page: Page, state: DiscoveryState) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices**", (route) =>
    json(route, { devices: state.apiDevices, ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/refresh**", (route) =>
    json(route, { state: "enabled" }),
  );
  await page.route("**/api/discovery/networks**", (route) =>
    json(route, { networks: [] }),
  );
  await page.route("**/api/discovery/mqtt-brokers/refresh**", (route) =>
    json(route, { ok: true }),
  );
  await page.route("**/api/discovery/mqtt-proposals**", (route) =>
    json(route, { proposals: state.proposals }),
  );
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings**", (route) =>
    json(route, { token_saved: false, tls_mode: "system_ca" }),
  );
  await page.route("**/api/discovery/scan**", (route) =>
    json(route, { scan_id: "e2e-scan" }),
  );
  await page.route("**/api/discovery/result/**", (route) =>
    json(route, { status: "complete", devices: [] }),
  );
}

async function login(page: Page) {
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
}

async function openMaintenanceEditor(page: Page) {
  await expect(page.locator("#view-start")).toBeVisible();
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="manual"]').click();
  const toggle = page.locator(
    '[data-maintenance-toggle="maintenance-config-card"]',
  );
  const editor = page.locator("#maintenance-config-editor");
  await expect(toggle).toContainText(/inverter/);
  await expect(async () => {
    if (!(await editor.isVisible())) await toggle.click();
    await expect(editor).toBeVisible({ timeout: 1_000 });
  }).toPass();
  const sources = page.locator("#maintenance-discovery-sources");
  await expect(async () => {
    if (!(await sources.isVisible())) {
      await page.locator("#maintenance-add-devices > summary").click();
    }
    await expect(sources).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

async function runDiscovery(page: Page) {
  await page.locator("#maintenance-discovery-start").click();
  await expect(page.locator("#maintenance-discovery-status")).toContainText(
    /Discovery completed/,
  );
}

function results(page: Page) {
  return page.locator("#maintenance-discovery-results");
}

function configuredCards(page: Page) {
  return page.locator("#maintenance-config-inverters .hardware-card");
}

function inverterCard(page: Page) {
  return configuredCards(page).first();
}

async function openCard(page: Page, card: ReturnType<typeof inverterCard>) {
  await expect(async () => {
    const body = card.locator(".hardware-card-body");
    if (!(await body.isVisible())) {
      await card.locator(".hardware-card-toggle").click();
    }
    await expect(body).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

function cardInput(page: Page, card: ReturnType<typeof inverterCard>, label: string) {
  return card
    .locator("label")
    .filter({ has: page.locator(".feature-field-label", { hasText: label }) })
    .locator('input[type="text"], input[type="number"]')
    .first();
}

// Exactly one alternative connection may be offered at a time, so the switch
// never depends on telling two identically named cards apart.
async function useTheOfferedConnection(page: Page) {
  const button = results(page).getByRole("button", { name: "Use connection" });
  await expect(button).toHaveCount(1);
  await button.click();
}

async function expectOneInverter(page: Page, kind: RegExp) {
  await expect(configuredCards(page)).toHaveCount(1);
  await expect(inverterCard(page)).toHaveClass(kind);
}

// The generated backend config, not the in-memory draft: Preview renders what
// preview_maintenance_config() merged, so this is the contract the apply writes.
// The pre is cleared first so a second preview can never read the first one.
async function previewedDevice(page: Page) {
  const raw = page.locator("#maintenance-config-raw-pre");
  await raw.evaluate((node) => {
    node.textContent = "";
  });
  await page.locator("#maintenance-config-preview-btn").click();
  await expect(raw).not.toBeEmpty();
  const preview = JSON.parse((await raw.textContent()) || "{}");
  const devices = (preview.devices || []).filter(
    (device: { type?: string }) => device.type === "zendure_mqtt",
  );
  expect(devices).toHaveLength(1);
  return devices[0].mqtt || {};
}

async function expectPreviewedConnection(
  page: Page,
  expected: { broker_ref: string; source?: string; device_id?: string },
) {
  const mqtt = await previewedDevice(page);
  expect(mqtt.broker_ref).toBe(expected.broker_ref);
  // A stored connection that states no mqtt.source keeps resolving it from its
  // broker profile, so only a stated source is asserted here.
  if (expected.source !== undefined) expect(mqtt.source).toBe(expected.source);
  if (expected.device_id !== undefined) expect(mqtt.device_id).toBe(expected.device_id);
  return mqtt;
}

// Cloud route identifiers stay redacted in the browser preview, so the landed
// selection is proven by its broker and transport, never by reading the route.
async function expectPreviewedCloudConnection(page: Page) {
  const mqtt = await expectPreviewedConnection(page, {
    broker_ref: "cloud_switch",
    source: "zendure_cloud_mqtt",
  });
  expect(String(mqtt.device_id || "")).not.toBe(ROUTE_CLOUD);
  expect(String(mqtt.device_id || "")).not.toBe(ROUTE_B1);
  expect(String(mqtt.device_id || "")).toMatch(/^(•+|<redacted>)$/);
}

async function expectPreservedCommonValues(page: Page) {
  const card = inverterCard(page);
  await openCard(page, card);
  await expect(cardInput(page, card, "Device name")).toHaveValue("INV_1");
  await expect(cardInput(page, card, "Device output limit")).toHaveValue("642");
  await expect(cardInput(page, card, "Minimum SoC")).toHaveValue("22");
  await expect(card).toHaveAttribute("data-disabled", "false");
}

test("Local MQTT b1 -> b2 -> b1 switches back without a rescan", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
    proposals: [
      mqttProposal("local_b1", ROUTE_B1),
      mqttProposal("local_b2", ROUTE_B2, "local_mqtt", "192.168.60.11"),
    ],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_local_broker_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);

  await runDiscovery(page);
  // b1 is the installed connection; b2 is the only alternative on offer.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-in-config"))
    .toHaveCount(1);
  await expect(results(page).locator(".mconfig-discovery-add-button.is-transport"))
    .toHaveCount(1);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B2,
  );
  // The whole connection follows the selection into the generated config, not
  // just the route: b1 is no longer this device's broker.
  await expectPreviewedConnection(page, {
    broker_ref: "local_b2",
    source: "local_mqtt",
    device_id: ROUTE_B2,
  });
  // b1 is free again immediately: the card was rebuilt, not hand-patched.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-transport"))
    .toHaveCount(1);
  await expect(results(page).locator(".mconfig-discovery-add-button.is-added"))
    .toHaveCount(1);
  await expect(results(page).getByRole("button", { name: "Connection selected" }))
    .toHaveCount(0);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B1,
  );
  // Back on the installed connection, which reports as configured again.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-in-config"))
    .toHaveCount(1);
  await expectPreservedCommonValues(page);

  // Back on the stored connection exactly: b1 with its original route, and no
  // stated source invented for a config that always resolved it from the profile.
  await expectPreviewedConnection(page, {
    broker_ref: "local_b1",
    device_id: ROUTE_B1,
  });
  await expect(page.locator("#maintenance-config-apply-btn")).toBeVisible();
});

test("Local MQTT <-> Zendure MQTT keeps the selected connection in the preview", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
    proposals: [
      mqttProposal("local_b1", ROUTE_B1),
      mqttProposal(
        "cloud_switch",
        ROUTE_CLOUD,
        "zendure_cloud_mqtt",
        "mqtt.zen-iot.com",
      ),
    ],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_local_cloud_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expectPreviewedCloudConnection(page);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expectPreservedCommonValues(page);
  await expectPreviewedConnection(page, {
    broker_ref: "local_b1",
    device_id: ROUTE_B1,
  });
});

test("API -> Zendure MQTT -> API switches back in one session", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [apiInverter(SERIAL, "192.168.60.20")],
    proposals: [
      mqttProposal(
        "cloud_switch",
        ROUTE_CLOUD,
        "zendure_cloud_mqtt",
        "mqtt.zen-iot.com",
      ),
    ],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_api_cloud_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, /hardware-card-inverter/);

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expectPreviewedCloudConnection(page);

  // The Local API connection is offered again straight away.
  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-inverter/);
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText("API");
  await expectPreservedCommonValues(page);
  await expect(cardInput(page, inverterCard(page), "Device IP address")).toHaveValue(
    "192.168.60.20",
  );
});

test("Zendure MQTT -> API -> Zendure MQTT switches back in one session", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [apiInverter(SERIAL, "192.168.60.20")],
    proposals: [
      mqttProposal(
        "cloud_switch",
        ROUTE_CLOUD,
        "zendure_cloud_mqtt",
        "mqtt.zen-iot.com",
      ),
    ],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_cloud_api_switchback");
  await page.reload();
  await openMaintenanceEditor(page);

  // The installed config states no mqtt.source; the broker profile resolves it,
  // so the configured card names Zendure MQTT before any discovery has run.
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expect(inverterCard(page).locator(".connection-pill")).toHaveAttribute(
    "data-connection",
    "zendure_mqtt",
  );

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-inverter/);
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText("API");

  await useTheOfferedConnection(page);
  await expectOneInverter(page, /hardware-card-zendure-mqtt/);
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expectPreservedCommonValues(page);
});
