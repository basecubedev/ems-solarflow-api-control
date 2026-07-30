import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  currentWorkflow,
  currentWorkflowId,
  post,
  startSetupWorkflow,
  storedWorkflow,
} from "./helpers/setup-authority";

// Terminating a Guided Setup and clearing its files are two separate facts. An
// unnamed abandon is never authority over whatever workflow is stored, a delayed
// preview cannot repaint a superseded tab, and an unfinished cleanup keeps the
// same workflow the owner across a reload — blocking a replacement Setup and both
// Guided Upgrade phases until its retry converges.

async function enterSetup(page) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("v0.7.0");
  await setup.continueToDevices();
  return setup;
}

async function seedCleanupPending(page) {
  const { status, body } = await post(page, "/api/admin/test/seed", {
    scenario: "setup_cleanup_pending",
  });
  expect(status, JSON.stringify(body)).toBe(200);
  expect(body.setup_workflow_id, JSON.stringify(body)).toBeTruthy();
  return body.setup_workflow_id as string;
}

test.describe("Setup lifecycle and cleanup recovery", () => {
  test("an old tab cannot abandon a newer workflow with an empty request", async ({
    page,
  }) => {
    await enterSetup(page);
    await page.goto("about:blank");

    const first = await currentWorkflowId(page);
    expect(first).toBeTruthy();
    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: first,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);

    const second = await startSetupWorkflow(page);
    expect(second).not.toBe(first);

    // The old tab's "abandon whatever is current" request must not adopt B.
    const refused = await post(page, "/api/setup/abandon", {});
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_workflow_required");

    const workflow = await currentWorkflow(page);
    expect(workflow?.workflow_id).toBe(second);
    expect(workflow?.status).toBe("active");
  });

  test("a delayed preview cannot repaint a tab whose workflow was superseded", async ({
    page,
  }) => {
    await enterSetup(page);

    // Reach a fully rendered, ready preview first: Apply is enabled and the tab
    // holds a real exact-preview id.
    const previewed = page.waitForResponse(
      (r) => r.url().includes("/api/setup/config-preview") && r.ok(),
    );
    await page.locator('[data-setup-step="config"]').click();
    await previewed;
    await page.locator("#config-preview-details > summary").click();
    await expect
      .poll(async () => (await storedWorkflow(page))?.workflow_id ?? null)
      .toBeTruthy();

    // Hold the *next* preview response on the wire.
    let release: (() => void) | null = null;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let heldOnce = false;
    await page.route("**/api/setup/config-preview", async (route) => {
      if (heldOnce) {
        await route.continue();
        return;
      }
      heldOnce = true;
      await held;
      await route.continue();
    });
    // Fire and forget: awaiting the returned promise would await the held
    // response itself.
    await page.evaluate(() => {
      void (window as any).requestConfigPreview();
    });
    await expect.poll(() => heldOnce).toBe(true);

    // While that preview is in flight, another session retires this workflow.
    const stale = await currentWorkflowId(page);
    expect(stale).toBeTruthy();
    const abandoned = await post(page, "/api/setup/abandon", {
      setup_workflow_id: stale,
    });
    expect(abandoned.status, JSON.stringify(abandoned.body)).toBe(200);
    const replacement = await startSetupWorkflow(page);
    expect(replacement).not.toBe(stale);

    // The tab learns it is stale through its own poll, which is refused.
    const conflict = page.waitForResponse(
      (r) =>
        r.url().includes("/api/setup/config-preview") && r.status() === 409,
    );
    await page.evaluate(() => {
      void (window as any).requestConfigPreview();
    });
    expect((await (await conflict).json()).error).toBe("setup_workflow_not_active");
    await expect(page.locator("#setup-workflow-conflict")).toBeVisible();
    await expect(page.locator("#config-preview-ready")).toHaveText(
      "Session superseded",
    );
    const paintedPreview = await page.locator("#config-preview").textContent();

    // …and only then does the delayed preview arrive. It must change nothing:
    // no repaint, no preview id, no re-enabled mutation.
    release!();
    await expect(page.locator("#config-preview-ready")).toHaveText(
      "Session superseded",
    );
    await expect(page.locator("#setup-workflow-conflict")).toBeVisible();
    await expect(page.locator("#config-apply")).toBeDisabled();
    await expect(page.locator("#config-download")).toBeDisabled();
    await expect(page.locator("#config-preview")).toHaveText(
      paintedPreview ?? "",
    );
    await expect
      .poll(async () => (await storedWorkflow(page))?.preview_id ?? null)
      .toBeNull();
  });

  test("a cleanup failure survives a reload and offers Retry cleanup", async ({
    page,
  }) => {
    await enterSetup(page);
    const workflowId = await seedCleanupPending(page);

    // A reload must not look like a clean slate.
    await page.reload();

    const warning = page.locator("#system-alignment-warning");
    await expect(warning).toBeVisible();
    await expect(warning).toContainText("Setup has stopped.");
    await expect(warning).toContainText("Temporary files remain.");
    await expect(warning).toContainText(
      "No new Setup or Upgrade can start until cleanup succeeds.",
    );
    await expect(warning).toContainText(
      "live config and the running EMS were not changed",
    );
    await expect(page.locator("#system-alignment-retry-cleanup")).toBeVisible();

    // The browser keeps the exact id the retry has to name, and no preview.
    await expect
      .poll(async () => (await storedWorkflow(page))?.workflow_id ?? null)
      .toBe(workflowId);
    await expect
      .poll(async () => (await storedWorkflow(page))?.preview_id ?? null)
      .toBeNull();
  });

  test("a new Fresh Setup stays blocked while cleanup is pending", async ({
    page,
  }) => {
    await enterSetup(page);
    const workflowId = await seedCleanupPending(page);

    const blocked = await post(page, "/api/admin/start-path", {
      choice: "setup_new",
      confirm: true,
    });
    expect(blocked.status, JSON.stringify(blocked.body)).toBe(409);
    expect(blocked.body.error).toBe("setup_cleanup_required");
    expect(blocked.body.setup_workflow_id).toBeUndefined();
    expect(blocked.body.setup_intent_id).toBeUndefined();

    // The failed cleanup keeps its owner and its blocking state.
    const workflow = await currentWorkflow(page);
    expect(workflow?.workflow_id).toBe(workflowId);
    expect(workflow?.cleanup?.state).toBe("pending");
    expect(workflow?.cleanup?.blocking).toBe(true);
  });

  test("Guided Upgrade stays blocked while cleanup is pending", async ({ page }) => {
    await enterSetup(page);
    await seedCleanupPending(page);

    const validate = await post(page, "/api/admin/maintenance/upgrade/validate", {
      tag: "v9.9.10",
    });
    expect(validate.status, JSON.stringify(validate.body)).toBe(409);
    expect(validate.body.error).toBe("setup_cleanup_required");

    const execute = await post(page, "/api/admin/maintenance/upgrade/execute", {
      confirm: true,
      target_release: "v9.9.10",
      selection_fingerprint: "not-reached",
    });
    expect(execute.status, JSON.stringify(execute.body)).toBe(409);
    expect(execute.body.error).toBe("setup_cleanup_required");
  });

  test("a successful cleanup retry unblocks the next Setup", async ({ page }) => {
    await enterSetup(page);
    const workflowId = await seedCleanupPending(page);

    // Only this exact workflow may retry.
    const foreign = await post(page, "/api/setup/abandon", {
      setup_workflow_id: "an-older-tab",
    });
    expect(foreign.status, JSON.stringify(foreign.body)).toBe(409);
    expect(foreign.body.error).toBe("setup_workflow_not_active");
    expect((await currentWorkflow(page))?.cleanup?.state).toBe("pending");

    const retried = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(retried.status, JSON.stringify(retried.body)).toBe(200);
    expect(retried.body.ok).toBe(true);
    expect((await currentWorkflow(page))?.cleanup?.state).toBe("complete");

    // A replacement Setup is allowed again, with a fresh identity and intent.
    const allowed = await post(page, "/api/admin/start-path", {
      choice: "setup_new",
      confirm: true,
    });
    expect(allowed.status, JSON.stringify(allowed.body)).toBe(200);
    expect(allowed.body.setup_workflow_id).not.toBe(workflowId);
    expect(allowed.body.setup_intent_id).toBeTruthy();
  });

  test("a successful cleanup retry unblocks Guided Upgrade", async ({ page }) => {
    await enterSetup(page);
    const workflowId = await seedCleanupPending(page);

    const retried = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(retried.status, JSON.stringify(retried.body)).toBe(200);

    const validate = await post(page, "/api/admin/maintenance/upgrade/validate", {
      tag: "v9.9.10",
    });
    expect(validate.status, JSON.stringify(validate.body)).not.toBe(409);
  });
});
