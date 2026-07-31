import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { currentWorkflow, post } from "./helpers/setup-authority";

// Returning Admin to the running build is two durable steps with no owner
// carried across them, so it is refused for a Guided Setup transition. These
// specs pin what a real browser is offered and what the server answers, in
// Chromium and Firefox.

async function alignment(page: Page) {
  return (await (
    await page.request.get("/api/admin/system-alignment/status")
  ).json()) as any;
}

/** Drive Fresh Setup into a Setup-owned, recoverable System Build failure. */
async function failedSetupTransition(page: Page, seed: (s: string) => Promise<void>) {
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await seed("system_build_v070_resource_failure");
  await setup.selectBuild("v0.7.0");
  const confirmation = page.waitForResponse((response) =>
    response.url().includes("/api/setup/system-build/confirm"),
  );
  await setup.continueButton.click();
  expect((await confirmation).ok()).toBe(false);

  const status = await alignment(page);
  expect(status.transition.mode).toBe("fresh_install");
  expect(status.transition.stage).toBe("failed_recoverable");
  return status.transition.operation_id as string;
}

test.describe("Setup return-to-running ownership", () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("the recovery panel offers no Return action for a Setup transition", async ({
    page,
    seedAdminScenario,
  }) => {
    const operationId = await failedSetupTransition(page, seedAdminScenario);

    const status = await alignment(page);
    expect(status.transition.return_available).toBe(false);
    // Discard setup stays the offered escape.
    expect(status.transition.cancel_available).toBe(true);

    // The rendered recovery panel must not show an action the server always
    // rejects — checked after a reload so this is the real first-paint state.
    await page.reload();
    await expect(page.locator("#system-alignment-partial")).toBeVisible();
    await expect(page.locator("#system-alignment-return")).toBeHidden();
    await expect(page.locator("#system-alignment-resume")).toBeVisible();

    const refused = await post(
      page,
      "/api/admin/system-alignment/return-to-running-build",
      { operation_id: operationId, confirm: true },
    );
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_return_unsupported");
    expect((await alignment(page)).transition.stage).toBe("failed_recoverable");
  });

  test("Discard setup wins and no align-existing transition is left behind", async ({
    page,
    seedAdminScenario,
  }) => {
    const operationId = await failedSetupTransition(page, seedAdminScenario);
    const workflowId = (await currentWorkflow(page))?.workflow_id;
    expect(workflowId).toBeTruthy();

    const refused = await post(
      page,
      "/api/admin/system-alignment/return-to-running-build",
      { operation_id: operationId, confirm: true },
    );
    expect(refused.status).toBe(409);

    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    expect(discarded.body.ok).toBe(true);
    expect((await currentWorkflow(page))?.status).toBe("abandoned");

    // The terminal action that won stays the only one: the refused return may
    // not resurface as a new operation after the workflow is gone.
    const after = await alignment(page);
    expect(after.transition.stage).toBe("cancelled");
    expect(after.transition.mode).toBe("fresh_install");
    expect(after.transition.operation_id).toBe(operationId);

    const retried = await post(
      page,
      "/api/admin/system-alignment/return-to-running-build",
      { operation_id: operationId, confirm: true },
    );
    expect(retried.status).toBe(409);
    expect((await alignment(page)).transition.mode).toBe("fresh_install");
  });

  test("a claimed resource verification blocks Discard setup until it ends", async ({
    page,
    seedAdminScenario,
  }) => {
    // The visible stage is still admin_aligned, but the resource importer holds
    // its claim, so the transition is externally mutating and must not be
    // cancellable — neither through the console nor through Discard setup.
    await seedAdminScenario("system_build_resource_verification_running");

    const status = await alignment(page);
    expect(status.transition.stage).toBe("admin_aligned");
    expect(status.transition.cancel_available).toBe(false);

    const refused = await post(page, "/api/setup/abandon", {});
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("mutation_in_progress");
    expect((await alignment(page)).transition.stage).toBe("admin_aligned");
  });
});
