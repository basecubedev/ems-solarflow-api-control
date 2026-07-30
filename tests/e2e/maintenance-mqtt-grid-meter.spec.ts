import { type Locator, type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// A Maintenance MQTT proposal the backend classified as a grid meter is adopted
// as the central grid meter — never as an inverter. The purple role card offers
// "Use as grid meter", the click updates only the draft, and the live config
// still changes through the normal preview/apply workflow. Discovery is
// deterministically mocked; draft, preview and apply run against the real
// test-mode backend. See admin/static/admin.js mqttGridMeterConfigFromProposal /
// mconfigAdoptMqttGridMeterProposal.

const D0_SERIAL = "E2EGRIDD0";
const D0_TOPIC = `Zendure/sensor/${D0_SERIAL}/totalPower`;
const CT3_SERIAL = "E2EGRID3CT";
const CT3_TOPIC = `Zendure/sensor/${CT3_SERIAL}/totalPower`;
// The broker profile the seeded mixed_transports install already owns, and its
// endpoint as discovery reports it.
const LOCAL_BROKER = "local_mixed";
const LOCAL_BROKER_HOST = "192.168.50.10";
// A broker discovered in this session that the seeded config never declared.
const NEW_BROKER = "local_mqtt_192_168_60_40_e2egrid";
const NEW_BROKER_HOST = "192.168.60.40";

type DiscoveryState = { proposals: unknown[] };

function mqttGridMeterProposal(
  serial: string,
  displayName: string,
  topic: string,
  source = "local_mqtt",
  brokerRef = LOCAL_BROKER,
  brokerHost = LOCAL_BROKER_HOST,
) {
  return {
    id: `zendure-mqtt:${brokerRef}:${serial}`,
    serial_number: serial,
    device_id: serial,
    target: "grid_meter",
    connection_source: source,
    broker_ref: brokerRef,
    broker_host: brokerHost,
    broker_port: 1883,
    broker_tls: false,
    topic_family: "zensdk_ha_scalar",
    display_name: displayName,
    hardware_generation_label: "Zendure Smart Meter",
    confidence: "high",
    role_hint: "grid_meter_candidate",
    capabilities: [],
    metrics: ["totalPower"],
    warnings: [],
    output_control_supported: false,
    seen_topics: [topic],
    grid_meter_fragment: {
      type: "zendure_smartmeter_d0",
      mqtt: {
        broker_ref: brokerRef,
        topic,
        payload_format: "number",
        max_age_seconds: 15,
      },
    },
  };
}

// Grid-meter hardware seen over Zendure Cloud MQTT: the backend never mints a
// grid-meter fragment for a cloud topic, so it stays a grid meter without a
// mapping — and must not fall back to an inverter action.
function cloudGridMeterCandidate() {
  return {
    id: "zendure-mqtt:cloud_mixed:E2EGRIDCLOUD",
    serial_number: "E2EGRIDCLOUD",
    device_id: "E2EGRIDCLOUD",
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    broker_ref: "cloud_mixed",
    topic_family: "legacy_zendure_json",
    display_name: "Cloud MQTT grid meter",
    hardware_generation_label: "Zendure Smart Meter",
    confidence: "medium",
    role_hint: "grid_meter_candidate",
    capabilities: [],
    metrics: ["gridInputPower"],
    warnings: ["grid_metric_without_topic"],
    output_control_supported: false,
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: "E2EGRIDCLOUD",
      enabled: true,
      name: "Cloud MQTT grid meter",
      mqtt: {
        broker_ref: "cloud_mixed",
        source: "zendure_cloud_mqtt",
        topic_family: "legacy_zendure_json",
        device_id: "E2EGRIDCLOUD",
      },
      capabilities: { read_power: true, read_soc: false, write_output_limit: false },
    },
  };
}

function mqttInverterProposal() {
  const serial = "E2EGRIDINV";
  return {
    id: `zendure-mqtt:${LOCAL_BROKER}:${serial}`,
    serial_number: serial,
    device_id: serial,
    target: "device",
    connection_source: "local_mqtt",
    broker_ref: LOCAL_BROKER,
    topic_family: "zensdk_ha_scalar",
    display_name: "Local MQTT inverter",
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
      name: "Local MQTT inverter",
      mqtt: {
        broker_ref: LOCAL_BROKER,
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

function proposalCard(page: Page, text: string) {
  return page
    .locator("#maintenance-discovery-results .mconfig-discovery-proposal-card")
    .filter({ hasText: text })
    .first();
}

function gridMeterCard(page: Page) {
  return page.locator("#maintenance-config-gridmeter .hardware-card").first();
}

async function expectGridMeterAction(card: Locator) {
  await expect(card).toHaveAttribute("data-role", "grid_meter");
  await expect(card).toHaveClass(/hardware-card-grid-meter/);
  await expect(card).not.toHaveClass(/hardware-card-inverter/);
  await expect(card.getByRole("button", { name: "Use as grid meter" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Add inverter" })).toHaveCount(0);
}

async function adoptAsGridMeter(page: Page, card: Locator) {
  // The seeded install already has a Shelly meter: the swap is confirmed, never
  // silent.
  page.once("dialog", (dialog) => dialog.accept());
  await card.getByRole("button", { name: "Use as grid meter" }).click();
}

async function applyDraft(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );
}

// Run Preview and return the merged config the server answered with, so the
// provisioned broker profile is asserted on the real preview payload.
async function previewConfig(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  const raw = page.locator("#maintenance-config-raw-pre");
  await expect(raw).not.toHaveText("");
  return JSON.parse((await raw.textContent()) ?? "{}");
}

function brokerProfiles(preview: any) {
  return (preview.zendure_mqtt ?? {}).brokers ?? {};
}

test("Maintenance: a D0 grid meter on an unknown broker is provisioned through preview and apply", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  await mockDiscovery(page, {
    proposals: [
      mqttGridMeterProposal(
        D0_SERIAL,
        "New broker smart meter D0",
        D0_TOPIC,
        "local_mqtt",
        NEW_BROKER,
        NEW_BROKER_HOST,
      ),
    ],
  });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  const invertersBefore = await page
    .locator("#maintenance-config-inverters .hardware-card")
    .count();
  await runDiscovery(page);

  const meterCard = proposalCard(page, "New broker smart meter D0");
  await expectGridMeterAction(meterCard);
  await adoptAsGridMeter(page, meterCard);

  // The draft references the broker the seeded config never declared.
  const configured = gridMeterCard(page);
  await expect(configured).toContainText("Zendure SmartMeter D0 via MQTT");
  await expect(configured).toContainText(`Broker profile ${NEW_BROKER}`);
  await expect(page.locator("#maintenance-config-inverters .hardware-card")).toHaveCount(
    invertersBefore,
  );

  // Preview validates: the broker profile is provisioned beside the MQTT meter.
  const preview = await previewConfig(page);
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-warnings")).not.toContainText(
    "not a configured",
  );
  expect(brokerProfiles(preview)[NEW_BROKER]).toMatchObject({
    enabled: true,
    source: "local_mqtt",
    host: NEW_BROKER_HOST,
    port: 1883,
  });
  expect(preview.grid_meter.mqtt.broker_ref).toBe(NEW_BROKER);
  expect(preview.grid_meter.mqtt.topic).toBe(D0_TOPIC);

  await applyDraft(page);
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(gridMeterCard(page)).toContainText(`Broker profile ${NEW_BROKER}`);

  // Reloaded from disk: the profile persisted exactly once beside the seeded ones.
  const persisted = await previewConfig(page);
  expect(Object.keys(brokerProfiles(persisted)).sort()).toEqual(
    [LOCAL_BROKER, NEW_BROKER, "cloud_mixed"].sort(),
  );
  expect(persisted.grid_meter.mqtt.broker_ref).toBe(NEW_BROKER);
});

test("Maintenance: a grid meter on a configured broker adds no second profile", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  // Same endpoint as the seeded local_mixed profile under a freshly minted ref:
  // broker identity decides, so the declared profile is reused.
  await mockDiscovery(page, {
    proposals: [
      mqttGridMeterProposal(
        D0_SERIAL,
        "Known broker smart meter D0",
        D0_TOPIC,
        "local_mqtt",
        "local_mqtt_192_168_50_10_e2egrid",
      ),
    ],
  });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  await runDiscovery(page);

  await adoptAsGridMeter(page, proposalCard(page, "Known broker smart meter D0"));
  const preview = await previewConfig(page);
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  expect(Object.keys(brokerProfiles(preview)).sort()).toEqual(
    [LOCAL_BROKER, "cloud_mixed"].sort(),
  );
  expect(preview.grid_meter.mqtt.broker_ref).toBe(LOCAL_BROKER);

  await applyDraft(page);
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(gridMeterCard(page)).toContainText(`Broker profile ${LOCAL_BROKER}`);
  const persisted = await previewConfig(page);
  expect(Object.keys(brokerProfiles(persisted)).sort()).toEqual(
    [LOCAL_BROKER, "cloud_mixed"].sort(),
  );
});

test("Maintenance: declining the grid-meter replacement leaves no broker profile behind", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  await mockDiscovery(page, {
    proposals: [
      mqttGridMeterProposal(
        D0_SERIAL,
        "New broker smart meter D0",
        D0_TOPIC,
        "local_mqtt",
        NEW_BROKER,
        NEW_BROKER_HOST,
      ),
    ],
  });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  await runDiscovery(page);

  const meterCard = proposalCard(page, "New broker smart meter D0");
  page.once("dialog", (dialog) => dialog.dismiss());
  await meterCard.getByRole("button", { name: "Use as grid meter" }).click();

  // The configured Shelly meter stays, and the proposal stays offerable.
  await expect(gridMeterCard(page)).toContainText("192.168.50.2");
  await expect(meterCard).toHaveAttribute("data-state", "new");

  const preview = await previewConfig(page);
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-changes")).not.toContainText(
    NEW_BROKER,
  );
  expect(Object.keys(brokerProfiles(preview)).sort()).toEqual(
    [LOCAL_BROKER, "cloud_mixed"].sort(),
  );
  expect(preview.grid_meter).toEqual({ type: "shelly", ip: "192.168.50.2" });
});

test("Maintenance: a Local MQTT D0 proposal is adopted as the grid meter, not as an inverter", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  await mockDiscovery(page, {
    proposals: [
      mqttGridMeterProposal(D0_SERIAL, "Local MQTT smart meter D0", D0_TOPIC),
      mqttInverterProposal(),
    ],
  });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  const invertersBefore = await page
    .locator("#maintenance-config-inverters .hardware-card")
    .count();
  await runDiscovery(page);

  const meterCard = proposalCard(page, "Local MQTT smart meter D0");
  await expectGridMeterAction(meterCard);
  // Role and transport stay separate: purple card, Local MQTT connection.
  await expect(meterCard).toHaveAttribute("data-connection", "local_mqtt");
  await expect(meterCard.locator(".connection-pill")).toHaveText("MQTT");
  await expect(meterCard).toContainText(D0_TOPIC);

  // An MQTT inverter is untouched by the grid-meter routing.
  const inverterCard = proposalCard(page, "Local MQTT inverter");
  await expect(inverterCard).toHaveAttribute("data-role", "inverter");
  await expect(inverterCard).toHaveClass(/hardware-card-inverter/);
  await expect(
    inverterCard.getByRole("button", { name: "Add inverter" }),
  ).toBeVisible();

  await adoptAsGridMeter(page, meterCard);

  // The draft grid meter carries the mapped D0 type, broker profile and serial.
  const configured = gridMeterCard(page);
  await expect(configured).toHaveClass(/hardware-card-grid-meter/);
  await expect(configured).toContainText("Zendure SmartMeter D0 via MQTT");
  await expect(configured).toContainText(`Broker profile ${LOCAL_BROKER}`);
  await expect(
    configured.locator('input[type="text"]').first(),
  ).toHaveValue(D0_SERIAL);
  // Nothing was added to the inverter list.
  await expect(page.locator("#maintenance-config-inverters .hardware-card")).toHaveCount(
    invertersBefore,
  );
  // A draft change requires a preview before anything can be applied.
  await expect(page.locator("#maintenance-config-summary")).toContainText(
    /preview required/,
  );

  // The proposal card rerenders as part of the draft and stays a grid meter.
  await expect(proposalCard(page, "Local MQTT smart meter D0")).toHaveAttribute(
    "data-state",
    "added",
  );
  await expect(
    proposalCard(page, "Local MQTT smart meter D0").getByRole("button", {
      name: "Added to draft",
    }),
  ).toBeDisabled();

  await applyDraft(page);
  await page.reload();
  await openMaintenanceEditor(page);
  const persisted = gridMeterCard(page);
  await expect(persisted).toHaveClass(/hardware-card-grid-meter/);
  await expect(persisted).toContainText("Zendure SmartMeter D0 via MQTT");
  await expect(persisted).toContainText(`Broker profile ${LOCAL_BROKER}`);
});

test("Maintenance: a Local MQTT 3CT grid meter uses the same adoption action", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  await mockDiscovery(page, {
    proposals: [
      mqttGridMeterProposal(CT3_SERIAL, "Local MQTT Smart Meter 3CT", CT3_TOPIC),
    ],
  });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  await runDiscovery(page);

  const card = proposalCard(page, "Local MQTT Smart Meter 3CT");
  await expectGridMeterAction(card);
  await adoptAsGridMeter(page, card);

  const configured = gridMeterCard(page);
  await expect(configured).toContainText("Zendure SmartMeter D0 via MQTT");
  await expect(
    configured.locator('input[type="text"]').first(),
  ).toHaveValue(CT3_SERIAL);
  await applyDraft(page);
});

test("Maintenance: a Zendure MQTT grid meter is never offered as an inverter", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(90_000);
  await mockDiscovery(page, {
    proposals: [cloudGridMeterCandidate(), mqttInverterProposal()],
  });
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);
  await runDiscovery(page);

  const cloud = proposalCard(page, "Cloud MQTT grid meter");
  await expectGridMeterAction(cloud);
  await expect(cloud).toHaveAttribute("data-connection", "zendure_mqtt");
  await expect(cloud.locator(".connection-pill")).toHaveText("Zendure MQTT");
  // No trusted totalPower topic over the cloud: the action is inert, not an add.
  await expect(cloud).toHaveAttribute("data-state", "unavailable");
  await expect(
    cloud.getByRole("button", { name: "Use as grid meter" }),
  ).toBeDisabled();
  // The configured Shelly meter is untouched by a proposal that cannot map.
  await expect(gridMeterCard(page)).toContainText("192.168.50.2");
});
