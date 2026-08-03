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

// Late Zendure cloud credentials + a discovery-priority change must carry the
// selected transport into Config: two Local-API inverters discovered first are
// reconfigured over Zendure Cloud MQTT once MQTT is prioritized and rescanned.
//
// Discovery is the *server's* own state — Setup's device plan is computed from
// it, so the journey seeds the deterministic Admin rather than answering the
// device list for the page. Only the cloud endpoints (which would otherwise
// reach the network) and the preview stay mocked.

const SERIAL_A = "EOD1AAA111";
const SERIAL_B = "EOD1BBB222";

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

// Only what would otherwise reach the network, plus a deterministic preview.
// The device list, the proposal list and the source preference all come from
// the server's own state — that is what the plan is computed from.
async function mockCloudAndPreview(page: Page, state: { mqttReady: boolean }) {
  await page.route("**/api/discovery/networks", (route) => json(route, { networks: [] }));
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings", (route) =>
    json(route, { token_saved: state.mqttReady, broker: "mqtt.zen-iot.com", tls_mode: "system_ca" }),
  );
  await page.route("**/api/discovery/zendure-cloud-mqtt/refresh", (route) =>
    json(route, {
      ok: true,
      candidates: [],
      device_list_count: 2,
      mqtt_observed_count: 2,
      broker: "mqtt.zen-iot.com",
      tls_mode: "system_ca",
      mqtt_message: "Zendure cloud discovery complete.",
    }),
  );

  await page.route("**/api/setup/config-preview", async (route) => {
    const body = route.request().postDataJSON() || {};
    const draftInverters = (body.devices || []).filter(
      (d: { role?: string; enabled?: boolean }) => d.role === "inverter" && d.enabled !== false,
    );
    const proposals = body.zendure_mqtt_proposals || [];
    const errors: { code: string; message: string }[] = [];
    const warnings: { code: string; message: string }[] = [];
    const configDevices = [
      ...draftInverters.map((d: { config_name: string; serial_number: string; ip: string }) => ({
        name: d.config_name,
        ip: d.ip,
        sn: d.serial_number,
        max_power: 800,
      })),
      ...proposals.map((p: { id: string; config_name: string }) => ({
        name: p.config_name,
        type: "zendure_mqtt",
        serial_number: String(p.id).replace("zendure-mqtt:", ""),
        capabilities: { write_output_limit: true },
      })),
    ];
    const seenSerials = new Map<string, number>();
    configDevices.forEach((d: { sn?: string; serial_number?: string }, i: number) => {
      const serial = (d.sn || d.serial_number || "").toLowerCase();
      if (!serial) return;
      if (seenSerials.has(serial)) {
        errors.push({ code: "zendure_device_identity_duplicate", message: "Configure each physical device only once." });
      } else {
        seenSerials.set(serial, i);
      }
    });
    if (draftInverters.length && !proposals.length) {
      warnings.push({
        code: "zendure_mqtt_cloud_devices_not_selected",
        message: "Zendure Cloud MQTT is connected but no Zendure MQTT device is selected.",
      });
    }
    return json(route, {
      ready: errors.length === 0,
      config: { devices: configDevices, grid_meter: { type: "shelly" } },
      summary: { inverters: draftInverters.length + proposals.length, grid_meters: 1 },
      release: "latest",
      base: { source: "template" },
      validation: { errors, warnings, info: [] },
    });
  });
}

async function reachDevices(
  page: Page,
  state: { mqttReady: boolean; priority: string[] },
  seed: (spec: Record<string, unknown>) => Promise<void>,
  setPriority: (priority: string[]) => Promise<void>,
) {
  await mockCloudAndPreview(page, state);
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seed({
    local_api_devices: [
      apiInverter(SERIAL_A, "192.168.100.78"),
      apiInverter(SERIAL_B, "192.168.100.79"),
      SHELLY_METER,
    ],
    ...(state.mqttReady
      ? { cloud_devices: [cloudInverter(SERIAL_A), cloudInverter(SERIAL_B)] }
      : {}),
  });
  await setPriority(state.priority);
  await page.reload();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
}

function inverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
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

test("late Zendure MQTT priority reconfigures the auto-added Local-API inverters", { tag: ["@maintenance"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  const state = { mqttReady: false, priority: ["local_api", "local_mqtt", "zendure_mqtt"] };
  await reachDevices(page, state, seedDiscoveryState, (priority) =>
    setDiscoveryPriority(page, priority),
  );

  // Initial discovery auto-adds two Local-API inverters + one Shelly grid meter.
  await page.locator('[data-setup-step="config"]').click();
  await expect(inverterCards(page)).toHaveCount(2);
  const initialCard = inverterCards(page).first();
  await expect(initialCard).toHaveAttribute("data-source-id", /^obs:v1:/);
  await initialCard.locator(".hardware-card-toggle").click();
  await expect(initialCard).toContainText("API");

  // Move Zendure MQTT to priority 1, then rescan it (proposals now arrive).
  await page.locator('[data-setup-step="devices"]').click();
  const zendureRow = page.locator('#discovery-priority-list [data-source="zendure_mqtt"]');
  await zendureRow.locator("[data-prep-up]").click();
  await zendureRow.locator("[data-prep-up]").click();

  // The Rescan control lives inside the source's inline config.
  await zendureRow.locator("[data-prep-configure]").click();
  state.mqttReady = true;
  // The credentials arrive late: the account's devices only now enter the
  // server's trusted candidate set.
  await seedDiscoveryState({
    cloud_devices: [cloudInverter(SERIAL_A), cloudInverter(SERIAL_B)],
  });
  const proposalsLoaded = page.waitForResponse(
    (r) => r.url().includes("/api/discovery/mqtt-proposals") && r.request().method() === "GET",
  );
  await zendureRow.locator("[data-prep-rescan]").click();
  await proposalsLoaded;

  // Config now shows both inverters over Zendure MQTT, no duplicate cards.
  await page.locator('[data-setup-step="config"]').click();
  await expect(inverterCards(page)).toHaveCount(2);
  const switchedCards = await inverterCards(page).all();
  for (const card of switchedCards) {
    await expect(card).toContainText("Zendure MQTT");
    await expect(card).not.toContainText("192.168.100.78");
  }
  await expect(switchedCards[0]).toContainText("INV_1");
  await expect(switchedCards[0]).toContainText(SERIAL_A);
  await expect(switchedCards[1]).toContainText("INV_2");
  await expect(switchedCards[1]).toContainText(SERIAL_B);
  await expect(page.locator("#config-preview")).toContainText('"name": "INV_1"');
  await expect(page.locator("#config-preview")).toContainText('"name": "INV_2"');
  await expect(page.locator("#config-preview")).not.toContainText(
    '"name": "Zendure SolarFlow 800 Pro 2"',
  );

  // The preview is ready with the MQTT devices; the silent-HTTP warning is gone
  // and Continue is enabled.
  await expect(page.locator("#config-preview-ready")).toHaveText(/Ready/i);
  await expect(page.locator("#config-validation")).not.toContainText("no Zendure MQTT device is selected");
  await expect(page.locator("#setup-next")).toBeEnabled();
});

test("Add more devices offers the API connection as an alternative", { tag: ["@maintenance"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  const state = { mqttReady: true, priority: ["zendure_mqtt", "local_api", "local_mqtt"] };
  await reachDevices(page, state, seedDiscoveryState, (priority) =>
    setDiscoveryPriority(page, priority),
  );

  // With Zendure MQTT prioritized from the start, the inverters are configured
  // over MQTT; the API connection stays offered in the candidate pool.
  await page.locator('[data-setup-step="config"]').click();
  await expect(inverterCards(page)).toHaveCount(2);
  await expect(inverterCards(page).first()).toContainText("INV_1");

  await openCandidatePool(page);
  const available = page.locator("#config-available-list");
  const apiCandidate = available.locator(
    '.hardware-card-inverter[data-candidate-state="alternative"]',
    { hasText: SERIAL_A },
  );
  await expect(apiCandidate).toContainText("Already configured as INV_1 via Zendure MQTT");
  await apiCandidate.getByRole("button", { name: "Use connection" }).click();

  // One logical inverter, same name, now over API.
  await expect(inverterCards(page)).toHaveCount(2);
  const switched = inverterCards(page).first();
  await expect(switched).toHaveAttribute("data-source-id", /^obs:v1:/);
  await expect(switched).toContainText("INV_1");
  await expect(switched).toContainText("API");

  // The Zendure MQTT connection for the same physical inverter is offered back
  // without deleting the inverter.
  await expect(
    available.locator(
      '.hardware-card-inverter[data-candidate-state="alternative"]',
      { hasText: SERIAL_A },
    ),
  ).toContainText("Already configured as INV_1 via API");
});
