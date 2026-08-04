import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Manual Fresh Setup entry exposes the physical serial and the MQTT route/payload
// device id as two independent inputs. The physical serial is never used as the
// MQTT route id: a telemetry-only device needs only a serial, while enabling
// output control requires the explicit MQTT device ID. Discovery is mocked empty
// so only the manual form under test drives the config draft.
// See admin/static/admin.js addManualMqttDevice.

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockDiscovery(page: Page) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices", (route) =>
    json(route, { devices: [], ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/status", (route) =>
    json(route, { state: "enabled", message: "", devices_found: 0 }),
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
    json(route, {
      discovery_priority: ["local_api", "local_mqtt", "zendure_mqtt"],
      sources: {
        local_api: { enabled: true },
        local_mqtt: { enabled: true },
        zendure_mqtt: { enabled: true },
      },
    }),
  );
}

async function reachDevices(page: Page) {
  await mockDiscovery(page);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
}

async function openManualMqttForm(page: Page) {
  // The manual "Add a device manually" form lives on the Config step (03).
  await page.locator('[data-setup-step="config"]').click();
  for (const id of ["#config-available-details", "#config-manual"]) {
    const details = page.locator(id);
    if (!(await details.getAttribute("open"))) {
      await details.locator("> summary").click();
    }
  }
  await expect(page.locator("#config-mqtt-device-serial")).toBeVisible();
}

test("Fresh Setup manual entry has separate physical serial and MQTT device ID fields", { tag: ["@setup"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page);
  await openManualMqttForm(page);

  const serial = page.locator("#config-mqtt-device-serial");
  const mqttId = page.locator("#config-mqtt-device-mqttid");
  await expect(serial).toBeVisible();
  await expect(mqttId).toBeVisible();

  // A telemetry-only entry needs only the physical serial, no MQTT device ID.
  await page.locator("#config-mqtt-device-generation").selectOption("solarflow_zensdk");
  await serial.fill("PHYSICAL-SERIAL");
  await page.locator("#config-mqtt-device-add").click();

  const list = page.locator("#config-mqtt-device-list");
  await expect(list).toBeVisible();
  await expect(list.locator(".config-mqtt-device-row")).toHaveCount(1);
  await expect(page.locator("#config-mqtt-device-error")).toBeHidden();
});

test("Fresh Setup manual output control follows the write route, and says so", { tag: ["@setup"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page);
  await openManualMqttForm(page);

  // A supported, controllable model on the legacy transport.
  await page.locator("#config-mqtt-device-generation").selectOption("hub_hyper_legacy");
  await page.locator("#config-mqtt-device-model").selectOption("hyper_2000");
  await page.locator("#config-mqtt-device-serial").fill("PHYSICAL-SERIAL");

  // Output control is a capability, not a checkbox: the form names the missing
  // part of the write route instead of claiming control. The serial is never
  // the route id.
  await expect(page.locator("#config-mqtt-device-control")).toHaveCount(0);
  const controlHelp = page.locator("#config-mqtt-device-control-help");
  await expect(controlHelp).toBeVisible();
  await expect(controlHelp).toContainText("Output control: Not ready");
  await expect(controlHelp).toContainText("product key");
  await expect(controlHelp).toContainText("telemetry source");

  await page.locator("#config-mqtt-device-productkey").fill("PRODUCT-KEY");
  await expect(controlHelp).toContainText("MQTT device ID");

  await page.locator("#config-mqtt-device-mqttid").fill("ROUTE-DEV-ID");
  await expect(controlHelp).toContainText("Output control: Available");

  await page.locator("#config-mqtt-device-add").click();
  await expect(page.locator("#config-mqtt-device-error")).toBeHidden();
  const list = page.locator("#config-mqtt-device-list");
  await expect(list.locator(".config-mqtt-device-row")).toHaveCount(1);
});

test("Fresh Setup manual entry names the broker source for a ZenSDK model", { tag: ["@setup"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page);
  await openManualMqttForm(page);

  // The manual broker form provisions a *local* broker. A ZenSDK generation
  // there publishes scalar telemetry, and no hardware evidence exists that such
  // a broker relays a command back to the device — so the form must say exactly
  // that rather than offering control or blaming the model. The product key
  // field stays visible: the model is control-capable, the carrier is not.
  await page
    .locator("#config-mqtt-device-generation")
    .selectOption("solarflow_zensdk");
  await page
    .locator("#config-mqtt-device-model")
    .selectOption("solarflow_800_pro_2");
  await page.locator("#config-mqtt-device-serial").fill("PHYSICAL-SERIAL");
  await page.locator("#config-mqtt-device-productkey").fill("PRODUCT-KEY");
  await page.locator("#config-mqtt-device-mqttid").fill("ROUTE-DEV-ID");

  const controlHelp = page.locator("#config-mqtt-device-control-help");
  await expect(controlHelp).toContainText("Output control: Not available");
  await expect(controlHelp).toContainText("MQTT broker source");
  // The blocked axis is named precisely: not the model, not the identifiers.
  await expect(controlHelp).not.toContainText("hardware model");
  await expect(controlHelp).not.toContainText("Not ready");

  await page.locator("#config-mqtt-device-add").click();
  await expect(page.locator("#config-mqtt-device-error")).toBeHidden();
  await expect(
    page.locator("#config-mqtt-device-list .config-mqtt-device-row"),
  ).toHaveCount(1);
});

test("Fresh Setup manual entry stays telemetry-only for an unresolved model", { tag: ["@setup"] }, async ({
  page,
}) => {
  test.setTimeout(90_000);
  await reachDevices(page);
  await openManualMqttForm(page);

  await page
    .locator("#config-mqtt-device-generation")
    .selectOption("solarflow_zensdk");
  await page.locator("#config-mqtt-device-serial").fill("PHYSICAL-SERIAL");

  // No exact model resolved: the verdict is unknown, never optimistically on.
  const controlHelp = page.locator("#config-mqtt-device-control-help");
  await expect(controlHelp).toContainText("Output control: Unknown");
  await expect(controlHelp).toContainText("hardware model");
});
