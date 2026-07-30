import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Guided Setup MQTT proposals use the one shared hardware-card system: the
// hardware role owns the card colour and the transport is only a label. A
// recognized inverter is blue (--output) and a recognized grid meter purple
// (--grid) over Local MQTT and Zendure MQTT alike; anything the backend did not
// positively classify stays neutral. See admin/static/admin.js
// mqttProposalHardwareRole / hardwareCardClass.

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

function localInverterProposal() {
  return {
    id: "zendure-mqtt:LOCAL-INV",
    serial_number: "SERIAL-LOCAL",
    device_id: "DEV-LOCAL",
    target: "device",
    connection_source: "local_mqtt",
    topic_family: "zensdk_ha_scalar",
    broker_ref: "default",
    display_name: "Local MQTT Inverter",
    hardware_model: "solarflow_800_pro_2",
    hardware_generation_label: "SolarFlow 800 Pro 2",
    confidence: "high",
    role_hint: "battery_inverter_candidate",
    capabilities: ["battery_storage", "output_control"],
    metrics: ["outputLimit", "electricLevel"],
    warnings: [],
    seen_topics: [],
    output_control_supported: true,
    output_control_reason: "legacy_properties_write",
    config_fragment: {
      type: "zendure_mqtt",
      enabled: true,
      name: "Local MQTT Inverter",
      serial_number: "SERIAL-LOCAL",
      mqtt: {
        broker_ref: "default",
        source: "local_mqtt",
        topic_family: "zensdk_ha_scalar",
        device_id: "DEV-LOCAL",
        write_protocol: "legacy_properties_write",
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: true },
    },
  };
}

function cloudInverterProposal() {
  return {
    id: "zendure-mqtt:CLOUD-INV",
    serial_number: "SERIAL-CLOUD",
    device_id: "DEV-CLOUD",
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    topic_family: "legacy_zendure_json",
    broker_ref: "cloud",
    display_name: "Zendure Cloud Inverter",
    hardware_model: "solarflow_800_pro_2",
    hardware_generation_label: "SolarFlow 800 Pro 2",
    confidence: "high",
    role_hint: "battery_inverter_candidate",
    capabilities: ["battery_storage", "output_control"],
    metrics: ["outputLimit", "electricLevel"],
    warnings: [],
    seen_topics: [],
    output_control_supported: true,
    output_control_reason: "legacy_properties_write",
    config_fragment: {
      type: "zendure_mqtt",
      enabled: true,
      name: "Zendure Cloud Inverter",
      serial_number: "SERIAL-CLOUD",
      mqtt: {
        broker_ref: "cloud",
        source: "zendure_cloud_mqtt",
        topic_family: "legacy_zendure_json",
        device_id: "DEV-CLOUD",
        write_protocol: "legacy_properties_write",
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: true },
    },
  };
}

function localGridMeterProposal() {
  // D0 smart meter: a grid_meter target with the read-only grid-meter fragment.
  return {
    id: "zendure-mqtt:LOCAL-D0",
    serial_number: "SERIAL-D0",
    device_id: "D0DEVICE",
    target: "grid_meter",
    connection_source: "local_mqtt",
    topic_family: "zensdk_ha_scalar",
    broker_ref: "default",
    display_name: "Local MQTT Smart Meter",
    hardware_generation_label: "Zendure Smart Meter",
    confidence: "high",
    role_hint: "grid_meter_candidate",
    capabilities: [],
    metrics: ["totalPower"],
    warnings: [],
    seen_topics: ["Zendure/sensor/D0DEVICE/totalPower"],
    output_control_supported: false,
    grid_meter_fragment: {
      type: "zendure_smartmeter_d0",
      mqtt: {
        broker_ref: "default",
        topic: "Zendure/sensor/D0DEVICE/totalPower",
      },
    },
  };
}

function unknownProposal() {
  // Telemetry without a positively identified hardware role: it may be shown,
  // but it must never borrow the inverter colour.
  return {
    id: "zendure-mqtt:UNKNOWN",
    serial_number: "SERIAL-UNKNOWN",
    device_id: "DEV-UNKNOWN",
    target: "device",
    connection_source: "zendure_cloud_mqtt",
    topic_family: "unknown",
    broker_ref: "cloud",
    display_name: "Unclassified MQTT Device",
    confidence: "low",
    role_hint: "unknown_candidate",
    capabilities: [],
    metrics: ["someValue"],
    warnings: ["insufficient_telemetry"],
    seen_topics: [],
    output_control_supported: false,
    output_control_reason: "output_control_not_observed",
    config_fragment: {
      type: "zendure_mqtt",
      enabled: true,
      name: "Unclassified MQTT Device",
      serial_number: "SERIAL-UNKNOWN",
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

function cardByName(page: Page, name: string) {
  return page.locator("#mqtt-proposals-list .mqtt-proposal-card").filter({
    has: page.locator(".mqtt-device-title", { hasText: name }),
  });
}

function transportValue(page: Page, name: string) {
  return cardByName(page, name)
    .locator(".device-fact", { hasText: "Transport" })
    .locator(".v");
}

// The literal colour the shared role classes resolve to, read from the live
// stylesheet so the assertion follows the CSS custom properties instead of a
// hard-coded hex value.
async function roleAccent(page: Page, token: string) {
  return page.evaluate(
    (name) =>
      getComputedStyle(document.documentElement).getPropertyValue(name).trim(),
    token,
  );
}

async function leftBorderColor(page: Page, name: string) {
  return cardByName(page, name).evaluate(
    (el) => getComputedStyle(el).borderLeftColor,
  );
}

function fullState(): DiscoveryState {
  return {
    proposals: [
      localInverterProposal(),
      cloudInverterProposal(),
      localGridMeterProposal(),
      unknownProposal(),
    ],
    priority: ["zendure_mqtt", "local_api", "local_mqtt"],
  };
}

test("Guided Setup: MQTT proposals take their card colour from the hardware role", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page, fullState());
  await loadProposalsPanel(page);

  const cards = page.locator("#mqtt-proposals-list .mqtt-proposal-card");
  await expect(cards).toHaveCount(4);
  // Every proposal sits in the shared hardware-card shell.
  await expect(
    page.locator("#mqtt-proposals-list .mqtt-proposal-card.hardware-card"),
  ).toHaveCount(4);

  const localInverter = cardByName(page, "Local MQTT Inverter");
  const cloudInverter = cardByName(page, "Zendure Cloud Inverter");
  const gridMeter = cardByName(page, "Local MQTT Smart Meter");
  const unknown = cardByName(page, "Unclassified MQTT Device");

  // Role decides the colour; the transport does not.
  await expect(localInverter).toHaveClass(/hardware-card-inverter/);
  await expect(cloudInverter).toHaveClass(/hardware-card-inverter/);
  await expect(gridMeter).toHaveClass(/hardware-card-grid-meter/);
  await expect(unknown).not.toHaveClass(/hardware-card-inverter/);
  await expect(unknown).not.toHaveClass(/hardware-card-grid-meter/);

  // Transport stays a label inside the card and keeps naming the real source.
  await expect(transportValue(page, "Local MQTT Inverter")).toHaveText("MQTT");
  await expect(transportValue(page, "Zendure Cloud Inverter")).toHaveText(
    "Zendure MQTT",
  );
  await expect(transportValue(page, "Local MQTT Smart Meter")).toHaveText(
    "Local MQTT",
  );
  await expect(transportValue(page, "Unclassified MQTT Device")).toHaveText(
    "Zendure MQTT",
  );
  await expect(gridMeter).toContainText("Grid meter");
  await expect(unknown).toContainText("Telemetry only");
});

test("Guided Setup: MQTT role colours resolve from the shared --output/--grid tokens", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page, fullState());
  await loadProposalsPanel(page);

  const output = await roleAccent(page, "--output");
  const grid = await roleAccent(page, "--grid");
  expect(output).not.toEqual("");
  expect(grid).not.toEqual(output);

  // Both inverters resolve to the same --output accent regardless of transport.
  const localBorder = await leftBorderColor(page, "Local MQTT Inverter");
  const cloudBorder = await leftBorderColor(page, "Zendure Cloud Inverter");
  expect(localBorder).toEqual(cloudBorder);

  const gridBorder = await leftBorderColor(page, "Local MQTT Smart Meter");
  const neutralBorder = await leftBorderColor(page, "Unclassified MQTT Device");
  expect(gridBorder).not.toEqual(localBorder);
  expect(neutralBorder).not.toEqual(localBorder);
  expect(neutralBorder).not.toEqual(gridBorder);

  // A Local API inverter candidate uses the very same accent as the MQTT ones.
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
  expect(gridBorder).toEqual(probe.gridMeter);
});

test("Guided Setup: role styling leaves proposal selection and markup intact", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page, fullState());
  await loadProposalsPanel(page);

  const localInverter = cardByName(page, "Local MQTT Inverter");
  const gridMeter = cardByName(page, "Local MQTT Smart Meter");

  // The device proposal is still selectable and reports its selected state.
  await localInverter.locator(".mqtt-proposal-add").click();
  await expect(localInverter.locator(".mqtt-proposal-add")).toHaveText(
    "Added to preview",
  );
  await expect(localInverter).toContainText("Output control enabled");
  await expect(localInverter).toHaveClass(/hardware-card-inverter/);

  // The D0 grid meter keeps its own read-only mapping action, including the
  // confirmation that replaces the already auto-selected Shelly grid meter.
  page.once("dialog", (dialog) => dialog.accept());
  await gridMeter.locator(".mqtt-proposal-add").click();
  await expect(gridMeter.locator(".mqtt-proposal-add")).toHaveText(
    "Selected as grid meter",
  );
  await expect(gridMeter).toContainText("Grid meter — read only");
  await expect(gridMeter).toHaveClass(/hardware-card-grid-meter/);

  // The collapsed config-fragment preview still opens.
  const fragment = localInverter.locator(".proposal-fragment");
  await fragment.locator("> summary").click();
  await expect(fragment.locator("pre")).toContainText("zendure_mqtt");

  const duplicateIds = await page.evaluate(() => {
    const seen = new Map<string, number>();
    document.querySelectorAll("[id]").forEach((el) => {
      seen.set(el.id, (seen.get(el.id) || 0) + 1);
    });
    return [...seen.entries()].filter(([, count]) => count > 1).map(([id]) => id);
  });
  expect(duplicateIds).toEqual([]);
});
