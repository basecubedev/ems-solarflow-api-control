import { type Page, expect } from "@playwright/test";
import { ADMIN_PASSWORD } from "../fixtures/admin";

// Drive the real Admin auth screen through the browser. The shared password may
// already exist (created by the reset fixture), so this handles both the
// create-password and login forms.
export class LoginPage {
  constructor(private readonly page: Page) {}

  async open() {
    await this.page.goto("/");
  }

  async authenticate(password = ADMIN_PASSWORD) {
    // Both auth blocks are present; only the applicable one lacks [hidden].
    const login = this.page.locator("#auth-login:not([hidden])");
    const create = this.page.locator("#auth-create:not([hidden])");
    await expect(login.or(create)).toBeVisible();
    if (await login.count()) {
      await this.page.fill("#auth-login-password", password);
      await this.page.locator("#auth-login-form button[type=submit]").click();
    } else {
      await this.page.fill("#auth-create-password", password);
      await this.page.fill("#auth-create-confirm", password);
      await this.page.locator("#auth-create-form button[type=submit]").click();
    }
    // The auth view is dismissed once a session is established.
    await expect(this.page.locator("#view-start")).toBeVisible();
  }
}
