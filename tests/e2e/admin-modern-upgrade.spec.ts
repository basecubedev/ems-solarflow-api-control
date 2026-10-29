// SPDX-License-Identifier: AGPL-3.0-or-later
// The Guided Upgrade as the Modern Admin console runs it: choosing a version,
// the server's verification, the run with its progress, the Admin console
// being replaced in the middle of it, and every way it can end. Verification
// and the setup switch run against the deterministic test-mode Admin; the run
// itself is answered by route fixtures shaped like admin/server.py's answers,
// because it would recreate real containers.
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page, Route } from "@playwright/test";

const TARGET = "v9.9.10";

async function signIn(page: Page) {
  await expect(page.getByTestId("sign-in-screen")).toBeVisible();
  await page.fill("#admin-password", ADMIN_PASSWORD);
  await page.getByTestId("sign-in-submit").click();
  await expect(page.locator(".app")).toBeVisible();
}

async function openUpdates(page: Page, seed: (scenario: string) => Promise<void>, scenario = "mqtt_migration") {
  await page.goto("/");
  await signIn(page);
  await seed(scenario);
  await page.goto("/#/updates");
  await page.reload();
  await expect(page.getByTestId("releases")).toBeVisible();
}

async function choose(page: Page, tag = TARGET) {
  await page.locator(`[data-tag="${tag}"]`).getByTestId("choose-release").click();
  await expect(page.getByTestId("upgrade")).toHaveAttribute("data-stage", "ready", { timeout: 30_000 });
}

async function startAndConfirm(page: Page) {
  await page.getByTestId("upgrade-start").click();
  await page.getByRole("dialog").getByTestId("confirm").click();
}

function gate() {
  let open: () => void = () => {};
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { opened, open: () => open() };
}

const PLANNED = ["verify_image", "preflight", "migration_review", "backup", "mqtt_migration", "target_resources", "config_check", "config_write", "pull_image", "update_compose", "recreate_ems", "diagnostics"];

function jobSteps(doneUntil: number, failedAt = -1) {
  return PLANNED.map((key, index) => ({
    key,
    label: key,
    state: index === failedAt ? "failed" : index < doneUntil ? "done" : index === doneUntil ? "running" : "pending",
    message: index === failedAt ? "manifest unknown" : null,
  }));
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function transition(stage: string, extra: Record<string, unknown> = {}) {
  return { mode: "guided_upgrade", stage, operation_id: "op-e2e", system_tag: TARGET, created_at: new Date().toISOString(), ...extra };
}

function status(route: Route, current: Record<string, unknown> | null) {
  const terminal = !current || ["completed", "cancelled"].includes(String(current.stage));
  return json(route, { ok: true, active: !terminal, transition: current, known_good: null, running_pair: null });
}

async function keepAdmin(page: Page) {
  await page.route("**/api/admin/maintenance/upgrade/validate", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await json(route, { ...body, admin_update_required: false, alignment: "aligned" }, response.status());
  });
}


test.describe("Modern Admin: the guided upgrade", { tag: ["@smoke", "@workflow"] }, () => {
  test("a chosen version is verified by the server and shows what the update will do before anything runs", async ({ page, seedAdminScenario }) => {
    await openUpdates(page, seedAdminScenario);
    const validated = page.waitForResponse((response) => response.url().endsWith("/api/admin/maintenance/upgrade/validate"));
    await page.locator(`[data-tag="${TARGET}"]`).getByTestId("choose-release").click();
    expect((await validated).ok()).toBeTruthy();
    const panel = page.getByTestId("upgrade");
    await expect(panel).toHaveAttribute("data-stage", "ready");
    await expect(panel).toBeFocused();
    await expect(page.getByRole("heading", { name: `Update to ${TARGET}` })).toBeVisible();
    await expect(page.getByTestId("upgrade-migration")).toContainText("The MQTT setup of 1 device is migrated");
    await expect(panel).toContainText("The Admin console is updated and restarts");
    await expect(page.getByTestId("upgrade-choice-backup")).toBeChecked();
    await page.getByTestId("upgrade-choice-backup").uncheck();
    await expect(panel).toContainText("No backup is made first");
    await expect(page.getByTestId("upgrade-start")).toHaveText(`Update to ${TARGET}`);
    await expect(page.getByTestId("start-upgrade")).toHaveCount(0);
    await expect(page.locator(`[data-tag="${TARGET}"]`).getByTestId("choose-release")).toContainText("Chosen");
    await page.getByTestId("upgrade-close").click();
    await expect(panel).toHaveCount(0);
  });

  test("while a chosen version is prepared the page says so at the top, in its card and on the version, then marks it ready", async ({ page, seedAdminScenario }) => {
    await openUpdates(page, seedAdminScenario);
    const release = gate();
    await page.route("**/api/admin/maintenance/upgrade/validate", async (route) => {
      await release.opened;
      await route.fallback();
    });
    await page.locator(`[data-tag="${TARGET}"]`).getByTestId("choose-release").click();
    const panel = page.getByTestId("upgrade");
    await expect(panel).toHaveAttribute("data-stage", "verifying");
    await expect(page.getByTestId("upgrade-working")).toContainText(`Checking ${TARGET}`);
    await expect(page.getByTestId("upgrade-clock")).toHaveText(/Running for 0:(0[2-9]|[1-5]\d)/, { timeout: 10_000 });
    await page.goto("/#/overview");
    await page.goto("/#/updates");
    await expect(page.getByTestId("upgrade-clock")).toHaveText(/Running for 0:(0[2-9]|[1-5]\d)/);
    await expect(page.getByTestId("upgrade-now")).toContainText(`Update to ${TARGET} · Checking the version`);
    await expect(page.locator(`[data-tag="${TARGET}"]`).getByTestId("choose-release")).toContainText("Checking");
    await expect(page.getByTestId("start-upgrade")).toHaveCount(0);
    const order = await page.locator(".content > .card").evaluateAll((cards) => cards.map((card) => card.querySelector(".card-title")?.textContent));
    expect(order.slice(0, 2)).toEqual(["What runs now", `Update to ${TARGET}`]);
    release.open();
    await expect(panel).toHaveAttribute("data-stage", "ready", { timeout: 30_000 });
    await expect(page.getByTestId("upgrade-working")).toHaveCount(0);
    await expect(page.getByTestId("upgrade-ready")).toContainText(`${TARGET} is ready to install`);
    await expect(page.getByTestId("upgrade-now")).toContainText("Checked. Nothing has changed yet");
    await expect(page.locator(`[data-tag="${TARGET}"]`).getByTestId("choose-release")).toContainText("Chosen");
  });

  test("a run that keeps the Admin console follows its phases to the end and says what runs now", async ({ page, seedAdminScenario }) => {
    await keepAdmin(page);
    await openUpdates(page, seedAdminScenario);
    await choose(page);
    let sent: Record<string, unknown> | null = null;
    const finish = gate();
    await page.route("**/api/admin/maintenance/upgrade/execute", async (route) => {
      sent = route.request().postDataJSON();
      await json(route, { ok: true, job_id: "e2e-run", status: "running", steps: jobSteps(0), transition: transition("ems_operation_running") }, 202);
    });
    let done = false;
    finish.opened.then(() => (done = true));
    await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-run", async (route) => {
      if (!done) {
        await json(route, { ok: true, job_id: "e2e-run", status: "running", steps: jobSteps(8), transition: transition("ems_operation_running") });
        return;
      }
      await json(route, {
        ok: true,
        job_id: "e2e-run",
        status: "succeeded",
        steps: jobSteps(PLANNED.length),
        result: { ok: true, target_release: TARGET, warnings: [], steps: [{ id: "backup", status: "ok", archive: "/opt/ems/backups/ems-backup-20261021-1200.tar.gz" }] },
        transition: transition("completed"),
      });
    });
    await startAndConfirm(page);
    const phases = page.getByTestId("upgrade-phases");
    await expect(phases.locator('[data-phase="install"]')).toHaveAttribute("data-state", "running");
    await expect(phases.locator('[data-phase="check"]')).toHaveAttribute("data-state", "done");
    await expect(phases.locator('[data-phase="admin"]')).toHaveCount(0);
    await expect(page.getByTestId("upgrade-working")).toContainText("Step 3 of 4 · Install EMS");
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuemax", "4");
    await expect(page.getByTestId("upgrade-clock")).toContainText("Running for");
    finish.open();
    await expect(page.getByTestId("upgrade-done")).toContainText("Update complete");
    await expect(page.getByTestId("upgrade-done")).toContainText(`EMS now runs ${TARGET}.`);
    await expect(page.getByTestId("upgrade")).toContainText("ems-backup-20261021-1200.tar.gz");
    expect(sent).toMatchObject({ confirm: true, target_release: TARGET, options: { backup: true, pull_image: true, recreate: true, diagnostics: true } });
    expect(typeof sent!.selection_fingerprint).toBe("string");
    expect(sent!.migration_revision).toBeTruthy();
    await page.getByTestId("upgrade-finish").click();
    await expect(page.getByTestId("upgrade")).toHaveCount(0);
  });

  test("an update that replaces the Admin console reconnects, asks to sign in to the new one and follows the run there", async ({ page, seedAdminScenario }) => {
    await openUpdates(page, seedAdminScenario);
    await choose(page);
    let started = false;
    let authAfterStart = 0;
    let replaced = false;
    let signedInAgain = false;
    await page.route("**/api/admin/maintenance/upgrade/execute", async (route) => {
      started = true;
      await json(route, { ok: true, status: "admin_alignment_started", stage: "admin_update_pending", reconnect: true, operation_id: "op-e2e" }, 202);
    });
    await page.route("**/api/admin/auth/status", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (started && !replaced && ++authAfterStart >= 2) replaced = true;
      if (replaced) {
        body.admin_instance_id = "replacement-instance";
        if (!signedInAgain) {
          body.authenticated = false;
          delete body.csrf_token;
        }
      }
      await json(route, body, response.status());
    });
    await page.route("**/api/admin/auth/login", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (replaced) {
        body.admin_instance_id = "replacement-instance";
        signedInAgain = true;
      }
      await json(route, body, response.status());
    });
    await page.route("**/api/admin/system-alignment/status", (route) =>
      status(route, transition(replaced ? "resources_verified" : "admin_update_pending", { admin_alignment_required: true })),
    );
    let resumed: unknown = null;
    await page.route("**/api/admin/maintenance/upgrade/resume", async (route) => {
      resumed = route.request().postDataJSON();
      await json(route, { ok: true, job_id: "e2e-resumed", status: "running", steps: jobSteps(5) }, 202);
    });
    await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-resumed", (route) =>
      json(route, { ok: true, job_id: "e2e-resumed", status: "succeeded", steps: jobSteps(PLANNED.length), result: { ok: true, target_release: TARGET, steps: [] } }),
    );
    const reloaded = page.waitForEvent("framenavigated", { predicate: (frame) => frame === page.mainFrame() });
    await startAndConfirm(page);
    await reloaded;
    await expect(page.getByTestId("sign-in-notice")).toContainText("An update is running");
    expect(resumed).toBeNull();
    await page.fill("#admin-password", ADMIN_PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await expect(page).toHaveURL(/\/#\/updates$/);
    await expect(page.getByTestId("upgrade-done")).toContainText(`EMS and the Admin console now run ${TARGET}`);
    expect(resumed).toEqual({ operation_id: "op-e2e" });
  });

  test("a refusal before anything ran says the update did not start and that nothing changed", async ({ page, seedAdminScenario }) => {
    await openUpdates(page, seedAdminScenario);
    await choose(page);
    await page.route("**/api/admin/maintenance/upgrade/execute", (route) =>
      json(route, { ok: false, status: "conflict", reason: "upgrade_direction_blocked", message: "This build may not replace the running EMS build." }, 409),
    );
    await startAndConfirm(page);
    const failed = page.getByTestId("upgrade-failed");
    await expect(failed).toContainText("The update did not start");
    await expect(page.getByTestId("upgrade")).toContainText("This build may not replace the running EMS build.");
    await expect(page.getByTestId("upgrade")).toContainText("It stopped before anything was changed");
    await page.getByTestId("upgrade-choose-again").click();
    await expect(page.getByTestId("upgrade")).toHaveCount(0);
  });

  test("a run that stops says where, offers only the server's ways out, and a cancel ends it", async ({ page, seedAdminScenario }) => {
    await keepAdmin(page);
    await openUpdates(page, seedAdminScenario);
    await choose(page);
    let cancelled = false;
    await page.route("**/api/admin/maintenance/upgrade/execute", (route) =>
      json(route, { ok: true, job_id: "e2e-fail", status: "running", steps: jobSteps(0), transition: transition("ems_operation_running") }, 202),
    );
    await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-fail", (route) =>
      json(route, {
        ok: true,
        job_id: "e2e-fail",
        status: "failed",
        steps: jobSteps(10, 10),
        result: { ok: false, message: "EMS could not be recreated.", steps: [{ id: "recreate_ems", status: "error", detail: "manifest unknown" }] },
      }),
    );
    await page.route("**/api/admin/system-alignment/status", (route) =>
      status(route, transition(cancelled ? "cancelled" : "failed_recoverable", { resume_available: true, return_available: false, cancel_available: true, worker_active: false })),
    );
    let cancelBody: unknown = null;
    await page.route("**/api/admin/system-alignment/cancel", async (route) => {
      cancelBody = route.request().postDataJSON();
      cancelled = true;
      await json(route, { ok: true, stage: "cancelled", status: "cancelled", transition: transition("cancelled") });
    });
    await startAndConfirm(page);
    const failed = page.getByTestId("upgrade-failed");
    await expect(failed).toContainText("The update did not finish");
    await expect(failed).toContainText(`Stopped at: Install EMS ${TARGET}`);
    await expect(page.getByTestId("upgrade")).toContainText("EMS could not be recreated.");
    await expect(page.getByTestId("upgrade")).toContainText("Parts of the update may already have been applied");
    await expect(page.getByTestId("upgrade-retry")).toBeVisible();
    await expect(page.getByTestId("upgrade-cancel")).toBeVisible();
    await expect(page.getByTestId("upgrade-return")).toHaveCount(0);
    await expect(page.getByTestId("upgrade-close")).toHaveCount(0);
    await expect(page.getByTestId("upgrade-choose-again")).toHaveCount(0);
    await page.getByTestId("upgrade-cancel").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("upgrade-failed")).toContainText("The update was cancelled");
    expect(cancelBody).toEqual({ operation_id: "op-e2e", confirm: true });
  });

  test("try again after a failed install continues the run and deploys once more", async ({ page, seedAdminScenario }) => {
    await keepAdmin(page);
    await openUpdates(page, seedAdminScenario);
    await choose(page);
    let stage = "ems_operation_running";
    await page.route("**/api/admin/maintenance/upgrade/execute", (route) =>
      json(route, { ok: true, job_id: "e2e-first", status: "running", steps: jobSteps(0), transition: transition(stage) }, 202),
    );
    await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-first", (route) => {
      stage = "failed_recoverable";
      return json(route, { ok: true, job_id: "e2e-first", status: "failed", steps: jobSteps(8, 8), result: { ok: false, reason: "image_pull_failed", message: "Pull failed.", steps: [] } });
    });
    await page.route("**/api/admin/system-alignment/status", (route) =>
      status(route, transition(stage, { resume_available: stage === "failed_recoverable", cancel_available: stage === "failed_recoverable", failed_stage: "ems_operation_running" })),
    );
    let retried: unknown = null;
    await page.route("**/api/admin/system-alignment/resume", async (route) => {
      retried = route.request().postDataJSON();
      stage = "ems_operation_pending";
      await json(route, { ok: true, operation_id: "op-e2e", stage });
    });
    let continued = 0;
    await page.route("**/api/admin/maintenance/upgrade/resume", async (route) => {
      continued += 1;
      await json(route, { ok: true, job_id: "e2e-second", status: "running", steps: jobSteps(8) }, 202);
    });
    await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-second", (route) => {
      stage = "completed";
      return json(route, { ok: true, job_id: "e2e-second", status: "succeeded", steps: jobSteps(PLANNED.length), result: { ok: true, target_release: TARGET, steps: [] }, transition: transition("completed") });
    });
    await startAndConfirm(page);
    await expect(page.getByTestId("upgrade-failed")).toContainText(`Stopped at: Install EMS ${TARGET}`);
    await page.getByTestId("upgrade-retry").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("upgrade-done")).toContainText("Update complete");
    expect(retried).toEqual({ operation_id: "op-e2e", tag: TARGET });
    expect(continued).toBe(1);
  });

  test("going back to the version that runs ends this update and says the Admin console restarts", async ({ page, seedAdminScenario }) => {
    await page.route("**/api/admin/system-alignment/status", (route) =>
      status(route, transition("failed_recoverable", { failed_stage: "admin_reconnect_pending", error_message: "The Admin replacement did not answer.", resume_available: true, return_available: true, cancel_available: true })),
    );
    let returnBody: unknown = null;
    await page.route("**/api/admin/system-alignment/return-to-running-build", async (route) => {
      returnBody = route.request().postDataJSON();
      await json(route, { ok: true, status: "admin_return_started", stage: "admin_update_pending", reconnect: true, operation_id: "op-return" });
    });
    await openUpdates(page, seedAdminScenario);
    const failed = page.getByTestId("upgrade-failed");
    await expect(failed).toContainText("Stopped at: Update the Admin console");
    await expect(page.getByTestId("upgrade")).toContainText("The Admin replacement did not answer.");
    await page.getByTestId("upgrade-return").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("upgrade-returned")).toContainText("Back to the version that runs");
    await expect(page.getByTestId("upgrade-returned")).toContainText("restarts");
    expect(returnBody).toEqual({ operation_id: "op-e2e", confirm: true, acknowledge_risk: false });
  });

  test("going back when the Admin console already runs that version continues the alignment here and leads to its release", async ({ page, seedAdminScenario }) => {
    let current = transition("failed_recoverable", { failed_stage: "ems_operation_running", error_message: "EMS did not start.", resume_available: true, return_available: true, cancel_available: true });
    await page.route("**/api/admin/system-alignment/status", (route) => status(route, current));
    await page.route("**/api/admin/system-alignment/return-to-running-build", async (route) => {
      current = transition("admin_aligned", { mode: "align_existing_install", operation_id: "op-return" });
      await json(route, { ok: true, status: "admin_return_started", stage: "admin_aligned", reconnect: false, operation_id: "op-return" });
    });
    let resumed: unknown = null;
    await page.route("**/api/admin/system-alignment/resume", async (route) => {
      resumed = route.request().postDataJSON();
      current = transition("resources_verified", { mode: "align_existing_install", operation_id: "op-return" });
      await json(route, { ok: true, ...current });
    });
    await openUpdates(page, seedAdminScenario);
    await page.getByTestId("upgrade-return").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("upgrade-returned")).toContainText("The last step is below, under System alignment.");
    await expect(page.getByTestId("upgrade-returned").getByRole("link")).toHaveCount(0);
    const card = page.getByTestId("alignment");
    await expect(card).toContainText("the files of that version still need checking");
    await card.getByTestId("alignment-continue").click();
    await expect(card).toContainText("What is left is to release the console, under System.");
    await expect(card.getByTestId("alignment-continue")).toHaveCount(0);
    await expect(card.getByTestId("alignment-release")).toHaveAttribute("href", "#/system");
    expect(resumed).toEqual({ operation_id: "op-return", tag: TARGET });
  });

  test("a start that failed after its work does not take over an earlier run of the same version", async ({ page, seedAdminScenario }) => {
    await page.route("**/api/admin/system-alignment/status", (route) => status(route, transition("cancelled", { operation_id: "op-earlier" })));
    await openUpdates(page, seedAdminScenario);
    await choose(page);
    await page.route("**/api/admin/maintenance/upgrade/execute", (route) =>
      json(route, { ok: false, status: "error", message: "Upgrade failed unexpectedly." }, 500),
    );
    await startAndConfirm(page);
    const failed = page.getByTestId("upgrade-failed");
    await expect(failed).toContainText("The update did not finish");
    await expect(page.getByTestId("upgrade")).toContainText("Upgrade failed unexpectedly.");
    await expect(page.getByTestId("upgrade")).toContainText("Parts of the update may already have been applied");
  });

  test("a retry that relaunches the Admin console waits for the new one instead of asking the old one again", async ({ page, seedAdminScenario }) => {
    let stage = "failed_recoverable";
    let polls = 0;
    let pollsAfterRetry: () => void = () => {};
    const waited = new Promise<void>((resolve) => {
      pollsAfterRetry = resolve;
    });
    await page.route("**/api/admin/system-alignment/status", (route) => {
      if (stage !== "failed_recoverable" && ++polls >= 3) pollsAfterRetry();
      return status(route, transition(stage, { failed_stage: "admin_reconnect_pending", resume_available: stage === "failed_recoverable", admin_alignment_required: true }));
    });
    await page.route("**/api/admin/system-alignment/resume", async (route) => {
      stage = "admin_reconnect_pending";
      await json(route, { ok: true, status: "admin_alignment_started", stage, reconnect: true, operation_id: "op-e2e" });
    });
    let continued = 0;
    await page.route("**/api/admin/maintenance/upgrade/resume", async (route) => {
      continued += 1;
      await json(route, { ok: true, status: "admin_reconnect_pending", stage: "admin_reconnect_pending", operation_id: "op-e2e", reconnect: true }, 202);
    });
    await openUpdates(page, seedAdminScenario);
    await expect(page.getByTestId("upgrade-retry")).toBeVisible();
    await page.evaluate((tag) => sessionStorage.setItem("ems-admin.upgrade", JSON.stringify({ target: tag, operationId: "op-e2e", instance: "an-earlier-admin-process" })), TARGET);
    await page.getByTestId("upgrade-retry").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await waited;
    await expect(page.getByTestId("upgrade")).toHaveAttribute("data-stage", "replacing");
    expect(continued).toBe(0);
  });

  test("a run the server will not continue is shown as stopped once, not asked again and again", async ({ page, seedAdminScenario }) => {
    let resumes = 0;
    let reads = 0;
    await page.clock.install();
    await page.route("**/api/admin/system-alignment/status", (route) => {
      reads += 1;
      return status(route, transition("ems_operation_pending"));
    });
    await page.route("**/api/admin/maintenance/upgrade/resume", async (route) => {
      resumes += 1;
      await json(route, { ok: false, error: "guided_upgrade_context_unavailable", message: "The Guided Upgrade context could not be restored." }, 409);
    });
    await openUpdates(page, seedAdminScenario);
    await expect(page.getByTestId("upgrade-failed")).toContainText("The update did not finish");
    await expect(page.getByTestId("upgrade")).toContainText("The Guided Upgrade context could not be restored.");
    const before = reads;
    const asked = resumes;
    await page.clock.runFor(130_000);
    await expect.poll(() => reads).toBeGreaterThan(before);
    expect(resumes).toBe(asked);
  });

  test("an unfinished guided setup is discarded through the workflow switch, then the version is verified", async ({ page, seedAdminScenario }) => {
    await openUpdates(page, seedAdminScenario, "guided_upgrade_blocking_setup");
    await page.locator(`[data-tag="${TARGET}"]`).getByTestId("choose-release").click();
    await expect(page.getByTestId("upgrade")).toHaveAttribute("data-stage", "blocked");
    await page.getByTestId("upgrade-discard-setup").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("This is stopped and removed:");
    await expect(dialog.locator('p[lang="en"]').first()).toBeVisible();
    const switched = page.waitForResponse((response) => response.url().endsWith("/api/admin/workflow-lifecycle/switch"));
    await dialog.getByTestId("confirm").click();
    expect((await switched).ok()).toBeTruthy();
    await expect(page.getByTestId("upgrade")).toHaveAttribute("data-stage", "ready", { timeout: 30_000 });
  });
});
