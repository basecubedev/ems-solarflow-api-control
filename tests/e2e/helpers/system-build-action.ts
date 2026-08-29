import { expect, type Page, type TestInfo } from "@playwright/test";
import { SetupPage } from "../pages/setup-page";

type ActionState = {
  admin_update_allowed?: boolean;
  continue_allowed?: boolean;
  busy?: boolean;
  polling_required?: boolean;
  progress_message?: string | null;
  terminal_error?: { code?: string; message?: string } | null;
};

async function attachActionDiagnostics(
  page: Page,
  setup: SetupPage,
  testInfo: TestInfo,
) {
  const selectedBuild = await setup.buildSelect.inputValue().catch(() => "");
  const visibleBuildLabel = await setup.buildSelect
    .locator("option:checked")
    .textContent()
    .catch(() => null);
  const transitionResponse = await page.request
    .get("/api/admin/system-alignment/status")
    .catch(() => null);
  const transition = transitionResponse
    ? await transitionResponse.json().catch(() => null)
    : null;
  await testInfo.attach("system-build-action-state.json", {
    body: JSON.stringify(
      {
        selectedBuild,
        visibleBuildLabel,
        statusText: await setup.status.textContent().catch(() => null),
        errorText: await setup.error.textContent().catch(() => null),
        transition,
        buttons: {
          update: {
            disabled: await setup.adminUpdateButton.isDisabled().catch(() => null),
            ariaDisabled: await setup.adminUpdateButton
              .getAttribute("aria-disabled")
              .catch(() => null),
          },
          continue: {
            disabled: await setup.continueButton.isDisabled().catch(() => null),
            ariaDisabled: await setup.continueButton
              .getAttribute("aria-disabled")
              .catch(() => null),
          },
        },
      },
      null,
      2,
    ),
    contentType: "application/json",
  });
  await testInfo.attach("system-build-page.html", {
    body: await page.content(),
    contentType: "text/html",
  });
}

export async function expectValidSystemBuildAction(
  page: Page,
  setup: SetupPage,
  testInfo: TestInfo,
): Promise<"continue" | "admin_update" | "terminal_error"> {
  try {
    // Client-side transients only. The words below are rendered by admin.js
    // while it is mid-request; they say nothing about the server's own state.
    await expect(setup.status).not.toHaveText(
      /checking|confirming|updating|reconnecting/i,
    );

    // The server's own busy stages need their own wait, and this is the one
    // that was missing. `admin_update_pending`, `admin_reconnect_pending` and
    // `admin_aligned` render "Preparing the Admin Server update…", "Waiting for
    // the updated Admin Server to reconnect…" and "Verifying selected System
    // Build resources…" -- not one of which contains any word the guard above
    // looks for. So the guard passed while a transition was still running, the
    // snapshot below caught `busy: true`, and its progress message was then
    // compared against a status line that had already settled. Firefox lost
    // that race twice on main; Chromium landed after the settle and passed.
    //
    // `busy` is the authoritative answer -- the server derives it from
    // `_ACTION_BUSY_STAGES` in admin/system_alignment.py -- so waiting on it
    // needs no timer and no list of stage names kept in step over here.
    await expect
      .poll(
        async () => {
          const seen = await setup.latestValidation().catch(() => null);
          const state = seen?.action_state as ActionState | undefined;
          return state ? state.busy === true : null;
        },
        {
          // Longer than the 7s `expect` default this replaces, because a slow
          // runner is the whole failure mode -- and well inside the 30s test
          // timeout, so a transition that never settles still fails here, with
          // the message above, rather than as an anonymous test timeout.
          message: "the System Build action never left its busy stages",
          timeout: 15_000,
        },
      )
      .toBe(false);

    // A validation response is recorded by a listener, which can run before the
    // page has rendered what it says. The button and status reads below are
    // single shots, so without this they could still see the busy frame.
    const progressMessages = new Set(
      setup
        .validationHistory()
        .map((entry) => (entry.action_state as ActionState | undefined)?.progress_message)
        .filter((message): message is string => Boolean(message)),
    );
    if (progressMessages.size > 0) {
      await expect
        .poll(
          async () => progressMessages.has(((await setup.status.textContent()) ?? "").trim()),
          { message: "the status line kept showing a busy stage after it settled" },
        )
        .toBe(false);
    }

    const validation = await setup.latestValidation();
    const action = validation.action_state as ActionState | undefined;
    expect(action, "validation must include authoritative action_state").toBeTruthy();

    const updateEnabled = await setup.adminUpdateButton.isEnabled();
    const continueEnabled = await setup.continueButton.isEnabled();
    const errorVisible = await setup.error.isVisible();
    const status = (await setup.status.textContent())?.trim() ?? "";

    if (action?.busy === true) {
      expect(action.polling_required).toBe(true);
      expect(action.progress_message).toBeTruthy();
      await expect(setup.status).toContainText(action.progress_message as string);
      throw new Error("System Build action did not settle before the assertion");
    }

    if (action?.terminal_error) {
      expect(updateEnabled).toBe(false);
      expect(continueEnabled).toBe(false);
      expect(errorVisible).toBe(true);
      await expect(setup.error).not.toHaveText(/^\s*$/);
      expect(status).not.toMatch(/ready|compatible|verified/i);
      return "terminal_error";
    }

    expect(
      Boolean(action?.admin_update_allowed) !==
        Boolean(action?.continue_allowed),
      "a valid settled build must expose exactly one server-authorized action",
    ).toBe(true);

    if (action?.continue_allowed) {
      expect(continueEnabled).toBe(true);
      expect(updateEnabled).toBe(false);
      expect(errorVisible).toBe(false);
      return "continue";
    }

    expect(updateEnabled).toBe(true);
    expect(continueEnabled).toBe(false);
    expect(errorVisible).toBe(false);
    return "admin_update";
  } catch (error) {
    await attachActionDiagnostics(page, setup, testInfo);
    throw error;
  }
}
