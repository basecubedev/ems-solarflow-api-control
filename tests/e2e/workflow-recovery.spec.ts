import { test, expect, type Page } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { post } from "./helpers/setup-authority";

// Recovering a stranded Admin workflow used to mean deleting a JSON file over
// SSH. Maintenance → Workflow recovery now offers a safe reset that runs the
// normal domain operations, and an advanced release that backs up unreadable
// Admin workflow metadata before quarantining it — never anything the installed
// system owns.
//
// See docs/technical/admin-workflow-state.md.

async function lifecycle(page: Page): Promise<Record<string, any>> {
  return (await page.request.get("/api/admin/workflow-lifecycle")).json();
}

async function installState(page: Page): Promise<Record<string, any>> {
  return (await page.request.get("/api/admin/install-state")).json();
}

async function recoveryBackups(page: Page): Promise<Record<string, any>[]> {
  const { status, body } = await post(page, "/api/admin/test/seed", {
    scenario: "workflow_recovery_backups",
  });
  expect(status, JSON.stringify(body)).toBe(200);
  return body.manifests as Record<string, any>[];
}

/** Walk back to the task selection from whichever panel a workflow reopened. */
async function returnToTaskSelection(page: Page) {
  const start = page.locator('[data-start-path="manage_existing"]');
  if (await start.isVisible()) return;
  await page.locator("[data-back]:visible").first().click();
  if (await start.isVisible()) return;
  await page.locator('#maintenance-hub [data-back="landing"]').click();
  await expect(start).toBeVisible();
}

async function openRecoveryCard(page: Page) {
  await returnToTaskSelection(page);
  await page.locator('[data-start-path="manage_existing"]').click();
  const loaded = page.waitForResponse((response) =>
    response.url().endsWith("/api/admin/workflow-lifecycle/recovery/preview"),
  );
  await page.locator('[data-open-maintenance-path="manual"]').click();
  expect((await loaded).ok()).toBeTruthy();
  const body = page.locator("#maintenance-workflow-recovery-body");
  // A blocking verdict opens the card by itself; a healthy one is expanded by
  // the operator, which is exactly the difference this helper preserves.
  if (!(await body.isVisible())) {
    await page
      .locator('[data-maintenance-toggle="maintenance-workflow-recovery"]')
      .click();
  }
  await expect(body).toBeVisible();
}

async function login(page: Page) {
  const view = new LoginPage(page);
  await view.open();
  await view.authenticate();
}

/** Accept the next ``count`` confirmations the console shows, in order. */
function acceptConfirmations(page: Page, count: number): Promise<string[]> {
  const seen: string[] = [];
  return new Promise((resolve) => {
    const handler = async (dialog: { message(): string; accept(): Promise<void> }) => {
      seen.push(dialog.message());
      await dialog.accept();
      if (seen.length >= count) {
        page.off("dialog", handler as never);
        resolve(seen);
      }
    };
    page.on("dialog", handler as never);
  });
}

test.describe("Workflow recovery", { tag: ["@authority", "@workflow"] }, () => {
  test("a healthy console keeps the recovery card quiet", async ({ page }) => {
    await login(page);
    await openRecoveryCard(page);

    await expect(page.locator("#maintenance-workflow-recovery-summary")).toHaveText(
      /No guided workflow needs recovery/i,
    );
    await expect(page.locator("#maintenance-workflow-recovery-safe")).toBeHidden();
    await expect(
      page.locator("#maintenance-workflow-recovery-advanced"),
    ).toBeHidden();
  });

  test("safe reset clears a stranded Setup cleanup and unblocks both workflows", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("setup_cleanup_pending");
    await page.reload();

    const blocked = await lifecycle(page);
    expect(blocked.state, JSON.stringify(blocked)).toBe("cleanup_pending");
    expect(blocked.switchable).toBe(false);
    const installBefore = await installState(page);

    await openRecoveryCard(page);
    // The blocking state opens the card by itself and offers the safe reset.
    await expect(page.locator("#maintenance-workflow-recovery-safe")).toBeVisible();
    await expect(
      page.locator("#maintenance-workflow-recovery-advanced"),
    ).toBeHidden();

    const confirmations = acceptConfirmations(page, 1);
    const executed = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/admin/workflow-lifecycle/recovery") &&
        response.request().method() === "POST",
    );
    await page.locator("#maintenance-workflow-recovery-safe").click();
    const [plan] = await confirmations;
    expect(plan).toContain("installed system is not changed");
    expect((await executed).ok()).toBeTruthy();

    await expect(page.locator("#maintenance-workflow-recovery-message")).toHaveText(
      /guided workflow was reset/i,
    );
    const after = await lifecycle(page);
    expect(after.switchable, JSON.stringify(after)).toBe(true);
    expect(after.setup.cleanup).toBe("complete");
    // Safe recovery never quarantines a state file, and never touches the install.
    expect(await recoveryBackups(page)).toEqual([]);
    expect(await installState(page)).toEqual(installBefore);
  });

  test("advanced release backs up unreadable workflow state before clearing it", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("installed_system_artifacts");
    const seeded = await post(page, "/api/admin/test/seed", {
      scenario: "workflow_state_corrupt",
    });
    expect(seeded.status, JSON.stringify(seeded.body)).toBe(200);
    await page.reload();

    const malformed = await lifecycle(page);
    expect(malformed.state, JSON.stringify(malformed)).toBe("malformed");
    expect(malformed.switchable).toBe(false);
    const installBefore = await installState(page);

    await openRecoveryCard(page);
    await expect(page.locator("#maintenance-workflow-recovery-state")).toHaveText(
      /Unreadable state/i,
    );
    await expect(page.locator("#maintenance-workflow-recovery-safe")).toBeHidden();
    await expect(
      page.locator("#maintenance-workflow-recovery-advanced"),
    ).toBeVisible();
    await page.locator("#maintenance-workflow-recovery-details > summary").click();
    await expect(page.locator("#maintenance-workflow-recovery-files")).toHaveText(
      "state/guided-setup-workflow.json (cannot be read)",
    );
    await expect(page.locator("#maintenance-workflow-recovery-preserved")).toContainText(
      "config/config.json",
    );

    // Two confirmations: the plan, then the last-chance warning.
    const confirmations = acceptConfirmations(page, 2);
    const executed = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/admin/workflow-lifecycle/recovery") &&
        response.request().method() === "POST",
    );
    await page.locator("#maintenance-workflow-recovery-advanced").click();
    const dialogs = await confirmations;
    expect(dialogs).toHaveLength(2);
    expect(dialogs[0]).toContain("Release stale Admin workflow state?");
    expect(dialogs[1]).toContain("last confirmation");
    expect((await executed).ok()).toBeTruthy();

    const manifests = await recoveryBackups(page);
    expect(manifests).toHaveLength(1);
    expect(manifests[0].mode).toBe("release_stale_state");
    expect(manifests[0].files).toEqual([
      {
        name: "state/guided-setup-workflow.json",
        reason: "unreadable_state",
        sha256: seeded.body.digest,
        bytes: expect.any(Number),
      },
    ]);
    expect(manifests[0].lifecycle_fingerprint).toBe(malformed.fingerprint);

    // Both guided workflows are available again, and the install is untouched.
    const after = await lifecycle(page);
    expect(after.owner, JSON.stringify(after)).toBe("none");
    expect(after.switchable).toBe(true);
    expect(await installState(page)).toEqual(installBefore);
    const digests = await post(page, "/api/admin/test/seed", {
      scenario: "installed_system_artifact_digests",
    });
    expect(digests.body.deployment_marker).toBeTruthy();
    expect(digests.body.legacy_generated_config).toBeTruthy();

    // Guided Setup starts normally afterwards.
    await page.goto("/");
    await page.locator('[data-start-path="setup_new"]').click();
    await expect(page.getByTestId("system-build-select")).toBeVisible();
  });

  test("two records claiming the console are named, not silently resolved", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("workflow_owner_conflict");
    await page.reload();

    const conflicted = await lifecycle(page);
    expect(conflicted.owner, JSON.stringify(conflicted)).toBe("conflict");
    expect(conflicted.state).toBe("conflict");
    expect(conflicted.switchable).toBe(false);
    expect(conflicted.blocking_reason).toBe("workflow_owner_conflict");

    // Neither workflow may be entered around it.
    const refused = await post(page, "/api/admin/workflow-lifecycle/switch", {
      target: "guided_upgrade",
      confirm: true,
      fingerprint: conflicted.fingerprint,
    });
    expect(refused.status).toBe(409);
    expect(refused.body.error).toBe("workflow_owner_conflict");
    const start = await post(page, "/api/admin/start-path", {
      choice: "setup_new",
      confirm: true,
    });
    expect(start.status).toBe(409);
    expect(start.body.error).toBe("workflow_switch_required");

    await openRecoveryCard(page);
    await expect(page.locator("#maintenance-workflow-recovery-owner")).toHaveText(
      /Conflicting workflow records/i,
    );
    // The contradiction survives inspection untouched.
    expect((await lifecycle(page)).owner).toBe("conflict");
  });

  test("recovery is refused while a replacement cannot be disproven", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("workflow_state_corrupt");
    await seedAdminScenario("workflow_replacement_status_unknown");
    await page.reload();

    const blocked = await lifecycle(page);
    const refused = await post(page, "/api/admin/workflow-lifecycle/recovery", {
      mode: "release_stale_state",
      confirm: true,
      reason: "corrupt workflow metadata",
      fingerprint: blocked.fingerprint,
    });

    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("workflow_recovery_unsafe");
    expect(refused.body.detail).toBe("install_state_unavailable");
    expect(await recoveryBackups(page)).toEqual([]);

    // Once Docker proves there is no replacement, the same release succeeds.
    await seedAdminScenario("workflow_replacement_status_inactive");
    const released = await post(page, "/api/admin/workflow-lifecycle/recovery", {
      mode: "release_stale_state",
      confirm: true,
      reason: "corrupt workflow metadata",
      fingerprint: (await lifecycle(page)).fingerprint,
    });

    expect(released.status, JSON.stringify(released.body)).toBe(200);
    expect(released.body.released).toEqual(["state/guided-setup-workflow.json"]);
    expect(await recoveryBackups(page)).toHaveLength(1);
  });

  test("a stale recovery preview cannot execute", async ({
    page,
    seedAdminScenario,
  }) => {
    await login(page);
    await seedAdminScenario("workflow_state_corrupt");
    const stale = (await lifecycle(page)).fingerprint;

    // The state changes after the preview was taken.
    await seedAdminScenario("setup_cleanup_pending");

    const refused = await post(page, "/api/admin/workflow-lifecycle/recovery", {
      mode: "release_stale_state",
      confirm: true,
      reason: "stale preview",
      fingerprint: stale,
    });

    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("workflow_lifecycle_changed");
    expect(await recoveryBackups(page)).toEqual([]);
  });
});
