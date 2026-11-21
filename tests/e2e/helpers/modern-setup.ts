// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type Locator, type Page, expect } from "@playwright/test";
import { ADMIN_PASSWORD } from "../fixtures/admin";

/**
 * The console's guided setup, its first step: the sign-in (creating the
 * password when there is none), the start of a fresh setup, the System Build
 * choice and its verification, read back from the server's own answer.
 */
export class ModernSetup {
  readonly select: Locator;
  readonly message: Locator;
  readonly left: Locator;
  readonly right: Locator;

  constructor(private readonly page: Page) {
    this.select = page.getByTestId("setup-release-select");
    this.message = page.getByTestId("setup-build-message");
    this.left = page.getByTestId("setup-left");
    this.right = page.getByTestId("setup-right");
  }

  option(tag: string): Locator {
    return this.select.locator(`option[value="${tag}"]`);
  }

  async signIn(password = ADMIN_PASSWORD) {
    const screen = this.page.getByTestId("sign-in-screen");
    await expect(screen).toHaveAttribute("data-mode", /^(login|create)$/);
    await this.page.fill("#admin-password", password);
    if ((await screen.getAttribute("data-mode")) === "create") await this.page.fill("#admin-password-confirm", password);
    await this.page.getByTestId("sign-in-submit").click();
    await expect(this.page.locator(".app")).toBeVisible();
  }

  async open(password = ADMIN_PASSWORD) {
    await this.page.goto("/#/setup");
    await this.signIn(password);
    await this.page.getByTestId("setup-begin").click();
    const replace = this.page.getByRole("dialog", { name: "Set up over the existing installation?" });
    if (await replace.isVisible().catch(() => false)) await replace.getByTestId("confirm").click();
    await expect(this.page.getByTestId("setup-release")).toBeVisible();
  }

  /** Choose a build and verify it; the server's verification answer. */
  async verify(tag: string): Promise<Record<string, any>> {
    await this.select.selectOption(tag);
    const answered = this.page.waitForResponse(
      (response) => response.url().includes("/api/admin/system-alignment/validate") && response.request().method() === "POST",
    );
    await this.right.click();
    const body = await (await answered).json();
    await expect(this.right).not.toHaveText("Verify the System Build");
    return body;
  }

  /** The Admin update a build needs, when it needs one, then the step's confirmation into the devices. */
  async continueToDevices() {
    const update = this.left.filter({ hasText: "Update the Admin console" });
    if ((await update.count()) && (await update.isEnabled())) {
      await update.click();
      await expect(this.message).toHaveText("The Admin console is ready for this build.", { timeout: 60_000 });
    }
    await this.right.click();
    await expect(this.page.getByTestId("setup-step-devices")).toHaveAttribute("aria-selected", "true");
  }
}
