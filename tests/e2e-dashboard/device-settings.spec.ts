// SPDX-License-Identifier: AGPL-3.0-or-later
// Light and fan on the inverter that has them, and on no other: the preview
// scenario's WR1 reports both, WR2 neither. What is checked here is what only a
// browser shows -- which form carries the selects, what Save asks before it
// sends, and what it sends.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream.
import { expect, test } from "@playwright/test";

const SIGNED_IN = {
  auth_configured: true,
  authenticated: true,
  csrf_token: "preview-csrf-token",
  write_mode_available: true,
  write_mode_active: true,
};

function deviceForm(page, name) {
  return page.locator(`form[data-runtime-endpoint="/api/runtime/device/${name}"]`);
}

test.describe("device settings", () => {
  test("only the inverter that has them offers light and fan, and a change asks first", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await page.goto("/preview/control", { waitUntil: "domcontentloaded" });

    const wr1 = deviceForm(page, "WR1");
    await expect(wr1.locator('select[name="light"]')).toBeVisible();
    await expect(wr1.locator('select[name="fan"]')).toHaveValue("unmanaged");
    await expect(wr1.locator('select[name="buzzer"]')).toHaveCount(0);
    await expect(deviceForm(page, "WR2").locator('select[name="fan"]')).toHaveCount(0);

    await page.route("**/api/runtime/device/WR1", (route) => route.fulfill({
      json: { updated: true, device: "WR1", state: { fan: "off" } },
    }));
    let asked = "";
    page.once("dialog", (dialog) => {
      asked = dialog.message();
      return dialog.accept();
    });

    await wr1.locator('select[name="fan"]').selectOption("off");
    const [request] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/runtime/device/WR1") && sent.method() === "PATCH"),
      wr1.locator('button[type="submit"]').click(),
    ]);

    expect(request.postDataJSON()).toEqual({ fan: "off" });
    expect(asked).toContain("Change the fan of WR1?");
    expect(asked).not.toContain("cool");
  });

  test("the device card shows the settings as the inverter reports them", async ({ page }) => {
    await page.goto("/preview/devices", { waitUntil: "domcontentloaded" });

    const card = page.locator(".device-card").filter({ hasText: "WR1" });
    const settings = card.locator(".device-settings");
    await expect(settings).toContainText("Device settings");
    await expect(settings).toContainText("Light");
    await expect(settings).toContainText("Auto");
    await expect(page.locator(".device-card").filter({ hasText: "WR2" }).locator(".device-settings")).toHaveCount(0);
  });
});
