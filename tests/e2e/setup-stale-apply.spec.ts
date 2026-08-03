import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  authorizeSetupMutation,
  currentDevicePlanId,
  currentWorkflowId,
  holdBrowserPreviews,
  post,
  seedSetupInverter,
  startSetupWorkflow,
  storedWorkflow,
  waitForConfigApplyReady,
} from "./helpers/setup-authority";

// A Setup draft may only be applied when the server can prove two things: the
// mutation belongs to the active workflow, and it is byte-for-byte the draft
// the exact preview was issued for. These drive the real endpoints through the
// browser session and then the real conflict UI.

const INVERTER = {
  role: "inverter",
  enabled: true,
  config_name: "WR1",
  display_name: "Inv",
  ip: "192.168.1.100",
  serial_number: "SN1",
};
const DRAFT = { devices: [INVERTER], supported_grid_meter_count: 0 };
// A second valid draft that generates different config bytes than DRAFT.
const OTHER_DRAFT = {
  devices: [{ ...INVERTER, ip: "192.168.1.101" }],
  supported_grid_meter_count: 0,
};

async function maintenanceConfig(page: Page) {
  return (await (await page.request.get("/api/admin/maintenance/config")).json()) as any;
}

async function storedDraft(page: Page) {
  return page.evaluate(() => window.localStorage.getItem("ems-admin-config-draft"));
}

async function liveConfigBytes(page: Page) {
  const config = await maintenanceConfig(page);
  return JSON.stringify(config.draft);
}

async function alignment(page: Page) {
  return (await (await page.request.get("/api/admin/system-alignment/status")).json()) as any;
}

/**
 * Park the browser away from Setup for API-driven cases.
 *
 * The wizard re-previews on its own render/poll cycle; in production that is
 * the same client that then mutates, but in an API-driven test it would race
 * the preview the test just obtained.
 */
async function stopBrowserPreviews(page: Page) {
  await page.goto("about:blank");
}

async function reachResourcesVerified(page: Page) {
  const status = await alignment(page);
  const verified = await post(page, "/api/admin/system-alignment/verify-resources", {
    operation_id: status.transition.operation_id,
  });
  expect(verified.status, JSON.stringify(verified.body)).toBe(200);
}

test.describe("Stale Setup apply", () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedSetupInverter(page, seedAdminScenario);
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();
  });

  test("a stale draft is refused and the UI offers review or discard", async ({
    page,
    seedAdminScenario,
  }) => {
    // Revision A: the browser creates the live config through its own Apply,
    // so the whole scenario runs on the client that owns the workflow.
    await page.locator('[data-setup-step="config"]').click();
    await waitForConfigApplyReady(page);
    const [created] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().includes("/api/setup/config/apply") &&
          r.request().method() === "POST",
      ),
      page.locator("#config-apply").click(),
    ]);
    expect(created.status()).toBe(200);

    // A successful Apply consumes its preview, so the browser reviews again —
    // this is the valid, unspent preview bound to revision A.
    await page.reload();
    await page.locator('[data-setup-step="config"]').click();
    const { previewId: reviewedPreview } = await waitForConfigApplyReady(page);

    // Revision A's preview authority has to survive the backend change below,
    // so no later render/poll preview may replace it.
    const releasePreviews = await holdBrowserPreviews(page);
    const revisionA = (await maintenanceConfig(page)).revision;
    let revisionB: string;
    let draftBefore: string | null;
    let previewBefore: string | null;
    try {
      // The live config changes underneath that open draft, without issuing a
      // new preview — exactly the state the baseline check exists for.
      await seedAdminScenario("mqtt_mutate");
      revisionB = (await maintenanceConfig(page)).revision;
      expect(revisionB).not.toBe(revisionA);

      // The real Apply button, still carrying the now-stale preview.
      draftBefore = await storedDraft(page);
      previewBefore = await page.locator("#config-preview").textContent();
      expect((await storedWorkflow(page))!.preview_id).toBe(reviewedPreview);
      await expect(page.locator("#config-apply")).toBeEnabled();
      const [refused] = await Promise.all([
        page.waitForResponse(
          (r) =>
            r.url().includes("/api/setup/config/apply") &&
            r.request().method() === "POST" &&
            r.status() === 409,
        ),
        page.locator("#config-apply").click(),
      ]);
      expect((await refused.json()).error).toBe("stale_setup_config");
    } finally {
      await releasePreviews();
    }

    // The UI explains it and offers both ways forward; the draft is untouched.
    const conflict = page.locator("#setup-config-conflict");
    await expect(conflict).toBeVisible();
    await expect(conflict).toContainText(/changed after this setup was opened/i);
    await expect(page.locator("#setup-config-conflict-review")).toBeVisible();
    await expect(page.locator("#setup-config-conflict-discard")).toBeVisible();
    // The conflict never discards the user's work or moves them off the step.
    expect(await storedDraft(page)).toBe(draftBefore);
    expect(await page.locator("#config-preview").textContent()).toBe(previewBefore);
    await expect(page.locator('[data-setup-step-panel="config"]')).toBeVisible();

    // Revision B survived byte-for-byte.
    expect((await maintenanceConfig(page)).revision).toBe(revisionB);

    // Reviewing the current configuration earns a NEW exact preview…
    await page.locator("#setup-config-conflict-review").click();
    await expect(conflict).toBeHidden();
    await expect
      .poll(async () => (await storedWorkflow(page))?.preview_id)
      .not.toBe(reviewedPreview);
    await waitForConfigApplyReady(page);

    // …and only then does Apply succeed.
    const [accepted] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().includes("/api/setup/config/apply") &&
          r.request().method() === "POST",
      ),
      page.locator("#config-apply").click(),
    ]);
    expect(accepted.status()).toBe(200);
  });

  test("a preview for one draft cannot authorize a different draft", async ({
    page,
  }) => {
    await stopBrowserPreviews(page);
    const workflow = await startSetupWorkflow(page);
    const authorizedForA = await authorizeSetupMutation(page, DRAFT, workflow);
    const bytesBefore = await liveConfigBytes(page).catch(() => null);

    // Draft B submitted under draft A's preview authority.
    const forged = await post(page, "/api/setup/config/apply", {
      ...OTHER_DRAFT,
      setup_workflow_id: workflow,
      config_preview_id: authorizedForA.config_preview_id,
      device_plan_id: authorizedForA.device_plan_id,
    });
    expect(forged.status, JSON.stringify(forged.body)).toBe(409);
    expect(forged.body.error).toBe("setup_preview_mismatch");
    expect(await liveConfigBytes(page).catch(() => null)).toBe(bytesBefore);

    // The rejection did not consume the preview: the reviewed draft still applies.
    const accepted = await post(page, "/api/setup/config/apply", authorizedForA);
    expect(accepted.status, JSON.stringify(accepted.body)).toBe(200);
  });

  test("an external live-config change is preserved byte-for-byte", async ({
    page,
    seedAdminScenario,
  }) => {
    await stopBrowserPreviews(page);
    const workflow = await startSetupWorkflow(page);
    const created = await post(
      page,
      "/api/setup/config/apply",
      await authorizeSetupMutation(page, DRAFT, workflow),
    );
    expect(created.status, JSON.stringify(created.body)).toBe(200);

    // Reviewed against revision A, then the live config moves to revision B.
    const authorized = await authorizeSetupMutation(page, DRAFT, workflow);
    await seedAdminScenario("mqtt_mutate");
    const revisionB = (await maintenanceConfig(page)).revision;

    const stale = await post(page, "/api/setup/config/apply", authorized);
    expect(stale.status, JSON.stringify(stale.body)).toBe(409);
    expect(stale.body.error).toBe("stale_setup_config");

    const after = await maintenanceConfig(page);
    expect(after.revision).toBe(revisionB);

    // The stale rejection revoked the preview: a re-review is required first.
    const replayed = await post(page, "/api/setup/config/apply", authorized);
    expect(replayed.status, JSON.stringify(replayed.body)).toBe(409);
    expect(["setup_preview_required", "setup_preview_mismatch"]).toContain(
      replayed.body.error,
    );

    const reviewed = await post(
      page,
      "/api/setup/config/apply",
      await authorizeSetupMutation(page, DRAFT, await currentWorkflowId(page)!),
    );
    expect(reviewed.status, JSON.stringify(reviewed.body)).toBe(200);
  });

  test("a deleted live config is a conflict for an existing-config draft", async ({
    page,
  }) => {
    await stopBrowserPreviews(page);
    const workflow = await startSetupWorkflow(page);
    const created = await post(
      page,
      "/api/setup/config/apply",
      await authorizeSetupMutation(page, DRAFT, workflow),
    );
    expect(created.status, JSON.stringify(created.body)).toBe(200);
    const authorized = await authorizeSetupMutation(page, DRAFT, workflow);

    await post(page, "/api/admin/test/seed", { scenario: "delete_install_config" });

    const stale = await post(page, "/api/setup/config/apply", authorized);
    expect(stale.status, JSON.stringify(stale.body)).toBe(409);
    expect(stale.body.error).toBe("stale_setup_config");
  });

  test("a config that appeared after a fresh-install draft is a conflict", async ({
    page,
  }) => {
    await stopBrowserPreviews(page);
    const workflow = await startSetupWorkflow(page);
    const authorized = await authorizeSetupMutation(page, DRAFT, workflow);

    // Something else created a live config while the fresh-install draft was
    // open, spending this workflow's preview on different bytes.
    const created = await post(
      page,
      "/api/setup/config/apply",
      await authorizeSetupMutation(page, OTHER_DRAFT, workflow),
    );
    expect(created.status, JSON.stringify(created.body)).toBe(200);
    const appeared = await liveConfigBytes(page);

    const stale = await post(page, "/api/setup/config/apply", authorized);
    expect(stale.status, JSON.stringify(stale.body)).toBe(409);
    expect([
      "stale_setup_config",
      "setup_preview_mismatch",
      "setup_preview_required",
    ]).toContain(stale.body.error);
    // Whichever authority check fires first, the config that appeared stands.
    expect(await liveConfigBytes(page)).toBe(appeared);
  });

  test("an unreviewed draft cannot mutate anything", async ({ page }) => {
    await stopBrowserPreviews(page);
    const refused = await post(page, "/api/setup/config/apply", DRAFT);
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_workflow_required");
  });

  test("a valid workflow without an exact preview cannot mutate", async ({
    page,
  }) => {
    await stopBrowserPreviews(page);
    const workflow = await startSetupWorkflow(page);
    const refused = await post(page, "/api/setup/config/apply", {
      ...DRAFT,
      setup_workflow_id: workflow,
      device_plan_id: await currentDevicePlanId(page, DRAFT.devices),
    });
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_preview_required");
  });
});
