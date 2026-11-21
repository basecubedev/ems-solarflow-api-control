// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { expect, type Page, type TestInfo } from "@playwright/test";
import { SetupPage } from "../pages/setup-page";
import { STRINGS } from "../../../frontend/admin-modern/src/i18n/strings.js";

/**
 * The status lines the console shows while a request of its own is under way
 * (``buildMessage`` in frontend/admin-modern/src/lib/setup.js). They say
 * nothing about the server's state, which is waited on through
 * ``action_state.busy``.
 */
export const CONSOLE_TRANSIENT_MESSAGES = ["validating", "confirming", "updating", "reconnecting", "busy", "inProgress"];
const TRANSIENT_TEXTS = CONSOLE_TRANSIENT_MESSAGES.map((key) => STRINGS[`setup.build.${key}`].en);

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
  const selectedBuild = await setup.buildSelect.inputValue({ timeout: 1_000 }).catch(() => "");
  const visibleBuildLabel = await setup.buildSelect
    .locator("option:checked")
    .textContent({ timeout: 1_000 })
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
        statusText: await setup.statusText(),
        errorText: (await setup.error.count()) ? await setup.error.textContent({ timeout: 1_000 }).catch(() => null) : null,
        transition,
        buttons: {
          update: {
            disabled: await setup.adminUpdateButton.isDisabled({ timeout: 1_000 }).catch(() => null),
            ariaDisabled: await setup.adminUpdateButton
              .getAttribute("aria-disabled", { timeout: 1_000 })
              .catch(() => null),
          },
          continue: {
            disabled: await setup.continueButton.isDisabled({ timeout: 1_000 }).catch(() => null),
            ariaDisabled: await setup.continueButton
              .getAttribute("aria-disabled", { timeout: 1_000 })
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
    await expect
      .poll(async () => TRANSIENT_TEXTS.includes(await setup.statusText()), {
        message: "the console kept showing a request of its own as under way",
        timeout: 15_000,
      })
      .toBe(false);

    // The server's own busy stages are waited on through `busy`, which the
    // server derives from `_ACTION_BUSY_STAGES` in admin/system_alignment.py;
    // the console never shows the server's progress messages, so no text says
    // when they end.
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

    const validation = await setup.latestValidation();
    const action = validation.action_state as ActionState | undefined;
    expect(action, "validation must include authoritative action_state").toBeTruthy();

    const updateEnabled = await setup.adminUpdateOffered();
    const continueEnabled = await setup.continueButton.isEnabled();
    const errorVisible = await setup.error.isVisible();
    const status = await setup.statusText();

    if (action?.busy === true) {
      expect(action.polling_required).toBe(true);
      expect(action.progress_message).toBeTruthy();
      throw new Error(`System Build action did not settle before the assertion: ${action.progress_message}`);
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
