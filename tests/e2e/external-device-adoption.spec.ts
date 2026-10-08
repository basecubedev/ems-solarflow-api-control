import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { MaintenancePage } from "./pages/maintenance-page";

// A Kostal Piko whose output FHEM republishes as KostalPiko/<serial>/solarPower
// is a device the external hardware catalog lists. The deterministic Admin's own
// discovery store carries the observation, so the proposal, its trust check and
// the applied entry all come from the real backend; only what would reach the
// network is answered here.

const SERIAL = "E2EKOSTAL0001";
const BROKER_HOST = "192.168.100.71";

function kostalBroker() {
  return {
    host: BROKER_HOST,
    port: 1883,
    devices: [
      {
        broker_id: `mqtt:${BROKER_HOST}:1883`,
        broker_host: BROKER_HOST,
        broker_port: 1883,
        source_type: "local_mqtt",
        topic_family: "kostal_piko",
        device_id: SERIAL,
        serial_number: SERIAL,
        display_name: "Kostal Piko",
        metrics_seen: ["outputHomePower"],
        topics_seen: [`KostalPiko/${SERIAL}/solarPower`],
      },
    ],
  };
}

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

// Everything discovery would otherwise fetch from the network is answered here;
// the proposal list alone goes to the backend, whose store holds the seed.
async function mockNetwork(page: Page) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  await page.route("**/api/discovery/devices**", (route) =>
    json(route, { devices: [], ignored_devices: [] }),
  );
  await page.route("**/api/discovery/networks**", (route) => json(route, { networks: [] }));
  await page.route("**/api/discovery/mqtt-brokers/refresh**", (route) =>
    json(route, { ok: true }),
  );
  await page.route("**/api/discovery/mdns/refresh**", (route) =>
    json(route, { state: "enabled" }),
  );
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings**", (route) =>
    json(route, { token_saved: false, tls_mode: "system_ca" }),
  );
  await page.route("**/api/discovery/scan**", (route) => json(route, { scan_id: "e2e-scan" }));
  await page.route("**/api/discovery/result/**", (route) =>
    json(route, { status: "complete", devices: [] }),
  );
  await page.route("**/api/discovery/mqtt-proposals**", (route) => route.continue());
}

async function openMaintenance(page: Page) {
  await page.reload();
  const maintenance = new MaintenancePage(page);
  await maintenance.openEditor();
  return maintenance;
}

async function runDiscovery(page: Page) {
  await page.locator("#maintenance-discovery-start").click();
  await expect(page.locator("#maintenance-discovery-status")).toContainText(
    /Discovery completed/,
  );
}

function kostalProposal(page: Page) {
  return page
    .locator("#maintenance-discovery-results .mconfig-discovery-proposal-card")
    .filter({ hasText: "Kostal Piko" })
    .first();
}

function kostalCard(page: Page) {
  return page
    .locator("#maintenance-config-inverters .hardware-card")
    .filter({ hasText: "Kostal Piko · read over MQTT" })
    .first();
}

async function applyDraft(page: Page) {
  await page.locator("#maintenance-config-preview-btn").click();
  const applyBtn = page.locator("#maintenance-config-apply-btn");
  await expect(applyBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await applyBtn.click();
  await expect(page.locator("#maintenance-config-apply-status")).toContainText(
    /Config updated at/,
  );
}

test("Maintenance: a discovered catalog device is added, renamed and removed like any device", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
  seedDiscoveryState,
}) => {
  test.setTimeout(120_000);
  await mockNetwork(page);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await seedDiscoveryState({ local_mqtt_brokers: [kostalBroker()] });
  await openMaintenance(page);
  await runDiscovery(page);

  const proposal = kostalProposal(page);
  await expect(proposal).toHaveClass(/hardware-card-inverter/);
  await expect(proposal).toContainText(SERIAL);
  await expect(proposal).toContainText("takes no commands");
  await proposal.getByRole("button", { name: "Add inverter" }).click();

  // An accidental add is taken back on its own, before anything is applied.
  await kostalCard(page).getByRole("button", { name: "Remove" }).click();
  await expect(kostalCard(page)).toHaveCount(0);
  await kostalProposal(page).getByRole("button", { name: "Add inverter" }).click();

  const added = kostalCard(page);
  await expect(added).toBeVisible();
  await expect(added).toContainText("added from discovery");
  // A name and an on/off switch, as every device has; nothing a catalog device lacks.
  await expect(added.locator("input")).toHaveCount(2);
  await expect(added.locator("select, textarea")).toHaveCount(0);
  await expect(kostalProposal(page).getByRole("button", { name: "Added to draft" })).toBeDisabled();
  await applyDraft(page);

  await openMaintenance(page);
  const installed = kostalCard(page);
  await expect(installed).toContainText(SERIAL);
  await runDiscovery(page);
  await expect(kostalProposal(page).getByRole("button", { name: "In config" })).toBeDisabled();

  await installed.locator(".hardware-card-toggle").click();
  await installed.locator('input[type="text"]').fill("Kostal Dach");
  await applyDraft(page);
  await openMaintenance(page);
  await expect(kostalCard(page)).toContainText("Kostal Dach");

  await kostalCard(page).getByRole("button", { name: "Remove" }).click();
  await expect(kostalCard(page)).toHaveCount(0);
  await applyDraft(page);
  await openMaintenance(page);
  await expect(kostalCard(page)).toHaveCount(0);
  await runDiscovery(page);
  await expect(kostalProposal(page).getByRole("button", { name: "Add inverter" })).toBeEnabled();
});
