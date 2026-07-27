import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// A physical serial is never an MQTT control route device id. Two discovery
// proposal cards prove the user-facing reason:
//  1. a supported inverter with a product key but no explicit mqtt.device_id is
//     telemetry-only and the reason names the missing MQTT device ID (never
//     "output control enabled");
//  2. one physical inverter carrying two precise product routes is control-
//     blocked, and the visible reason describes the route conflict — not the
//     unrelated capability/write-protocol name (control_block_reason wins).
// Discovery is deterministically mocked; only the proposals under test drive the
// #mqtt-proposals-list panel. See admin/static/admin.js renderMqttProposalCard.

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

function missingRouteProposal() {
  // Supported model + product key + physical serial, but no mqtt.device_id: the
  // write route is unaddressable, so the backend blocks control with
  // write_target_missing (the reason the card must surface).
  return {
    id: "zendure-mqtt:MISSINGROUTE",
    serial_number: "SERIAL-1",
    device_id: null,
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    topic_family: "legacy_zendure_json",
    broker_ref: "cloud",
    display_name: "Zendure Cloud Inverter (no route)",
    hardware_model: "solarflow_800_pro_2",
    hardware_generation_label: "SolarFlow 800 Pro 2",
    confidence: "high",
    role_hint: "battery_inverter_candidate",
    capabilities: ["battery_storage", "output_control"],
    metrics: ["outputLimit"],
    warnings: [],
    seen_topics: [],
    output_control_supported: false,
    output_control_reason: "write_target_missing",
    control_block_reason: "write_target_missing",
    config_fragment: {
      type: "zendure_mqtt",
      enabled: true,
      name: "Zendure Cloud Inverter (no route)",
      serial_number: "SERIAL-1",
      mqtt: {
        broker_ref: "cloud",
        source: "zendure_cloud_mqtt",
        topic_family: "legacy_zendure_json",
        product_key: "PK-A",
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: false },
    },
  };
}

function routeConflictProposal() {
  // One physical inverter carrying two precise product routes: control is
  // blocked. output_control_reason is only the capability/write-protocol name;
  // the visible reason must come from control_block_reason instead.
  return {
    id: "zendure-mqtt:ROUTECONFLICT",
    serial_number: "SERIAL-2",
    device_id: "DEV-X",
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    topic_family: "legacy_zendure_json",
    broker_ref: "cloud",
    display_name: "Zendure Cloud Inverter (route conflict)",
    hardware_model: "solarflow_800_pro_2",
    hardware_generation_label: "SolarFlow 800 Pro 2",
    confidence: "high",
    role_hint: "battery_inverter_candidate",
    capabilities: ["battery_storage", "output_control"],
    metrics: ["outputLimit"],
    warnings: ["identity_route_product_conflict"],
    seen_topics: [],
    output_control_supported: false,
    output_control_reason: "zensdk_properties_write",
    control_block_reason: "identity_route_product_conflict",
    config_fragment: {
      type: "zendure_mqtt",
      enabled: true,
      name: "Zendure Cloud Inverter (route conflict)",
      serial_number: "SERIAL-2",
      mqtt: {
        broker_ref: "cloud",
        source: "zendure_cloud_mqtt",
        topic_family: "legacy_zendure_json",
        device_id: "DEV-X",
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

async function loadProposalsPanel(page: Page) {
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
  const details = page.locator("#mqtt-proposals-details");
  if (!(await details.getAttribute("open"))) {
    await details.locator("> summary").click();
  }
  await expect(page.locator("#mqtt-proposals-list")).toBeVisible();
}

test("Fresh Setup: a supported inverter without a route device id is telemetry-only", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = {
    proposals: [missingRouteProposal()],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);
  await loadProposalsPanel(page);

  const card = page.locator("#mqtt-proposals-list .mqtt-proposal-card");
  await expect(card).toHaveCount(1);
  // Telemetry-only, and the reason names the missing MQTT device ID rather than
  // presenting it as controllable.
  await expect(card).toContainText("Telemetry only");
  await expect(card).toContainText("MQTT device ID");
  await expect(card).not.toContainText("Output control available");
});

test("Fresh Setup: a two-route physical inverter shows the route-conflict reason", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = {
    proposals: [routeConflictProposal()],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);
  await loadProposalsPanel(page);

  const card = page.locator("#mqtt-proposals-list .mqtt-proposal-card");
  await expect(card).toHaveCount(1);
  // The visible reason describes the route conflict (from control_block_reason),
  // never the unrelated capability/write-protocol name.
  await expect(card).toContainText("two MQTT product routes");
  await expect(card).not.toContainText("No verified write protocol");
});
