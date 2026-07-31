import { type Browser, type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import {
  currentWorkflow,
  enterSetupWorkflow,
  post,
  postSystemBuild,
} from "./helpers/setup-authority";

// A Setup System Build transition may be created, resumed or cancelled only by
// the exact active Guided Setup workflow that owns the authorizing setup intent.
// Two browser sessions can be inside one Fresh Setup at the same time, so these
// specs prove across real browser contexts that a confirmation never outlives its
// workflow, that abandonment and transition creation can never both win, and that
// a linked transition survives a reload while staying owner-only.

const CONFIRM = "/api/setup/system-build/confirm";
const UPDATE_ADMIN = "/api/setup/system-build/update-admin";
const ALIGNED_TAG = "latest";

// The rejections that all mean "this session is not the workflow the server is
// willing to change". Which one a caller sees depends on whose session
// terminalized the workflow — never on what it is allowed to change.
const STALE_AUTHORITY_ERRORS = [
  "setup_workflow_not_active",
  "setup_intent_workflow_mismatch",
  "setup_intent_required",
];

async function alignment(page: Page) {
  return (await (await page.request.get("/api/admin/system-alignment/status")).json()) as any;
}

/** A second real browser session against the same Admin. */
async function secondSession(browser: Browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  return { context, page };
}

async function seed(page: Page, scenario: string) {
  const { status, body } = await post(page, "/api/admin/test/seed", { scenario });
  expect(status, JSON.stringify(body)).toBe(200);
  return body;
}

test.describe("Setup transition authority", () => {
  test("a superseded workflow's intent cannot authorize its replacement", async ({
    page,
    browser,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();

    // Session A confirms Fresh Setup and keeps its authority.
    const sessionA = await enterSetupWorkflow(page);
    const second = await secondSession(browser);

    // Session B enters the very same setup, so both hold a confirmation for it.
    const sessionB = await enterSetupWorkflow(second.page);
    expect(sessionB.workflowId).toBe(sessionA.workflowId);

    // B changes the selected build: the old workflow is retired as one backend
    // operation and a replacement workflow is issued to B.
    const superseded = await post(second.page, "/api/setup/system-build/supersede", {
      setup_workflow_id: sessionA.workflowId,
      tag: "v9.9.10",
    });
    expect(superseded.status, JSON.stringify(superseded.body)).toBe(200);
    const replacement = superseded.body.setup_workflow_id as string;
    expect(replacement).not.toBe(sessionA.workflowId);

    // A still holds the intent it was issued for W1. It authorizes neither the
    // retired workflow nor the replacement it was never issued for.
    const retired = await postSystemBuild(
      page,
      CONFIRM,
      { tag: ALIGNED_TAG, setup_workflow_id: sessionA.workflowId },
      sessionA.intentId,
    );
    expect(retired.status, JSON.stringify(retired.body)).toBe(409);
    expect(retired.body.error).toBe("setup_workflow_not_active");

    const adopted = await postSystemBuild(
      page,
      UPDATE_ADMIN,
      { tag: ALIGNED_TAG, setup_workflow_id: replacement },
      sessionA.intentId,
    );
    expect(adopted.status, JSON.stringify(adopted.body)).toBe(409);
    expect(STALE_AUTHORITY_ERRORS).toContain(adopted.body.error);

    // The replacement workflow and its selected build are untouched, and A
    // launched no transition at all.
    const workflow = await currentWorkflow(page);
    expect(workflow.workflow_id).toBe(replacement);
    expect(workflow.status).toBe("active");
    expect(workflow.operation_id).toBeNull();
    expect(workflow.selected_system_tag).toBe("v9.9.10");
    expect((await alignment(page)).transition).toBeFalsy();

    await second.context.close();
  });

  test("abandon and System Build creation cannot both win", async ({
    page,
    browser,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    const { workflowId, intentId } = await enterSetupWorkflow(page);
    const second = await secondSession(browser);

    // Park the confirm inside its lifecycle claim, before its transition can be
    // committed. Controlled fixture, not a delay: the seed handshake below
    // returns only once the request is actually held there.
    await seed(page, "setup_transition_hold_commit");
    const held = postSystemBuild(
      page,
      CONFIRM,
      { tag: ALIGNED_TAG, setup_workflow_id: workflowId },
      intentId,
    );
    const parked = await seed(second.page, "setup_transition_await_commit");
    expect(parked.holding, "the confirm never reached its commit boundary").toBe(true);

    // The other session's Discard setup is refused while creation owns W.
    const abandoned = await post(second.page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(abandoned.status, JSON.stringify(abandoned.body)).toBe(409);
    expect(abandoned.body.error).toBe("setup_operation_in_progress");

    await seed(second.page, "setup_transition_release_commit");
    const confirmed = await held;
    expect(confirmed.status, JSON.stringify(confirmed.body)).toBe(200);

    // Exactly one side won: the workflow is still active and now durably owns
    // the exact transition that was committed. No orphan exists.
    const transition = (await alignment(page)).transition;
    const workflow = await currentWorkflow(page);
    expect(workflow.status).toBe("active");
    expect(workflow.workflow_id).toBe(workflowId);
    expect(workflow.operation_id).toBe(transition.operation_id);
    expect(confirmed.body.operation_id).toBe(transition.operation_id);

    // The other ordering: once the abandon owns W, creation commits nothing.
    const discarded = await post(second.page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    const late = await postSystemBuild(
      page,
      CONFIRM,
      { tag: ALIGNED_TAG, setup_workflow_id: workflowId },
      intentId,
    );
    expect(late.status, JSON.stringify(late.body)).toBe(409);
    expect(STALE_AUTHORITY_ERRORS).toContain(late.body.error);
    expect((await alignment(page)).transition?.stage ?? "cancelled").toBe("cancelled");

    await second.context.close();
  });

  test("a linked transition survives a reload and stays owner-only", async ({
    page,
    browser,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    // Both sessions log in before a transition exists: a browser that arrives
    // while Guided Setup is mid-flight is taken straight into the wizard, so the
    // start view it would otherwise wait for is not shown.
    const second = await secondSession(browser);
    const { workflowId, intentId } = await enterSetupWorkflow(page);

    const created = await postSystemBuild(
      page,
      CONFIRM,
      { tag: ALIGNED_TAG, setup_workflow_id: workflowId },
      intentId,
    );
    expect(created.status, JSON.stringify(created.body)).toBe(200);
    const operationId = created.body.operation_id as string;
    expect(operationId).toBeTruthy();

    // A browser restart changes nothing about the durable ownership.
    await page.reload();
    await expect
      .poll(async () => (await currentWorkflow(page))?.operation_id)
      .toBe(operationId);
    const restored = await currentWorkflow(page);
    expect(restored.workflow_id).toBe(workflowId);
    expect(restored.transition_mode).toBe("fresh_install");

    // A foreign/old tab can neither resume nor cancel that transition.
    const foreignResume = await postSystemBuild(
      second.page,
      CONFIRM,
      { tag: ALIGNED_TAG, setup_workflow_id: "an-older-tab" },
      intentId,
    );
    expect(foreignResume.status, JSON.stringify(foreignResume.body)).toBe(409);
    expect(foreignResume.body.error).toBe("setup_workflow_not_active");
    const foreignCancel = await post(second.page, "/api/setup/abandon", {
      setup_workflow_id: "an-older-tab",
    });
    expect(foreignCancel.status, JSON.stringify(foreignCancel.body)).toBe(409);
    expect(foreignCancel.body.error).toBe("setup_workflow_not_active");
    expect((await alignment(page)).transition.operation_id).toBe(operationId);

    // The exact workflow resumes its own transition — same operation id, no
    // second transition — and only it may discard it.
    const resumed = await postSystemBuild(
      page,
      CONFIRM,
      { tag: ALIGNED_TAG, setup_workflow_id: workflowId },
      (await enterSetupWorkflow(page)).intentId,
    );
    expect(resumed.status, JSON.stringify(resumed.body)).toBe(200);
    expect(resumed.body.operation_id).toBe(operationId);

    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: workflowId,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    expect(discarded.body.ok).toBe(true);

    await second.context.close();
  });
});
