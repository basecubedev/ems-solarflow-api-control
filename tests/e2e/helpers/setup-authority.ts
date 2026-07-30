import { expect, type Page } from "@playwright/test";

// Guided Setup mutations are bound to one server-owned workflow identity and to
// the exact preview the server issued for that request. Specs that drive the
// endpoints directly stand in for a browser that confirmed Fresh Setup and
// reviewed the preview first, so they perform the same two backend steps rather
// than weakening the contract.

export async function csrf(page: Page): Promise<string> {
  return (await (await page.request.get("/api/admin/auth/status")).json()).csrf_token;
}

export async function post(page: Page, url: string, data: unknown) {
  const res = await page.request.post(url, {
    headers: { "X-CSRF-Token": await csrf(page) },
    data: data as any,
  });
  return { status: res.status(), body: await res.json().catch(() => ({})) };
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
 * Return `body` carrying real workflow + exact-preview authority.
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
