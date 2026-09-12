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
  await expect(page.getByRole("heading", { level: 2, name: "Overview" })).toBeVisible();
}

export async function openView(page: Page, view: string) {
  await page.locator(`[data-test="nav-${view}"]`).click();
  await expect(page.locator(`[data-test="nav-${view}"]`)).toHaveAttribute("aria-current", "page");
}

// Park the page at its bottom the way a reader does, and read back where it
// actually came to rest. A programmatic window.scrollTo is not the same act: a
// scroll-into-view -- the one focus() performs when it has to bring its target
// on screen -- stays pending in Gecko until the next reflow, and it is applied
// on top of the next programmatic scroll, one frame later. A park made that way
// is silently undone before anything under test has run, and the position the
// caller wrote down was never the position of the page. A key press is real
// input, it is not undone in either engine, and expect.poll waits for the
// scroll itself rather than for a clock. The caller has to leave focus
// somewhere that does not consume End -- a text field would take it as a caret
// move and the page would not scroll at all, which the poll then reports.
export async function parkAtBottom(page: Page) {
  const bottom = await page.evaluate(() =>
    Math.round(document.documentElement.scrollHeight - window.innerHeight),
  );
  expect(bottom).toBeGreaterThan(0);
  await page.keyboard.press("End");
  await expect.poll(() => page.evaluate(() => Math.round(window.scrollY))).toBe(bottom);
  return bottom;
}

export async function setMode(page: Page, mode: "basic" | "expert") {
  await page.locator(`#mode-${mode}`).click();
  await expect(page.locator(`#mode-${mode}`)).toHaveAttribute("aria-pressed", "true");
}
