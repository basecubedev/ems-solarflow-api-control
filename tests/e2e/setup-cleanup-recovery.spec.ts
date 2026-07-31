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

// --- installed-system files are not a workflow's cleanup responsibility ------

// The abandoned workflow that blocked the console: it created nothing, but the
// old cleanup inspected every known path, found the installed system's
// generated config and deployment marker, and reported an ownership review no
// operator could resolve.

async function seedInstalledArtifacts(page) {
  // An upgradable installation first: Guided Upgrade verification is only
  // meaningful when a concrete older System Build is running.
  const upgradable = await post(page, "/api/admin/test/seed", {
    scenario: "mqtt_migration",
  });
  expect(upgradable.status, JSON.stringify(upgradable.body)).toBe(200);
  const { status, body } = await post(page, "/api/admin/test/seed", {
    scenario: "installed_system_artifacts",
  });
  expect(status, JSON.stringify(body)).toBe(200);
  return body;
}

async function installedDigests(page) {
  const { status, body } = await post(page, "/api/admin/test/seed", {
    scenario: "installed_system_artifact_digests",
  });
  expect(status, JSON.stringify(body)).toBe(200);
  return {
    legacy_generated_config: body.legacy_generated_config,
    deployment_marker: body.deployment_marker,
  };
}

async function verifyUpgradeTarget(page) {
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="upgrade"]').click();
  const select = page.locator("#upgrade-release-select");
  await expect(select).toBeEnabled();
  await select.selectOption("v9.9.10");
  const validation = page.waitForResponse((response) =>
    response.url().endsWith("/maintenance/upgrade/validate"),
  );
  await page.locator("#upgrade-prepare-btn").click();
  const body = await (await validation).json();
  expect(body.error, JSON.stringify(body)).not.toBe("setup_cleanup_required");
  expect(body.error, JSON.stringify(body)).not.toBe("setup_abandon_required");
  await expect(page.locator("#upgrade-release-status")).toHaveText(
    /System Build verified/i,
  );
}

test.describe("Installed-system artifacts stay out of Setup cleanup", () => {
  test("an empty setup leaves them untouched and keeps Guided Upgrade available", async ({
    page,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedInstalledArtifacts(page);
    const before = await installedDigests(page);
    await page.reload();

    // A Guided Setup that creates nothing: started and abandoned, exactly the
    // sequence that stranded the live console.
    const workflowId = await startSetupWorkflow(page);
    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    expect(discarded.body.ok, JSON.stringify(discarded.body)).toBe(true);

    await expect
      .poll(async () => (await currentWorkflow(page))?.cleanup?.state ?? "absent", {
        message: "an empty setup must converge without an operator review",
      })
      .toMatch(/^(complete|not_required|absent)$/);
    await expect(page.locator("#system-alignment-recheck-cleanup")).toBeHidden();
    await expect(page.locator("#system-alignment-retry-cleanup")).toBeHidden();

    await page.reload();
    await verifyUpgradeTarget(page);
    expect(await installedDigests(page)).toEqual(before);

    await page.reload();
    await verifyUpgradeTarget(page);
    expect(await installedDigests(page)).toEqual(before);
  });

  test("a workflow stranded by the old cleanup recovers without deleting anything", async ({
    page,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedInstalledArtifacts(page);
    const before = await installedDigests(page);
    const { status, body } = await post(page, "/api/admin/test/seed", {
      scenario: "setup_cleanup_stranded_review",
    });
    expect(status, JSON.stringify(body)).toBe(200);
    await page.reload();

    await expect
      .poll(async () => (await currentWorkflow(page))?.cleanup?.state ?? "absent", {
        message: "a zero-claim review state must reconcile itself",
      })
      .toBe("complete");
    await expect(page.locator("#system-alignment-warning")).toBeHidden();

    await verifyUpgradeTarget(page);
    expect(await installedDigests(page)).toEqual(before);

    await page.reload();
    await verifyUpgradeTarget(page);
    expect(await installedDigests(page)).toEqual(before);
  });
});
