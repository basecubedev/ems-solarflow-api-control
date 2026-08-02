import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Fresh Setup route-to-serial identity enrichment. A serial-less Cloud MQTT
// inverter selected in Setup must be recognized as the SAME inverter once the
// identical scoped route is rediscovered carrying a physical serial: one card,
// the custom name preserved, no second "Add", one device in the preview, and no
// unknown-proposal / stale-id error. The same raw route in two broker scopes
// stays two distinct candidates. Discovery + preview are deterministically
// mocked; only opaque identity tokens (never a raw Cloud route) cross into the
// browser. The grouping itself is admin/setup_planner.py's.

const ROUTE_TOKEN = "opaque:v1:E2EROUTE7501TOKEN";
const SERIAL_TOKEN = "opaque:v1:E2ESERIAL7501TOKEN";
const STABLE_ID = `zendure-mqtt:${ROUTE_TOKEN}:cloud`;
const ENRICH_SERIAL = "E2ESERIAL7501";
const MASKED_ROUTE = "…7501"; // "…7501" — the public, masked route form.

const SHELLY = {
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

function cloudInverterFragment(serial: string | null) {
  const mqtt: Record<string, unknown> = {
    broker_ref: "cloud",
    source: "zendure_cloud_mqtt",
    topic_family: "legacy_zendure_json",
  };
  if (serial) mqtt.device_id = serial;
  const fragment: Record<string, unknown> = {
    type: "zendure_mqtt",
    enabled: true,
    name: "Zendure Cloud Inverter",
    mqtt,
    capabilities: { read_power: true, read_soc: true, write_output_limit: false },
  };
  if (serial) fragment.serial_number = serial;
  return fragment;
}

function routeOnlyProposal() {
  return {
    id: STABLE_ID,
    serial_number: null,
    device_id: MASKED_ROUTE,
    physical_identity_token: ROUTE_TOKEN,
    physical_identity_alias_tokens: [ROUTE_TOKEN],
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    topic_family: "legacy_zendure_json",
    broker_ref: "cloud",
    output_control_supported: false,
    display_name: "Zendure Cloud Inverter",
    hardware_model: "solarFlow800Pro2",
    hardware_generation_label: "SolarFlow 800 Pro 2",
    confidence: "high",
    role_hint: "inverter",
    capabilities: [],
    metrics: [],
    warnings: [],
    seen_topics: [],
    config_fragment: cloudInverterFragment(null),
  };
}

function enrichedProposal() {
  return {
    ...routeOnlyProposal(),
    serial_number: ENRICH_SERIAL,
    device_id: ENRICH_SERIAL,
    physical_identity_token: SERIAL_TOKEN,
    physical_identity_alias_tokens: [SERIAL_TOKEN, ROUTE_TOKEN],
    config_fragment: cloudInverterFragment(ENRICH_SERIAL),
  };
}

// The same raw route observed in a different broker/account scope carries a
// distinct opaque token, so it must remain a separate candidate.
function otherScopeProposal() {
  return {
    ...routeOnlyProposal(),
    id: `zendure-mqtt:opaque:v1:E2EOTHERSCOPETOKEN:garage`,
    physical_identity_token: "opaque:v1:E2EOTHERSCOPETOKEN",
    physical_identity_alias_tokens: ["opaque:v1:E2EOTHERSCOPETOKEN"],
    connection_source: "local_mqtt",
    broker_ref: "garage",
    display_name: "Zendure Local Inverter",
    config_fragment: {
      ...cloudInverterFragment(null),
      name: "Zendure Local Inverter",
      mqtt: {
        broker_ref: "garage",
        source: "local_mqtt",
        topic_family: "legacy_zendure_json",
      },
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

type DiscoveryState = { proposals: unknown[]; priority: string[] };

async function mockDiscovery(page: Page, state: DiscoveryState) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices", (route) =>
    json(route, { devices: [SHELLY], ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/status", (route) =>
    json(route, { state: "enabled", message: "", devices_found: 1 }),
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
    json(route, { proposals: state.proposals }),
  );
  await page.route("**/api/discovery/preparation", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() || {};
      state.priority = body.discovery_priority || state.priority;
      return json(route, {
        discovery_priority: state.priority,
        sources: body.sources,
      });
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
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings", (route) =>
    json(route, { token_saved: true, broker: "mqtt.zen-iot.com", tls_mode: "system_ca" }),
  );
  await page.route("**/api/discovery/zendure-cloud-mqtt/refresh", (route) =>
    json(route, {
      ok: true,
      candidates: [],
      device_list_count: 1,
      mqtt_observed_count: 1,
      broker: "mqtt.zen-iot.com",
      tls_mode: "system_ca",
      mqtt_message: "Zendure cloud discovery complete.",
    }),
  );

  // Preview resolves the submitted selections to their configured devices. A
  // stable-id selection always resolves; an unknown/stale id would surface as a
  // preview error (which this test asserts never happens).
  await page.route("**/api/setup/config-preview", async (route) => {
    const body = route.request().postDataJSON() || {};
    const proposals = body.zendure_mqtt_proposals || [];
    const known = new Set([STABLE_ID, enrichedProposal().id]);
    const errors: { code: string; message: string }[] = [];
    const configDevices = proposals.map(
      (p: { id: string; config_name: string }) => {
        if (!known.has(String(p.id))) {
          errors.push({
            code: "zendure_mqtt_proposal_unknown",
            message: "The selected MQTT proposal is not present.",
          });
        }
        return {
          name: p.config_name,
          type: "zendure_mqtt",
          serial_number: ENRICH_SERIAL,
          capabilities: { write_output_limit: false },
        };
      },
    );
    return json(route, {
      ready: errors.length === 0,
      config: { devices: configDevices, grid_meter: { type: "shelly" } },
      summary: { inverters: configDevices.length, grid_meters: 1 },
      release: "latest",
      base: { source: "template" },
      validation: { errors, warnings: [], info: [] },
    });
  });
}

async function reachDevices(page: Page, state: DiscoveryState) {
  await mockDiscovery(page, state);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
}

function draftInverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

async function openAddMore(page: Page) {
  const details = page.locator("#config-available-details");
  if (!(await details.getAttribute("open"))) {
    await details.locator("> summary").click();
  }
  await expect(page.locator("#config-available-list")).toBeVisible();
}

async function addRouteOnlyInverter(page: Page) {
  await openAddMore(page);
  const addButton = page.locator(
    `.config-mqtt-add[data-proposal-id="${STABLE_ID}"]`,
  );
  await expect(addButton).toBeVisible();
  await addButton.click();
  await expect(draftInverterCards(page)).toHaveCount(1);
}

async function openDraftCard(page: Page, sourceId: string) {
  const card = page.locator(`#config-draft-list [data-source-id="${sourceId}"]`);
  await expect(async () => {
    if (!(await card.locator("[data-mqtt-config-name]").count())) {
      await card.locator(".hardware-card-toggle").click();
    }
    await expect(card.locator("[data-mqtt-config-name]")).toBeVisible({
      timeout: 1_000,
    });
  }).toPass();
  return card;
}

async function rescanZendureMqtt(page: Page) {
  await page.locator('[data-setup-step="devices"]').click();
  const zendureRow = page.locator(
    '#discovery-priority-list [data-source="zendure_mqtt"]',
  );
  await zendureRow.locator("[data-prep-configure]").click();
  const proposalsLoaded = page.waitForResponse(
    (r) =>
      r.url().includes("/api/discovery/mqtt-proposals") &&
      r.request().method() === "GET",
  );
  await zendureRow.locator("[data-prep-rescan]").click();
  await proposalsLoaded;
  await page.locator('[data-setup-step="config"]').click();
}

test("Fresh Setup: a serial-bearing rediscovery of a serial-less Cloud route is the same inverter", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = {
    proposals: [routeOnlyProposal()],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  // Select the serial-less Cloud inverter and give it a custom name.
  await page.locator('[data-setup-step="config"]').click();
  await addRouteOnlyInverter(page);
  const card = await openDraftCard(page, STABLE_ID);
  await card.locator("[data-mqtt-config-name]").fill("Roof Enriched");
  await expect(card.locator("[data-mqtt-config-name]")).toHaveValue(
    "Roof Enriched",
  );
  await expect(page.locator("body")).not.toContainText("E2EROUTE7501");

  // Reload: the selection (and its custom name) persists across a reload.
  await page.reload();
  await page.locator('[data-setup-step="config"]').click();
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${STABLE_ID}"]`),
  ).toContainText("Roof Enriched");

  // The identical scoped route is rediscovered now carrying a physical serial.
  state.proposals = [enrichedProposal()];
  await rescanZendureMqtt(page);

  // Still exactly one inverter, still named "Roof Enriched", and no second
  // "Add" action is offered (the enriched proposal keeps the same stable id).
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${STABLE_ID}"]`),
  ).toContainText("Roof Enriched");
  await openAddMore(page);
  await expect(page.locator(".config-mqtt-add")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("E2EROUTE7501");

  // The preview holds exactly one device and resolves without an unknown /
  // stale-id error, so Continue is enabled.
  await expect(page.locator("#config-preview-ready")).toHaveText(/Ready/i);
  await expect(page.locator("#config-preview-devices")).toContainText(
    /1 inverter/i,
  );
  await expect(page.locator("#config-validation")).not.toContainText(
    /not present/i,
  );
  await expect(page.locator("#setup-next")).toBeEnabled();
});

test("Fresh Setup: the same raw route in two broker scopes stays two distinct candidates", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = {
    proposals: [routeOnlyProposal(), otherScopeProposal()],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  await page.locator('[data-setup-step="config"]').click();
  await openAddMore(page);
  // Both scopes are offered as distinct inverter candidates; the shared raw
  // route is never used to merge them.
  await expect(page.locator(".config-mqtt-add")).toHaveCount(2);
  await expect(
    page.locator(`.config-mqtt-add[data-proposal-id="${STABLE_ID}"]`),
  ).toHaveCount(1);
  await expect(
    page.locator(`.config-mqtt-add[data-proposal-id="${otherScopeProposal().id}"]`),
  ).toHaveCount(1);
});
