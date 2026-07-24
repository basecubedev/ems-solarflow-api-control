import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

const SECRET_VALUES = [
  "e2e-local-broker-secret",
  "e2e-cloud-broker-secret",
];

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

test("mixed API, Local MQTT and Cloud MQTT identities survive save and reload", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const apiCard = page.locator('[data-source-id="maintenance-inverter-0"]');
  const localCard = page.locator('[data-source-id="maintenance-mqtt-device-1"]');
  const cloudCard = page.locator('[data-source-id="maintenance-mqtt-device-2"]');
  await expect(apiCard).toContainText("Local API inverter");
  await expect(localCard).toContainText("Local MQTT inverter");
  await expect(cloudCard).toContainText("Cloud MQTT inverter");
  await expect(localCard).toContainText("Hyper 2000");
  await expect(cloudCard).toContainText("Hyper 2000");

  const before = await readDraft(page);
  const beforeMqtt = before.devices.filter(
    (device: { kind?: string }) => device.kind === "zendure_mqtt",
  );
  expect(beforeMqtt.map((device: any) => device.mqtt.broker_ref)).toEqual([
    "local_mixed",
    "cloud_mixed",
  ]);
  expect(beforeMqtt.map((device: any) => device.hardware_model)).toEqual([
    "hyper_2000",
    "hyper_2000",
  ]);

  await apiCard.locator(".hardware-card-summary").click();
  const name = apiCard
    .locator("label")
    .filter({ has: page.locator(".feature-field-label", { hasText: "Name" }) })
    .locator('input[type="text"]')
    .first();
  await name.fill("Local API inverter edited");
  await page.locator("#maintenance-config-preview-btn").click();
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-apply-panel")).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#maintenance-config-apply-btn").click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/i,
  );

  const after = await readDraft(page);
  expect(after.devices[0].name).toBe("Local API inverter edited");
  expect(after.devices.slice(1)).toEqual(before.devices.slice(1));
  expect(after.devices).toHaveLength(3);
  const serialized = JSON.stringify(after);
  for (const secret of SECRET_VALUES) expect(serialized).not.toContain(secret);
  for (const secret of SECRET_VALUES) {
    await expect(page.locator("body")).not.toContainText(secret);
  }

  await page.reload();
  await openMaintenanceConfig(page);
  await expect(
    page.locator('[data-source-id="maintenance-inverter-0"]'),
  ).toContainText("Local API inverter edited");
  await expect(
    page.locator('[data-source-id="maintenance-mqtt-device-1"]'),
  ).toContainText("Local MQTT inverter");
  await expect(
    page.locator('[data-source-id="maintenance-mqtt-device-2"]'),
  ).toContainText("Cloud MQTT inverter");
});

test("Maintenance additions use one compact sequence without renaming existing devices", async ({
  page,
  seedAdminScenario,
}) => {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceConfig(page);

  const before = await readDraft(page);
  const existingNames = before.devices.map((device: { name: string }) => device.name);

  await page.locator("#maintenance-manual > summary").click();
  await page.locator("#maintenance-config-add-inverter").click();
  await page.locator("#maintenance-config-add-mqtt-device").click();

  const local = page.locator('[data-source-id="maintenance-inverter-3"]');
  const mqtt = page.locator('[data-source-id="maintenance-mqtt-device-4"]');
  await expect(local).toContainText("INV_4");
  await expect(mqtt).toContainText("INV_5");

  for (let index = 0; index < existingNames.length; index += 1) {
    await expect(
      page.locator(`[data-source-id="${index === 0 ? "maintenance-inverter" : "maintenance-mqtt-device"}-${index}"]`),
    ).toContainText(existingNames[index]);
  }
});
