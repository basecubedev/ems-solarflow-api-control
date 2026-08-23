// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared drivers for the appliance browser suites. Kept out of a spec file so
// importing them does not register that file's tests as well.
import { expect, Page, APIRequestContext } from "@playwright/test";

export const PASSWORD = "appliance-secret-1";
export const PUBLIC_KEY =
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf " +
  "appliance-test@example.invalid";

export async function resetAppliance(request: APIRequestContext, options: object = {}) {
  const response = await request.post("/api/test/reset", { data: options });
  expect(response.ok()).toBeTruthy();
}

export async function signIn(page: Page) {
  await page.goto("/");
  await expect(page.locator("#gate")).toBeVisible();
  await page.locator("#gate-password").fill(PASSWORD);
  await page.locator("#gate-confirm").fill(PASSWORD);
  await Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/session/setup")),
    page.locator("#gate-submit").click(),
  ]);
  await expect(page.locator("#shell")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Appliance overview" })).toBeVisible();
}

export async function openView(page: Page, view: string) {
  await page.locator(`[data-test="nav-${view}"]`).click();
  await expect(page.locator(`[data-test="nav-${view}"]`)).toHaveAttribute("aria-current", "page");
}

export async function setMode(page: Page, mode: "basic" | "expert") {
  await page.locator(`#mode-${mode}`).click();
  await expect(page.locator(`#mode-${mode}`)).toHaveAttribute("aria-pressed", "true");
}
