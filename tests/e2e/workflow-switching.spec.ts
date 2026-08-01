import { test, expect, type Page } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { post } from "./helpers/setup-authority";

// Switching between the two guided workflows used to mean finding a separate
// discard action first — or, when the other workflow owned a transition, having
// no supported way forward at all. It is now one previewed, confirmed backend
// operation that terminates the previous owner through that owner's own service.
//
// See docs/technical/admin-workflow-state.md.

async function lifecycle(page: Page): Promise<Record<string, any>> {
  return (await page.request.get("/api/admin/workflow-lifecycle")).json();
}

async function alignment(page: Page): Promise<Record<string, any>> {
  return (await page.request.get("/api/admin/system-alignment/status")).json();
}

async function installState(page: Page): Promise<Record<string, any>> {
  return (await page.request.get("/api/admin/install-state")).json();
}

async function login(page: Page) {
  const view = new LoginPage(page);
  await view.open();
  await view.authenticate();
}

/** Accept the one switch confirmation the console shows, once. */
function acceptOneConfirmation(page: Page): Promise<string> {
  return new Promise((resolve) => {
    page.once("dialog", async (dialog) => {
      const message = dialog.message();
      await dialog.accept();
      resolve(message);
    });
  });
}

test.describe("Guided workflow switching", () => {
  test("Guided Setup switches to Guided Upgrade and leaves the install alone", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("guided_upgrade_blocking_setup");
    await page.reload();

    const before = await lifecycle(page);
    expect(before.owner, JSON.stringify(before)).toBe("guided_setup");
    const installBefore = await installState(page);

    // An active setup transition reopens Guided Setup on load; the operator
    // returns to the landing gate before entering Maintenance. Opening Guided
    // Upgrade then meets the Setup owner: the console previews the switch,
    // names what it stops, and only then executes it.
    await page.locator("#upgrade-release-select").waitFor({ state: "attached" });
    await page.locator("[data-back]:visible").first().click();
    await page.locator('[data-start-path="manage_existing"]').click();
    await page.locator('[data-open-maintenance-path="upgrade"]').click();
    const select = page.locator("#upgrade-release-select");
    await expect(select).toBeEnabled();
    await select.selectOption("v9.9.10");
    const confirmation = acceptOneConfirmation(page);
    const switched = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/admin/workflow-lifecycle/switch") &&
        response.request().method() === "POST",
    );
    // The narrow primitives are never used for a cross-workflow switch.
    const bypasses: string[] = [];
    page.on("request", (request) => {
      const url = request.url();
      if (url.endsWith("/system-alignment/cancel") || url.endsWith("/api/setup/abandon")) {
        bypasses.push(url);
      }
    });
    await page.locator("#upgrade-prepare-btn").click();

    const text = await confirmation;
    expect(text).toContain("Guided Setup workflow");
    expect(text).toContain("live EMS configuration");
    expect((await switched).ok()).toBeTruthy();
    expect(bypasses).toEqual([]);

    await expect(page.locator("#upgrade-release-status")).toHaveText(
      /System Build verified/i,
    );

    const after = await lifecycle(page);
    expect(after.owner, JSON.stringify(after)).toBe("none");
    expect(after.transition?.terminal ?? null).toBe(true);
    // The installed system is exactly as it was.
    expect(await installState(page)).toEqual(installBefore);

    // The same verdict survives a reload: nothing here lived in the browser.
    await page.reload();
    const reloaded = await lifecycle(page);
    expect(reloaded.owner).toBe("none");
    expect(reloaded.switchable).toBe(true);
  });

  test("Guided Upgrade switches to Guided Setup and issues a fresh intent", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("guided_upgrade_transition");
    await page.reload();

    const before = await lifecycle(page);
    expect(before.owner, JSON.stringify(before)).toBe("guided_upgrade");
    const operationId = before.transition.operation_id;

    // A live upgrade transition reopens its panel on load; the operator walks
    // back to task selection to pick the other workflow.
    await page.locator("[data-back]:visible").first().click();
    await page.locator('#maintenance-hub [data-back="landing"]').click();

    const confirmation = acceptOneConfirmation(page);
    const switched = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/admin/workflow-lifecycle/switch") &&
        response.request().method() === "POST",
    );
    await page.locator('[data-start-path="setup_new"]').click();

    const text = await confirmation;
    expect(text).toContain("Guided Upgrade System Build transition");
    const response = await switched;
    expect(response.ok()).toBeTruthy();
    const body = await response.json();
    expect(body.action).toBe("cancel_guided_upgrade");
    expect(body.cancelled_operation_id).toBe(operationId);
    expect(body.setup_workflow_id).toBeTruthy();
    expect(body.setup_intent_id).toBeTruthy();

    // The console really is in Guided Setup, and the transition is terminal.
    await expect(page.getByTestId("system-build-select")).toBeVisible();
    const status = await alignment(page);
    expect(status.transition?.stage).toBe("cancelled");

    const after = await lifecycle(page);
    expect(after.owner).toBe("guided_setup");
    expect(after.setup.workflow_id).toBe(body.setup_workflow_id);
    expect(after.upgrade_context).toBeNull();

    // A reload resumes the same Setup workflow rather than minting a second.
    await page.reload();
    const resumed = await lifecycle(page);
    expect(resumed.setup.workflow_id).toBe(body.setup_workflow_id);
  });

  test("a direct start-path call cannot open Setup beside a live Upgrade", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("guided_upgrade_transition");

    // No lifecycle call, no fingerprint — an old console or a script.
    const started = await post(page, "/api/admin/start-path", {
      choice: "setup_new",
      confirm: true,
    });

    expect(started.status, JSON.stringify(started.body)).toBe(409);
    expect(started.body.error).toBe("workflow_switch_required");
    expect(started.body.switch_preview).toBe(
      "/api/admin/workflow-lifecycle/switch/preview",
    );
    expect(started.body.setup_workflow_id).toBeUndefined();

    // The upgrade still owns the console, alone.
    const after = await lifecycle(page);
    expect(after.owner).toBe("guided_upgrade");
    expect(after.setup).toBeNull();
    expect((await alignment(page)).transition?.stage).not.toBe("cancelled");
  });

  test("a running operation blocks the switch and offers no force action", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("setup_resource_import_running");
    try {
      await page.reload();
      const before = await lifecycle(page);
      expect(before.state, JSON.stringify(before)).toBe("operation_running");
      expect(before.switchable).toBe(false);

      const preview = await post(
        page,
        "/api/admin/workflow-lifecycle/switch/preview",
        { target: "guided_upgrade" },
      );
      expect(preview.status).toBe(200);
      expect(preview.body.blocked, JSON.stringify(preview.body)).toBe(true);
      expect(preview.body.confirmation_required).toBe(false);
      // A blocked switch promises nothing, so it lists no reset scope.
      expect(preview.body.will_reset).toEqual([]);

      // Executing it anyway changes nothing.
      const refused = await post(page, "/api/admin/workflow-lifecycle/switch", {
        target: "guided_upgrade",
        confirm: true,
        fingerprint: before.fingerprint,
      });
      expect(refused.status).toBe(409);
      expect(refused.body.error).toMatch(/operation_in_progress/);

      const after = await lifecycle(page);
      expect(after.state).toBe("operation_running");
      expect(after.setup?.status ?? null).not.toBe("abandoned");

      // The console never offers a force reset for this state.
      await page.locator("#upgrade-release-select").waitFor({ state: "attached" });
      await page.locator("[data-back]:visible").first().click();
      await page.locator('[data-start-path="manage_existing"]').click();
      const loaded = page.waitForResponse((response) =>
        response.url().endsWith("/api/admin/workflow-lifecycle/recovery/preview"),
      );
      await page.locator('[data-open-maintenance-path="manual"]').click();
      expect((await loaded).ok()).toBeTruthy();
      await page
        .locator('[data-maintenance-toggle="maintenance-workflow-recovery"]')
        .click();
      await expect(page.locator("#maintenance-workflow-recovery-safe")).toBeHidden();
      await expect(
        page.locator("#maintenance-workflow-recovery-advanced"),
      ).toBeHidden();
      await expect(page.locator("#maintenance-workflow-recovery-summary")).toHaveText(
        /operation is running/i,
      );
    } finally {
      await post(page, "/api/admin/test/seed", {
        scenario: "setup_resource_import_release",
      });
    }
  });

  test("returning to task selection keeps the Setup workflow", async ({ page }) => {
    await login(page);
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();
    const before = await lifecycle(page);
    expect(before.setup.workflow_id).toBeTruthy();

    // Navigating away from the task and into Maintenance is not a decision to
    // discard the draft: only an explicit switch or reset terminates it.
    await page.locator('#view-setup [data-back="landing"]').click();
    await page.locator('[data-start-path="manage_existing"]').click();
    await expect(page.locator("#maintenance-hub")).toBeVisible();

    const after = await lifecycle(page);
    expect(after.owner).toBe("guided_setup");
    expect(after.setup.workflow_id).toBe(before.setup.workflow_id);
    expect(after.setup.status).toBe("active");
  });
});
