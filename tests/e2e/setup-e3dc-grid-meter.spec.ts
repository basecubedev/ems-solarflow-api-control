import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { SHELLY_METER, apiInverter } from "./helpers/discovery-state";

// A discovered Shelly switched to an E3/DC in Setup does not keep its HTTP
// port: the port belongs to the protocol, so the card offers the E3/DC's
// Modbus port and the real preview writes a meter the EMS factory accepts.

test("Setup: a discovered meter switched to an E3/DC gets the Modbus port", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(120_000);
  await page.route("**/api/discovery/networks", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"networks":[]}' }),
  );
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
  await selection.getByRole("button", { name: "Expand Grid meter" }).click();
  const variant = selection.locator("[data-feature-variant-select]").first();
  await expect(variant).toBeVisible();

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
