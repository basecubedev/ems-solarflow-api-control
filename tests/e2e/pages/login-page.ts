// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type Page, expect } from "@playwright/test";
import { ADMIN_PASSWORD } from "../fixtures/admin";

/**
 * The console's sign-in through the browser. The shared password may already
 * exist, so this answers both the sign-in and the first password.
 */
export class LoginPage {
  constructor(private readonly page: Page) {}

  /** The guided setup at the console's address; the sign-in stands in its place until a session exists. */
  async open() {
    await this.page.goto("/#/setup");
  }

  async authenticate(password = ADMIN_PASSWORD) {
    const screen = this.page.getByTestId("sign-in-screen");
    await expect(screen).toHaveAttribute("data-mode", /^(login|create)$/);
    await this.page.fill("#admin-password", password);
    if ((await screen.getAttribute("data-mode")) === "create") await this.page.fill("#admin-password-confirm", password);
    await this.page.getByTestId("sign-in-submit").click();
    await expect(screen).toBeHidden();
  }
}
