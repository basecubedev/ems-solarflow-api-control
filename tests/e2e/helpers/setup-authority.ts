import { expect, type Page } from "@playwright/test";

// Guided Setup mutations are bound to one server-owned workflow identity and to
// the exact preview the server issued for that request. Specs that drive the
// endpoints directly stand in for a browser that confirmed Fresh Setup and
// reviewed the preview first, so they perform the same two backend steps rather
// than weakening the contract.

export async function csrf(page: Page): Promise<string> {
  return (await (await page.request.get("/api/admin/auth/status")).json()).csrf_token;
}

export async function post(
  page: Page,
  url: string,
  data: unknown,
  headers?: Record<string, string>,
) {
  const res = await page.request.post(url, {
    headers: { "X-CSRF-Token": await csrf(page), ...(headers ?? {}) },
    data: data as any,
  });
  return { status: res.status(), body: await res.json().catch(() => ({})) };
}

/**
 * Confirm Fresh Setup and keep both halves of the authority it issues.
 *
 * The durable workflow id names the owner; the one-shot intent is that session's
 * confirmation *for that workflow*. Every System Build mutation needs both.
 */
export async function enterSetupWorkflow(page: Page) {
  const { status, body } = await post(page, "/api/admin/start-path", {
    choice: "setup_new",
    confirm: true,
  });
  expect(status, JSON.stringify(body)).toBe(200);
  expect(body.setup_workflow_id, JSON.stringify(body)).toBeTruthy();
  expect(body.setup_intent_id, JSON.stringify(body)).toBeTruthy();
  return {
    workflowId: body.setup_workflow_id as string,
    intentId: body.setup_intent_id as string,
  };
}

/** One System Build mutation carrying its exact workflow and its intent. */
export async function postSystemBuild(
  page: Page,
  url: string,
  data: Record<string, unknown>,
  intentId: string | null,
) {
  return post(
    page,
    url,
    data,
    intentId ? { "X-Setup-Intent-ID": intentId } : undefined,
  );
}

/** Confirm Fresh Setup and return the durable workflow id. */
export async function startSetupWorkflow(page: Page): Promise<string> {
  const { status, body } = await post(page, "/api/admin/start-path", {
    choice: "setup_new",
    confirm: true,
  });
  expect(status, JSON.stringify(body)).toBe(200);
  expect(body.setup_workflow_id, JSON.stringify(body)).toBeTruthy();
  return body.setup_workflow_id as string;
}

/** The server's current workflow record, or null when none is active. */
export async function currentWorkflow(page: Page) {
  const res = await page.request.get("/api/setup/workflow");
  const body = await res.json().catch(() => ({}));
  return body.workflow ?? null;
}

export async function currentWorkflowId(page: Page): Promise<string | null> {
  const workflow = await currentWorkflow(page);
  return workflow && workflow.status === "active" ? workflow.workflow_id : null;
}

/** The workflow identity the browser stored beside its draft. */
export async function storedWorkflow(page: Page) {
  const raw = await page.evaluate(() =>
    window.localStorage.getItem("ems-admin-setup-workflow"),
  );
  return raw ? JSON.parse(raw) : null;
}

/**
 * The device plan the server currently considers authoritative for `devices`.
 *
 * A plan authorizes the draft it was computed over, so planning happens against
 * the same device list the caller goes on to review — exactly as the browser
 * does, which re-plans whenever its draft changes.
 */
export async function currentDevicePlanId(
  page: Page,
  devices?: unknown[],
): Promise<string> {
  const plan = await post(page, "/api/setup/device-plan", {
    state: { draft_items: devices ?? [] },
  });
  expect(plan.status, JSON.stringify(plan.body)).toBe(200);
  return plan.body.plan_id as string;
}

/**
 * Return `body` carrying real workflow, device-plan and exact-preview authority.
 *
 * The draft must produce a ready preview — a draft that cannot be previewed can
 * never be authorized, exactly like in the browser.
 */
export async function authorizeSetupMutation(
  page: Page,
  body: Record<string, unknown>,
  workflowId?: string,
) {
  const workflow = workflowId ?? (await startSetupWorkflow(page));
  const authorized: Record<string, unknown> = { ...body };
  delete authorized.config_revision;
  authorized.setup_workflow_id = workflow;
  // Config Preview issues mutation authority only for a device plan the server
  // issued and still considers current, so plan first — as the browser does.
  authorized.device_plan_id ??= await currentDevicePlanId(
    page,
    (authorized.devices as unknown[]) ?? [],
  );
  const preview = await post(page, "/api/setup/config-preview", authorized);
  expect(preview.status, JSON.stringify(preview.body)).toBe(200);
  expect(
    preview.body.config_preview_id,
    `draft did not produce a ready preview: ${JSON.stringify(preview.body.validation)}`,
  ).toBeTruthy();
  authorized.config_preview_id = preview.body.config_preview_id;
  return authorized;
}

/** Discard the active Setup through its backend owner. */
export async function discardSetup(page: Page) {
  const workflow = await currentWorkflowId(page);
  return post(page, "/api/setup/abandon", workflow ? { setup_workflow_id: workflow } : {});
}

/**
 * Seed the one discovered inverter a previewable Setup draft needs.
 *
 * Guided Setup auto-adds a discovered inverter; without one the browser's own
 * draft never reaches a ready preview and Apply stays disabled. The test mDNS
 * provider is inert, so nothing appears unless a scenario seeds it — and the
 * device list is fetched on load and then only every `MDNS_POLL_INTERVAL_MS`,
 * so the reload is what puts the seeded device there before the page reads it.
 */
export async function seedSetupInverter(
  page: Page,
  seedAdminScenario: (scenario: string) => Promise<void>,
) {
  await seedAdminScenario("setup_local_api_inverter");
  await page.reload();
}

/** Expand the generated-config disclosure that holds Apply; idempotent. */
export async function openConfigPreviewDetails(page: Page) {
  const details = page.locator("#config-preview-details");
  await expect(details).toBeVisible();
  if (!(await details.evaluate((el) => (el as HTMLDetailsElement).open))) {
    await page.locator("#config-preview-details > summary").click();
  }
  await expect(details).toHaveAttribute("open", "");
}

/**
 * Wait until Apply carries real authority, not merely a successful response.
 *
 * A 200 config-preview proves nothing about the UI: the frontend drops a
 * response whose request id or generation was superseded, and a preview that is
 * not `ready` (no control device yet) issues no preview id at all. Apply is
 * authoritative only once the accepted preview, its persisted id and the
 * enabled button agree.
 *
 * `#config-preview-ready` is deliberately not the signal: it reports validation
 * tone, so a benign warning such as a missing grid meter paints "Needs
 * attention" over a preview that is ready and applicable.
 */
export async function waitForConfigApplyReady(
  page: Page,
): Promise<{ workflowId: string; previewId: string }> {
  await expect(page.locator("#config-preview-ready")).not.toHaveText(/Checking/i);
  await expect
    .poll(async () => (await storedWorkflow(page))?.preview_id ?? null)
    .not.toBeNull();
  await openConfigPreviewDetails(page);
  await expect(page.locator("#config-apply")).toBeVisible();
  await expect(page.locator("#config-apply")).toBeEnabled();
  const stored = (await storedWorkflow(page))!;
  expect(stored.workflow_id, JSON.stringify(stored)).toBeTruthy();
  return {
    workflowId: stored.workflow_id as string,
    previewId: stored.preview_id as string,
  };
}

/**
 * Hold every later page-originated config-preview until `release()` is called.
 *
 * The wizard re-previews on its own render/poll cycle, which would replace the
 * exact preview a stale-Apply assertion has to keep. `page.request` calls are
 * unaffected, so the test can still drive the backend directly.
 */
export async function holdBrowserPreviews(page: Page) {
  const held: { abort: () => Promise<void> }[] = [];
  let releasing = false;
  await page.route("**/api/setup/config-preview", async (route) => {
    if (releasing) {
      await route.continue();
      return;
    }
    held.push(route);
  });
  return async function release() {
    releasing = true;
    await page.unroute("**/api/setup/config-preview");
    for (const route of held.splice(0)) {
      await route.abort().catch(() => {});
    }
  };
}
