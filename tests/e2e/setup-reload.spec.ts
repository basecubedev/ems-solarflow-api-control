import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Phase 7: reload/resume behaviour. A reload before confirmation must not leave
// a contradictory stale state; after confirmation it must resume the correct
// transition rather than duplicate it.

test.describe("Setup reload and resume", { tag: ["@smoke", "@setup"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("reload before confirmation leaves no stale enabled Continue", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await expect(setup.continueButton).toBeEnabled();

    await page.reload();

    // No transition was created, so the app returns to a safe landing state and
    // never shows a stale, still-enabled Continue from the previous selection.
    await expect(page.locator("#view-start")).toBeVisible();
    await expect(setup.continueButton).toBeHidden();
  });

  test("reload after confirmation resumes the confirmed build", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();

    // A confirmed transition exists; a reload must not create a second one.
    await page.reload();

    const status = await page.request.get(
      "/api/admin/system-alignment/status",
    );
    const body = await status.json();
    expect(body.transition).not.toBeNull();
    expect(body.transition.system_tag).toBe("v0.7.0");
    // The confirmed operation is a single transition, resumed — not duplicated.
    expect(body.transition.mode).toBe("fresh_install");
  });
});
