import { type Locator, type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Maintenance reuses the one shared hardware-role card system: a recognized
// inverter is blue and a recognized grid meter purple over Local MQTT and
// Zendure MQTT alike, unclassified hardware stays neutral, and the transport
// only shows up as a label, a pill and data-connection. Discovery is
// deterministically mocked; the draft, preview and apply run against the real
// test-mode backend. See admin/static/admin.js mqttProposalHardwareRole /
// hardwareCardClass / mconfigHardwareCard.

const LOCAL_SERIAL = "E2EROLELOCAL1";
const CLOUD_SERIAL = "E2EROLECLOUD1";

type DiscoveryState = { proposals: unknown[] };

function mqttInverterProposal(serial: string, source: string, brokerRef: string) {
  return {
    id: `zendure-mqtt:${brokerRef}:${serial}`,
    serial_number: serial,
    device_id: serial,
    target: "device",
    connection_source: source,
    broker_ref: brokerRef,
    broker_host: "192.168.60.10",
    broker_port: 1883,
    broker_tls: false,
    topic_family: "zensdk_ha_scalar",
    display_name: `${source === "local_mqtt" ? "Local" : "Cloud"} MQTT inverter`,
    hardware_generation: "solarflow_zensdk",
    hardware_generation_label: "SolarFlow 800 Pro 2",
    confidence: "high",
    role_hint: "battery_inverter_candidate",
    capabilities: ["battery_storage", "output_control"],
    metrics: ["outputLimit", "electricLevel"],
    warnings: [],
    output_control_supported: false,
    output_control_reason: "output_control_not_observed",
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: serial,
      enabled: true,
      name: `${source === "local_mqtt" ? "Local" : "Cloud"} MQTT inverter`,
      mqtt: {
        broker_ref: brokerRef,
        source,
        topic_family: "zensdk_ha_scalar",
        device_id: serial,
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: false },
    },
  };
}

function d0GridMeterProposal() {
  return {
    id: "zendure-mqtt:local:D0ROLE",
    serial_number: "E2EROLED0",
    device_id: "D0ROLE",
    target: "grid_meter",
    connection_source: "local_mqtt",
    broker_ref: "local_mqtt_e2e",
    topic_family: "zensdk_ha_scalar",
    display_name: "Local MQTT smart meter",
    hardware_generation_label: "Zendure Smart Meter",
    confidence: "high",
    role_hint: "grid_meter_candidate",
    capabilities: [],
    metrics: ["totalPower"],
    warnings: [],
    output_control_supported: false,
    grid_meter_fragment: {
      type: "zendure_smartmeter_d0",
      mqtt: {
        broker_ref: "local_mqtt_e2e",
        topic: "Zendure/sensor/D0ROLE/totalPower",
      },
    },
  };
}

function unknownProposal() {
  return {
    id: "zendure-mqtt:cloud:UNKNOWNROLE",
    serial_number: "E2EROLEUNK",
    device_id: "UNKNOWNROLE",
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    broker_ref: "cloud",
    topic_family: "unknown",
    display_name: "Unclassified MQTT device",
    confidence: "low",
    role_hint: "unknown_candidate",
    capabilities: [],
    metrics: ["someValue"],
    warnings: ["insufficient_telemetry"],
    output_control_supported: false,
    output_control_reason: "output_control_not_observed",
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: "E2EROLEUNK",
      enabled: true,
      name: "Unclassified MQTT device",
      mqtt: { broker_ref: "cloud", source: "zendure_cloud_mqtt", topic_family: "unknown" },
      capabilities: { read_power: false, read_soc: false, write_output_limit: false },
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
    json(route, { devices: [], ignored_devices: [] }),
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

function proposalCard(page: Page, text: string) {
  return results(page)
    .locator(".mconfig-discovery-proposal-card")
    .filter({ hasText: text })
    .first();
}

function configuredCards(page: Page) {
  return page.locator("#maintenance-config-inverters .hardware-card");
}

function configuredCard(page: Page, text: string) {
  return configuredCards(page).filter({ hasText: text }).first();
}

async function expectRole(card: Locator, role: "inverter" | "grid_meter" | "unknown") {
  await expect(card).toHaveClass(/(^|\s)hardware-card(\s|$)/);
  await expect(card).toHaveAttribute("data-role", role);
  if (role === "inverter") {
    await expect(card).toHaveClass(/hardware-card-inverter/);
    await expect(card).not.toHaveClass(/hardware-card-grid-meter/);
  } else if (role === "grid_meter") {
    await expect(card).toHaveClass(/hardware-card-grid-meter/);
    await expect(card).not.toHaveClass(/hardware-card-inverter/);
  } else {
    await expect(card).not.toHaveClass(/hardware-card-inverter/);
    await expect(card).not.toHaveClass(/hardware-card-grid-meter/);
  }
}

function leftBorder(card: Locator) {
  return card.evaluate((el) => getComputedStyle(el).borderLeftColor);
}

function fullState(): DiscoveryState {
  return {
    proposals: [
      mqttInverterProposal(LOCAL_SERIAL, "local_mqtt", "local_mqtt_e2e"),
      mqttInverterProposal(CLOUD_SERIAL, "zendure_cloud_mqtt", "cloud"),
      d0GridMeterProposal(),
      unknownProposal(),
    ],
  };
}

test("Maintenance discovery: MQTT proposals take their card colour from the hardware role", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(90_000);
  await mockDiscovery(page, fullState());
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  await runDiscovery(page);

  const localInverter = proposalCard(page, "Local MQTT inverter");
  const cloudInverter = proposalCard(page, "Cloud MQTT inverter");
  const gridMeter = proposalCard(page, "Local MQTT smart meter");
  const unknown = proposalCard(page, "Unclassified MQTT device");

  await expectRole(localInverter, "inverter");
  await expectRole(cloudInverter, "inverter");
  await expectRole(gridMeter, "grid_meter");
  await expectRole(unknown, "unknown");

  // Transport stays visible and distinguishable next to the shared role colour.
  await expect(localInverter).toHaveAttribute("data-connection", "local_mqtt");
  await expect(cloudInverter).toHaveAttribute("data-connection", "zendure_mqtt");
  await expect(localInverter.locator(".connection-pill")).toHaveText("MQTT");
  await expect(cloudInverter.locator(".connection-pill")).toHaveText("Zendure MQTT");
  await expect(unknown.locator(".connection-pill")).toHaveText("Zendure MQTT");

  // Both transports resolve to the same accent; the other roles do not.
  const localBorder = await leftBorder(localInverter);
  expect(await leftBorder(cloudInverter)).toEqual(localBorder);
  expect(await leftBorder(gridMeter)).not.toEqual(localBorder);
  expect(await leftBorder(unknown)).not.toEqual(localBorder);

  // The same accents a bare Local API hardware card resolves to.
  const probe = await page.evaluate(() => {
    const make = (extra: string) => {
      const el = document.createElement("article");
      el.className = "hardware-card " + extra;
      document.body.appendChild(el);
      const color = getComputedStyle(el).borderLeftColor;
      el.remove();
      return color;
    };
    return {
      inverter: make("hardware-card-inverter"),
      gridMeter: make("hardware-card-grid-meter"),
    };
  });
  expect(localBorder).toEqual(probe.inverter);
  expect(await leftBorder(gridMeter)).toEqual(probe.gridMeter);
});

test("Maintenance draft: an added MQTT inverter keeps the inverter card through apply and reload", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(90_000);
  // Without a server-issued proposal id the add takes the manual local-MQTT
  // path, which is the one an add-and-apply run can drive deterministically.
  const manual = mqttInverterProposal(LOCAL_SERIAL, "local_mqtt", "local_mqtt_e2e");
  delete (manual as { id?: string }).id;
  await mockDiscovery(page, { proposals: [manual] });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  const before = await configuredCards(page).count();

  await runDiscovery(page);
  await proposalCard(page, "Local MQTT inverter")
    .getByRole("button", { name: "Add inverter" })
    .click();
  await expect(configuredCards(page)).toHaveCount(before + 1);

  // The newly drafted MQTT inverter is an inverter card, not a transport card.
  const added = configuredCards(page).nth(before);
  await expect(added).toHaveClass(/hardware-card-inverter/);
  await expect(added).toHaveAttribute("data-connection", "local_mqtt");
  await expect(added.locator(".connection-pill")).toHaveText("MQTT");

  await page.locator("#maintenance-config-preview-btn").click();
  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );

  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(before + 1);
  const persisted = configuredCards(page).nth(before);
  await expect(persisted).toHaveClass(/hardware-card-inverter/);
  await expect(persisted).toHaveAttribute("data-connection", "local_mqtt");
  await expect(persisted.locator(".connection-pill")).toHaveText("MQTT");
});

test("Maintenance: configured API and MQTT inverters share one inverter card", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(90_000);
  await mockDiscovery(page, { proposals: [] });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);

  const cards = configuredCards(page);
  await expect(cards.first()).toBeVisible();
  const total = await cards.count();
  const connections = new Set<string>();
  for (let index = 0; index < total; index++) {
    const card = cards.nth(index);
    await expect(card).toHaveClass(/hardware-card-inverter/);
    connections.add((await card.getAttribute("data-connection")) || "");
  }
  // The seeded install mixes transports, and every one of them is blue.
  expect(connections.size).toBeGreaterThan(1);

  // The configured grid meter keeps its own purple role card.
  const meter = page.locator("#maintenance-config-gridmeter .hardware-card").first();
  await expect(meter).toHaveClass(/hardware-card-grid-meter/);
  await expect(meter).not.toHaveClass(/hardware-card-inverter/);
});
