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
    await expect(setup.status).not.toHaveText(
      /checking|confirming|updating|reconnecting/i,
    );

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
