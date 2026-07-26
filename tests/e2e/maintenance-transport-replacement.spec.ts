import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Maintenance transport replacement: one physical inverter keeps one config
// entry, one name and one common value set while its connection moves between
// Zendure MQTT and Local API. Discovery is deterministically mocked; the
// maintenance draft, preview and apply run against the real test-mode backend.

const SERIAL_A = "E2EMQTTAAA1";
const SERIAL_B = "E2EMQTTBBB2";

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

// Proposals are mocked without an opaque server proposal id: the resulting
// draft entries take the manual local-MQTT path through the real backend, so
// the trusted-proposal boundary stays enforced (a browser-invented id would be
// rejected by design).
function mqttProposal(serial: string) {
  return {
    serial_number: serial,
    device_id: serial,
    target: "device",
    connection_source: "local_mqtt",
    broker_ref: "local_mqtt_e2e",
    broker_host: "192.168.60.10",
    broker_port: 1883,
    broker_tls: false,
    output_control_supported: false,
    display_name: "SolarFlow 800 Pro 2",
    hardware_generation: "solarflow_zensdk",
    role_hint: "inverter",
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: serial,
      enabled: true,
      mqtt: {
        broker_ref: "local_mqtt_e2e",
        source: "local_mqtt",
        topic_family: "zensdk_ha_scalar",
        device_id: serial,
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: false },
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

async function openMaintenanceEditor(page: Page) {
  await expect(page.locator("#view-start")).toBeVisible();
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="manual"]').click();
  const toggle = page.locator(
    '[data-maintenance-toggle="maintenance-config-card"]',
  );
  const editor = page.locator("#maintenance-config-editor");
  await expect(toggle).toContainText("inverters");
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

function configuredCards(page: Page) {
  return page.locator("#maintenance-config-inverters .hardware-card");
}

function cardByText(page: Page, text: string) {
  return configuredCards(page).filter({ hasText: text }).first();
}

async function openCard(page: Page, card: ReturnType<typeof cardByText>) {
  await expect(async () => {
    const body = card.locator(".hardware-card-body");
    if (!(await body.isVisible())) {
      await card.locator(".hardware-card-toggle").click();
    }
    await expect(body).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

function fieldInput(card: ReturnType<typeof cardByText>, label: string) {
  return card
    .locator("label.feature-field-row", { hasText: label })
    .locator("input, select")
    .first();
}

async function previewAndApply(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );
}

async function login(page: Page) {
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
}

test("MQTT then API: discovery offers a transport switch, not a duplicate inverter", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
    proposals: [mqttProposal(SERIAL_A), mqttProposal(SERIAL_B)],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);

  // The seeded config holds three inverters.
  await expect(configuredCards(page)).toHaveCount(3);

  // MQTT discovery offers both new inverters; add them to the draft.
  await runDiscovery(page);
  const addButtons = page.locator(
    "#maintenance-discovery-results .mconfig-discovery-add-button.is-add",
  );
  await expect(addButtons).toHaveCount(2);
  await addButtons.first().click();
  await addButtons.first().click();
  await expect(configuredCards(page)).toHaveCount(5);

  // Both new MQTT devices carry the central common defaults, visibly.
  const added = cardByText(page, SERIAL_A);
  await openCard(page, added);
  await expect(fieldInput(added, "Device output limit")).toHaveValue("800");
  await expect(fieldInput(added, "Minimum SoC")).toHaveValue("15");

  // Rename the first added inverter, then apply.
  await fieldInput(added, "Device name").fill("Roof West");
  await previewAndApply(page);

  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(5);
  const renamed = cardByText(page, "Roof West");
  await expect(renamed).toHaveClass(/hardware-card-zendure-mqtt/);

  // Local API discovery now sees the same serials: the review offers a
  // transport switch on the configured devices, never an "Add as inverter".
  state.apiDevices = [
    apiInverter(SERIAL_A, "192.168.60.21"),
    apiInverter(SERIAL_B, "192.168.60.22"),
  ];
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  const switchButtons = results.getByRole("button", {
    name: "Use Local API instead",
  });
  await expect(switchButtons).toHaveCount(2);
  await expect(
    results.getByRole("button", { name: "Add as inverter" }),
  ).toHaveCount(0);

  // Switch the renamed inverter to Local API: same card count, same name,
  // same tuning values, new connection fields.
  await results
    .locator(".mconfig-discovery-device-card", { hasText: "Roof West" })
    .getByRole("button", { name: "Use Local API instead" })
    .click();
  await expect(configuredCards(page)).toHaveCount(5);
  const switched = cardByText(page, "Roof West");
  await expect(switched).toHaveClass(/hardware-card-inverter/);
  await openCard(page, switched);
  await expect(fieldInput(switched, "Device IP address")).toHaveValue(
    "192.168.60.21",
  );
  await expect(fieldInput(switched, "Serial number")).toHaveValue(SERIAL_A);
  await expect(fieldInput(switched, "Device output limit")).toHaveValue("800");
  await expect(fieldInput(switched, "Device name")).toHaveValue("Roof West");

  await previewAndApply(page);
  await page.reload();
  await openMaintenanceEditor(page);

  // Exactly five inverter entries persist — the switch replaced, not added.
  await expect(configuredCards(page)).toHaveCount(5);
  const persisted = cardByText(page, "Roof West");
  await expect(persisted).toHaveClass(/hardware-card-inverter/);
  await openCard(page, persisted);
  await expect(fieldInput(persisted, "Device IP address")).toHaveValue(
    "192.168.60.21",
  );
  await expect(fieldInput(persisted, "Device output limit")).toHaveValue("800");
});

test("API then MQTT: proposal for a configured serial switches the transport in place", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
    proposals: [mqttProposal("API-SERIAL")],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);

  // The proposal matches the configured Local API inverter's serial: the only
  // offer is the transport switch.
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  const switchButton = results.getByRole("button", {
    name: "Use Local MQTT instead",
  });
  await expect(switchButton).toHaveCount(1);
  await expect(
    results.getByRole("button", { name: "Add to draft" }),
  ).toHaveCount(0);

  await switchButton.click();
  await expect(configuredCards(page)).toHaveCount(3);
  const switched = cardByText(page, "Local API inverter");
  await expect(switched).toHaveClass(/hardware-card-zendure-mqtt/);
  await openCard(page, switched);
  await expect(fieldInput(switched, "Serial number")).toHaveValue("API-SERIAL");
  // The seeded max_power survives; missing values materialize from defaults.
  await expect(fieldInput(switched, "Device output limit")).toHaveValue("800");
  await expect(fieldInput(switched, "Device name")).toHaveValue(
    "Local API inverter",
  );

  await previewAndApply(page);
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);
  const persisted = cardByText(page, "Local API inverter");
  await expect(persisted).toHaveClass(/hardware-card-zendure-mqtt/);
});
