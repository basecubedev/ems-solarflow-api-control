import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  SHELLY_METER,
  apiInverter,
  cloudInverter,
  setDiscoveryPriority,
} from "./helpers/discovery-state";

// One physical inverter discovered over both API and Zendure MQTT. Its
// alternative connection stays offered under "Add more devices" and switching to
// it replaces the configured connection in place: one logical inverter, same
// name, same common EMS values, no confirmation dialog, and the previous
// connection is offered right back.
//
// Discovery is the deterministic Admin's own state — Setup's device plan is
// computed from it — so the journey seeds it. Only the preview is mocked, to
// keep the browser's candidate/switch behavior the thing under test.

const SERIAL = "EOD1SWITCH01";
const API_IP = "192.168.100.78";

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type State = { priority: string[]; lastPreview: Record<string, unknown> | null };

async function mockCloudAndPreview(page: Page, state: State) {
  await page.route("**/api/discovery/networks", (route) => json(route, { networks: [] }));
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings", (route) =>
    json(route, { token_saved: true, broker: "mqtt.zen-iot.com", tls_mode: "system_ca" }),
  );

  // The preview mirrors the backend contract closely enough to prove the review
  // step still runs: one device per selected connection, duplicate physical
  // identities rejected, common values carried through.
  await page.route("**/api/setup/config-preview", async (route) => {
    const body = route.request().postDataJSON() || {};
    state.lastPreview = body;
    const draftInverters = (body.devices || []).filter(
      (d: { role?: string; enabled?: boolean }) => d.role === "inverter" && d.enabled !== false,
    );
    const proposals = (body.zendure_mqtt_proposals || []).filter(
      (p: { enabled?: boolean }) => p.enabled !== false,
    );
    const configDevices = [
      ...draftInverters.map(
        (d: {
          config_name: string;
          serial_number: string;
          ip: string;
          config_values?: Record<string, unknown>;
        }) => ({
          name: d.config_name,
          ip: d.ip,
          sn: d.serial_number,
          max_power: Number((d.config_values || {}).max_power ?? 800),
        }),
      ),
      ...proposals.map(
        (p: { id: string; config_name: string; config_values?: Record<string, unknown> }) => ({
          name: p.config_name,
          type: "zendure_mqtt",
          serial_number: String(p.id).replace("zendure-mqtt:", ""),
          max_power: Number((p.config_values || {}).max_power ?? 800),
          capabilities: { write_output_limit: true },
        }),
      ),
    ];
    const errors: { code: string; message: string }[] = [];
    const seen = new Set<string>();
    for (const device of configDevices as { sn?: string; serial_number?: string }[]) {
      const serial = String(device.sn || device.serial_number || "").toLowerCase();
      if (!serial) continue;
      if (seen.has(serial)) {
        errors.push({
          code: "zendure_device_identity_duplicate",
          message: "Configure each physical device only once.",
        });
      }
      seen.add(serial);
    }
    return json(route, {
      ready: errors.length === 0,
      config: { devices: configDevices, grid_meter: { type: "shelly" } },
      summary: { inverters: configDevices.length, grid_meters: 1 },
      release: "latest",
      base: { source: "template" },
      validation: { errors, warnings: [], info: [] },
    });
  });
}

async function reachConfig(
  page: Page,
  state: State,
  seed: (spec: Record<string, unknown>) => Promise<void>,
) {
  await mockCloudAndPreview(page, state);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seed({
    local_api_devices: [apiInverter(SERIAL, API_IP), SHELLY_METER],
    cloud_devices: [cloudInverter(SERIAL)],
  });
  await setDiscoveryPriority(page, state.priority);
  await page.reload();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
  await page.locator('[data-setup-step="config"]').click();
  await openCandidatePool(page);
}

function inverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

function candidate(page: Page, state: string) {
  return page
    .locator(`#config-available-list .hardware-card-inverter[data-candidate-state="${state}"]`)
    .first();
}

// "Add more devices" is a collapsed <details>; its cards are only operable once
// it is open. Re-assert instead of sleeping: the list re-renders on every draft
// change, but the open state is owned by the element.
async function openCandidatePool(page: Page) {
  const details = page.locator("#config-available-details");
  await expect(async () => {
    if (!(await details.evaluate((node: HTMLDetailsElement) => node.open))) {
      await details.locator("> summary").click();
    }
    expect(await details.evaluate((node: HTMLDetailsElement) => node.open)).toBe(true);
  }).toPass();
}

function fieldInput(card: ReturnType<typeof inverterCards>, label: string) {
  return card.locator("label.feature-field-row", { hasText: label }).locator("input").first();
}

// A confirmation dialog would block the flow; nothing in the switch path may
// open one.
function trackDialogs(page: Page) {
  const dialogs: string[] = [];
  page.on("dialog", async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.dismiss();
  });
  return dialogs;
}

test("API to Zendure MQTT: the alternative connection replaces the inverter in place", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  const state: State = { priority: ["local_api", "local_mqtt", "zendure_mqtt"], lastPreview: null };
  const dialogs = trackDialogs(page);
  await reachConfig(page, state, seedDiscoveryState);

  // 01-03: the inverter is configured over API and the card says so.
  await expect(inverterCards(page)).toHaveCount(1);
  const configured = inverterCards(page).first();
  await expect(configured).toContainText("INV_1");
  await expect(configured.locator(".connection-pill")).toHaveText("API");

  // A common EMS value the switch must preserve.
  await configured.locator(".hardware-card-toggle").click();
  await fieldInput(configured, "Device output limit").fill("642");
  await expect(page.locator("#config-preview")).toContainText('"max_power": 642');

  // 04-05: the Zendure MQTT connection stays offered as an alternative.
  const mqttCandidate = candidate(page, "alternative");
  await expect(mqttCandidate).toContainText("Zendure MQTT");
  await expect(mqttCandidate).toContainText("Already configured as INV_1 via API");
  const useConnection = mqttCandidate.getByRole("button", { name: "Use connection" });
  await expect(useConnection).toBeEnabled();

  // 06-07: one click, no popup.
  await useConnection.click();
  expect(dialogs).toEqual([]);

  // 08-10: same single inverter, now over Zendure MQTT, name and value kept.
  await expect(inverterCards(page)).toHaveCount(1);
  const switched = inverterCards(page).first();
  await expect(switched).toContainText("INV_1");
  await expect(switched.locator(".connection-pill")).toHaveText("Zendure MQTT");
  await expect(page.locator("#config-preview")).toContainText('"name": "INV_1"');
  await expect(page.locator("#config-preview")).toContainText('"max_power": 642');

  // 11: the API connection is offered back without deleting the inverter.
  const apiCandidate = candidate(page, "alternative");
  await expect(apiCandidate).toContainText("Already configured as INV_1 via Zendure MQTT");
  await expect(apiCandidate.getByRole("button", { name: "Use connection" })).toBeEnabled();

  // 12: Config Preview stays the review step and is valid.
  await expect(page.locator("#config-preview-ready")).toHaveText(/Ready/i);
  await expect(page.locator("#config-validation")).not.toContainText(
    "Configure each physical device only once",
  );
});

test("Zendure MQTT to API: switching back keeps one inverter and its values", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  const state: State = { priority: ["zendure_mqtt", "local_api", "local_mqtt"], lastPreview: null };
  const dialogs = trackDialogs(page);
  await reachConfig(page, state, seedDiscoveryState);

  // Configured over Zendure MQTT from the start.
  await expect(inverterCards(page)).toHaveCount(1);
  await expect(inverterCards(page).first().locator(".connection-pill")).toHaveText("Zendure MQTT");

  const apiCandidate = candidate(page, "alternative");
  await expect(apiCandidate).toContainText("API");
  await expect(apiCandidate).toContainText("Already configured as INV_1 via Zendure MQTT");
  await apiCandidate.getByRole("button", { name: "Use connection" }).click();
  expect(dialogs).toEqual([]);

  await expect(inverterCards(page)).toHaveCount(1);
  const switched = inverterCards(page).first();
  await expect(switched).toHaveAttribute("data-source-id", /^obs:v1:/);
  await expect(switched).toContainText("INV_1");
  await expect(switched.locator(".connection-pill")).toHaveText("API");

  // Common values entered over API survive a full round trip back to MQTT.
  await switched.locator(".hardware-card-toggle").click();
  await fieldInput(switched, "Device output limit").fill("555");
  await candidate(page, "alternative")
    .getByRole("button", { name: "Use connection" })
    .click();
  await expect(inverterCards(page)).toHaveCount(1);
  await expect(inverterCards(page).first().locator(".connection-pill")).toHaveText("Zendure MQTT");
  await expect(page.locator("#config-preview")).toContainText('"max_power": 555');
  await expect(page.locator("#config-preview-ready")).toHaveText(/Ready/i);
});

test("the selected connection and its values survive a reload", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  const state: State = { priority: ["local_api", "local_mqtt", "zendure_mqtt"], lastPreview: null };
  await reachConfig(page, state, seedDiscoveryState);

  const configured = inverterCards(page).first();
  await configured.locator(".hardware-card-toggle").click();
  await fieldInput(configured, "Device output limit").fill("701");
  await candidate(page, "alternative").getByRole("button", { name: "Use connection" }).click();
  await expect(inverterCards(page).first().locator(".connection-pill")).toHaveText("Zendure MQTT");

  await page.reload();
  await page.locator('[data-setup-step="config"]').click();
  await openCandidatePool(page);

  // No duplicate device, the same connection stays selected, values kept, and
  // the other connection is still an alternative.
  await expect(inverterCards(page)).toHaveCount(1);
  await expect(inverterCards(page).first().locator(".connection-pill")).toHaveText("Zendure MQTT");
  await expect(page.locator("#config-preview")).toContainText('"max_power": 701');
  await expect(candidate(page, "alternative")).toContainText(
    "Already configured as INV_1 via Zendure MQTT",
  );
});
