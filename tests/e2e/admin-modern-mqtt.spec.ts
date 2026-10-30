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
});
