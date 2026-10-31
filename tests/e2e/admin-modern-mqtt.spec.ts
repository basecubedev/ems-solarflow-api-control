// SPDX-License-Identifier: AGPL-3.0-or-later
// The built-in MQTT broker and the MQTT mapping in the Modern Admin console,
// against the deterministic test-mode Admin: the add-ons tab, the switch's
// confirmation, and a mapped device drafted under Devices whose server preview
// says why it would not publish yet. Nothing here starts a container.
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page } from "@playwright/test";

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
    await expect(page.getByTestId("mqtt-wizard")).toContainText("What does this device measure?");
    await page.getByTestId("mqtt-kind-other").click();
    const editor = page.getByTestId("mqtt-mapping-editor");
    await editor.getByTestId("mqtt-device-id").fill("balkon");
    await editor.getByTestId("mqtt-source-topic").fill("shellyplusplugs-balkon/status/switch:0");
    await editor.getByTestId("mqtt-source-path").fill("apower");
    await editor.getByTestId("mqtt-source-add").click();
    await expect(editor).toContainText("shellyplusplugs-balkon/status/switch:0 → apower");

    const answer = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/value-preview"));
    await editor.getByTestId("mqtt-device-preview").click();
    expect((await answer).ok()).toBeTruthy();
    await expect(page.getByTestId("mqtt-preview-silent")).toBeVisible();
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
});
