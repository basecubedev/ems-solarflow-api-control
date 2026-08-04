import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  authorizeSetupMutation,
  currentWorkflow,
  currentWorkflowId,
  holdBrowserPreviews,
  post,
  seedSetupInverter,
  startSetupWorkflow,
  storedWorkflow,
  waitForConfigApplyReady,
} from "./helpers/setup-authority";

// Server-owned workflow authority end to end: an old tab cannot mutate the
// current workflow, a build change retires the old workflow's artifacts through
// the backend, an unresolved Setup blocks Guided Upgrade until its owner clears
// it, and the narrow transition primitive refuses Setup-owned transitions.

const INVERTER = {
  role: "inverter",
  enabled: true,
  config_name: "WR1",
  display_name: "Inv",
  ip: "192.168.1.100",
  serial_number: "SN1",
};
const DRAFT = { devices: [INVERTER], supported_grid_meter_count: 0 };

async function generatedConfig(page: Page) {
  return (await (await page.request.get("/api/setup/config/status")).json()) as any;
}

async function alignment(page: Page) {
  return (await (await page.request.get("/api/admin/system-alignment/status")).json()) as any;
}

async function enterSetup(
  page: Page,
  seedAdminScenario?: (scenario: string) => Promise<void>,
) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  if (seedAdminScenario) {
    await seedSetupInverter(page, seedAdminScenario);
  }
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("v0.7.0");
  await setup.continueToDevices();
  return setup;
}

test.describe("Setup workflow authority", { tag: ["@setup", "@authority"] }, () => {
  test("an old tab cannot mutate the workflow that replaced it", async ({
    page,
  }) => {
    await enterSetup(page);
    await page.goto("about:blank");

    // Tab A's authority.
    const oldWorkflow = await startSetupWorkflow(page);
    const oldAuthority = await authorizeSetupMutation(page, DRAFT, oldWorkflow);

    // The setup is restarted elsewhere; tab A never learns about it.
    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: oldWorkflow,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    const newWorkflow = await startSetupWorkflow(page);
    expect(newWorkflow).not.toBe(oldWorkflow);

    // Tab A's Apply is refused; the live config is untouched.
    const refused = await post(page, "/api/setup/config/apply", oldAuthority);
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_workflow_not_active");
    expect((await generatedConfig(page)).exists).toBe(false);

    // Workflow B keeps full authority once its own System Build is verified
    // again (the abandon ended the old transition together with the workflow).
    const seeded = await post(page, "/api/admin/test/seed", {
      scenario: "system_build_admin_aligned",
    });
    expect(seeded.status, JSON.stringify(seeded.body)).toBe(200);
    const verified = await post(page, "/api/admin/system-alignment/verify-resources", {
      operation_id: (await alignment(page)).transition.operation_id,
    });
    expect(verified.status, JSON.stringify(verified.body)).toBe(200);
    const accepted = await post(
      page,
      "/api/setup/config/apply",
      await authorizeSetupMutation(page, DRAFT, newWorkflow),
    );
    expect(accepted.status, JSON.stringify(accepted.body)).toBe(200);
  });

  test("the browser surfaces an old workflow and offers the recovery actions", async ({
    page,
    seedAdminScenario,
  }) => {
    await enterSetup(page, seedAdminScenario);

    // The browser previews its own draft, then its workflow is retired by
    // another session while this tab keeps the stale identity.
    await page.locator('[data-setup-step="config"]').click();
    const { workflowId: stale, previewId: stalePreview } =
      await waitForConfigApplyReady(page);

    // A background preview would detect the retirement below and disable Apply
    // before the intended stale click ever reaches the server.
    const releasePreviews = await holdBrowserPreviews(page);
    let replacement: string;
    try {
      const discarded = await post(page, "/api/setup/abandon", {
        setup_workflow_id: stale,
      });
      expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
      replacement = await startSetupWorkflow(page);
      expect(replacement).not.toBe(stale);

      // The stale tab's Apply is refused and the panel names the situation.
      const held = (await storedWorkflow(page))!;
      expect(held.workflow_id).toBe(stale);
      expect(held.preview_id).toBe(stalePreview);
      const [refused] = await Promise.all([
        page.waitForResponse(
          (r) =>
            r.url().includes("/api/setup/config/apply") &&
            r.request().method() === "POST" &&
            r.status() === 409,
        ),
        page.locator("#config-apply").click(),
      ]);
      expect((await refused.json()).error).toBe("setup_workflow_not_active");
    } finally {
      await releasePreviews();
    }

    const panel = page.locator("#setup-workflow-conflict");
    await expect(panel).toBeVisible();
    await expect(panel).toContainText(/older setup session/i);
    await expect(page.locator("#setup-workflow-conflict-open")).toBeVisible();
    await expect(page.locator("#setup-workflow-conflict-discard")).toBeVisible();

    // Opening the current setup adopts the replacement identity.
    await page.locator("#setup-workflow-conflict-open").click();
    await expect
      .poll(async () => (await storedWorkflow(page))?.workflow_id)
      .toBe(replacement);
    await expect(panel).toBeHidden();
  });

  test("the narrow cancel primitive refuses a Setup-owned transition", async ({
    page,
  }) => {
    await enterSetup(page);
    const before = await alignment(page);
    expect(before.transition.mode).toBe("fresh_install");

    const refused = await post(page, "/api/admin/system-alignment/cancel", {
      operation_id: before.transition.operation_id,
      confirm: true,
    });
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_abandon_required");

    // State unchanged: same operation, same stage, workflow still active.
    const after = await alignment(page);
    expect(after.transition.operation_id).toBe(before.transition.operation_id);
    expect(after.transition.stage).toBe(before.transition.stage);
    expect((await currentWorkflow(page))?.status).toBe("active");
  });

  test("changing the build leaves no artifact from the superseded workflow", async ({
    page,
  }) => {
    const setup = await enterSetup(page);
    await page.goto("about:blank");

    // Build A generated a config under workflow A.
    const workflowA = await currentWorkflowId(page);
    const written = await post(
      page,
      "/api/setup/config/write",
      await authorizeSetupMutation(page, { ...DRAFT, overwrite: true }, workflowA!),
    );
    expect(written.status, JSON.stringify(written.body)).toBe(200);
    expect((await generatedConfig(page)).exists).toBe(true);
    const generatedPath = written.body.path as string;

    // Selecting build B in the UI supersedes workflow A through the backend.
    await page.goto("/#setup");
    await page.locator('[data-setup-step="release"]').click();
    const superseded = page.waitForResponse(
      (r) =>
        r.url().includes("/api/setup/system-build/supersede") &&
        r.request().method() === "POST",
    );
    await setup.previewBuild("latest");
    const body = await (await superseded).json();
    expect(body.ok, JSON.stringify(body)).toBe(true);
    expect(body.superseded_workflow_id).toBe(workflowA);

    // No artifact from build A survives, and the replacement is active.
    const replacement = await currentWorkflow(page);
    expect(replacement?.status).toBe("active");
    expect(replacement?.workflow_id).not.toBe(workflowA);
    expect((await generatedConfig(page)).exists).toBe(false);
    expect(generatedPath).toContain(workflowA!);
  });

  test("Guided Upgrade stays blocked until the Setup owner cleaned up", async ({
    page,
  }) => {
    await enterSetup(page);
    await page.goto("about:blank");
    const workflow = await currentWorkflowId(page);
    const written = await post(
      page,
      "/api/setup/config/write",
      await authorizeSetupMutation(page, { ...DRAFT, overwrite: true }, workflow!),
    );
    expect(written.status, JSON.stringify(written.body)).toBe(200);

    // Upgrade validation refuses while Setup still owns unresolved state.
    const blocked = await post(page, "/api/admin/maintenance/upgrade/validate", {
      tag: "v9.9.10",
    });
    expect(blocked.status, JSON.stringify(blocked.body)).toBe(409);
    expect(blocked.body.error).toBe("setup_abandon_required");
    expect((await generatedConfig(page)).exists).toBe(true);

    // The Setup owner clears it, artifacts included.
    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflow,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    expect(discarded.body.ok).toBe(true);
    expect((await generatedConfig(page)).exists).toBe(false);

    // Only then does validation run.
    const allowed = await post(page, "/api/admin/maintenance/upgrade/validate", {
      tag: "v9.9.10",
    });
    expect(allowed.status, JSON.stringify(allowed.body)).not.toBe(409);
  });
});
