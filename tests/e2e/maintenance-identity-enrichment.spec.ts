import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Route-to-serial identity enrichment: a serial-less Cloud MQTT device already
// in the config must be recognized as the SAME inverter when the identical Cloud
// route is re-discovered carrying a physical serial. The scoped-route alias
// token survives serial enrichment, so the proposal shows "In config" and offers
// no second "Add" — one logical inverter, never a duplicate. Discovery is seeded
// by the real test-mode backend; the browser-safe proposals never leak the raw
// Cloud route, product key or topic.

const CLOUD_ROUTE = "E2E_CLOUD_ROUTE_7501";
const CLOUD_PRODUCT = "E2E_CLOUD_PRODUCT_75";
const CLOUD_TOPIC = `iot/${CLOUD_PRODUCT}/${CLOUD_ROUTE}/properties/report`;
const ENRICHMENT_SERIAL = "E2E-CLOUD-SERIAL-7501";

type DiscoveryState = { apiDevices: unknown[]; proposals: unknown[] };

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockDiscovery(page: Page, state: DiscoveryState) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices**", (route) =>
    json(route, { devices: state.apiDevices, ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/refresh**", (route) =>
    json(route, { state: "enabled" }),
  );
  await page.route("**/api/discovery/networks**", (route) =>
    json(route, { networks: [] }),
  );
  await page.route("**/api/discovery/mqtt-brokers/refresh**", (route) =>
    json(route, { ok: true }),
  );
  await page.route("**/api/discovery/mqtt-proposals**", (route) =>
    json(route, { proposals: state.proposals }),
  );
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings**", (route) =>
    json(route, { token_saved: false, tls_mode: "system_ca" }),
  );
  await page.route("**/api/discovery/scan**", (route) =>
    json(route, { scan_id: "e2e-scan" }),
  );
  await page.route("**/api/discovery/result/**", (route) =>
    json(route, { status: "complete", devices: [] }),
  );
}

async function openMaintenanceEditor(page: Page) {
  await expect(page.locator("#view-start")).toBeVisible();
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="manual"]').click();
  const toggle = page.locator(
    '[data-maintenance-toggle="maintenance-config-card"]',
  );
  const editor = page.locator("#maintenance-config-editor");
  await expect(toggle).toContainText(/inverter/);
  await expect(async () => {
    if (!(await editor.isVisible())) await toggle.click();
    await expect(editor).toBeVisible({ timeout: 1_000 });
  }).toPass();
  const sources = page.locator("#maintenance-discovery-sources");
  await expect(async () => {
    if (!(await sources.isVisible())) {
      await page.locator("#maintenance-add-devices > summary").click();
    }
    await expect(sources).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

async function runDiscovery(page: Page) {
  await page.locator("#maintenance-discovery-start").click();
  await expect(page.locator("#maintenance-discovery-status")).toContainText(
    /Discovery completed/,
  );
}

function configuredCards(page: Page) {
  return page.locator("#maintenance-config-inverters .hardware-card");
}

function cardByText(page: Page, text: string) {
  return configuredCards(page).filter({ hasText: text }).first();
}

async function openCard(page: Page, card: ReturnType<typeof cardByText>) {
  await expect(async () => {
    const body = card.locator(".hardware-card-body");
    if (!(await body.isVisible())) {
      await card.locator(".hardware-card-toggle").click();
    }
    await expect(body).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

function fieldInput(card: ReturnType<typeof cardByText>, label: string) {
  return card
    .locator("label.feature-field-row", { hasText: label })
    .locator("input, select")
    .first();
}

async function previewAndApply(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );
}

async function login(page: Page) {
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
}

async function loadRealMqttProposals(page: Page, state: DiscoveryState) {
  const response = await page.request.get("/api/discovery/mqtt-proposals");
  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as { proposals: unknown[] };
  const flattened = JSON.stringify(payload);
  // The browser-safe proposal never leaks the raw Cloud route/product/topic.
  expect(flattened).not.toContain(CLOUD_ROUTE);
  expect(flattened).not.toContain(CLOUD_PRODUCT);
  expect(flattened).not.toContain(CLOUD_TOPIC);
  state.proposals = payload.proposals;
}

test("route-to-serial: a serial-bearing rediscovery of a serial-less Cloud route is the same inverter", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = { apiDevices: [], proposals: [] };
  await mockDiscovery(page, state);
  await login(page);
  // Config already holds a serial-less Cloud device for CLOUD_ROUTE; discovery
  // now reports the identical route carrying a physical serial.
  await seedAdminScenario("serialless_cloud_route_enrichment");
  await loadRealMqttProposals(page, state);

  await page.reload();
  await openMaintenanceEditor(page);
  // One API inverter + the serial-less Cloud device.
  await expect(configuredCards(page)).toHaveCount(2);
  await expect(cardByText(page, "Roof Serial-less")).toHaveCount(1);
  await expect(page.locator("body")).not.toContainText(CLOUD_ROUTE);
  await expect(page.locator("body")).not.toContainText(CLOUD_TOPIC);

  // The serial-bearing rediscovery of the same route is recognized as the
  // existing inverter: it shows "In config" and offers no second "Add".
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  await expect(
    results.locator(".mconfig-discovery-add-button.is-add"),
  ).toHaveCount(0);
  await expect(
    results.locator(".mconfig-discovery-add-button.is-in-config"),
  ).toHaveCount(1);
  // The rediscovery's physical serial is never rendered as a duplicate device.
  await expect(page.locator("body")).not.toContainText(CLOUD_ROUTE);

  // Rename the existing inverter and apply: exactly one logical inverter
  // survives with its name preserved and no raw Cloud route leaked.
  const existing = cardByText(page, "Roof Serial-less");
  await openCard(page, existing);
  await fieldInput(existing, "Device name").fill("Roof Enriched");
  await previewAndApply(page);

  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(2);
  await expect(cardByText(page, "Roof Enriched")).toHaveCount(1);
  await expect(page.locator("body")).not.toContainText(CLOUD_ROUTE);
  await expect(page.locator("body")).not.toContainText(CLOUD_TOPIC);
});
