import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Fresh Setup transitive identity grouping and stable route anchors.
//
// 1. A Local-API serial, a stored route-only Cloud selection, and a serial+route
//    bridge rediscovery must collapse into ONE physical inverter (transitive).
// 2. The same scoped device rediscovered with a product key and a different
//    topic family keeps its stable selection (anchor unchanged).
// 3. One device id carrying two different product keys never becomes one writable
//    proposal.
//
// Discovery + preview are deterministically mocked; only opaque identity tokens
// (never a raw Cloud route or product key) cross into the browser.

const ANCHOR_TOKEN = "opaque:v1:E2EANCHOR7501TOKEN";
const SERIAL_TOKEN = "opaque:v1:E2ESERIAL7501TOKEN";
const ROUTE_A_TOKEN = "opaque:v1:E2EROUTEPKA7501";
const ROUTE_B_TOKEN = "opaque:v1:E2EROUTEPKB7501";
const STABLE_ID = `zendure-mqtt:${ANCHOR_TOKEN}:cloud`;
const SERIAL = "E2ESERIAL7501";
const MASKED_ROUTE = "…7501";

function httpInverter(serial: string, ip: string) {
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

function cloudFragment(serial: string | null, topicFamily: string) {
  const mqtt: Record<string, unknown> = {
    broker_ref: "cloud",
    source: "zendure_cloud_mqtt",
    topic_family: topicFamily,
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
    physical_identity_token: ANCHOR_TOKEN,
    physical_identity_alias_tokens: [ANCHOR_TOKEN],
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    topic_family: "zensdk_ha_scalar",
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
    config_fragment: cloudFragment(null, "zensdk_ha_scalar"),
  };
}

// Enriched with a physical serial: the stable id is unchanged (anchor-derived),
// the anchor token stays the primary equality token, and the serial token is an
// added alias.
function serialEnrichedProposal() {
  return {
    ...routeOnlyProposal(),
    serial_number: SERIAL,
    device_id: SERIAL,
    physical_identity_token: ANCHOR_TOKEN,
    physical_identity_alias_tokens: [ANCHOR_TOKEN, SERIAL_TOKEN],
    config_fragment: cloudFragment(SERIAL, "zensdk_ha_scalar"),
  };
}

// Enriched with a product key and a different topic family, still serial-less:
// the stable id and anchor token are unchanged, only a precise-route alias and
// the schema change.
function semanticEnrichedProposal() {
  return {
    ...routeOnlyProposal(),
    topic_family: "legacy_zendure_json",
    physical_identity_alias_tokens: [ANCHOR_TOKEN, ROUTE_A_TOKEN],
    config_fragment: cloudFragment(null, "legacy_zendure_json"),
  };
}

// Two different product keys on one device id: two distinct precise routes. They
// never share a browser token and neither is writable (control blocked).
function productConflictProposals() {
  const base = routeOnlyProposal();
  const pk = (token: string, suffix: string) => ({
    ...base,
    id: `zendure-mqtt:${token}:cloud`,
    physical_identity_token: token,
    physical_identity_alias_tokens: [token],
    display_name: `Zendure Cloud Inverter ${suffix}`,
    output_control_supported: false,
    control_block_reason: "identity_route_product_conflict",
    warnings: ["identity_route_product_conflict"],
    config_fragment: cloudFragment(null, "legacy_zendure_json"),
  });
  return [pk(ROUTE_A_TOKEN, "A"), pk(ROUTE_B_TOKEN, "B")];
}

// Two routes that differ only in case: distinct write addresses, so distinct
// server tokens and ids. They must render as two candidates, never one merged
// proposal. (Anonymized synthetic tokens stand in for PK/DEV vs pk/dev.)
const ROUTE_LOWER_TOKEN = "opaque:v1:E2Eroutelower7501";
function caseDistinctRouteProposals() {
  const base = routeOnlyProposal();
  const make = (token: string, suffix: string) => ({
    ...base,
    id: `zendure-mqtt:${token}:cloud`,
    physical_identity_token: token,
    physical_identity_alias_tokens: [token],
    display_name: `Zendure Cloud Inverter ${suffix}`,
    config_fragment: cloudFragment(null, "legacy_zendure_json"),
  });
  return [make(ROUTE_A_TOKEN, "UPPER"), make(ROUTE_LOWER_TOKEN, "lower")];
}

// One physical serial reporting two precise product routes: one inverter, but the
// write address is ambiguous, so output control is blocked and no product key is
// pinned.
function serialRouteConflictProposal() {
  return {
    ...routeOnlyProposal(),
    serial_number: SERIAL,
    device_id: SERIAL,
    physical_identity_token: ANCHOR_TOKEN,
    physical_identity_alias_tokens: [ANCHOR_TOKEN, SERIAL_TOKEN],
    topic_family: "legacy_zendure_json",
    output_control_supported: false,
    control_block_reason: "identity_route_product_conflict",
    warnings: ["identity_route_product_conflict"],
    config_fragment: cloudFragment(SERIAL, "legacy_zendure_json"),
  };
}

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type DiscoveryState = {
  devices: unknown[];
  proposals: unknown[];
  priority: string[];
};

async function mockDiscovery(page: Page, state: DiscoveryState) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices", (route) =>
    json(route, { devices: state.devices, ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/status", (route) =>
    json(route, { state: "enabled", message: "", devices_found: state.devices.length }),
  );
  await page.route("**/api/discovery/mdns/refresh", (route) =>
    json(route, { state: "enabled" }),
  );
  await page.route("**/api/discovery/networks", (route) => json(route, { networks: [] }));
  await page.route("**/api/discovery/mqtt-brokers", (route) => json(route, { candidates: [] }));
  await page.route("**/api/discovery/mqtt-brokers/**", (route) => json(route, { candidates: [] }));
  await page.route("**/api/discovery/mqtt-proposals", (route) =>
    json(route, { proposals: state.proposals }),
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

  await page.route("**/api/setup/config-preview", async (route) => {
    const body = route.request().postDataJSON() || {};
    const draftInverters = (body.devices || []).filter(
      (d: { role?: string; enabled?: boolean }) => d.role === "inverter" && d.enabled !== false,
    );
    const proposals = body.zendure_mqtt_proposals || [];
    const known = new Set(state.proposals.map((p: { id: string }) => p.id));
    const errors: { code: string; message: string }[] = [];
    const configDevices = [
      ...draftInverters.map((d: { config_name: string; serial_number: string; ip: string }) => ({
        name: d.config_name,
        ip: d.ip,
        sn: d.serial_number,
        max_power: 800,
      })),
      ...proposals.map((p: { id: string; config_name: string }) => {
        if (!known.has(String(p.id))) {
          errors.push({
            code: "zendure_mqtt_proposal_unknown",
            message: "The selected MQTT proposal is not present.",
          });
        }
        return {
          name: p.config_name,
          type: "zendure_mqtt",
          serial_number: SERIAL,
          capabilities: { write_output_limit: false },
        };
      }),
    ];
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
  if (!(await details.evaluate((el: HTMLDetailsElement) => el.open))) {
    await details.locator("> summary").click();
  }
  await expect(page.locator("#config-available-list")).toBeVisible();
}

async function addProposal(page: Page, proposalId: string) {
  await openAddMore(page);
  const addButton = page.locator(`.config-mqtt-add[data-proposal-id="${proposalId}"]`);
  await expect(addButton).toBeVisible();
  await addButton.click();
}

async function rescanZendureMqtt(page: Page) {
  await page.locator('[data-setup-step="devices"]').click();
  const zendureRow = page.locator('#discovery-priority-list [data-source="zendure_mqtt"]');
  await zendureRow.locator("[data-prep-configure]").click();
  const proposalsLoaded = page.waitForResponse(
    (r) => r.url().includes("/api/discovery/mqtt-proposals") && r.request().method() === "GET",
  );
  await zendureRow.locator("[data-prep-rescan]").click();
  await proposalsLoaded;
  await page.locator('[data-setup-step="config"]').click();
}

// Not currently reachable end to end: the journey needs a Local-API observation
// the *server* discovered (only then is it adopted) together with Cloud
// proposals whose opaque anchor tokens this spec pins by hand, and a spec cannot
// mint those tokens. The grouping itself is pinned by
// tests/test_admin_setup_batch_planner.py (`test_a_transitive_chain_is_one_group`,
// `test_a_route_only_selection_and_its_enriched_proposal_are_one_group`).
test.fixme("Fresh Setup: a Local-API serial, a route-only Cloud selection and a serial bridge are one inverter", { tag: ["@setup", "@authority"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = {
    devices: [httpInverter(SERIAL, "192.168.100.78"), SHELLY],
    proposals: [routeOnlyProposal()],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  // Local-API SERIAL auto-adds; the user also selects the route-only Cloud
  // candidate — two distinct physical devices so far — and names it.
  await page.locator('[data-setup-step="config"]').click();
  await addProposal(page, STABLE_ID);
  await expect(draftInverterCards(page)).toHaveCount(2);
  const cloudCard = page.locator(`#config-draft-list [data-source-id="${STABLE_ID}"]`);
  await expect(async () => {
    if (!(await cloudCard.locator("[data-mqtt-config-name]").count())) {
      await cloudCard.locator(".hardware-card-toggle").click();
    }
    await expect(cloudCard.locator("[data-mqtt-config-name]")).toBeVisible({ timeout: 1_000 });
  }).toPass();
  await cloudCard.locator("[data-mqtt-config-name]").fill("Roof Bridge");

  // The identical scoped route is rediscovered carrying the physical serial: it
  // now bridges the Local-API serial group and the route-only Cloud group.
  state.proposals = [serialEnrichedProposal()];
  await rescanZendureMqtt(page);

  // The three observations collapse into exactly one physical inverter, over one
  // transport, custom name preserved, and no stale/unknown proposal error.
  await expect(draftInverterCards(page)).toHaveCount(1);
  const merged = draftInverterCards(page).first();
  await expect(merged).toContainText("Roof Bridge");
  await expect(merged).toContainText("Zendure MQTT");
  await expect(page.locator("body")).not.toContainText("E2EANCHOR7501");
  await expect(page.locator("#config-preview-devices")).toContainText(/1 inverter/i);
  await expect(page.locator("#config-validation")).not.toContainText(/not present/i);
  await expect(page.locator("#setup-next")).toBeEnabled();
});

test("Fresh Setup: a semantic (product-key/topic-family) rediscovery keeps the same selection", { tag: ["@setup", "@authority"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = {
    devices: [SHELLY],
    proposals: [routeOnlyProposal()],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  await page.locator('[data-setup-step="config"]').click();
  await addProposal(page, STABLE_ID);
  await expect(draftInverterCards(page)).toHaveCount(1);
  const card = page.locator(`#config-draft-list [data-source-id="${STABLE_ID}"]`);
  await expect(async () => {
    if (!(await card.locator("[data-mqtt-config-name]").count())) {
      await card.locator(".hardware-card-toggle").click();
    }
    await expect(card.locator("[data-mqtt-config-name]")).toBeVisible({ timeout: 1_000 });
  }).toPass();
  await card.locator("[data-mqtt-config-name]").fill("Semantic Roof");

  // The same scoped device now includes a product key and a different topic
  // family. The stable id and anchor token are unchanged.
  state.proposals = [semanticEnrichedProposal()];
  await rescanZendureMqtt(page);

  // Still one inverter, the selection retained, the name preserved, no second
  // "Add", and no stale/unknown proposal error.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${STABLE_ID}"]`),
  ).toContainText("Semantic Roof");
  await openAddMore(page);
  await expect(page.locator(".config-mqtt-add")).toHaveCount(0);
  await expect(page.locator("#config-preview-devices")).toContainText(/1 inverter/i);
  await expect(page.locator("#config-validation")).not.toContainText(/not present/i);
});

test("Fresh Setup: two product keys on one device id never become one writable proposal", { tag: ["@setup", "@authority"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  const [pkA, pkB] = productConflictProposals();
  const state: DiscoveryState = {
    devices: [SHELLY],
    proposals: [pkA, pkB],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  await page.locator('[data-setup-step="config"]').click();
  await openAddMore(page);
  // Two distinct candidates, never merged into one, and neither offers output
  // control (the write target is ambiguous — control is blocked).
  await expect(page.locator(".config-mqtt-add")).toHaveCount(2);
  await expect(page.locator(`.config-mqtt-add[data-proposal-id="${pkA.id}"]`)).toHaveCount(1);
  await expect(page.locator(`.config-mqtt-add[data-proposal-id="${pkB.id}"]`)).toHaveCount(1);
  await expect(page.locator("#config-available-list")).not.toContainText(
    /Output control[^<]*Supported/i,
  );
  await expect(page.locator("body")).not.toContainText("E2EROUTEPKA7501");
});

test("Fresh Setup: case-distinct MQTT routes stay two candidates and never merge", { tag: ["@setup", "@authority"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  const [upper, lower] = caseDistinctRouteProposals();
  const state: DiscoveryState = {
    devices: [SHELLY],
    proposals: [upper, lower],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  await page.locator('[data-setup-step="config"]').click();
  await openAddMore(page);
  // Two distinct candidates for two case-distinct write addresses — never one.
  await expect(page.locator(".config-mqtt-add")).toHaveCount(2);
  await expect(page.locator(`.config-mqtt-add[data-proposal-id="${upper.id}"]`)).toHaveCount(1);
  await expect(page.locator(`.config-mqtt-add[data-proposal-id="${lower.id}"]`)).toHaveCount(1);

  // Adding both keeps two separate inverter cards; they never collapse into one.
  await addProposal(page, upper.id);
  await addProposal(page, lower.id);
  await expect(draftInverterCards(page)).toHaveCount(2);
  await expect(page.locator("body")).not.toContainText("E2Eroutelower7501");
});

test("Fresh Setup: a serial with two precise routes is one blocked inverter", { tag: ["@setup", "@authority"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  const conflict = serialRouteConflictProposal();
  const state: DiscoveryState = {
    devices: [SHELLY],
    proposals: [conflict],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
  await reachDevices(page, state);

  await page.locator('[data-setup-step="config"]').click();
  await openAddMore(page);
  // One physical inverter, offered as a single candidate with control blocked.
  await expect(page.locator(".config-mqtt-add")).toHaveCount(1);
  await expect(page.locator("#config-available-list")).not.toContainText(
    /Output control[^<]*Supported/i,
  );

  await addProposal(page, conflict.id);
  // Exactly one inverter card, and it is telemetry-only (control blocked).
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(draftInverterCards(page).first()).not.toContainText(/Output control[^<]*Enabled/i);
  await expect(page.locator("#config-preview-devices")).toContainText(/1 inverter/i);
});
