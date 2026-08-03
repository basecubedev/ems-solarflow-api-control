import { type Page, type Request, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Maintenance credential management must not depend on Guided Setup operation
// state: after the reset fixture there is no transition and no browser-side
// setupOperationId, yet the shared source-config nodes mounted under
// Maintenance "Add more devices" must drive the generic /api/discovery routes
// with the Admin session + CSRF only. The Zendure cloud endpoints are mocked
// deterministically (the real handler would call the Zendure cloud); the local
// MQTT credential pool runs against the real test-mode server.

type SeenRequest = { url: string; method: string; headers: Record<string, string> };

function snapshot(request: Request): SeenRequest {
  return { url: request.url(), method: request.method(), headers: request.headers() };
}

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function trackSetupAliasRequests(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/setup/discovery/")) {
      seen.push(`${request.method()} ${request.url()}`);
    }
  });
  return seen;
}

async function openMaintenanceAddDevices(page: Page) {
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
  // The async config load re-renders the editor; retry until the details stays
  // open (same pattern as the card toggle above, no fixed waits).
  const sources = page.locator("#maintenance-discovery-sources");
  await expect(async () => {
    if (!(await sources.isVisible())) {
      await page.locator("#maintenance-add-devices > summary").click();
    }
    await expect(sources).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

async function openMaintenanceSourceRow(page: Page, source: string, formId: string) {
  const slot = page.locator(`[data-maintenance-source-slot="${source}"]`);
  const form = slot.locator(`#${formId}`);
  await expect(async () => {
    if (!(await form.isVisible())) {
      await page
        .locator(`[data-maintenance-source="${source}"] > summary`)
        .click();
    }
    await expect(form).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

test("Maintenance Zendure credential lifecycle stays on generic discovery routes", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  const setupAliasRequests = trackSetupAliasRequests(page);
  const mutations: SeenRequest[] = [];
  const state = { tokenSaved: false };
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings", (route) =>
    json(route, {
      token_saved: state.tokenSaved,
      tls_mode: "system_ca",
      last_broker: state.tokenSaved ? "mqtt.zen-iot.com" : null,
      last_status: state.tokenSaved ? "ok" : null,
      last_device_count: state.tokenSaved ? 2 : 0,
    }),
  );
  await page.route("**/api/discovery/zendure-cloud-mqtt/test", (route) => {
    mutations.push(snapshot(route.request()));
    return json(route, {
      ok: true,
      devices_found: 2,
      broker: "mqtt.zen-iot.com",
      tls_required: true,
      tls_mode: "system_ca",
      message: "Zendure device list loaded.",
    });
  });
  await page.route("**/api/discovery/zendure-cloud-mqtt/token", (route) => {
    mutations.push(snapshot(route.request()));
    state.tokenSaved = route.request().method() !== "DELETE";
    return json(
      route,
      state.tokenSaved
        ? { ok: true, token_saved: true, message: "Zendure credential saved." }
        : { ok: true, token_saved: false, removed: true, message: "Zendure credential removed." },
    );
  });
  await page.route("**/api/discovery/zendure-cloud-mqtt/refresh", (route) => {
    mutations.push(snapshot(route.request()));
    return json(route, {
      ok: true,
      candidates: [],
      device_list_count: 2,
      mqtt_observed_count: 2,
      broker: "mqtt.zen-iot.com",
      tls_mode: "system_ca",
      mqtt_message: "Zendure cloud discovery complete.",
    });
  });

  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceAddDevices(page);

  await openMaintenanceSourceRow(page, "zendure_mqtt", "zendure-cloud-token-form");

  // Test before any credential is saved: the request must use the generic
  // route and succeed; the Setup-operation refusal must never surface.
  const firstTest = page.waitForResponse((response) =>
    response.url().includes("/api/discovery/zendure-cloud-mqtt/test"),
  );
  await page.fill("#zendure-cloud-token-input", "maintenance-zendure-api-key");
  await page.locator("#zendure-cloud-test").click();
  expect((await firstTest).status()).toBe(200);
  await expect(page.locator("#zendure-cloud-message")).not.toContainText(
    "confirmed Setup operation",
  );
  await expect(page.locator("#zendure-cloud-message")).not.toContainText(
    "credential test failed",
  );

  await page.fill("#zendure-cloud-token-input", "maintenance-zendure-api-key");
  await page.locator("#zendure-cloud-save").click();
  await expect(page.locator("#zendure-cloud-token-state")).toHaveText("saved");

  // With the credential saved the tested-OK rendering is stable.
  await page.locator("#zendure-cloud-test").click();
  await expect(page.locator("#zendure-cloud-message")).toContainText(
    "Zendure credential OK: 2 device(s)",
  );

  await page.locator("#zendure-cloud-refresh").click();
  await expect(page.locator("#zendure-cloud-message")).toContainText(
    "Zendure cloud discovery complete.",
  );

  await expect(page.locator("#zendure-cloud-forget")).toBeVisible();
  await page.locator("#zendure-cloud-forget").click();
  await expect(page.locator("#zendure-cloud-token-state")).toHaveText("not saved");
  await expect(page.locator("#zendure-cloud-forget")).toBeHidden();

  expect(mutations.map((request) => request.method)).toEqual([
    "POST",
    "POST",
    "POST",
    "POST",
    "DELETE",
  ]);
  for (const request of mutations) {
    expect(request.url).toContain("/api/discovery/zendure-cloud-mqtt/");
    expect(request.url).not.toContain("/api/setup/");
    expect(request.headers["x-setup-operation-id"]).toBeUndefined();
    expect(request.headers["x-csrf-token"]).toBeTruthy();
  }
  expect(setupAliasRequests).toEqual([]);
});

test("Maintenance local MQTT credential save and delete use the real generic routes", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  const setupAliasRequests = trackSetupAliasRequests(page);
  const credentialRequests: SeenRequest[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/discovery/connections/mqtt-credentials")) {
      credentialRequests.push(snapshot(request));
    }
  });

  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceAddDevices(page);

  await openMaintenanceSourceRow(page, "local_mqtt", "mqtt-credential-form");

  await page.fill("#mqtt-credential-label", "Maintenance broker");
  await page.fill("#mqtt-credential-username", "svc");
  await page.fill("#mqtt-credential-password", "maintenance-broker-secret");
  await page.locator("#mqtt-credential-save").click();
  await expect(page.locator("#mqtt-credential-message")).toHaveText(
    "Credential saved.",
  );
  const card = page.locator("#mqtt-credential-list .mqtt-credential-card");
  await expect(card).toContainText("Maintenance broker");
  await expect(page.locator("body")).not.toContainText(
    "maintenance-broker-secret",
  );

  await card.locator("[data-forget-credential]").click();
  await expect(page.locator("#mqtt-credential-message")).toHaveText(
    "Credential removed.",
  );
  await expect(card).toHaveCount(0);

  const mutating = credentialRequests.filter(
    (request) => request.method !== "GET",
  );
  expect(mutating.map((request) => request.method)).toEqual(["POST", "DELETE"]);
  for (const request of mutating) {
    expect(request.url).toContain("/api/discovery/connections/mqtt-credentials");
    expect(request.url).not.toContain("/api/setup/");
    expect(request.headers["x-setup-operation-id"]).toBeUndefined();
    expect(request.headers["x-csrf-token"]).toBeTruthy();
  }
  expect(setupAliasRequests).toEqual([]);
});
