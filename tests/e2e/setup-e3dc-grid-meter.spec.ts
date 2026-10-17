import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { E3DC_SYSTEM, SHELLY_METER, apiInverter } from "./helpers/discovery-state";

async function stubNetworkDiscovery(page: Page) {
  const json = (route: Route, body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/discovery/networks", (route) => json(route, { networks: [] }));
  await page.route("**/discovery/gateway-probe", (route) => json(route, { candidates: [] }));
  await page.route("**/discovery/scan**", (route) => json(route, { scan_id: "e2e-scan" }));
  await page.route("**/discovery/result/**", (route) =>
    json(route, { status: "complete", devices: [] }),
  );
}

// A discovered Shelly switched to an E3/DC in Setup does not keep its HTTP
// port: the port belongs to the protocol, so the card offers the E3/DC's
// Modbus port and the real preview writes a meter the EMS factory accepts.

test("Setup: a discovered meter switched to an E3/DC gets the Modbus port", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(120_000);
  await stubNetworkDiscovery(page);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedDiscoveryState({
    local_api_devices: [apiInverter("E3DCSETUP01", "192.168.100.78"), SHELLY_METER],
  });
  await page.reload();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
  await page.locator('[data-setup-step="config"]').click();

  const selection = page.locator("#config-grid-meter-selection");
  await expect(selection).toContainText("192.168.100.93:80");
  const variant = selection.locator("[data-feature-variant-select]").first();
  await expect(async () => {
    if (!(await variant.isVisible())) {
      await selection.getByRole("button", { name: "Expand Grid meter" }).click();
    }
    await expect(variant).toBeVisible({ timeout: 1_000 });
  }).toPass();

  const previewResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/setup/config-preview" &&
      (response.request().postDataJSON()?.features ?? {})["grid_meter.type"] === "e3dc_modbus",
  );
  await variant.selectOption("e3dc_modbus");

  await expect(selection.locator("#grid-meter-port")).toHaveValue("502");
  await expect(selection).toContainText("Modbus TCP port. The E3/DC default is 502.");

  const response = await previewResponse;
  const meter = (response.request().postDataJSON().devices ?? []).find(
    (item: { role?: string }) => item.role === "grid_meter",
  );
  expect(meter.port).toBe(502);
  expect(meter.grid_meter_type).toBe("e3dc_modbus");
  const preview = await response.json();
  expect(preview.config.grid_meter).toMatchObject({
    type: "e3dc_modbus",
    ip: "192.168.100.93",
    port: 502,
  });
});

// A scanned E3/DC is the only meter found, so Setup takes it as the grid meter
// on its Modbus endpoint, and the real preview shows it as a read-only device
// as well.

test("Setup: a scanned E3/DC becomes the grid meter and a read-only device", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(120_000);
  await stubNetworkDiscovery(page);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedDiscoveryState({
    local_api_devices: [apiInverter("E3DCSETUP02", "192.168.100.78"), E3DC_SYSTEM],
  });
  await page.reload();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();

  await page.locator('[data-setup-step="config"]').click();

  const selection = page.locator("#config-grid-meter-selection");
  await expect(selection).toContainText("192.168.100.94:502");
  await expect(selection).toContainText("E3/DC storage system via Modbus TCP");

  const rendered = page.locator("#config-preview");
  let preview: { grid_meter?: { type?: string }; devices?: { type?: string }[] } = {};
  await expect(async () => {
    preview = JSON.parse((await rendered.textContent()) || "{}");
    expect(preview.grid_meter?.type).toBe("e3dc_modbus");
  }).toPass();
  expect(preview.grid_meter).toMatchObject({
    type: "e3dc_modbus",
    ip: "192.168.100.94",
    port: 502,
  });
  const readOnly = (preview.devices ?? []).filter(
    (device: { type?: string }) => device.type === "e3dc_modbus",
  );
  expect(readOnly).toHaveLength(1);
  expect(readOnly[0]).toMatchObject({ name: "E3DC", ip: "192.168.100.94", port: 502 });
});
