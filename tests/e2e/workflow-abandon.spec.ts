import { request, type Page } from "@playwright/test";
import { test, expect, ADMIN_PASSWORD } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Abandoning a workflow must be backend-authoritative. "Start over" used to be a
// browser-only reset, so the durable artifacts Guided Setup created (generated
// config, deployment marker, pending transition) survived it — and the surviving
// transition kept blocking Maintenance config writes.
//
// See docs/technical/admin-workflow-state.md for the state inventory.

type Diagnostics = Record<string, unknown>;

async function csrf(page: Page): Promise<string> {
  const res = await page.request.get("/api/admin/auth/status");
  return (await res.json()).csrf_token as string;
}

async function alignment(page: Page): Promise<Record<string, any>> {
  const res = await page.request.get("/api/admin/system-alignment/status");
  return res.json();
}

async function generatedConfig(page: Page): Promise<Record<string, any>> {
  const res = await page.request.get("/api/setup/config/status");
  return res.json();
}

// Probe the Maintenance write gate without changing anything: an empty body is
// rejected on its own merits, so only the transition gate's reason is meaningful.
async function maintenanceWriteGate(page: Page): Promise<Diagnostics> {
  const res = await page.request.post("/api/admin/maintenance/config/apply", {
    headers: { "X-CSRF-Token": await csrf(page) },
    data: {},
  });
  const body = await res.json().catch(() => ({}));
  return { status: res.status(), error: body.error, reason: body.reason };
}

async function diagnostics(page: Page): Promise<Diagnostics> {
  const status = await alignment(page);
  const generated = await generatedConfig(page);
  return {
    // Read directly: a diagnostic must never out-wait the test it explains.
    view: await page.evaluate(() => {
      const panel = document.querySelector("[data-admin-view-panel]:not([hidden])");
      return panel ? panel.getAttribute("data-admin-view-panel") : null;
    }),
    transitionMode: status.transition?.mode ?? null,
    transitionStage: status.transition?.stage ?? null,
    operationId: status.transition?.operation_id ?? null,
    alignmentActive: status.active ?? null,
    generatedConfigPath: generated.path ?? null,
    generatedConfigExists: generated.exists ?? null,
    maintenanceWriteGate: await maintenanceWriteGate(page),
  };
}

test.describe("Workflow abandonment", () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  // Scenario A — Setup abandoned before Maintenance.
  test("Start over clears the backend workflow and survives a refresh", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();

    // A durable transition now exists and blocks unrelated Maintenance writes.
    const before = await alignment(page);
    expect(before.transition, JSON.stringify(await diagnostics(page))).not.toBeNull();
    expect(before.transition.mode).toBe("fresh_install");
    const blocked = await maintenanceWriteGate(page);
    expect(blocked.error, JSON.stringify(blocked)).toBe("system_transition_in_progress");

    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#setup-start-over").click();

    // The backend workflow is gone, not just the browser state.
    await expect
      .poll(async () => (await alignment(page)).transition?.stage ?? "absent", {
        message: "Start over must cancel the owning transition",
      })
      .toMatch(/^(cancelled|absent)$/);
    expect((await generatedConfig(page)).exists).toBe(false);
    const afterGate = await maintenanceWriteGate(page);
    expect(afterGate.error, JSON.stringify(afterGate)).not.toBe(
      "system_transition_in_progress",
    );

    await page.reload();

    const afterReload = await diagnostics(page);
    expect(afterReload.generatedConfigExists, JSON.stringify(afterReload)).toBe(false);
    expect(afterReload.transitionStage).not.toBe("resources_verified");
    expect(afterReload.maintenanceWriteGate).not.toMatchObject({
      error: "system_transition_in_progress",
    });
  });

  // Scenario C — Recovery stays reachable while ordinary actions are gated.
  test("a blocking setup transition gates Maintenance but leaves recovery reachable", async ({
    page,
    seedAdminScenario,
  }) => {
    await seedAdminScenario("guided_upgrade_blocking_setup");
    await page.reload();

    // Loading Setup resumes the seeded transition from the page itself, and that
    // resume verifies resources under the operation's worker claim. Abandonment
    // is refused while such a worker is live, so the recovery assertions below
    // must observe a settled worker, never race the browser's own request.
    await expect
      .poll(async () => (await alignment(page)).transition?.worker_active ?? null, {
        message: "the page-driven resume must settle before abandonment",
      })
      .toBe(false);

    const status = await alignment(page);
    expect(status.transition, JSON.stringify(await diagnostics(page))).not.toBeNull();
    expect(status.transition.mode).toBe("fresh_install");

    // Ordinary conflicting actions are gated…
    const gate = await maintenanceWriteGate(page);
    expect(gate.error, JSON.stringify(gate)).toBe("system_transition_in_progress");

    // …and the narrow primitive refuses this Setup-owned transition, because
    // ending it alone would orphan the workflow's artifacts.
    const primitive = await page.request.post(
      "/api/admin/system-alignment/cancel",
      {
        headers: { "X-CSRF-Token": await csrf(page) },
        data: { operation_id: status.transition.operation_id, confirm: true },
      },
    );
    expect(primitive.status()).toBe(409);
    expect((await primitive.json()).error).toBe("setup_abandon_required");

    // The recovery path that owns this transition stays available and needs no
    // manual JSON edit.
    const discarded = await page.request.post("/api/setup/abandon", {
      headers: { "X-CSRF-Token": await csrf(page) },
      data: {},
    });
    expect(discarded.ok(), JSON.stringify(await discarded.json())).toBeTruthy();
    expect((await discarded.json()).transition.stage).toBe("cancelled");

    await page.reload();
    const recovered = await diagnostics(page);
    expect(recovered.maintenanceWriteGate, JSON.stringify(recovered)).not.toMatchObject({
      error: "system_transition_in_progress",
    });
  });

  // Scenario D — the backend is the single authority: a browser that never saw
  // the workflow reads exactly the same state as the one that created it.
  test("workflow state is read identically by a fresh session", async ({
    page,
    baseURL,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();
    const owner = await alignment(page);

    // A brand-new session that never saw this workflow must read exactly the
    // same owner, stage and operation id: the backend is the single authority.
    const clean = await request.newContext({ baseURL });
    const login = await clean.post("/api/admin/auth/login", {
      data: { password: ADMIN_PASSWORD },
    });
    expect(login.ok(), JSON.stringify(await login.json())).toBeTruthy();
    const observed = await (
      await clean.get("/api/admin/system-alignment/status")
    ).json();

    expect(observed.transition?.operation_id).toBe(owner.transition.operation_id);
    expect(observed.transition?.mode).toBe(owner.transition.mode);
    expect(observed.transition?.stage).toBe(owner.transition.stage);

    await clean.dispose();
  });
});
