import { type Locator, type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Maintenance transport replacement: one physical inverter keeps one config
// entry, one name and one common value set while its connection moves between
// Zendure MQTT and Local API. Discovery is deterministically mocked; the
// maintenance draft, preview and apply run against the real test-mode backend.

const SERIAL_A = "E2EMQTTAAA1";
const SERIAL_B = "E2EMQTTBBB2";
const CLOUD_ROUTE_SERIALLESS = "E2E_CLOUD_ROUTE_7501";
const CLOUD_PRODUCT_SERIALLESS = "E2E_CLOUD_PRODUCT_75";
const CLOUD_TOPIC_SERIALLESS =
  `iot/${CLOUD_PRODUCT_SERIALLESS}/${CLOUD_ROUTE_SERIALLESS}/properties/report`;
const CLOUD_PHYSICAL_SERIAL = "E2E-CLOUD-SERIAL-7502";
const CLOUD_ROUTE_SERIALIZED = "E2E_CLOUD_ROUTE_7502";
const CLOUD_PRODUCT_SERIALIZED = "E2E_CLOUD_PRODUCT_76";
const CLOUD_TOPIC_SERIALIZED =
  `iot/${CLOUD_PRODUCT_SERIALIZED}/${CLOUD_ROUTE_SERIALIZED}/properties/report`;

type DiscoveryState = {
  apiDevices: unknown[];
  proposals: unknown[];
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

// Mocked without a proposal id: these entries are *added* as new devices, which
// takes the manual local-MQTT path through the real backend. Replacing a stored
// device's connection is proposal-authorized and cannot be mocked this way — the
// switch case below reads the backend's own discovery state instead.
function mqttProposal(serial: string) {
  return {
    serial_number: serial,
    // Discovery issues an identity for every proposal it serves. The value is
    // opaque; what matters is that one physical device keeps one token, so the
    // same offer is recognized once it is in the draft.
    device_id: serial,
    target: "device",
    connection_source: "local_mqtt",
    broker_ref: "local_mqtt_e2e",
    broker_host: "192.168.60.10",
    broker_port: 1883,
    broker_tls: false,
    output_control_supported: false,
    display_name: "SolarFlow 800 Pro 2",
    hardware_generation: "solarflow_zensdk",
    role_hint: "inverter",
    config_fragment: {
      type: "zendure_mqtt",
      serial_number: serial,
      enabled: true,
      mqtt: {
        broker_ref: "local_mqtt_e2e",
        source: "local_mqtt",
        topic_family: "zensdk_ha_scalar",
        device_id: serial,
      },
      capabilities: { read_power: true, read_soc: true, write_output_limit: false },
    },
  };
}

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

// Where the backend publishes the identity it issued for a serial. Discovery
// proposals carry it where they are served for real; where they are mocked, the
// installed config does — the Maintenance response stamps every configured
// device. page.request bypasses page.route, so neither read re-enters a mock.
const ISSUED_IDENTITY_SOURCES = [
  {
    url: "/api/discovery/mqtt-proposals",
    read: (payload: Record<string, any>) => payload.proposals,
  },
  {
    url: "/api/admin/maintenance/config",
    read: (payload: Record<string, any>) => (payload.draft || {}).devices,
  },
];

async function stampObservations(page: Page, devices: unknown[]) {
  const issued = new Map<string, string>();
  for (const source of ISSUED_IDENTITY_SOURCES) {
    try {
      const response = await page.request.get(source.url);
      if (!response.ok()) continue;
      const entries = source.read(await response.json()) || [];
      for (const entry of entries) {
        const serial = String(entry.serial_number || entry.sn || "");
        const token = String(
          entry.physical_device_id || entry.physical_identity_token || "",
        );
        if (serial && token && !issued.has(serial)) issued.set(serial, token);
      }
    } catch {
      /* a scenario that does not serve this endpoint contributes nothing */
    }
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

// The hardware role owns the card class for every transport, so a card's
// configured connection is asserted through its transport metadata.
async function expectInverterConnection(card: Locator, connection: string) {
  await expect(card).toHaveClass(/hardware-card-inverter/);
  await expect(card).toHaveAttribute("data-connection", connection);
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

async function loadRealMqttProposals(
  page: Page,
  state: DiscoveryState,
) {
  // APIRequestContext requests are not intercepted by page.route(), so this
  // reads the real backend's browser-safe proposals and their server-issued
  // opaque identity tokens, then feeds that response into deterministic UI
  // discovery.
  const response = await page.request.get("/api/discovery/mqtt-proposals");
  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as { proposals: unknown[] };
  const flattened = JSON.stringify(payload);
  expect(flattened).not.toContain(CLOUD_ROUTE_SERIALLESS);
  expect(flattened).not.toContain(CLOUD_PRODUCT_SERIALLESS);
  expect(flattened).not.toContain(CLOUD_TOPIC_SERIALLESS);
  expect(flattened).not.toContain(CLOUD_ROUTE_SERIALIZED);
  expect(flattened).not.toContain(CLOUD_PRODUCT_SERIALIZED);
  expect(flattened).not.toContain(CLOUD_TOPIC_SERIALIZED);
  state.proposals = payload.proposals;
}

test("MQTT then API: discovery offers a transport switch, not a duplicate inverter", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
    proposals: [mqttProposal(SERIAL_A), mqttProposal(SERIAL_B)],
  };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("mixed_transports");
  await page.reload();
  await openMaintenanceEditor(page);

  // The seeded config holds three inverters.
  await expect(configuredCards(page)).toHaveCount(3);

  // MQTT discovery offers both new inverters; add them to the draft.
  await runDiscovery(page);
  const addButtons = page.locator(
    "#maintenance-discovery-results .mconfig-discovery-add-button.is-add",
  );
  await expect(addButtons).toHaveCount(2);
  // Each offer is added through its own card. These proposals are mocked in the
  // browser and therefore carry neither an id nor an issued identity, so the
  // review cannot tell them apart the way it does for a real discovery
  // response — addressing them by serial keeps the journey about what it tests.
  for (const serial of [SERIAL_A, SERIAL_B]) {
    await page
      .locator("#maintenance-discovery-results .mconfig-discovery-device-card", {
        hasText: serial,
      })
      .getByRole("button", { name: "Add inverter" })
      .click();
  }
  await expect(configuredCards(page)).toHaveCount(5);

  // Both new MQTT devices carry the central common defaults, visibly.
  const added = cardByText(page, SERIAL_A);
  await openCard(page, added);
  await expect(fieldInput(added, "Device output limit")).toHaveValue("800");
  await expect(fieldInput(added, "Minimum SoC")).toHaveValue("15");

  // Rename the first added inverter, then apply.
  await fieldInput(added, "Device name").fill("Roof West");
  await previewAndApply(page);

  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(5);
  const renamed = cardByText(page, "Roof West");
  await expectInverterConnection(renamed, "local_mqtt");

  // Local API discovery now sees the same serials: the review offers a
  // connection switch on the configured devices, never an "Add inverter".
  state.apiDevices = [
    apiInverter(SERIAL_A, "192.168.60.21"),
    apiInverter(SERIAL_B, "192.168.60.22"),
  ];
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  const switchButtons = results.getByRole("button", {
    name: "Use connection",
  });
  await expect(switchButtons).toHaveCount(2);
  // Scoped to the rediscovered devices, which is what this journey is about. The
  // MQTT proposal cards for the same inverters are re-offered here only because
  // these proposals are mocked in the browser and therefore carry no issued
  // identity; a real discovery response identifies them, and that recognition is
  // pinned in tests/test_admin_physical_identity_contract.py.
  await expect(
    results
      .locator(".mconfig-discovery-device-card", { hasText: "Inverter candidate" })
      .getByRole("button", { name: "Add inverter" }),
  ).toHaveCount(0);

  // Switch the renamed inverter to API: same card count, same name,
  // same tuning values, new connection fields.
  await results
    .locator(".mconfig-discovery-device-card", { hasText: "Roof West" })
    .getByRole("button", { name: "Use connection" })
    .click();
  await expect(configuredCards(page)).toHaveCount(5);
  const switched = cardByText(page, "Roof West");
  await expectInverterConnection(switched, "local_api");
  await openCard(page, switched);
  await expect(fieldInput(switched, "Device IP address")).toHaveValue(
    "192.168.60.21",
  );
  await expect(fieldInput(switched, "Serial number")).toHaveValue(SERIAL_A);
  await expect(fieldInput(switched, "Device output limit")).toHaveValue("800");
  await expect(fieldInput(switched, "Device name")).toHaveValue("Roof West");

  await previewAndApply(page);
  await page.reload();
  await openMaintenanceEditor(page);

  // Exactly five inverter entries persist — the switch replaced, not added.
  await expect(configuredCards(page)).toHaveCount(5);
  const persisted = cardByText(page, "Roof West");
  await expectInverterConnection(persisted, "local_api");
  await openCard(page, persisted);
  await expect(fieldInput(persisted, "Device IP address")).toHaveValue(
    "192.168.60.21",
  );
  await expect(fieldInput(persisted, "Device output limit")).toHaveValue("800");
});

test("API then MQTT: proposal for a configured serial switches the transport in place", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = {
    apiDevices: [],
    proposals: [],
  };
  await mockDiscovery(page, state);
  // Replacing a stored device's transport needs a proposal the server can
  // resolve, so this case reads the backend's own seeded discovery state.
  await page.route("**/api/discovery/mqtt-proposals**", (route) =>
    route.continue(),
  );
  await login(page);
  await seedAdminScenario("mixed_transports_api_mqtt_switch");
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);

  // The proposal matches the configured Local API inverter's serial, so the
  // only offer is the connection switch. This observation carries no product
  // key, so no write route resolves and taking it gives up the regulation - the
  // action names the replacement it performs instead of reading as an addition.
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  const switchButton = results.getByRole("button", {
    name: "Replace control connection",
  });
  await expect(switchButton).toHaveCount(1);
  await expect(
    results.getByRole("button", { name: "Add as telemetry source" }),
  ).toHaveCount(0);
  await expect(
    results.getByRole("button", { name: "Add inverter" }),
  ).toHaveCount(0);
  await expect(results.locator(".candidate-downgrade-note")).toContainText(
    "can no longer be controlled by EMS",
  );

  await switchButton.click();
  await expect(configuredCards(page)).toHaveCount(3);
  const switched = cardByText(page, "Local API inverter");
  await expectInverterConnection(switched, "local_mqtt");
  await openCard(page, switched);
  await expect(fieldInput(switched, "Serial number")).toHaveValue("API-SERIAL");
  // The seeded max_power survives; missing values materialize from defaults.
  await expect(fieldInput(switched, "Device output limit")).toHaveValue("800");
  await expect(fieldInput(switched, "Device name")).toHaveValue(
    "Local API inverter",
  );

  await previewAndApply(page);
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);
  const persisted = cardByText(page, "Local API inverter");
  await expectInverterConnection(persisted, "local_mqtt");
});

test("API to Zendure Cloud MQTT on a scalar family keeps the inverter controllable", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = { apiDevices: [], proposals: [] };
  await mockDiscovery(page, state);
  // The connection switch is proposal-authorized, so the backend must resolve
  // its own candidate for that serial.
  await page.route("**/api/discovery/mqtt-proposals**", (route) =>
    route.continue(),
  );
  await login(page);
  await seedAdminScenario("mixed_transports_api_mqtt_control_switch");
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);

  // Same physical inverter, scalar telemetry family, complete write route, on
  // the broker source that carries that route: the connection is
  // control-capable, so it is offered as an equal swap and never as a downgrade.
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  const useConnection = results.getByRole("button", { name: "Use connection" });
  await expect(useConnection).toHaveCount(1);
  await expect(
    results.getByRole("button", { name: "Replace control connection" }),
  ).toHaveCount(0);
  await expect(results.locator(".candidate-downgrade-note")).toHaveCount(0);

  await useConnection.click();
  await expect(configuredCards(page)).toHaveCount(3);
  const switched = cardByText(page, "Local API inverter");
  await expectInverterConnection(switched, "zendure_mqtt");
  await openCard(page, switched);
  await expect(fieldInput(switched, "Enabled")).toBeChecked();
  await expect(
    switched.locator("label.feature-field-row", { hasText: "Output control" }),
  ).toContainText("Available");

  // The preview agrees with the card: it treats the switched device as a cloud
  // *control* device and raises the single-controller advisory that only a
  // control-capable cloud device produces. Apply is not exercised here — the
  // deterministic test runtime holds no Zendure account credential, so writing
  // a cloud broker profile is refused by design. That is a credential
  // precondition, not a capability verdict, and the switchback spec stops at
  // preview for the same reason.
  await page.locator("#maintenance-config-preview-btn").click();
  await expect(page.locator("#maintenance-config-warnings")).toContainText(
    "Zendure Cloud MQTT output control is enabled",
  );
});

test("API to a local scalar MQTT connection does not become control-capable", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  const state: DiscoveryState = { apiDevices: [], proposals: [] };
  await mockDiscovery(page, state);
  await page.route("**/api/discovery/mqtt-proposals**", (route) =>
    route.continue(),
  );
  await login(page);
  await seedAdminScenario("mixed_transports_api_local_scalar_switch");
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);

  // Identical device, identical complete write route — only the broker source
  // differs. A local broker seen publishing scalar metrics only is not a proven
  // write carrier, so the swap must not be offered as a control-capable one.
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  await expect(
    results.getByRole("button", { name: "Replace control connection" }),
  ).toHaveCount(1);
  await expect(results.getByRole("button", { name: "Use connection" })).toHaveCount(
    0,
  );
  await expect(results.locator(".candidate-downgrade-note")).toContainText(
    "no longer be controlled",
  );
});

test("serial-less Cloud identity survives apply, reload, rediscovery and scope changes", { tag: ["@maintenance"] }, async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(90_000);
  const state: DiscoveryState = { apiDevices: [], proposals: [] };
  await mockDiscovery(page, state);
  await login(page);
  await seedAdminScenario("serialless_cloud_identity");
  await loadRealMqttProposals(page, state);

  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(1);
  await expect(page.locator("body")).not.toContainText(CLOUD_ROUTE_SERIALLESS);
  await expect(page.locator("body")).not.toContainText(CLOUD_TOPIC_SERIALLESS);

  // Add both real Cloud proposals: one has no physical serial and is grouped
  // solely through its opaque scoped token; the other has a physical serial.
  await runDiscovery(page);
  const results = page.locator("#maintenance-discovery-results");
  const addButtons = results.locator(
    ".mconfig-discovery-add-button.is-add",
  );
  await expect(addButtons).toHaveCount(2);
  await addButtons.first().click();
  await addButtons.first().click();
  await expect(configuredCards(page)).toHaveCount(3);

  const serialless = cardByText(page, "…7501");
  await openCard(page, serialless);
  await fieldInput(serialless, "Device name").fill("Roof Serial-less");
  await previewAndApply(page);

  // A derived token remains stable after persistence and reload; it is not
  // browser-authored or stored as route-like config data.
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);
  await expect(cardByText(page, "Roof Serial-less")).toHaveCount(1);
  await expect(page.locator("body")).not.toContainText(CLOUD_ROUTE_SERIALLESS);

  // Rediscovering the identical scoped Cloud routes offers no duplicate Add.
  await runDiscovery(page);
  await expect(
    results.locator(".mconfig-discovery-add-button.is-add"),
  ).toHaveCount(0);
  await expect(
    results.locator(".mconfig-discovery-add-button.is-in-config"),
  ).toHaveCount(2);

  // Physical serial remains the higher-confidence cross-transport identity:
  // the serialized Cloud inverter can switch to Local API in place.
  state.apiDevices = [
    apiInverter(CLOUD_PHYSICAL_SERIAL, "192.168.75.22"),
  ];
  await runDiscovery(page);
  const apiSwitch = results.getByRole("button", {
    name: "Use connection",
  });
  await expect(apiSwitch).toHaveCount(1);
  await apiSwitch.click();
  await expect(configuredCards(page)).toHaveCount(3);
  const serializedSwitched = cardByText(page, CLOUD_PHYSICAL_SERIAL);
  await expect(serializedSwitched).toHaveCount(1);
  await expectInverterConnection(serializedSwitched, "local_api");
  await openCard(page, serializedSwitched);
  await expect(
    fieldInput(serializedSwitched, "Device IP address"),
  ).toHaveValue("192.168.75.22");
  await expect(fieldInput(serializedSwitched, "Serial number")).toHaveValue(
    CLOUD_PHYSICAL_SERIAL,
  );
  await previewAndApply(page);
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);
  const serializedPersisted = cardByText(page, CLOUD_PHYSICAL_SERIAL);
  await expect(serializedPersisted).toHaveCount(1);
  await expectInverterConnection(serializedPersisted, "local_api");
  await openCard(page, serializedPersisted);
  await expect(
    fieldInput(serializedPersisted, "Device IP address"),
  ).toHaveValue("192.168.75.22");
  await expect(fieldInput(serializedPersisted, "Serial number")).toHaveValue(
    CLOUD_PHYSICAL_SERIAL,
  );

  // The same raw route under another broker/source scope is a separate device,
  // but known Cloud route/product values stay masked across the whole response.
  await seedAdminScenario("serialless_cloud_identity_other_scope");
  await loadRealMqttProposals(page, state);
  await runDiscovery(page);
  await expect(
    results.locator(".mconfig-discovery-add-button.is-add"),
  ).toHaveCount(1);
  await expect(configuredCards(page)).toHaveCount(3);
  await expect(page.locator("body")).not.toContainText(CLOUD_ROUTE_SERIALLESS);
  await expect(page.locator("body")).not.toContainText(CLOUD_TOPIC_SERIALLESS);

  // Leaving that separately scoped candidate unselected while renaming and
  // applying the original does not duplicate or discard its scoped identity.
  const original = cardByText(page, "Roof Serial-less");
  await openCard(page, original);
  await fieldInput(original, "Device name").fill("Roof Serial-less Final");
  await previewAndApply(page);
  await page.reload();
  await openMaintenanceEditor(page);
  await expect(configuredCards(page)).toHaveCount(3);
  await expect(cardByText(page, "Roof Serial-less Final")).toHaveCount(1);
});
