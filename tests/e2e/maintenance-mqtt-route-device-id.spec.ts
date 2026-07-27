import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Maintenance keeps the physical serial and the MQTT route/payload device id as
// two independent editable fields. Editing one never changes the other, and the
// route id is written only to mqtt.device_id (never a legacy top-level device_id
// nor the serial). See admin/static/admin.js renderMaintenanceZendureMqttDevice.

async function openMaintenanceConfig(page: Page) {
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="manual"]').click();
  const toggle = page.locator(
    '[data-maintenance-toggle="maintenance-config-card"]',
  );
  const editor = page.locator("#maintenance-config-editor");
  await expect(toggle).toContainText("inverters");
  await expect(async () => {
    if (!(await editor.isVisible())) await toggle.click();
    await expect(editor).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

async function readDraft(page: Page) {
  const response = await page.request.get("/api/admin/maintenance/config");
  expect(response.ok()).toBeTruthy();
  return (await response.json()).draft;
}

function cardInput(page: Page, card: ReturnType<Page["locator"]>, label: string) {
  return card
    .locator("label")
    .filter({ has: page.locator(".feature-field-label", { hasText: label }) })
    .locator('input[type="text"]')
    .first();
}

function cardReadiness(page: Page, card: ReturnType<Page["locator"]>) {
  return card
    .locator("label")
    .filter({
      has: page.locator(".feature-field-label", {
        hasText: "Current control readiness",
      }),
    })
    .locator(".feature-readonly-value")
    .first();
}

async function reopenCard(page: Page, sourceId: string) {
  await page.reload();
  await openMaintenanceConfig(page);
  const card = page.locator(`[data-source-id="${sourceId}"]`);
  await card.locator(".hardware-card-summary").click();
  return card;
}

async function previewAndApply(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-apply-panel")).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#maintenance-config-apply-btn").click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/i,
  );
}

test("Maintenance renders exactly one MQTT device ID field per inverter", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  await expect(card.getByText("MQTT device ID", { exact: true })).toHaveCount(1);
  await expect(cardInput(page, card, "Serial number")).toHaveValue("LOCAL-MQTT-SERIAL");
  await expect(cardInput(page, card, "MQTT device ID")).toHaveValue("LOCAL-MQTT-ID");
});

test("Maintenance MQTT device ID edit changes only the route id", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  await cardInput(page, card, "MQTT device ID").fill("LOCAL-MQTT-ID-EDITED");
  await previewAndApply(page);

  const after = await readDraft(page);
  const mqtt = after.devices.filter((d: { kind?: string }) => d.kind === "zendure_mqtt");
  expect(mqtt[0].mqtt.device_id).toBe("LOCAL-MQTT-ID-EDITED");
  expect(mqtt[0].serial_number).toBe("LOCAL-MQTT-SERIAL");
  // The draft exposes one route identity: its display device_id is the route id,
  // never a second editable value. That the stored config gains no legacy
  // top-level device_id is pinned by tests/test_admin_zendure_mqtt_config_draft.py.
  expect(mqtt[0].device_id).toBe(mqtt[0].mqtt.device_id);
});

test("Maintenance serial edit does not change the MQTT device ID field", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  const serial = cardInput(page, card, "Serial number");
  const routeId = cardInput(page, card, "MQTT device ID");
  // Editing the physical serial never touches the MQTT route id: two inputs,
  // one edit affects one field.
  await serial.fill("LOCAL-MQTT-SERIAL-EDITED");
  await expect(routeId).toHaveValue("LOCAL-MQTT-ID");
});

test("Maintenance MQTT device ID edit does not change the serial field", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  const serial = cardInput(page, card, "Serial number");
  const routeId = cardInput(page, card, "MQTT device ID");
  await routeId.fill("LOCAL-MQTT-ID-EDITED-2");
  await expect(serial).toHaveValue("LOCAL-MQTT-SERIAL");
});

test("Maintenance clearing the MQTT device ID removes the stored route id", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  await cardInput(page, card, "MQTT device ID").fill("");
  await expect(cardReadiness(page, card)).toHaveText("MQTT device ID is missing");
  await previewAndApply(page);

  const after = await readDraft(page);
  const mqtt = after.devices.filter((d: { kind?: string }) => d.kind === "zendure_mqtt");
  expect(mqtt[0].mqtt.device_id).toBe("");
  expect(mqtt[0].control_readiness.ready).toBe(false);
  // Clearing the route id never touches the independent physical serial.
  expect(mqtt[0].serial_number).toBe("LOCAL-MQTT-SERIAL");

  const reloaded = await reopenCard(page, "maintenance-mqtt-device-1");
  await expect(cardInput(page, reloaded, "MQTT device ID")).toHaveValue("");
  await expect(cardInput(page, reloaded, "Serial number")).toHaveValue(
    "LOCAL-MQTT-SERIAL",
  );
  await expect(cardReadiness(page, reloaded)).toHaveText("MQTT device ID is missing");
});

test("Maintenance clearing the MQTT device ID cannot leave writes enabled", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  const outputControl = card
    .locator("label")
    .filter({
      has: page.locator(".feature-field-label", { hasText: "Output control" }),
    })
    .locator('input[type="checkbox"]')
    .first();
  await expect(outputControl).toBeChecked();
  await cardInput(page, card, "MQTT device ID").fill("");
  await expect(outputControl).not.toBeChecked();
  await previewAndApply(page);

  const after = await readDraft(page);
  const mqtt = after.devices.filter((d: { kind?: string }) => d.kind === "zendure_mqtt");
  expect(mqtt[0].capabilities.write_output_limit).toBe(false);
});

test("Maintenance clearing the physical serial is not silently restored", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const card = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  await card.locator(".hardware-card-summary").click();
  await cardInput(page, card, "Serial number").fill("");
  await page.locator("#maintenance-config-preview-btn").click();

  const validation = page.locator("#maintenance-config-validation");
  await expect(validation).not.toBeEmpty();
  if ((await validation.textContent())?.trim() !== "valid") {
    // The selected contract requires the serial: an actionable error, never a
    // silent restore of the value the operator cleared.
    return;
  }
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#maintenance-config-apply-btn").click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/i,
  );

  const after = await readDraft(page);
  const mqtt = after.devices.filter((d: { kind?: string }) => d.kind === "zendure_mqtt");
  expect(mqtt[0].serial_number).toBe("");
  // The independent MQTT route identity survives a serial clear.
  expect(mqtt[0].mqtt.device_id).toBe("LOCAL-MQTT-ID");

  const reloaded = await reopenCard(page, "maintenance-mqtt-device-1");
  await expect(cardInput(page, reloaded, "Serial number")).toHaveValue("");
  await expect(cardInput(page, reloaded, "MQTT device ID")).toHaveValue("LOCAL-MQTT-ID");
});
