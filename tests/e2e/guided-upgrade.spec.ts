import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

const ORDERED_STEPS = [
  ["resolve", "Resolve and verify target System Build"],
  ["inspect", "Inspect current installation"],
  ["migration_review", "Review Zendure MQTT migration"],
  ["backup", "Create and verify backup"],
  ["migration_apply", "Apply Zendure MQTT migration"],
  ["config_upgrade", "Run generic config upgrade"],
  ["config_validate", "Validate final config"],
  ["admin_align", "Align Admin"],
  ["prepare", "Prepare target EMS image and resources"],
  ["recreate", "Recreate EMS"],
  ["health", "Health check"],
  ["diagnostics", "Diagnostics"],
  ["known_good", "Mark Known-Good"],
] as const;

async function openPlannedUpgrade(
  page: Page,
  seedAdminScenario: (scenario: string) => Promise<void>,
) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mqtt_migration");
  await page.reload();
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="upgrade"]').click();
  const select = page.locator("#upgrade-release-select");
  await expect(select).toBeEnabled();
  await select.selectOption("v9.9.10");
  const validation = page.waitForResponse((response) =>
    response.url().endsWith("/maintenance/upgrade/validate"),
  );
  await page.locator("#upgrade-prepare-btn").click();
  expect((await validation).ok()).toBeTruthy();
  await expect(page.locator("#upgrade-release-status")).toHaveText(
    /System Build verified/i,
  );
  await page.locator("#upgrade-plan-btn").click();
  await expect(page.locator("#upgrade-validation")).toContainText(
    /Review Zendure MQTT migration: 1 affected device/i,
  );
  await expect(page.locator("#upgrade-validation")).toContainText(
    /Create a pre-upgrade backup/i,
  );
  await expect(
    page.locator('[data-upgrade-option="backup"]'),
  ).toBeChecked();
  await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();
}

// Open Maintenance → Guided Upgrade without verifying, so selection-only
// behaviour can be observed.
async function openUpgradePanel(
  page: Page,
  seedAdminScenario: (scenario: string) => Promise<void>,
) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario("mqtt_migration");
  await page.reload();
  await page.locator('[data-start-path="manage_existing"]').click();
  await page.locator('[data-open-maintenance-path="upgrade"]').click();
  const select = page.locator("#upgrade-release-select");
  await expect(select).toBeEnabled();
  return select;
}

function countUpgradeValidations(page: Page) {
  const state = { count: 0 };
  page.on("request", (request) => {
    if (
      request.url().includes("/maintenance/upgrade/validate") &&
      request.method() === "POST"
    ) {
      state.count += 1;
    }
  });
  return state;
}

function responseGate() {
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { pending, release: () => release() };
}

function pendingSteps() {
  return ORDERED_STEPS.map(([key, label]) => ({ key, label, state: "pending" }));
}

function resultSteps(
  failureKey?: string,
  failureDetail = "simulated failure",
) {
  let failed = false;
  return ORDERED_STEPS.map(([key, label]) => {
    if (failed) return { key, label, status: "skipped" };
    if (key === failureKey) {
      failed = true;
      return { key, label, status: "error", detail: failureDetail };
    }
    return { key, label, status: "ok", detail: "completed" };
  });
}

async function mockUpgradeJob(
  page: Page,
  result: {
    ok: boolean;
    steps: ReturnType<typeof resultSteps>;
    message?: string;
    reason?: string;
  },
) {
  await page.route("**/api/admin/maintenance/upgrade/execute", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, job_id: "e2e-upgrade", steps: pendingSteps() }),
    });
  });
  await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-upgrade", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        status: result.ok ? "succeeded" : "failed",
        steps: pendingSteps().map((step) => ({ ...step, state: result.ok ? "done" : "failed" })),
        result: { ...result, target_release: "v9.9.10" },
      }),
    });
  });
}

async function confirmAndExecute(page: Page) {
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#upgrade-execute-btn").click();
  await expect(page.locator("#upgrade-execute-btn")).toHaveText("Upgrade system");
}

test.describe("Guided Upgrade", () => {
  test("preflight shows migration and backup, then renders ordered progress", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    let postPlanReviewRequests = 0;
    page.on("request", (request) => {
      if (request.url().includes("/zendure-mqtt/migration-review")) {
        postPlanReviewRequests += 1;
      }
    });
    await mockUpgradeJob(page, { ok: true, steps: resultSteps() });
    await confirmAndExecute(page);

    await expect(page.locator("#upgrade-validation")).toContainText(
      /Upgrade completed: v9.9.10/i,
    );
    const rows = await page
      .locator("#upgrade-validation .config-validation-item")
      .allTextContents();
    const labels = rows.slice(0, ORDERED_STEPS.length).map((row) =>
      ORDERED_STEPS.find(([, label]) => row.includes(label))?.[1],
    );
    expect(labels).toEqual(ORDERED_STEPS.map(([, label]) => label));
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Upgrade completed");
    await expect(page.locator("#upgrade-plan-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await page.locator("#upgrade-plan-btn").dispatchEvent("click");
    expect(postPlanReviewRequests).toBe(0);
  });

  test("one plan click is busy immediately and produces one executable plan", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);
    await select.selectOption("v9.9.10");
    await page.locator("#upgrade-prepare-btn").click();
    await expect(page.locator("#upgrade-release-status")).toHaveText(
      /System Build verified/i,
    );
    await expect(page.locator("#upgrade-plan-btn")).toBeEnabled();

    const gate = responseGate();
    let reviewRequests = 0;
    let markSeen: () => void = () => {};
    const seen = new Promise<void>((resolve) => {
      markSeen = resolve;
    });
    await page.route(
      "**/api/admin/maintenance/zendure-mqtt/migration-review",
      async (route: Route) => {
        reviewRequests += 1;
        markSeen();
        await gate.pending;
        await route.continue();
      },
    );

    await page.locator("#upgrade-plan-btn").click();
    await seen;
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Planning…");
    await expect(page.locator("#upgrade-plan-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-validation")).toContainText(
      /Refreshing migration review and building the upgrade plan/i,
    );
    expect(reviewRequests).toBe(1);

    gate.release();
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Plan ready");
    await expect(page.locator("#upgrade-plan-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();
    expect(reviewRequests).toBe(1);
  });

  test("verification keeps planning gated until the verified response is current", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);
    await select.selectOption("v9.9.10");

    const gate = responseGate();
    let markSeen: () => void = () => {};
    const seen = new Promise<void>((resolve) => {
      markSeen = resolve;
    });
    await page.route(
      "**/api/admin/maintenance/upgrade/validate",
      async (route: Route) => {
        markSeen();
        await gate.pending;
        await route.continue();
      },
    );
    let reviewRequests = 0;
    await page.route(
      "**/api/admin/maintenance/zendure-mqtt/migration-review",
      async (route: Route) => {
        reviewRequests += 1;
        await route.continue();
      },
    );

    await page.locator("#upgrade-prepare-btn").click();
    await seen;
    await expect(page.locator("#upgrade-release-status")).toHaveText(
      /Downloading and verifying/i,
    );
    await expect(page.locator("#upgrade-plan-btn")).toBeDisabled();
    await page.locator("#upgrade-plan-btn").dispatchEvent("click");
    expect(reviewRequests).toBe(0);
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();

    gate.release();
    await expect(page.locator("#upgrade-release-status")).toHaveText(
      /System Build verified/i,
    );
    await expect(page.locator("#upgrade-plan-btn")).toBeEnabled();
    await page.locator("#upgrade-plan-btn").click();
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Plan ready");
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();
    expect(reviewRequests).toBe(1);
  });

  test("rapid plan clicks share one migration review", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);
    await select.selectOption("v9.9.10");
    await page.locator("#upgrade-prepare-btn").click();
    await expect(page.locator("#upgrade-plan-btn")).toBeEnabled();

    const gate = responseGate();
    let reviewRequests = 0;
    let markSeen: () => void = () => {};
    const seen = new Promise<void>((resolve) => {
      markSeen = resolve;
    });
    await page.route(
      "**/api/admin/maintenance/zendure-mqtt/migration-review",
      async (route: Route) => {
        reviewRequests += 1;
        markSeen();
        await gate.pending;
        await route.continue();
      },
    );

    await page.locator("#upgrade-plan-btn").dispatchEvent("click");
    await page.locator("#upgrade-plan-btn").dispatchEvent("click");
    await seen;
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Planning…");
    expect(reviewRequests).toBe(1);
    gate.release();
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Plan ready");
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();
    expect(reviewRequests).toBe(1);
  });

  test("a stale planning response cannot enable a newly selected target", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);
    await select.selectOption("v9.9.10");
    await page.locator("#upgrade-prepare-btn").click();
    await expect(page.locator("#upgrade-plan-btn")).toBeEnabled();

    const gate = responseGate();
    let markSeen: () => void = () => {};
    const seen = new Promise<void>((resolve) => {
      markSeen = resolve;
    });
    await page.route(
      "**/api/admin/maintenance/zendure-mqtt/migration-review",
      async (route: Route) => {
        markSeen();
        await gate.pending;
        await route.continue();
      },
    );

    await page.locator("#upgrade-plan-btn").click();
    await seen;
    await select.selectOption("v9.9.11");
    await expect(select).toHaveValue("v9.9.11");
    gate.release();

    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Plan upgrade");
    await expect(page.locator("#upgrade-plan-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-validation")).toContainText(
      /verified System Build changed while planning/i,
    );
    await expect(select).toHaveValue("v9.9.11");
  });

  test("changing a planned option invalidates the visible plan", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Plan ready");
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();

    await page.locator('[data-upgrade-option="backup"]').uncheck();

    await expect(page.locator("#upgrade-plan-btn")).toHaveText("Plan upgrade");
    await expect(page.locator("#upgrade-plan-btn")).toBeEnabled();
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Create a pre-upgrade backup/i,
    );
  });

  test("migration/config failure stops before replacement", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await mockUpgradeJob(page, {
      ok: false,
      steps: resultSteps("config_upgrade", "target config validation failed"),
      message: "Upgrade stopped before container replacement.",
    });
    await confirmAndExecute(page);

    await expect(page.locator("#upgrade-validation")).toContainText(
      /target config validation failed/i,
    );
    await expect(page.locator("#upgrade-validation")).toContainText(
      /stopped before container replacement/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Recreate EMS/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Upgrade completed/i,
    );
  });

  test("health failure never reports success or Known-Good", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await mockUpgradeJob(page, {
      ok: false,
      steps: resultSteps("health", "target EMS did not become healthy"),
      message: "Health verification failed; recovery is available.",
    });
    await confirmAndExecute(page);

    await expect(page.locator("#upgrade-validation")).toContainText(
      /target EMS did not become healthy/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Mark Known-Good/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Upgrade completed/i,
    );
  });

  test("validate cancels an abandoned setup transition that would block execute", async ({
    page,
    seedAdminScenario,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("guided_upgrade_blocking_setup");
    await page.reload();

    const blocking = await page.evaluate(async () => {
      const res = await fetch("/api/admin/system-alignment/status", {
        cache: "no-store",
      });
      return res.json();
    });
    expect(blocking.active).toBe(true);
    expect(blocking.transition.mode).toBe("fresh_install");

    // An active setup transition reopens Guided Setup on load; the operator
    // returns to the landing gate before entering Maintenance.
    await page.locator("#upgrade-release-select").waitFor({ state: "attached" });
    await page.locator('[data-back]:visible').first().click();
    await page.locator('[data-start-path="manage_existing"]').click();
    await page.locator('[data-open-maintenance-path="upgrade"]').click();
    const select = page.locator("#upgrade-release-select");
    await expect(select).toBeEnabled();
    await select.selectOption("v9.9.10");

    const cancelled = page.waitForResponse((response) =>
      response.url().endsWith("/system-alignment/cancel"),
    );
    await page.locator("#upgrade-prepare-btn").click();
    expect((await cancelled).ok()).toBeTruthy();

    await expect(page.locator("#upgrade-release-status")).toHaveText(
      /System Build verified/i,
    );

    const cleared = await page.evaluate(async () => {
      const res = await fetch("/api/admin/system-alignment/status", {
        cache: "no-store",
      });
      return res.json();
    });
    expect(cleared.active).toBe(false);
  });

  test("selecting targets is side-effect free", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);
    const validations = countUpgradeValidations(page);

    // Browse several targets without verifying.
    await select.selectOption("v9.9.10");
    await select.selectOption("v9.9.11");
    await select.selectOption("v9.9.10");

    // No verification request, no download progress, Upgrade System stays blocked.
    expect(validations.count).toBe(0);
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(
      /Verify System Build/i,
    );
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-release-status")).not.toHaveText(
      /Downloading and verifying/i,
    );
  });

  test("a stale verification cannot verify a newer target", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);

    let releaseFirst: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let markSeen: () => void = () => {};
    const seen = new Promise<void>((resolve) => {
      markSeen = resolve;
    });
    await page.route(
      "**/api/admin/maintenance/upgrade/validate",
      async (route: Route) => {
        if (route.request().postDataJSON()?.tag === "v9.9.10") {
          markSeen();
          await held;
        }
        await route.continue();
      },
    );

    // Verify v9.9.10; hold its response, then select v9.9.11.
    await select.selectOption("v9.9.10");
    await page.locator("#upgrade-prepare-btn").click();
    await seen;
    await select.selectOption("v9.9.11");
    await expect(select).toHaveValue("v9.9.11");
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(
      /Verify System Build/i,
    );

    // Release the stale v9.9.10 response; it must not verify v9.9.11.
    releaseFirst();
    await expect(select).toHaveValue("v9.9.11");
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(
      /Verify System Build/i,
    );
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
  });

  test("a registry rate-limit is shown and keeps the upgrade unverified", async ({
    page,
    seedAdminScenario,
  }) => {
    const select = await openUpgradePanel(page, seedAdminScenario);
    await select.selectOption("v9.9.10");

    await page.route(
      "**/api/admin/maintenance/upgrade/validate",
      (route: Route) =>
        route.fulfill({
          status: 429,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            error: "system_build_registry_rate_limited",
            message:
              "GitHub Container Registry rate limit reached.\n\nNo installation changes were made. Wait before retrying, or authenticate Docker with a GitHub account to increase the available request quota.",
          }),
        }),
    );

    await page.locator("#upgrade-prepare-btn").click();

    await expect(page.locator("#upgrade-release-error")).toContainText(
      /rate limit/i,
    );
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(/Try again/i);
    await expect(page.locator("#upgrade-prepare-btn")).toBeEnabled();
  });

  test("a registry rate-limit during execute shows actionable GHCR guidance and allows retry", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await mockUpgradeJob(page, {
      ok: false,
      reason: "system_build_registry_rate_limited",
      steps: resultSteps("prepare", "GitHub Container Registry rate limit reached."),
      message:
        "GitHub Container Registry rate limit reached. No installation changes were made. Wait before retrying, or authenticate Docker with a GitHub account to increase the available request quota.",
    });
    await confirmAndExecute(page);

    await expect(page.locator("#upgrade-validation")).toContainText(
      /rate limit reached/i,
    );
    await expect(page.locator("#upgrade-validation")).toContainText(
      /Wait before retrying/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Upgrade completed/i,
    );
    await expect(page.locator("#upgrade-release-select")).toHaveValue("v9.9.10");
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();
  });

  test("a network pull failure stays distinct from a rate-limit throttle", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await mockUpgradeJob(page, {
      ok: false,
      reason: "image_pull_network_error",
      steps: resultSteps(
        "prepare",
        "The image could not be downloaded because of a network error.",
      ),
      message: "The image could not be downloaded because of a network error.",
    });
    await confirmAndExecute(page);

    await expect(page.locator("#upgrade-validation")).toContainText(
      /network error/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /rate limit reached/i,
    );
    await expect(page.locator("#upgrade-validation")).not.toContainText(
      /Upgrade completed/i,
    );
  });

  test("executing a verified upgrade never re-verifies the build", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario); // one explicit verification
    const validations = countUpgradeValidations(page);
    await mockUpgradeJob(page, { ok: true, steps: resultSteps() });
    await confirmAndExecute(page);
    await expect(page.locator("#upgrade-validation")).toContainText(
      /Upgrade completed: v9\.9\.10/i,
    );
    // Execute reuses the verified build; it never calls the validate endpoint.
    expect(validations.count).toBe(0);
  });
});

test.describe("Guided Upgrade verified-fingerprint enforcement", () => {
  test("a prepared release is not treated as a verified System Build", async ({
    page,
    seedAdminScenario,
  }) => {
    // The catalogue reports v9.9.10 as prepared (resources cached), but that is
    // not verification.
    await page.route("**/api/setup/releases**", async (route: Route) => {
      const response = await route.fetch();
      const data = await response.json();
      for (const release of data.releases || []) {
        if (release.tag === "v9.9.10") release.prepared = true;
      }
      data.default_release = "v9.9.10";
      data.prepared_release = "v9.9.10";
      await route.fulfill({ json: data });
    });

    const validations = countUpgradeValidations(page);
    const select = await openUpgradePanel(page, seedAdminScenario);
    await expect(select).toHaveValue("v9.9.10");
    // Cached-resources badge is shown, but the target is not verified.
    await expect(page.locator("#upgrade-release-badges")).toContainText(/prepared/i);
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(
      /Verify System Build/i,
    );
    // No verification happened on load.
    expect(validations.count).toBe(0);
  });

  test("execute sends the exact verified fingerprint before preflight", async ({
    page,
    seedAdminScenario,
  }) => {
    let verifiedFingerprint: string | null = null;
    page.on("response", async (response) => {
      if (response.url().endsWith("/maintenance/upgrade/validate") && response.ok()) {
        verifiedFingerprint = (await response.json()).selection_fingerprint;
      }
    });
    let executeFingerprint: string | undefined;
    await page.route(
      "**/api/admin/maintenance/upgrade/execute",
      async (route: Route) => {
        executeFingerprint = route.request().postDataJSON()?.selection_fingerprint;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ok: true, job_id: "e2e-fp", steps: pendingSteps() }),
        });
      },
    );
    await page.route("**/api/admin/maintenance/upgrade/jobs/e2e-fp", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          status: "succeeded",
          steps: pendingSteps().map((step) => ({ ...step, state: "done" })),
          result: { ok: true, steps: resultSteps(), target_release: "v9.9.10" },
        }),
      });
    });

    await openPlannedUpgrade(page, seedAdminScenario);
    await confirmAndExecute(page);

    expect(verifiedFingerprint).toBeTruthy();
    expect(executeFingerprint).toBe(verifiedFingerprint);
    await expect(page.locator("#upgrade-validation")).toContainText(
      /Upgrade completed: v9\.9\.10/i,
    );
  });

  test("a moved target after verification is rejected before any mutation", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    // The verified tag is re-pushed to a new digest before Upgrade System.
    await seedAdminScenario("guided_upgrade_target_moved");

    let deploymentStarted = false;
    page.on("request", (request) => {
      if (request.url().includes("/maintenance/upgrade/jobs/")) deploymentStarted = true;
    });
    await confirmAndExecute(page);

    await expect(page.locator("#upgrade-validation")).toContainText(
      /verification is no longer current/i,
    );
    // The verified state is dropped and an explicit re-verify is required.
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(
      /Verify System Build/i,
    );
    expect(deploymentStarted).toBe(false);
  });

  test("changing the target after planning clears verification", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();

    // Selecting a different build drops the verified + planned fingerprints, so
    // Upgrade System is disabled until the new target is verified again.
    await page.locator("#upgrade-release-select").selectOption("v9.9.11");
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(
      /Verify System Build/i,
    );
  });

  test("a verify response without a selection fingerprint fails closed", async ({
    page,
    seedAdminScenario,
  }) => {
    // The server (implausibly) returns a validated pair but no selection
    // fingerprint. Without it Upgrade System can never run, so the UI must not
    // claim the build is verified — it fails closed and asks to verify again.
    await page.route(
      "**/api/admin/maintenance/upgrade/validate",
      async (route: Route) => {
        const response = await route.fetch();
        const data = await response.json();
        delete data.selection_fingerprint;
        await route.fulfill({ json: data });
      },
    );

    const select = await openUpgradePanel(page, seedAdminScenario);
    await select.selectOption("v9.9.10");
    await page.locator("#upgrade-prepare-btn").click();

    await expect(page.locator("#upgrade-release-status")).not.toHaveText(
      /System Build verified/i,
    );
    await expect(page.locator("#upgrade-release-error")).toContainText(
      /fingerprint/i,
    );
    await expect(page.locator("#upgrade-execute-btn")).toBeDisabled();
    await expect(page.locator("#upgrade-prepare-btn")).toHaveText(/Try again/i);
  });

  test("returning to Guided Upgrade in the same session keeps the verification", async ({
    page,
    seedAdminScenario,
  }) => {
    await openPlannedUpgrade(page, seedAdminScenario);
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();

    // Only new validate requests after this point count.
    const validations = countUpgradeValidations(page);
    // Leave Guided Upgrade for another maintenance panel, then return. The
    // maintenance nav buttons drive this by setting the hash; do the same.
    await page.evaluate(() => {
      window.location.hash = "maintenance-backup";
    });
    await expect(page.locator("#maintenance-backup-panel")).toBeVisible();
    await page.evaluate(() => {
      window.location.hash = "maintenance-upgrade";
    });
    await expect(page.locator("#upgrade-release-select")).toBeEnabled();

    // The same build is still selected and still verified — no second Verify.
    await expect(page.locator("#upgrade-release-select")).toHaveValue("v9.9.10");
    await expect(page.locator("#upgrade-release-status")).toHaveText(
      /System Build verified/i,
    );
    await expect(page.locator("#upgrade-execute-btn")).toBeEnabled();
    expect(validations.count).toBe(0);
  });
});
