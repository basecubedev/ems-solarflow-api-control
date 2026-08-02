import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Reversible maintenance connection switching: one physical inverter moves back
// and forth between its discovered connections inside a single discovery
// session. The connection it no longer uses becomes selectable again straight
// away — no rescan, no reload, no duplicate device.
//
// MQTT proposals come from the backend's own seeded discovery state, never from
// a browser mock: a connection switch is only authorized by a proposal the
// server can resolve, so mocking them would bypass the boundary under test.

const SERIAL = "SWITCH-SERIAL";
const ROUTE_B1 = "SWITCH-ROUTE-B1";
const ROUTE_B2 = "SWITCH-ROUTE-B2";
const ROUTE_CLOUD = "SWITCH-ROUTE-CLOUD";
// The refs discovery derives for the seeded endpoints; the scenario config names
// its broker profiles exactly the same way.
const REF_B1 = "local_mqtt_192_168_60_10_a176fa84";
const REF_B2 = "local_mqtt_192_168_60_11_1e93cabd";
const REF_CLOUD = "zendure_cloud";

type DiscoveryState = {
  apiDevices: unknown[];
};

function apiInverter(serial: string, ip: string) {
  return {
    serial_number: serial,
    role_suggestion: "inverter",
    ip,
    port: 8080,
    api_family: "zendure_local_http",
    device_type: "zendure_solarflow_800_pro",
    display_name: "SolarFlow 800 Pro 2",
    model: "SolarFlow 800 Pro 2",
    verified: true,
    usable_for_config: true,
    config_ready: true,
  };
}

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

// The issued identity the backend holds for a serial, read from its own trusted
// proposals. page.request bypasses page.route, so this never re-enters a mock.
async function stampObservations(page: Page, devices: unknown[]) {
  let issued = new Map<string, string>();
  try {
    const response = await page.request.get("/api/discovery/mqtt-proposals");
    if (response.ok()) {
      const payload = await response.json();
      for (const proposal of payload.proposals || []) {
        const serial = String(proposal.serial_number || "");
        const token = String(proposal.physical_identity_token || "");
        if (serial && token) issued.set(serial, token);
      }
    }
  } catch {
    issued = new Map();
  }
  return (devices as Record<string, unknown>[]).map((device) => {
    const token = issued.get(String(device.serial_number || ""));
    return token
      ? { ...device, physical_device_id: token, identity_status: "confirmed" }
      : device;
  });
}

async function mockDiscovery(page: Page, state: DiscoveryState) {
  await page.route("**/api/discovery/**", (route) => json(route, {}));
  // Discovery stamps every observation it serves with the identity it resolved.
  // These devices are mocked, so the stamp is taken from the backend's own
  // proposals for the same serial — the same token, from the same key, exactly
  // as a real discovery response would carry.
  await page.route("**/api/discovery/devices**", async (route) => {
    const devices = await stampObservations(page, state.apiDevices);
    return json(route, { devices, ignored_devices: [] });
  });
  await page.route("**/api/discovery/mdns/refresh**", (route) =>
    json(route, { state: "enabled" }),
  );
  await page.route("**/api/discovery/networks**", (route) =>
    json(route, { networks: [] }),
  );
  await page.route("**/api/discovery/mqtt-brokers/refresh**", (route) =>
    json(route, { ok: true }),
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
  // Proposals are served by the real backend so the ids the browser selects are
  // the ones the server can resolve.
  await page.route("**/api/discovery/mqtt-proposals**", (route) =>
    route.continue(),
  );
}

async function login(page: Page) {
  const loginPage = new LoginPage(page);
  await loginPage.open();
  await loginPage.authenticate();
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

function results(page: Page) {
  return page.locator("#maintenance-discovery-results");
}

function configuredCards(page: Page) {
  return page.locator("#maintenance-config-inverters .hardware-card");
}

function inverterCard(page: Page) {
  return configuredCards(page).first();
}

async function openCard(page: Page, card: ReturnType<typeof inverterCard>) {
  await expect(async () => {
    const body = card.locator(".hardware-card-body");
    if (!(await body.isVisible())) {
      await card.locator(".hardware-card-toggle").click();
    }
    await expect(body).toBeVisible({ timeout: 1_000 });
  }).toPass();
}

function cardInput(page: Page, card: ReturnType<typeof inverterCard>, label: string) {
  return card
    .locator("label")
    .filter({ has: page.locator(".feature-field-label", { hasText: label }) })
    .locator('input[type="text"], input[type="number"]')
    .first();
}

// Exactly one alternative connection may be offered at a time, so the switch
// never depends on telling two identically named cards apart.
async function useTheOfferedConnection(page: Page) {
  const button = results(page).getByRole("button", { name: "Use connection" });
  await expect(button).toHaveCount(1);
  await button.click();
}

// The hardware role owns the card class for every transport, so the configured
// connection is asserted through the card's transport metadata instead.
async function expectOneInverter(page: Page, connection: string) {
  await expect(configuredCards(page)).toHaveCount(1);
  await expect(inverterCard(page)).toHaveClass(/hardware-card-inverter/);
  await expect(inverterCard(page)).toHaveAttribute("data-connection", connection);
}

// The generated backend config, not the in-memory draft: Preview renders what
// preview_maintenance_config() merged, so this is the contract the apply writes.
// The pre is cleared first so a second preview can never read the first one.
async function previewedDevice(page: Page) {
  const raw = page.locator("#maintenance-config-raw-pre");
  await raw.evaluate((node) => {
    node.textContent = "";
  });
  await page.locator("#maintenance-config-preview-btn").click();
  await expect(raw).not.toBeEmpty();
  const preview = JSON.parse((await raw.textContent()) || "{}");
  const devices = (preview.devices || []).filter(
    (device: { type?: string }) => device.type === "zendure_mqtt",
  );
  expect(devices).toHaveLength(1);
  return devices[0].mqtt || {};
}

async function expectPreviewedConnection(
  page: Page,
  expected: { broker_ref: string; source?: string; device_id?: string },
) {
  const mqtt = await previewedDevice(page);
  expect(mqtt.broker_ref).toBe(expected.broker_ref);
  // A stored connection that states no mqtt.source keeps resolving it from its
  // broker profile, so only a stated source is asserted here.
  if (expected.source !== undefined) expect(mqtt.source).toBe(expected.source);
  if (expected.device_id !== undefined) expect(mqtt.device_id).toBe(expected.device_id);
  return mqtt;
}

// Cloud route identifiers stay redacted in the browser preview, so the landed
// selection is proven by its broker and transport, never by reading the route.
// ``source`` is passed explicitly for the same reason as above: landing back on
// a stored connection that never stated one must not invent it, so that case
// omits the argument.
async function expectPreviewedCloudConnection(page: Page, source?: string) {
  const mqtt = await expectPreviewedConnection(page, {
    broker_ref: REF_CLOUD,
    source,
  });
  expect(String(mqtt.device_id || "")).not.toBe(ROUTE_CLOUD);
  expect(String(mqtt.device_id || "")).not.toBe(ROUTE_B1);
  expect(String(mqtt.device_id || "")).toMatch(/^(•+|<redacted>)$/);
}

async function expectPreservedCommonValues(page: Page) {
  const card = inverterCard(page);
  await openCard(page, card);
  await expect(cardInput(page, card, "Device name")).toHaveValue("INV_1");
  await expect(cardInput(page, card, "Device output limit")).toHaveValue("642");
  await expect(cardInput(page, card, "Minimum SoC")).toHaveValue("22");
  await expect(card).toHaveAttribute("data-disabled", "false");
}

test("Local MQTT b1 -> b2 -> b1 switches back without a rescan", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_local_broker_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, "local_mqtt");

  await runDiscovery(page);
  // b1 is the installed connection; b2 is the only alternative on offer.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-in-config"))
    .toHaveCount(1);
  await expect(results(page).locator(".mconfig-discovery-add-button.is-transport"))
    .toHaveCount(1);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, "local_mqtt");
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B2,
  );
  // The whole connection follows the selection into the generated config, not
  // just the route: b1 is no longer this device's broker.
  await expectPreviewedConnection(page, {
    broker_ref: REF_B2,
    source: "local_mqtt",
    device_id: ROUTE_B2,
  });
  // b1 is free again immediately: the card was rebuilt, not hand-patched.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-transport"))
    .toHaveCount(1);
  await expect(results(page).locator(".mconfig-discovery-add-button.is-added"))
    .toHaveCount(1);
  await expect(results(page).getByRole("button", { name: "Connection selected" }))
    .toHaveCount(0);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, "local_mqtt");
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B1,
  );
  // Back on the installed connection, which reports as configured again.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-in-config"))
    .toHaveCount(1);
  await expectPreservedCommonValues(page);

  // Back on the stored connection exactly: b1 with its original route, and no
  // stated source invented for a config that always resolved it from the profile.
  await expectPreviewedConnection(page, {
    broker_ref: REF_B1,
    device_id: ROUTE_B1,
  });
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-apply-btn")).toBeVisible();
});

// One physical inverter observed on a local broker and on the Zendure account
// at the same time: both connections stay on offer, so either direction of the
// switch is reachable inside one discovery session.
test("Local MQTT -> Zendure MQTT -> Local MQTT switches back without a rescan", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_local_cloud_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, "local_mqtt");

  await runDiscovery(page);
  // b1 is the installed connection; the Cloud account is the alternative.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-in-config"))
    .toHaveCount(1);
  await expect(results(page).locator(".mconfig-discovery-add-button.is-transport"))
    .toHaveCount(1);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, "zendure_mqtt");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expectPreservedCommonValues(page);
  await expectPreviewedCloudConnection(page, "zendure_cloud_mqtt");
  // The freed local connection is immediately selectable again, no rescan.
  await expect(results(page).locator(".mconfig-discovery-add-button.is-transport"))
    .toHaveCount(1);

  await useTheOfferedConnection(page);
  await expectOneInverter(page, "local_mqtt");
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B1,
  );
  await expectPreservedCommonValues(page);
  await expectPreviewedConnection(page, {
    broker_ref: REF_B1,
    device_id: ROUTE_B1,
  });
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-apply-btn")).toBeVisible();
});

test("Zendure MQTT -> Local MQTT -> Zendure MQTT switches back without a rescan", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_cloud_local_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, "zendure_mqtt");

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expectOneInverter(page, "local_mqtt");
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B1,
  );
  await expectPreservedCommonValues(page);
  await expectPreviewedConnection(page, {
    broker_ref: REF_B1,
    source: "local_mqtt",
    device_id: ROUTE_B1,
  });

  await useTheOfferedConnection(page);
  await expectOneInverter(page, "zendure_mqtt");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expectPreservedCommonValues(page);
  // Back on the installed Cloud connection exactly, without inventing a stated
  // source for a config that always resolved it from its broker profile.
  await expectPreviewedCloudConnection(page);
  await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");
  await expect(page.locator("#maintenance-config-apply-btn")).toBeVisible();
});

test("API -> Zendure MQTT -> API switches back in one session", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [apiInverter(SERIAL, "192.168.60.20")],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_api_cloud_switchback");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, "local_api");

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expectOneInverter(page, "zendure_mqtt");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expectPreviewedCloudConnection(page, "zendure_cloud_mqtt");

  // The Local API connection is offered again straight away.
  await useTheOfferedConnection(page);
  await expectOneInverter(page, "local_api");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText("API");
  await expectPreservedCommonValues(page);
  await expect(cardInput(page, inverterCard(page), "Device IP address")).toHaveValue(
    "192.168.60.20",
  );
});

test("Zendure MQTT -> API -> Zendure MQTT switches back in one session", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [apiInverter(SERIAL, "192.168.60.20")],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_cloud_api_switchback");
  await page.reload();
  await openMaintenanceEditor(page);

  // The installed config states no mqtt.source; the broker profile resolves it,
  // so the configured card names Zendure MQTT before any discovery has run.
  await expectOneInverter(page, "zendure_mqtt");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expect(inverterCard(page).locator(".connection-pill")).toHaveAttribute(
    "data-connection",
    "zendure_mqtt",
  );

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expectOneInverter(page, "local_api");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText("API");

  await useTheOfferedConnection(page);
  await expectOneInverter(page, "zendure_mqtt");
  await expect(inverterCard(page).locator(".connection-pill")).toHaveText(
    "Zendure MQTT",
  );
  await expectPreservedCommonValues(page);
});

// The browser's broker endpoint block is not proof that a proposal exists: with
// the server-resolvable selection stripped from the submitted draft, the switch
// must be refused rather than re-homing the device onto the submitted broker.
test("a connection switch without its proposal id is refused", async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("maintenance_local_broker_switchback");
  await page.reload();
  await openMaintenanceEditor(page);

  await runDiscovery(page);
  await useTheOfferedConnection(page);
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B2,
  );

  await page.route("**/api/admin/maintenance/config/preview", async (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    for (const device of (body.draft && body.draft.devices) || []) {
      delete device.proposal_id;
      delete device.proposal_broker_ref;
    }
    await route.continue({ postData: JSON.stringify(body) });
  });

  await page.locator("#maintenance-config-preview-btn").click();
  await expect(page.locator("#maintenance-config-warnings")).toContainText(
    /not backed by a current discovery proposal/,
  );
  await expect(page.locator("#maintenance-config-validation")).toHaveText(
    "invalid",
  );
  await expect(page.locator("#maintenance-config-apply-btn")).toBeHidden();

  // Nothing was written: the reloaded config still uses the stored connection.
  await page.unroute("**/api/admin/maintenance/config/preview");
  await page.reload();
  await openMaintenanceEditor(page);
  await expectOneInverter(page, "local_mqtt");
  await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
    ROUTE_B1,
  );
  await expectPreviewedConnection(page, {
    broker_ref: REF_B1,
    device_id: ROUTE_B1,
  });
});

// A current, resolvable proposal proves a connection exists — not that it
// belongs to the configured inverter the draft names. The foreign proposal here
// is the backend's own, so only the server-side device binding can refuse it.
async function foreignProposal(page: Page) {
  const response = await page.request.get("/api/discovery/mqtt-proposals");
  expect(response.ok()).toBeTruthy();
  const { proposals } = await response.json();
  const foreign = (proposals || []).filter(
    (proposal: { serial_number?: string }) =>
      proposal.serial_number === "SWITCH-SERIAL-OTHER",
  );
  expect(foreign).toHaveLength(1);
  return foreign[0];
}

for (const tampering of [
  { name: "keeping the foreign identity fields", drop: false },
  { name: "dropping the stored identity fields", drop: true },
]) {
  test(`a proposal for another inverter is refused when ${tampering.name}`, async ({
    page,
    seedAdminScenario,
  }) => {
    const state: DiscoveryState = {
      apiDevices: [],
    };
    await mockDiscovery(page, state);
    await login(page);
    await seedAdminScenario("maintenance_foreign_inverter_proposal");
    await page.reload();
    await openMaintenanceEditor(page);
    await runDiscovery(page);

    const proposal = await foreignProposal(page);
    const fragmentMqtt = (proposal.config_fragment || {}).mqtt || {};
    await page.route("**/api/admin/maintenance/config/preview", async (route) => {
      const body = JSON.parse(route.request().postData() || "{}");
      for (const device of (body.draft && body.draft.devices) || []) {
        if (device.original_name !== "INV_1") continue;
        device.proposal_id = proposal.id;
        device.proposal_broker_ref = proposal.broker_ref;
        device.broker = {
          ref: proposal.broker_ref,
          host: proposal.broker_host,
          port: proposal.broker_port,
          tls: proposal.broker_tls === true,
          tls_insecure: proposal.broker_tls_insecure === true,
          tls_mode: proposal.broker_tls_mode || "",
          credentials_ref: proposal.credentials_ref || "",
          source: proposal.connection_source || "",
        };
        if (tampering.drop) {
          // The stored device's identity is gone from the submitted draft.
          delete device.physical_identity_token;
          delete device.serial_number;
          delete device.device_id;
          device.mqtt = { broker_ref: proposal.broker_ref };
        } else {
          device.physical_identity_token = proposal.physical_identity_token;
          device.serial_number = proposal.serial_number;
          device.device_id = fragmentMqtt.device_id;
          device.mqtt = {
            broker_ref: fragmentMqtt.broker_ref,
            source: fragmentMqtt.source,
            topic_family: fragmentMqtt.topic_family,
            device_id: fragmentMqtt.device_id,
          };
        }
      }
      await route.continue({ postData: JSON.stringify(body) });
    });

    await page.locator("#maintenance-config-preview-btn").click();
    await expect(page.locator("#maintenance-config-warnings")).toContainText(
      /belongs to a different inverter/,
    );
    await expect(page.locator("#maintenance-config-validation")).toHaveText(
      "invalid",
    );
    await expect(page.locator("#maintenance-config-apply-btn")).toBeHidden();

    // Nothing landed: the stored connection is still the configured one.
    await page.unroute("**/api/admin/maintenance/config/preview");
    await page.reload();
    await openMaintenanceEditor(page);
    await expectOneInverter(page, "local_mqtt");
    await expect(cardInput(page, inverterCard(page), "MQTT device ID")).toHaveValue(
      ROUTE_B1,
    );
    await expectPreviewedConnection(page, {
      broker_ref: REF_B1,
      device_id: ROUTE_B1,
    });
  });
}
