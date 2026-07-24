import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { assertReadyActionInvariant } from "./helpers/invariants";

// Phase 8: a ready state never disables every valid action. Applied to each
// installable System Build so the deadlock class of bug cannot regress silently.

test.describe("System Build ready/action invariant", () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  for (const tag of ["latest", "v9.9.10", "v0.7.0"]) {
    test(`invariant holds for ${tag}`, async ({ page }) => {
      const setup = new SetupPage(page);
      await setup.chooseFreshInstall();
      await setup.selectBuild(tag);
      await assertReadyActionInvariant(setup);
    });
  }

  test("legacy resource verification failure blocks progress visibly", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.6.9");

    // Validation still reports the release as installable (release-archive), so
    // Continue is offered — the failure surfaces only when preparation runs.
    await expect(setup.continueButton).toBeEnabled();

    const confirm = page.waitForResponse((r) =>
      r.url().includes("/api/setup/system-build/confirm"),
    );
    await setup.continueButton.click();
    const response = await confirm;
    expect(response.ok()).toBeFalsy();

    // The UI shows an actionable error and never advances to Device Discovery.
    await expect(setup.error).toBeVisible();
    await expect(setup.devicesTab()).toHaveAttribute("aria-selected", "false");
  });
});
