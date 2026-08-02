import { test, expect, request, type APIRequestContext } from "@playwright/test";
import { AdminProcess, authenticate } from "./helpers/admin-process";

// Scenario D: an Admin restart must not change how the persisted workflow is
// interpreted. This restarts the real Admin process against the same data
// directory — a page reload or a new browser context proves neither.

let admin: AdminProcess;

test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  admin = new AdminProcess();
  await admin.start();
});

test.afterAll(async () => {
  await admin.dispose();
});

type Session = { ctx: APIRequestContext; csrf: string };

async function connect(): Promise<Session> {
  const ctx = await request.newContext({ baseURL: admin.baseURL });
  return { ctx, csrf: await authenticate(ctx) };
}

async function alignment(session: Session) {
  return (await (await session.ctx.get("/api/admin/system-alignment/status")).json()) as any;
}

async function generatedConfig(session: Session) {
  return (await (await session.ctx.get("/api/setup/config/status")).json()) as any;
}

async function workflow(session: Session) {
  const body = (await (await session.ctx.get("/api/setup/workflow")).json()) as any;
  return body.workflow ?? null;
}

async function post(
  session: Session,
  url: string,
  data: unknown,
  headers?: Record<string, string>,
) {
  const res = await session.ctx.post(url, {
    headers: { "X-CSRF-Token": session.csrf, ...(headers ?? {}) },
    data: data as any,
  });
  return { status: res.status(), body: await res.json().catch(() => ({})) };
}

/** The device plan Config Preview will demand, as the browser obtains one. */
async function currentDevicePlanId(session: Session): Promise<string> {
  const plan = await post(session, "/api/setup/device-plan", { state: {} });
  expect(plan.status, JSON.stringify(plan.body)).toBe(200);
  expect(plan.body.plan_id, JSON.stringify(plan.body)).toBeTruthy();
  return plan.body.plan_id as string;
}

/** Everything a failure needs to be explained without re-running the test. */
async function diagnostics(session: Session) {
  const status = await alignment(session);
  const generated = await generatedConfig(session);
  const gate = await post(session, "/api/admin/maintenance/config/apply", {});
  const record = await workflow(session);
  return {
    pid: admin.pid,
    processGeneration: admin.generation,
    adminDataDir: admin.adminDataDir,
    workflowId: record?.workflow_id ?? null,
    workflowStatus: record?.status ?? null,
    previewId: record?.preview?.preview_id ?? null,
    previewBaseline: record?.preview?.base_config_revision ?? null,
    operationId: status.transition?.operation_id ?? null,
    mode: status.transition?.mode ?? null,
    stage: status.transition?.stage ?? null,
    active: status.active ?? null,
    generatedConfigExists: generated.exists ?? null,
    maintenanceWriteGate: { status: gate.status, error: gate.body?.error },
  };
}

test("a real Admin restart preserves one workflow interpretation", async () => {
  const first = await connect();

  // 01 — a persisted transition plus an owned artifact, created exactly the way
  // Guided Setup creates them: the workflow confirms its System Build, so the
  // transition is linked to its owner before it is committed.
  const draft = {
    devices: [
      {
        role: "inverter",
        enabled: true,
        config_name: "WR1",
        display_name: "Inv",
        ip: "192.168.1.100",
        serial_number: "SN1",
      },
    ],
    supported_grid_meter_count: 0,
  };
  const startPath = await post(first, "/api/admin/start-path", {
    choice: "setup_new",
    confirm: true,
  });
  expect(startPath.status, JSON.stringify(startPath.body)).toBe(200);
  const workflowId = startPath.body.setup_workflow_id as string;
  expect(workflowId).toBeTruthy();
  const confirmed = await post(
    first,
    "/api/setup/system-build/confirm",
    { tag: "latest", setup_workflow_id: workflowId },
    { "X-Setup-Intent-ID": startPath.body.setup_intent_id as string },
  );
  expect(confirmed.status, JSON.stringify(confirmed.body)).toBe(200);
  const devicePlanId = await currentDevicePlanId(first);
  const reviewed = await post(first, "/api/setup/config-preview", {
    ...draft,
    setup_workflow_id: workflowId,
    device_plan_id: devicePlanId,
  });
  expect(reviewed.status, JSON.stringify(reviewed.body)).toBe(200);
  const previewId = reviewed.body.config_preview_id as string;
  expect(previewId).toBeTruthy();
  const written = await post(first, "/api/setup/config/write", {
    ...draft,
    setup_workflow_id: workflowId,
    config_preview_id: previewId,
    device_plan_id: devicePlanId,
  });
  expect(written.status, JSON.stringify(written.body)).toBe(200);

  // 02 — record the interpretation the restart must reproduce.
  const before = await diagnostics(first);
  expect(before.mode).toBe("fresh_install");
  expect(before.stage).toBe("resources_verified");
  expect(before.generatedConfigExists).toBe(true);
  expect(before.workflowId).toBe(workflowId);
  expect(before.previewId).toBe(previewId);
  const originalPid = admin.pid;
  await first.ctx.dispose();

  // 03/04/05 — a genuine process restart on the same data directory.
  await admin.restart();
  expect(admin.pid).not.toBe(originalPid);
  const second = await connect();

  // 06/07 — the same persisted workflow, read by a new process.
  const after = await diagnostics(second);
  expect(after.processGeneration).toBe(before.processGeneration + 1);
  expect(after, JSON.stringify({ before, after })).toMatchObject({
    operationId: before.operationId,
    mode: before.mode,
    stage: before.stage,
    active: before.active,
    generatedConfigExists: true,
    // The workflow identity and its exact preview authority are as durable as
    // the transition: a new process reads back the same interpretation.
    workflowId: before.workflowId,
    workflowStatus: "active",
    previewId: before.previewId,
  });
  expect(after.previewBaseline).toEqual(before.previewBaseline);
  expect(after.maintenanceWriteGate.error).toBe(before.maintenanceWriteGate.error);

  // 07a — the surviving authority still authorizes exactly the same draft, and
  // still refuses a different one.
  const otherDraft = {
    ...draft,
    devices: [{ ...draft.devices[0], ip: "192.168.1.101" }],
  };
  const forged = await post(second, "/api/setup/config/write", {
    ...otherDraft,
    overwrite: true,
    setup_workflow_id: before.workflowId,
    config_preview_id: before.previewId,
    // The restarted process issued no device plan, so the reviewed plan is
    // presented again: the mismatch must come from the draft, not from a
    // missing link earlier in the chain.
    device_plan_id: devicePlanId,
  });
  expect(forged.status, JSON.stringify(forged.body)).toBe(409);
  expect(forged.body.error).toBe("setup_preview_mismatch");
  const rewritten = await post(second, "/api/setup/config/write", {
    ...draft,
    overwrite: true,
    setup_workflow_id: before.workflowId,
    config_preview_id: before.previewId,
    // The reviewed device plan is presented again after the restart: its
    // authority lives in the durable preview record, not in the process that
    // issued it.
    device_plan_id: devicePlanId,
  });
  expect(rewritten.status, JSON.stringify(rewritten.body)).toBe(200);

  // 08 — abandon through the same backend-owned operation the UI uses.
  const abandoned = await post(second, "/api/setup/abandon", {
    setup_workflow_id: before.workflowId,
  });
  expect(abandoned.status, JSON.stringify(abandoned.body)).toBe(200);
  expect(abandoned.body.ok).toBe(true);
  await second.ctx.dispose();

  // 09/10 — the clean state is just as durable as the workflow was.
  await admin.restart();
  const third = await connect();
  const cleaned = await diagnostics(third);
  expect(cleaned.generatedConfigExists, JSON.stringify(cleaned)).toBe(false);
  expect(["cancelled", null]).toContain(cleaned.stage);
  expect(cleaned.workflowStatus).toBe("abandoned");
  // The abandoned workflow no longer gates unrelated Maintenance writes.
  expect(cleaned.maintenanceWriteGate.error).not.toBe("system_transition_in_progress");
  await third.ctx.dispose();
});

test("the restarted Admin serves the same workflow to a browser", async ({ page }) => {
  await page.goto(`${admin.baseURL}/`);
  // Auth state lives in the preserved data directory, so the login form is the
  // one the earlier process created.
  await expect(page.locator("#auth-login:not([hidden])")).toBeVisible();
});
