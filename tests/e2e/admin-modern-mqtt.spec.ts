// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// The built-in MQTT broker and the MQTT mapping in the Modern Admin console,
// against the deterministic test-mode Admin: the add-ons tab, the switch's
// confirmation, and a mapped device drafted under Devices whose server preview
// says why it would not publish yet. Nothing here starts a container.
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page } from "@playwright/test";
import { routeRunnablePlan } from "./helpers/container-plan";

async function seededAt(page: Page, seed: (scenario: string) => Promise<void>, hash: string) {
  await page.goto("/");
  await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
  await page.fill("#admin-password", ADMIN_PASSWORD);
  await page.getByTestId("sign-in-submit").click();
  await expect(page.locator(".app")).toBeVisible();
  await seed("mixed_transports");
  await page.goto(`/${hash}`);
  await page.reload();
  await expect(page.locator(".app")).toBeVisible();
}

const liveValue = (path: string, value: number, likely: string | null = null) => ({ path, value, likely, mapped_to: [] });
const liveTopic = (name: string) => ({
  topic: name, format: "json", retained: false, messages_per_minute: 4, changing: true, last_seen_seconds: 1, first_seen_seconds: 60, payload_preview: "{}",
  values: [liveValue("apower", -412.3, "power"), liveValue("voltage", 230.1), liveValue("aenergy.total", 1234.5)],
});
const PLUGS = Array.from({ length: 14 }, (_, index) => {
  const key = `shellyplusplugs-${String(index + 1).padStart(2, "0")}`;
  return { key, label: "shelly_gen2", mappable: true, topics: [liveTopic(`${key}/status/switch:0`), liveTopic(`${key}/status/em:0`)] };
});

/** The built-in broker answers with fourteen plugs, each on two topics: a list far longer than a screen. */
async function fourteenPlugs(page: Page) {
  const builtin = { ref: "ems-mqtt", kind: "builtin", host: "192.0.2.40", port: 1883, state: "running", usable: true };
  await page.route("**/api/discovery/mqtt-mapping/brokers", (route) => route.fulfill({ json: { searching: false, brokers: [builtin] } }));
  await page.route("**/api/discovery/mqtt-topics/sessions", (route) => route.fulfill({ json: { ok: true, session: "plugs" } }));
  await page.route("**/api/discovery/mqtt-topics/sessions/plugs", (route) =>
    route.fulfill({ json: { session: "plugs", broker_ref: "ems-mqtt", state: "listening", truncated: false, groups: PLUGS } }),
  );
}

type Seed = (scenario: string) => Promise<void>;
type SeedState = (state: { local_mqtt_brokers: unknown[] }) => Promise<void>;

/**
 * The fourteen plugs with a mapping saved in the browser and its EMS devices
 * written by the test Admin, against a container plan that can restart EMS.
 * ``sync(attempt)`` answers each restart; ``ems`` names the test Admin's
 * scenario of the EMS that runs (``ems_started`` names what it read,
 * ``older_ems_started`` is one of an earlier release that says neither when it
 * started nor what it read, ``later_ems_started`` one of a later release that
 * names it by a rule this Admin cannot compare), seeded at the start and,
 * unless ``reseeds`` is false, again by every restart that went well.
 */
async function mappingWithRestart(page: Page, seed: Seed, seedState: SeedState, sync: (attempt: number) => { status?: number; json: unknown }, ems: string, reseeds = true) {
  await fourteenPlugs(page);
  let saved: unknown = null;
  let written = 0;
  let restarted = 1;
  let attempts = 0;
  const observed = (id: string) => ({
    broker_id: "mqtt:192.0.2.41:1883", broker_host: "192.0.2.41", broker_port: 1883, source_type: "local_mqtt", topic_family: "ems_solarflow",
    device_id: id, serial_number: null, display_name: "External device", metrics_seen: ["outputHomePower"], topics_seen: [`ems-solarflow/${id}/state`],
  });
  await page.route("**/api/discovery/mqtt-proposals**", async (route) => {
    const response = await route.fetch();
    const answer = await response.json();
    const proposals = (answer.proposals || []).map((proposal: { device_id?: string }) => ({ ...proposal, mapping_broker_ref: "ems-mqtt" }));
    await route.fulfill({ response, json: { ...answer, proposals } });
  });
  await page.route("**/api/discovery/mqtt-mapping/preview", (route) => route.fulfill({ json: { ok: true, issues: [] } }));
  await page.route("**/api/discovery/mqtt-mapping/apply", (route) => {
    saved = route.request().postDataJSON().document;
    return route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/discovery/mqtt-mapping", async (route) => {
    const response = await route.fetch();
    const view = await response.json();
    if (saved) view.document = saved;
    view.taken_ids = {};
    await route.fulfill({ response, json: view });
  });
  await page.route("**/api/discovery/mqtt-topics/sessions/plugs", (route) => {
    const devices = (saved as { devices?: Array<{ id: string; values?: Record<string, { sources?: Array<{ topic: string; path?: string }> }> }> } | null)?.devices || [];
    const reads = (topic: string, path: string) => [...new Set(devices.filter((device) => Object.values(device.values || {}).some((spec) => (spec.sources || []).some((source) => source.topic === topic && (source.path ?? "") === path))).map((device) => device.id))];
    const groups = PLUGS.map((group) => ({ ...group, topics: group.topics.map((topic) => ({ ...topic, values: topic.values.map((value) => ({ ...value, mapped_to: reads(topic.topic, value.path) })) })) }));
    return route.fulfill({ json: { session: "plugs", broker_ref: "ems-mqtt", state: "listening", truncated: false, groups } });
  });
  await page.route("**/api/admin/maintenance/config/apply", async (route) => {
    const response = await route.fetch();
    written = Date.now();
    await route.fulfill({ response });
  });
  await routeRunnablePlan(page, () => ({ state: restarted > written ? "current" : "pending" }));
  await page.route("**/api/admin/maintenance/containers/sync", async (route) => {
    attempts += 1;
    const answer = sync(attempts);
    if (!answer.status && reseeds) await seed(ems);
    if (!answer.status) restarted = Date.now();
    return route.fulfill(answer);
  });
  await seededAt(page, seed, "#/settings/devices");
  await seed(ems);
  await seedState({ local_mqtt_brokers: [{ host: "192.0.2.41", port: 1883, devices: ["balcony-west", "garage", "shed", "carport"].map(observed) }] });
  await page.getByTestId("mqtt-topics-start").click();
  const map = async (key: string, name: string) => {
    await page.locator(`li[data-label="shelly_gen2"][data-key="${key}"]`).getByTestId("mqtt-map-group").click();
    const dialog = page.getByRole("dialog", { name: `Map ${key} as an external device` });
    await dialog.getByTestId("mqtt-kind-balcony").check();
    await dialog.getByTestId("mqtt-wizard-name").fill(name);
    await dialog.getByTestId("mqtt-wizard-save").click();
    await page.getByRole("dialog", { name: "Apply the mapping?" }).getByTestId("confirm").click();
    return dialog;
  };
  return {
    map,
    rowOf: (id: string) => page.getByTestId("mqtt-mapped-devices").locator(`li[data-id="${id}"]`),
    offer: page.getByTestId("mqtt-mapping").getByTestId("restart-offer"),
  };
}

const syncFailsFirst = (attempt: number) =>
  attempt === 1
    ? { status: 500, json: { ok: false, status: "error", error: "sync_failed", message: "docker compose up failed" } }
    : { json: { ok: true, status: "completed", steps: [{ service: "ems", action: "recreate", status: "ok" }], warnings: attempt === 3 ? ["influxdb: not started"] : [] } };

test.describe("Modern Admin: built-in MQTT broker and mapping", () => {
  test("the add-ons tab shows the broker switched off and asks before switching it on", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/addons");
    const broker = page.getByTestId("mqtt-broker");
    await expect(broker).toBeVisible();
    const enable = broker.getByTestId("mqtt-broker-enable");
    await expect(enable).toBeEnabled();

    await enable.click();
    const dialog = page.locator("dialog[open]");
    await expect(dialog).toContainText("Mosquitto");
    await page.keyboard.press("Escape");
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(enable).toBeVisible();
  });

  test("a mapped device is drafted under Devices and the server preview says why it would not publish yet", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const mapping = page.getByTestId("mqtt-mapping");
    await expect(mapping).toBeVisible();

    await mapping.getByTestId("mqtt-new-device").click();
    const dialog = page.getByRole("dialog", { name: "New external device" });
    await expect(dialog.getByTestId("mqtt-wizard")).toContainText("What does this device measure?");
    await dialog.getByTestId("mqtt-kind-other").click();
    const editor = dialog.getByTestId("mqtt-mapping-editor");
    await editor.getByTestId("mqtt-device-id").fill("balkon");
    await editor.getByTestId("mqtt-source-topic").fill("shellyplusplugs-balkon/status/switch:0");
    await editor.getByTestId("mqtt-source-path").fill("apower");
    await editor.getByTestId("mqtt-source-add").click();
    await expect(editor).toContainText("shellyplusplugs-balkon/status/switch:0 → apower");

    const answer = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/value-preview"));
    await dialog.getByTestId("mqtt-device-preview").click();
    expect((await answer).ok()).toBeTruthy();
    await expect(dialog.getByTestId("mqtt-preview-silent")).toBeInViewport();
  });

  test("a device is connected step by step and, once it publishes, set up by answering what it measures", async ({ page, seedAdminScenario }) => {
    const broker = {
      configured: true, issues: [], running: true, profile_ref: "ems-mqtt", profile_users: [], grid_meter_topic: null, host_address: "192.0.2.40",
      settings: { schema_version: 1, enabled: true, port: 1883, anonymous: false, accounts: [{ name: "devices", credentials_ref: "mqtt-broker-devices", topic_prefix: null, bridge: false, zendure: false }], grid_meter_account: null },
      accounts: [{ name: "devices", topic_prefix: null, bridge: false, zendure: false, password_saved: true }],
      containers: { "ems-mqtt-broker": "running", "ems-mqtt-mapping": null },
      stop_impact: { external_devices: [], zendure_devices: [], grid_meter: false },
    };
    const topic = (name: string, since: number) => ({ topic: name, format: "json", retained: false, messages_per_minute: 4, changing: true, last_seen_seconds: 1, first_seen_seconds: (Date.now() - since) / 1000, payload_preview: "{}", values: [{ path: "apower", value: -612.3, likely: "power", mapped_to: [] }, { path: "voltage", value: 230.1, likely: null, mapped_to: [] }] });
    let opened = 0;
    let joined = 0;
    let polls = 0;
    await page.route("**/api/discovery/mqtt-broker", (route) => route.fulfill({ json: broker }));
    await page.route("**/api/discovery/mqtt-topics/sessions", (route) => {
      opened = Date.now();
      route.fulfill({ json: { ok: true, session: "e2e" } });
    });
    await page.route("**/api/discovery/mqtt-topics/sessions/e2e", (route) => {
      polls += 1;
      if (polls === 3) joined = Date.now();
      const old = { key: "shellyplus1pm-oldone", label: "shelly_gen2", mappable: true, topics: [topic("shellyplus1pm-oldone/status/switch:0", opened)] };
      const plug = { key: "shellyplusplugs-e2etest", label: "shelly_gen2", mappable: true, topics: [topic("shellyplusplugs-e2etest/status/switch:0", joined)] };
      route.fulfill({ json: { session: "e2e", broker_ref: "ems-mqtt", state: "listening", truncated: false, groups: polls >= 3 ? [old, plug] : [old] } });
    });
    await seededAt(page, seedAdminScenario, "#/settings/addons");

    await page.getByTestId("mqtt-connect").click();
    const guide = page.getByTestId("mqtt-connect-guide");
    await expect(guide).toContainText("Generic status update over MQTT");
    await guide.getByLabel("Tasmota").check();
    await expect(guide).toContainText("Configuration → Configure MQTT.");
    await expect(guide).toContainText("192.0.2.40");
    await page.route("**/api/discovery/mqtt-broker/reveal", (route) => route.fulfill({ json: { ok: true, password: "demo-device-password" } }));
    await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true }));
    await guide.getByRole("button", { name: "Copy" }).last().click();
    await expect(guide.getByTestId("mqtt-connect-copy-failed")).toBeVisible();
    await expect(guide.getByTestId("mqtt-connect-password")).toHaveText("demo-device-password");
    await guide.getByTestId("mqtt-connect-wait").click();
    await expect(guide.getByTestId("mqtt-connect-found")).toContainText("shellyplusplugs-e2etest");
    await expect(guide.getByTestId("mqtt-connect-found")).not.toContainText("shellyplus1pm-oldone");
    await guide.getByTestId("mqtt-connect-setup").click();

    await expect(page).toHaveURL(/#\/settings\/devices$/);
    const wizard = page.getByTestId("mqtt-wizard");
    await expect(wizard).toContainText("What does this device measure?");
    await wizard.getByTestId("mqtt-kind-balcony").check();
    await wizard.getByTestId("mqtt-wizard-name").fill("Balcony South");
    await expect(wizard.getByTestId("mqtt-wizard-power")).toHaveValue("shellyplusplugs-e2etest/status/switch:0|apower");
    await expect(wizard.getByTestId("mqtt-wizard-turned")).toBeVisible();
    await expect(wizard).toContainText("Sends only on change");
    await wizard.getByText("Advanced").click();
    await expect(wizard.getByTestId("mqtt-wizard-id")).toHaveValue("balcony-south");
  });

  test("anonymous access is asked for first, and each account sets its own password in its row", async ({ page, seedAdminScenario }) => {
    const broker = {
      configured: true, issues: [], running: true, profile_ref: "ems-mqtt", profile_users: [], grid_meter_topic: null, host_address: "192.0.2.40",
      settings: { schema_version: 1, enabled: true, port: 1883, anonymous: false, accounts: [{ name: "devices", credentials_ref: "mqtt-broker-devices", topic_prefix: null, bridge: false, zendure: false }], grid_meter_account: null },
      accounts: [{ name: "devices", topic_prefix: null, bridge: false, zendure: false, password_saved: true }],
      containers: { "ems-mqtt-broker": "running", "ems-mqtt-mapping": null },
      stop_impact: { external_devices: [], zendure_devices: [], grid_meter: false },
    };
    let changes = 0;
    await page.route("**/api/discovery/mqtt-broker", (route) => route.fulfill({ json: broker }));
    await page.route("**/api/discovery/mqtt-broker/settings", (route) => {
      changes += 1;
      route.fulfill({ json: { ok: true, status: "saved" } });
    });
    await seededAt(page, seedAdminScenario, "#/settings/addons");

    await page.getByTestId("mqtt-broker-anonymous").click();
    const dialog = page.locator("dialog[open]");
    await expect(dialog).toContainText("Every device on your network can then read and publish without an account.");
    await expect(dialog).toContainText("ems-solarflow/");
    await page.keyboard.press("Escape");
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    expect(changes).toBe(0);

    const row = page.getByTestId("mqtt-broker-accounts").locator("li").filter({ hasText: "devices" });
    await expect(row.getByTestId("mqtt-account-password-input")).toHaveCount(0);
    await row.getByTestId("mqtt-account-own-password").click();
    await expect(row.getByTestId("mqtt-account-password-input")).toBeVisible();
  });

  test("a mapped device keeps its id when it is edited", async ({ page, seedAdminScenario }) => {
    await page.route("**/api/discovery/mqtt-mapping", async (route) => {
      const response = await route.fetch();
      const view = await response.json();
      view.document = { schema_version: 1, devices: [{ id: "balkon", label: "Balkon", broker_ref: "ems-mqtt", values: { inverterPower: { sources: [{ topic: "plug/status", format: "json", path: "apower" }] } } }] };
      await route.fulfill({ response, json: view });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const mapping = page.getByTestId("mqtt-mapping");

    await mapping.getByRole("button", { name: "Edit" }).click();

    const id = page.getByTestId("mqtt-mapping-editor").getByTestId("mqtt-device-id");
    await expect(id).toHaveValue("balkon");
    await expect(id).toHaveAttribute("readonly", "");
  });

  test("with the built-in broker off, a broker found on the network is offered and takes its account", async ({ page, seedAdminScenario }) => {
    const builtin = { ref: "ems-mqtt", kind: "builtin", host: "192.0.2.40", port: 1883, state: "off", usable: false };
    const found = { ref: "broker-192-0-2-7-1883", kind: "found", host: "192.0.2.7", port: 1883, tls: false, origin: "discovery", access: "needs_account", usable: false };
    let accounts: { ref: string; username: string; password: string }[] = [];
    await page.route("**/api/discovery/mqtt-mapping/brokers", (route) =>
      route.fulfill({ json: { searching: false, brokers: [builtin, accounts.length ? { ...found, access: "account", usable: true } : found] } }),
    );
    await page.route("**/api/discovery/mqtt-mapping/brokers/account", async (route) => {
      accounts.push(route.request().postDataJSON());
      await route.fulfill({ json: { ok: true, ref: found.ref } });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const chooser = page.getByTestId("mqtt-broker-chooser");

    await expect(chooser.getByTestId("mqtt-chooser-none")).toBeVisible();
    await expect(chooser.getByTestId("mqtt-chooser-enable")).toBeVisible();
    await expect(page.getByTestId("mqtt-topics-start")).toBeDisabled();
    const row = chooser.locator('[data-testid="mqtt-broker-choice"][data-ref="broker-192-0-2-7-1883"]');
    await expect(row).toContainText("Needs an account");

    await row.getByTestId("mqtt-chooser-account").click();
    await row.getByTestId("mqtt-chooser-user").fill("iob");
    await row.getByTestId("mqtt-chooser-password").fill("iob-password");
    await row.getByTestId("mqtt-chooser-account-use").click();

    await expect(row).toContainText("Account works");
    await expect(row.locator("input[type=radio]")).toBeChecked();
    await expect(chooser.getByTestId("mqtt-chooser-none")).toHaveCount(0);
    await expect(page.getByTestId("mqtt-topics-start")).toBeEnabled();
    expect(accounts).toEqual([{ ref: "broker-192-0-2-7-1883", username: "iob", password: "iob-password" }]);
  });

  test("an EMS output another broker carries too names that broker in the live topics", async ({ page, seedAdminScenario }) => {
    const builtin = { ref: "ems-mqtt", kind: "builtin", host: "192.0.2.40", port: 1883, state: "running", usable: true };
    await page.route("**/api/discovery/mqtt-mapping/brokers", (route) => route.fulfill({ json: { searching: false, brokers: [builtin] } }));
    await page.route("**/api/discovery/mqtt-topics/sessions", (route) => route.fulfill({ json: { ok: true, session: "twice" } }));
    await page.route("**/api/discovery/mqtt-topics/sessions/twice", (route) =>
      route.fulfill({
        json: {
          session: "twice", broker_ref: "ems-mqtt", state: "listening", truncated: false,
          groups: [{ key: "balkon", label: "ems_output", mappable: false, also_on: ["192.0.2.99:1883"], topics: [{ topic: "ems-solarflow/balkon/state", format: "json", retained: true, messages_per_minute: 4, changing: true, last_seen_seconds: 1, first_seen_seconds: 60, payload_preview: "{}", values: [] }] }],
        },
      }),
    );
    await seededAt(page, seedAdminScenario, "#/settings/devices");

    await page.getByTestId("mqtt-topics-start").click();

    const hint = page.locator('li[data-label="ems_output"][data-key="balkon"]').getByTestId("mqtt-topic-also-on");
    await expect(hint).toContainText("192.0.2.99:1883");
    await expect(hint).toContainText("only from the broker it was added through");
  });

  test("a device far down a long list of live topics is mapped in a dialog the reader sees", async ({ page, seedAdminScenario }) => {
    await fourteenPlugs(page);
    let saved: unknown = null;
    await page.route("**/api/discovery/mqtt-mapping/preview", (route) => route.fulfill({ json: { ok: true, issues: [] } }));
    await page.route("**/api/discovery/mqtt-mapping/apply", (route) => {
      saved = route.request().postDataJSON().document;
      return route.fulfill({ json: { ok: true } });
    });
    await page.route("**/api/discovery/mqtt-mapping", async (route) => {
      const response = await route.fetch();
      const view = await response.json();
      if (saved) view.document = saved;
      await route.fulfill({ response, json: view });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("mqtt-topics-start").click();

    await page.locator('li[data-label="shelly_gen2"][data-key="shellyplusplugs-12"]').getByTestId("mqtt-map-group").click();

    await expect(page.getByTestId("mqtt-wizard")).toBeInViewport();
    const dialog = page.getByRole("dialog", { name: "Map shellyplusplugs-12 as an external device" });
    await expect(dialog.getByTestId("mqtt-wizard")).toBeVisible();
    await dialog.getByTestId("mqtt-kind-balcony").check();
    await expect(dialog.getByTestId("mqtt-wizard-preview")).toBeEnabled();
    await expect(dialog.getByTestId("mqtt-wizard-save")).toBeDisabled();
    await dialog.getByTestId("mqtt-wizard-name").fill("Balcony West");
    await expect(dialog.getByTestId("mqtt-wizard-power")).toHaveValue("shellyplusplugs-12/status/switch:0|apower");
    await dialog.getByTestId("mqtt-wizard-save").click();
    await page.getByRole("dialog", { name: "Apply the mapping?" }).getByTestId("confirm").click();

    await expect(dialog).toBeHidden();
    const added = page.getByTestId("mqtt-mapped-devices").locator('li[data-id="balcony-west"]');
    await expect(added).toBeInViewport();
    await expect(added).toBeFocused();
  });

  test("a device saved with its EMS device ends in a step that restarts EMS in place and says what the running EMS reads", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    test.setTimeout(120_000);
    const { map, rowOf, offer } = await mappingWithRestart(page, seedAdminScenario, seedDiscoveryState, syncFailsFirst, "ems_started");

    const first = await map("shellyplusplugs-12", "Balcony West");
    const step = first.getByTestId("mqtt-saved");
    await expect(step).toContainText("Saved and added to the EMS", { timeout: 30_000 });
    await expect(step).toBeInViewport();
    await expect(step).toContainText("Balcony West becomes active once EMS restarts.");
    await expect(step.getByTestId("mqtt-saved-waiting")).toHaveCount(0);
    await expect(first.getByTestId("restart-ems")).toBeFocused();
    await first.getByTestId("restart-ems").click();
    await expect(first.getByTestId("services-result")).toHaveClass(/fault/);
    await expect(first.getByTestId("restart-ems")).toBeFocused();
    await first.getByTestId("restart-ems").click();
    await expect(first.getByTestId("mqtt-saved-live")).toHaveText("EMS has restarted and reads Balcony West now.", { timeout: 15_000 });
    await expect(first.getByTestId("services-result")).toBeVisible();
    await expect(first.getByTestId("mqtt-saved-done")).toBeFocused();
    await first.getByTestId("mqtt-saved-done").click();
    await expect(first).toBeHidden();
    await expect(rowOf("balcony-west")).toBeInViewport();
    await expect(rowOf("balcony-west")).toBeFocused();
    await expect(rowOf("balcony-west").getByTestId("mqtt-adopted")).toHaveText("Active in the EMS");

    const rename = async (name: string) => {
      await rowOf("balcony-west").getByRole("button", { name: "Edit" }).click();
      const dialog = page.getByRole("dialog", { name: "Edit balcony-west" });
      await dialog.getByTestId("mqtt-mapping-editor").getByLabel("Name").fill(name);
      await dialog.getByTestId("mqtt-device-save").click();
      await page.getByRole("dialog", { name: "Apply the mapping?" }).getByTestId("confirm").click();
      return dialog;
    };
    const east = await rename("Balcony East");
    await expect(east.getByTestId("mqtt-saved")).toContainText("Saved; its EMS device is renamed", { timeout: 30_000 });
    await expect(east.getByTestId("mqtt-saved")).toContainText("EMS shows the new name Balcony East once it restarts.");
    await east.getByRole("button", { name: "Close" }).click();
    await expect(rowOf("balcony-west").getByTestId("mqtt-adopted")).toHaveText("Waits for an EMS restart");
    await expect(offer).toContainText("Waiting for an EMS restart: Balcony East (renamed)");
    const west = await rename("Balcony West");
    await expect(west).toBeHidden();
    await expect(rowOf("balcony-west")).toBeFocused();
    await expect(rowOf("balcony-west").getByTestId("mqtt-adopted")).toHaveText("Active in the EMS");

    const second = await map("shellyplusplugs-05", "Garage");
    await expect(second.getByTestId("mqtt-saved")).toContainText("Saved and added to the EMS", { timeout: 30_000 });
    await expect(second.getByTestId("mqtt-saved-live")).toHaveCount(0);
    await expect(second.getByTestId("mqtt-saved-waiting")).toHaveCount(0);
    await second.getByTestId("mqtt-saved-another").click();
    await expect(second).toBeHidden();
    const search = page.getByTestId("mqtt-topics-filter");
    await expect(search).toBeFocused();
    await expect(search).toBeInViewport();
    await expect(rowOf("garage").getByTestId("mqtt-adopted")).toHaveText("Waits for an EMS restart");
    await expect(rowOf("balcony-west").getByTestId("mqtt-adopted")).toHaveText("Active in the EMS");
    await expect(page.locator('li[data-key="shellyplusplugs-05"]').getByText("Mapped: garage · Waits for an EMS restart")).toBeVisible();
    await expect(page.locator('li[data-key="shellyplusplugs-12"]').getByText("Mapped: balcony-west", { exact: true })).toBeVisible();

    const third = await map("shellyplusplugs-07", "Shed");
    await expect(third.getByTestId("mqtt-saved-waiting")).toHaveText("Waiting for an EMS restart: Garage, Shed", { timeout: 30_000 });
    await third.getByRole("button", { name: "Close" }).click();
    await expect(third).toBeHidden();
    await expect(rowOf("shed")).toBeInViewport();
    await expect(rowOf("shed")).toBeFocused();
    await expect(offer).toContainText("Waiting for an EMS restart: Garage, Shed");

    await rowOf("shed").getByRole("button", { name: "Remove" }).click();
    const removal = page.getByRole("dialog", { name: "Remove the mapping?" });
    await removal.getByTestId("mqtt-remove-ems").uncheck();
    await removal.getByTestId("confirm").click();
    await expect(rowOf("shed")).toHaveCount(0);
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await page.locator('li[data-label="shelly_gen2"][data-key="shellyplusplugs-07"]').getByTestId("mqtt-map-group").click();
    const again = page.getByRole("dialog", { name: "Map shellyplusplugs-07 as an external device" });
    await again.getByTestId("mqtt-kind-balcony").check();
    await again.getByTestId("mqtt-wizard-name").fill("Shed");
    await again.getByTestId("mqtt-wizard-save").click();
    await page.getByRole("dialog", { name: "Apply the mapping?" }).getByTestId("confirm").click();
    await expect(again).toBeHidden();
    await expect(rowOf("shed")).toBeFocused();
    await expect(rowOf("shed").getByTestId("mqtt-adopted")).toHaveText("Waits for an EMS restart");

    await rowOf("balcony-west").getByRole("button", { name: "Remove" }).click();
    await page.getByRole("dialog", { name: "Remove the mapping?" }).getByTestId("confirm").click();
    const removed = page.getByRole("dialog", { name: "Remove Balcony West" });
    await expect(removed.getByTestId("mqtt-saved")).toContainText("Removed from the mapping and from the EMS", { timeout: 30_000 });
    await expect(removed.getByTestId("mqtt-saved")).toContainText("EMS keeps reading Balcony West until it restarts.");
    await removed.getByTestId("mqtt-saved-later").click();
    await expect(offer.getByTestId("restart-ems")).toBeFocused();
    await expect(offer).toContainText("Balcony West (removed)");
    const back = await map("shellyplusplugs-12", "Balcony West");
    await expect(back).toBeHidden({ timeout: 30_000 });
    await expect(rowOf("balcony-west").getByTestId("mqtt-adopted")).toHaveText("Active in the EMS");
    await expect(offer).not.toContainText("Balcony West");

    const fourth = await map("shellyplusplugs-09", "Carport");
    await expect(fourth.getByTestId("mqtt-saved-waiting")).toHaveText("Waiting for an EMS restart: Carport, Garage, Shed", { timeout: 30_000 });
    await fourth.getByTestId("restart-ems").click();
    await expect(fourth.getByTestId("mqtt-saved-live")).toHaveText("EMS has restarted and reads Carport now.", { timeout: 15_000 });
    await expect(fourth.getByTestId("services-result")).toHaveClass(/partial/);
    await expect(fourth.getByTestId("mqtt-saved-done")).toBeFocused();
    await fourth.getByTestId("mqtt-saved-done").click();
    for (const id of ["carport", "garage", "shed", "balcony-west"]) await expect(rowOf(id).getByTestId("mqtt-adopted")).toHaveText("Active in the EMS");
    await expect(offer.getByTestId("services-result")).toHaveClass(/partial/);
    await expect(offer).not.toContainText("Waiting for an EMS restart");
    await expect(offer).not.toContainText("Some changes take effect once EMS restarts.");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a restart that went well, after which an EMS of a later release that cannot tell what it reads runs, ends in the step saying so at once", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    const { map, rowOf } = await mappingWithRestart(page, seedAdminScenario, seedDiscoveryState, () => ({ json: { ok: true, status: "completed", steps: [{ service: "ems", action: "recreate", status: "ok" }], warnings: [] } }), "later_ems_started");

    const step = await map("shellyplusplugs-03", "Garage");
    await expect(step.getByTestId("mqtt-saved")).toContainText("Garage becomes active once EMS restarts.", { timeout: 30_000 });
    await expect(step.getByTestId("mqtt-saved-silent")).toHaveCount(0);
    await step.getByTestId("restart-ems").click();
    await expect(step.getByTestId("mqtt-saved-silent")).toContainText("The running EMS does not tell this console what it reads: it is of another release.", { timeout: 15_000 });
    await expect(step.getByTestId("mqtt-saved-confirming")).toHaveCount(0);
    await expect(step.getByTestId("mqtt-saved-done")).toHaveCount(0);
    await expect(step.getByTestId("mqtt-saved-another")).toBeFocused();
    await step.getByTestId("mqtt-saved-another").click();
    await expect(step).toBeHidden();
    await expect(rowOf("garage").getByTestId("mqtt-adopted")).toHaveText("In the EMS");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a restart that went well, after which an EMS of an earlier release runs that says neither when it started nor what it reads, ends in the step saying so once the grace is over", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    await page.clock.install();
    const { map } = await mappingWithRestart(page, seedAdminScenario, seedDiscoveryState, () => ({ json: { ok: true, status: "completed", steps: [{ service: "ems", action: "recreate", status: "ok" }], warnings: [] } }), "older_ems_started");

    const step = await map("shellyplusplugs-03", "Garage");
    await expect(step.getByTestId("mqtt-saved")).toContainText("Garage becomes active once EMS restarts.", { timeout: 30_000 });
    await step.getByTestId("restart-ems").click();
    await expect(step.getByTestId("mqtt-saved-confirming")).toBeVisible({ timeout: 15_000 });
    await expect(step.getByTestId("mqtt-saved-silent")).toHaveCount(0);
    await page.clock.fastForward("02:05");
    await expect(step.getByTestId("mqtt-saved-silent")).toContainText("The running EMS does not tell this console what it reads: it is of another release.", { timeout: 15_000 });
    await expect(step.getByTestId("mqtt-saved-confirming")).toHaveCount(0);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a restart that went well, after which no restarted EMS reports for two minutes, ends in the step saying it may not run", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    await page.clock.install();
    const { map } = await mappingWithRestart(page, seedAdminScenario, seedDiscoveryState, () => ({ json: { ok: true, status: "completed", steps: [{ service: "ems", action: "recreate", status: "ok" }], warnings: [] } }), "ems_started", false);

    const step = await map("shellyplusplugs-03", "Garage");
    await expect(step.getByTestId("mqtt-saved")).toContainText("Garage becomes active once EMS restarts.", { timeout: 30_000 });
    await step.getByTestId("restart-ems").click();
    await expect(step.getByTestId("mqtt-saved-confirming")).toBeVisible({ timeout: 15_000 });
    await expect(step.getByTestId("mqtt-saved-silent")).toHaveCount(0);
    await page.clock.fastForward("02:05");
    await expect(step.getByTestId("mqtt-saved-silent")).toContainText("The restarted EMS has not said what it reads for two minutes", { timeout: 15_000 });
    await expect(step.getByTestId("mqtt-saved-confirming")).toHaveCount(0);
    await expect(step.getByTestId("mqtt-saved-done")).toHaveCount(0);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("the free editor a listed device hands over to picks its values in the same dialog", async ({ page, seedAdminScenario }) => {
    await fourteenPlugs(page);
    await page.route("**/api/discovery/mqtt-mapping/value-preview", (route) =>
      route.fulfill({ json: { ok: true, issues: [], evaluation: { state: { inverterPower: 412.3, solarPower: 412.3 }, reason: null, targets: [] } } }),
    );
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("mqtt-topics-start").click();

    await page.locator('li[data-label="shelly_gen2"][data-key="shellyplusplugs-12"]').getByTestId("mqtt-map-group").click();
    const dialog = page.getByRole("dialog", { name: "Map shellyplusplugs-12 as an external device" });
    await dialog.getByTestId("mqtt-kind-balcony").check();
    await dialog.getByTestId("mqtt-wizard-preview").click();
    await expect(dialog.getByTestId("mqtt-preview-state")).toBeVisible();
    await dialog.getByTestId("mqtt-kind-other").click();
    await expect(dialog.getByTestId("mqtt-mapping-editor")).toBeVisible();
    await expect(dialog.getByTestId("mqtt-preview-state")).toHaveCount(0);

    const editor = dialog.getByTestId("mqtt-mapping-editor");
    const own = dialog.locator('li[data-key="shellyplusplugs-12"]');
    await expect(dialog.locator("li.group").first()).toHaveAttribute("data-key", "shellyplusplugs-12");
    const use = own.locator(".value-row").filter({ hasText: "apower" }).first().getByTestId("mqtt-use-value");
    await use.click();
    await expect(editor).toContainText("shellyplusplugs-12/status/switch:0 → apower");
    await expect(use).toHaveText("Used as Inverter power");
    await expect(use).toBeDisabled();
    await expect(dialog.locator('li[data-key="shellyplusplugs-11"] .value-row:visible')).toHaveCount(0);
  });

  test("a mapping cancelled while its save is being checked asks nothing afterwards", async ({ page, seedAdminScenario }) => {
    await fourteenPlugs(page);
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/discovery/mqtt-mapping/preview", async (route) => {
      await held;
      await route.fulfill({ json: { ok: true, issues: [] } });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("mqtt-topics-start").click();
    const row = page.locator('li[data-label="shelly_gen2"][data-key="shellyplusplugs-05"]');
    await row.getByTestId("mqtt-map-group").click();
    const dialog = page.getByRole("dialog", { name: "Map shellyplusplugs-05 as an external device" });
    await dialog.getByTestId("mqtt-kind-balcony").check();
    await dialog.getByTestId("mqtt-wizard-name").fill("Garage");

    const checking = page.waitForRequest("**/api/discovery/mqtt-mapping/preview");
    await dialog.getByTestId("mqtt-wizard-save").click();
    await checking;
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
    await expect(row.getByTestId("mqtt-map-group")).toBeDisabled();
    release();

    await expect(row.getByTestId("mqtt-map-group")).toBeEnabled();
    await expect(page.getByRole("dialog", { name: "Apply the mapping?" })).toHaveCount(0);
  });

  test("a preview still on its way when the free editor takes over says nothing there", async ({ page, seedAdminScenario }) => {
    await fourteenPlugs(page);
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/discovery/mqtt-mapping/value-preview", async (route) => {
      await held;
      await route.fulfill({ json: { ok: true, issues: [], evaluation: { state: { inverterPower: 412.3, solarPower: 412.3 }, reason: null, targets: [] } } });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("mqtt-topics-start").click();
    const row = page.locator('li[data-label="shelly_gen2"][data-key="shellyplusplugs-07"]');
    await row.getByTestId("mqtt-map-group").click();
    const dialog = page.getByRole("dialog", { name: "Map shellyplusplugs-07 as an external device" });
    await dialog.getByTestId("mqtt-kind-balcony").check();

    const asked = page.waitForRequest("**/api/discovery/mqtt-mapping/value-preview");
    await dialog.getByTestId("mqtt-wizard-preview").click();
    await asked;
    await dialog.getByTestId("mqtt-kind-other").click();
    await expect(dialog.getByTestId("mqtt-mapping-editor")).toBeVisible();
    release();

    await expect(row.getByTestId("mqtt-map-group")).toBeEnabled();
    await expect(dialog.getByTestId("mqtt-preview-state")).toHaveCount(0);
  });

  test("live topics start folded, open one at a time or all at once, and narrow to what the filter names", async ({ page, seedAdminScenario }) => {
    await fourteenPlugs(page);
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("mqtt-topics-start").click();
    const list = page.getByTestId("mqtt-topic-groups");
    const third = list.locator('li[data-key="shellyplusplugs-03"]');
    await expect(list.locator("li.group")).toHaveCount(14);

    await expect(third).toContainText("2 topics");
    await expect(third).toContainText("apower -412.3");
    await expect(third).toContainText("Not mapped");
    await expect(list.locator(".value-row:visible")).toHaveCount(0);

    const fold = third.getByRole("button", { name: /shellyplusplugs-03/ });
    await fold.click();
    await expect(fold).toHaveAttribute("aria-expanded", "true");
    await expect(third.locator(".value-row:visible")).toHaveCount(6);
    await expect(list.locator(".value-row:visible")).toHaveCount(6);

    const all = page.getByTestId("mqtt-topics-toggle-all");
    await expect(all).toHaveText("Expand all");
    await all.click();
    await expect(list.locator(".value-row:visible")).toHaveCount(14 * 6);
    await expect(all).toHaveText("Collapse all");
    await all.click();
    await expect(list.locator(".value-row:visible")).toHaveCount(0);

    const filter = page.getByTestId("mqtt-topics-filter");
    await filter.fill("plugs-07");
    await expect(list.locator("li.group")).toHaveCount(1);
    await expect(list.locator("li.group")).toHaveAttribute("data-key", "shellyplusplugs-07");
    await filter.fill("nothing-like-this");
    await expect(list.locator("li.group")).toHaveCount(0);
    await expect(page.getByTestId("mqtt-topics-no-match")).toBeVisible();
  });
});
