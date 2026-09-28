import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { MaintenancePage } from "./pages/maintenance-page";

// Switching the grid meter to an E3/DC in Maintenance shows the Modbus fields
// the catalog declares for it, and the real preview/apply writes the block the
// EMS factory reads. Nothing about the type is decided in the browser: the
// field list and the port hint come from the catalog variant.

const E3DC_HOST = "192.168.50.60";

function gridMeterCard(page: Page) {
  return page.locator("#maintenance-config-gridmeter .hardware-card").first();
}

async function openGridMeter(page: Page) {
  const card = gridMeterCard(page);
  const typeSelect = card.getByLabel(/^Meter type/);
  if (!(await typeSelect.isVisible())) {
    await card.locator(".hardware-card-summary").click();
  }
  await expect(typeSelect).toBeVisible();
  return card;
}

async function previewConfig(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  const raw = page.locator("#maintenance-config-raw-pre");
  await expect(raw).not.toHaveText("");
  return JSON.parse((await raw.textContent()) ?? "{}");
}

test("Maintenance: a grid meter switched to an E3/DC is written with its Modbus settings", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await new MaintenancePage(page).openEditor({ discovery: false });

  const card = await openGridMeter(page);
  await card.getByLabel(/^Meter type/).selectOption("e3dc_modbus");
  await expect(card).toContainText("E3/DC storage system via Modbus TCP");
  await expect(card).toContainText("Modbus TCP port. The E3/DC default is 502.");
  await expect(card.getByLabel(/^Port/)).toHaveValue("502");

  await card.getByLabel(/^Meter IP address/).fill(E3DC_HOST);
  await card.locator("summary", { hasText: "Advanced" }).click();
  await card.getByLabel(/^Modbus unit ID/).fill("2");

  const preview = await previewConfig(page);
  expect(preview.grid_meter).toEqual({
    type: "e3dc_modbus",
    ip: E3DC_HOST,
    port: 502,
    unit_id: 2,
  });

  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );

  await page.reload();
  await new MaintenancePage(page).openEditor({ discovery: false });
  await expect(gridMeterCard(page)).toContainText(`${E3DC_HOST}:502`);
  const persisted = await previewConfig(page);
  expect(persisted.grid_meter.unit_id).toBe(2);
});

test("Maintenance: an E3/DC unit ID out of range is refused before apply", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await new MaintenancePage(page).openEditor({ discovery: false });

  const card = await openGridMeter(page);
  await card.getByLabel(/^Meter type/).selectOption("e3dc_modbus");
  await card.getByLabel(/^Meter IP address/).fill(E3DC_HOST);
  await card.locator("summary", { hasText: "Advanced" }).click();
  await card.getByLabel(/^Modbus unit ID/).fill("300");

  await page.locator("#maintenance-config-preview-btn").click();
  await expect(page.locator("#maintenance-config-editor")).toContainText(
    "grid_meter.unit_id must be in 0..255",
  );
  await expect(page.locator("#maintenance-config-apply-btn")).toBeHidden();
});

test("Maintenance: an E3/DC added as a read-only device is written without Zendure values", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  const maintenance = new MaintenancePage(page);
  await maintenance.openEditor();

  await page.locator("#maintenance-manual > summary").click();
  await page.locator("#maintenance-config-add-e3dc-device").click();
  const card = page.locator("#maintenance-config-inverters .hardware-card").filter({
    hasText: "E3/DC storage system",
  });
  await expect(card).toHaveCount(1);
  await expect(card.locator(".connection-pill")).toHaveText("Modbus TCP");
  await card.getByLabel(/^Host \/ IP/).fill(E3DC_HOST);

  const preview = await previewConfig(page);
  const added = preview.devices.find((device: any) => device.type === "e3dc_modbus");
  expect(added).toMatchObject({
    name: "E3DC",
    type: "e3dc_modbus",
    ip: E3DC_HOST,
    port: 502,
    unit_id: 1,
    enabled: true,
  });
  for (const zendureKey of ["sn", "max_power", "min_soc", "smart_mode", "pv_kwp"]) {
    expect(added).not.toHaveProperty(zendureKey);
  }

  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );
  await page.reload();
  await maintenance.openEditor({ discovery: false });
  await expect(maintenance.configSummary).toContainText("3 inverters");
  await expect(maintenance.configSummary).toContainText("1 read-only device");
});

test("Maintenance: switching to the E3/DC and back restores the meter's own port", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await new MaintenancePage(page).openEditor({ discovery: false });

  const card = await openGridMeter(page);
  await card.getByLabel(/^Port/).fill("8080");
  await card.getByLabel(/^Meter type/).selectOption("e3dc_modbus");
  await expect(card.getByLabel(/^Port/)).toHaveValue("502");
  await card.getByLabel(/^Meter type/).selectOption("shelly");
  await expect(card.getByLabel(/^Port/)).toHaveValue("8080");
});
