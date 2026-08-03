import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  currentWorkflow,
  post,
  startSetupWorkflow,
} from "./helpers/setup-authority";

// Transition expiry closes every forward path, but it proves nothing about the
// worker that was running when the TTL passed. These specs pin what the server
// answers and what a real browser is offered while an expired Setup transition's
// resource importer is still writing the shared cache, in Chromium and Firefox.
// Synchronization is a server-side hold plus a rewritten TTL — never a sleep.

const ABANDON = "#system-alignment-abandon";
const PARTIAL = "#system-alignment-partial";

async function alignment(page: Page) {
  return (await (
    await page.request.get("/api/admin/system-alignment/status")
  ).json()) as any;
}

test.describe("Expired Setup resource-worker ownership", { tag: ["@setup", "@authority", "@workflow"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("an expired transition keeps Discard setup refused until its import settles", async ({
    page,
    seedAdminScenario,
  }) => {
    // 01-02 A real Guided Setup workflow owns the transition, so every abandon
    // below is the workflow-owned contract rather than the legacy pre-B0 route.
    // The seed returns only once a request is really parked in the mutation.
    const workflowId = await startSetupWorkflow(page);
    await seedAdminScenario("setup_resource_import_running");
    const held = await alignment(page);
    expect(held.transition.mode).toBe("fresh_install");
    expect(held.transition.stage).toBe("admin_aligned");
    expect(held.transition.expired).toBe(false);
    expect(held.transition.worker_active).toBe(true);
    expect(held.transition.cancel_available).toBe(false);
    const operationId = held.transition.operation_id as string;
    expect((await currentWorkflow(page))?.operation_id).toBe(operationId);

    // 03 The transition expires while that import is still running.
    await seedAdminScenario("setup_transition_expire");
    const expired = await alignment(page);
    expect(expired.transition.expired).toBe(true);
    // 04 Expiry alone must not reopen the escape: a live worker still owns it.
    expect(expired.transition.worker_status_available).toBe(true);
    expect(expired.transition.worker_active).toBe(true);
    expect(expired.transition.cancel_available).toBe(false);

    const refused = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("transition_worker_active");
    expect((await alignment(page)).transition.stage).toBe("admin_aligned");
    expect((await currentWorkflow(page))?.status).toBe("active");

    // 05-06 Released, the import completes and the transition settles.
    await seedAdminScenario("setup_resource_import_release");
    const settled = await alignment(page);
    expect(settled.transition.operation_id).toBe(operationId);
    expect(settled.transition.stage).toBe("resources_verified");
    expect(settled.transition.worker_active).toBe(false);
    expect(settled.transition.cancel_available).toBe(true);

    // 07 Only now may the workflow's own Discard setup win.
    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    expect(discarded.body.ok).toBe(true);
    expect((await alignment(page)).transition.stage).toBe("cancelled");
    expect((await currentWorkflow(page))?.status).toBe("abandoned");
  });

  test("the recovery panel closes and reopens Discard setup with the worker", async ({
    page,
    seedAdminScenario,
  }) => {
    // A browser already inside Fresh Setup, so the transition it is shown is one
    // its own workflow owns — and the panel is on screen at first paint.
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await seedAdminScenario("setup_resource_import_running");
    await seedAdminScenario("setup_transition_expire");
    expect((await currentWorkflow(page))?.operation_id).toBe(
      (await alignment(page)).transition.operation_id,
    );

    await page.reload();
    await expect(page.locator(PARTIAL)).toBeVisible();
    await expect(page.locator(ABANDON)).toBeVisible();
    await expect(page.locator(ABANDON)).toBeDisabled();
    await expect(page.locator("#system-alignment-partial-message")).toContainText(
      "still running",
    );

    await seedAdminScenario("setup_resource_import_release");
    await page.reload();
    await expect(page.locator(PARTIAL)).toBeVisible();
    await expect(page.locator(ABANDON)).toBeEnabled();

    // The action the console now offers is the one the server accepts.
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator(ABANDON).click();
    await expect
      .poll(async () => (await alignment(page)).transition?.stage)
      .toBe("cancelled");
    expect((await currentWorkflow(page))?.status).toBe("abandoned");
  });
});
