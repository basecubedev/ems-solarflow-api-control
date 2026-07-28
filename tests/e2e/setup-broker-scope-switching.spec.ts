import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// One physical inverter reachable through two local MQTT brokers. Both brokers
// are separate concrete connections: the candidate pool keeps the one that is
// not selected, and clicking it must bind that exact broker and route — never
// "the first proposal with the same source". Discovery and preview are
// deterministically mocked; the browser targeting behavior is under test.

const SERIAL = "EOD1SCOPE01";

function brokerProposal(scope: string, route: string) {
  return {
    id: `local-mqtt:${SERIAL}:${scope}`,
    serial_number: SERIAL,
    device_id: route,
    target: "device",
    connection_source: "local_mqtt",
    topic_family: "zensdk_ha_scalar",
    broker_ref: scope,
    broker_host: scope === "local_b1" ? "10.0.0.11" : "10.0.0.12",
    broker_port: 1883,
    output_control_supported: true,
    display_name: "SolarFlow 800 Pro 2",
    hardware_model: "solarFlow800Pro2",
    confidence: "high",
    role_hint: "inverter",
    capabilities: [],
    metrics: [],
    warnings: [],
    seen_topics: [],
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: SERIAL,
      enabled: true,
      name: "Zendure SolarFlow 800 Pro 2",
      mqtt: {
        broker_ref: scope,
        source: "local_mqtt",
        topic_family: "zensdk_ha_scalar",
        device_id: route,
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: true },
    },
  };
}

const B1 = brokerProposal("local_b1", "ROUTE-B1");
const B2 = brokerProposal("local_b2", "ROUTE-B2");

const SHELLY = {
  serial_number: "SHELLY123",
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

function httpInverter() {
  return {
    serial_number: SERIAL,
    role_suggestion: "inverter",
    ip: "192.168.100.78",
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

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type State = { priority: string[] };

async function mockDiscovery(page: Page, state: State) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices", (route) =>
    json(route, { devices: [httpInverter(), SHELLY], ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/status", (route) =>
    json(route, { state: "enabled", message: "", devices_found: 2 }),
  );
  await page.route("**/api/discovery/mdns/refresh", (route) => json(route, { state: "enabled" }));
  await page.route("**/api/discovery/networks", (route) => json(route, { networks: [] }));
  await page.route("**/api/discovery/mqtt-brokers", (route) => json(route, { candidates: [] }));
  await page.route("**/api/discovery/mqtt-brokers/**", (route) => json(route, { candidates: [] }));
  await page.route("**/api/discovery/mqtt-proposals", (route) =>
    json(route, { proposals: [B1, B2] }),
  );
  await page.route("**/api/discovery/preparation", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() || {};
      state.priority = body.discovery_priority || state.priority;
      return json(route, { discovery_priority: state.priority, sources: body.sources });
    }
    return json(route, {
      discovery_priority: state.priority,
      sources: {
        local_api: { enabled: true },
        local_mqtt: { enabled: true },
        zendure_mqtt: { enabled: true },
      },
    });
  });
  await page.route("**/api/discovery/run", (route) =>
    json(route, {
      priority: state.priority,
      sources: {
        local_api: { enabled: true },
        local_mqtt: { enabled: true },
        zendure_mqtt: { enabled: true },
      },
      devices: [],
      details: {},
      refresh: true,
    }),
  );

  // The preview echoes the broker scope and route of each selected proposal, so
  // the assertions prove which concrete connection the draft actually holds.
  await page.route("**/api/setup/config-preview", async (route) => {
    const body = route.request().postDataJSON() || {};
    const draftInverters = (body.devices || []).filter(
      (d: { role?: string; enabled?: boolean }) => d.role === "inverter" && d.enabled !== false,
    );
    const proposals = (body.zendure_mqtt_proposals || []).filter(
      (p: { enabled?: boolean }) => p.enabled !== false,
    );
    const configDevices = [
      ...draftInverters.map((d: { config_name: string; serial_number: string; ip: string }) => ({
        name: d.config_name,
        ip: d.ip,
        sn: d.serial_number,
        max_power: 800,
      })),
      ...proposals.map(
        (p: {
          config_name: string;
          config_fragment?: { mqtt?: { broker_ref?: string; device_id?: string } };
        }) => ({
          name: p.config_name,
          type: "zendure_mqtt",
          serial_number: SERIAL,
          mqtt: {
            broker_ref: (p.config_fragment || {}).mqtt?.broker_ref,
            device_id: (p.config_fragment || {}).mqtt?.device_id,
          },
          capabilities: { write_output_limit: true },
        }),
      ),
    ];
    return json(route, {
      ready: true,
      config: { devices: configDevices, grid_meter: { type: "shelly" } },
      summary: { inverters: configDevices.length, grid_meters: 1 },
      release: "latest",
      base: { source: "template" },
      validation: { errors: [], warnings: [], info: [] },
    });
  });
}

function inverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

// "Add more devices" is a collapsed <details>; its cards are only operable once
// it is open. The list re-renders on every draft change, the open state does not.
async function openCandidatePool(page: Page) {
  const details = page.locator("#config-available-details");
  await expect(async () => {
    if (!(await details.evaluate((node: HTMLDetailsElement) => node.open))) {
      await details.locator("> summary").click();
    }
    expect(await details.evaluate((node: HTMLDetailsElement) => node.open)).toBe(true);
  }).toPass();
}

// Selected by the connection pill, not by text: an API candidate's relationship
// note also mentions MQTT when the configured connection is MQTT.
function alternativeCard(page: Page, source: string) {
  return page
    .locator('#config-available-list .hardware-card-inverter[data-candidate-state="alternative"]')
    .filter({ has: page.locator(`.connection-pill[data-connection="${source}"]`) })
    .first();
}

async function reachConfig(page: Page, state: State) {
  await mockDiscovery(page, state);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
  await page.locator('[data-setup-step="config"]').click();
  await openCandidatePool(page);
}

test("a second local broker is a separate connection and binds exactly", async ({ page }) => {
  const dialogs: string[] = [];
  page.on("dialog", async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.dismiss();
  });
  const state: State = { priority: ["local_mqtt", "local_api", "zendure_mqtt"] };
  await reachConfig(page, state);

  // 01-02: one inverter is configured over MQTT; the pool keeps the other broker.
  await expect(inverterCards(page)).toHaveCount(1);
  const preview = page.locator("#config-preview");
  await expect(preview).toContainText('"broker_ref": "local_b1"');
  await expect(preview).toContainText('"device_id": "ROUTE-B1"');

  // 03: exactly one alternative — the other broker, not a duplicate of the active one.
  const alternatives = page.locator(
    '#config-available-list .hardware-card-inverter[data-candidate-state="alternative"]',
  );
  await expect(alternatives.filter({ hasText: "SN " + SERIAL })).toHaveCount(2);
  const otherBroker = alternativeCard(page, "local_mqtt");
  await expect(otherBroker).toContainText("Already configured as INV_1 via MQTT");

  // 04-05: one click, no popup.
  await otherBroker.getByRole("button", { name: "Use connection" }).click();
  expect(dialogs).toEqual([]);

  // 06-07: still one inverter, now bound to the clicked broker and its route.
  await expect(inverterCards(page)).toHaveCount(1);
  await expect(preview).toContainText('"broker_ref": "local_b2"');
  await expect(preview).toContainText('"device_id": "ROUTE-B2"');
  await expect(preview).not.toContainText("ROUTE-B1");
  await expect(inverterCards(page).first()).toContainText("INV_1");

  // 08: the first broker is offered back for switching.
  const firstBroker = alternativeCard(page, "local_mqtt");
  await expect(firstBroker).toContainText("Already configured as INV_1 via MQTT");
  await firstBroker.getByRole("button", { name: "Use connection" }).click();
  await expect(inverterCards(page)).toHaveCount(1);
  await expect(preview).toContainText('"broker_ref": "local_b1"');
  await expect(preview).toContainText('"device_id": "ROUTE-B1"');
});

test("candidate cards use the short connection labels", async ({ page }) => {
  const state: State = { priority: ["local_api", "local_mqtt", "zendure_mqtt"] };
  await reachConfig(page, state);

  // Configured over API, so the draft card and the API candidate say API and
  // the two MQTT brokers say MQTT.
  await expect(inverterCards(page).first().locator(".connection-pill")).toHaveText("API");
  const pool = page.locator("#config-available-list");
  await expect(
    pool.locator('.hardware-card-inverter[data-candidate-state="active"] .connection-pill'),
  ).toHaveText("API");
  const mqttPills = pool.locator(
    '.hardware-card-inverter[data-candidate-state="alternative"] .connection-pill',
  );
  await expect(mqttPills).toHaveCount(2);
  for (const pill of await mqttPills.all()) {
    await expect(pill).toHaveText("MQTT");
  }
  await expect(alternativeCard(page, "local_mqtt")).toContainText(
    "Already configured as INV_1 via API",
  );
});
