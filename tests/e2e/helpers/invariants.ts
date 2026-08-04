import { expect } from "../fixtures/admin";
import { type SetupPage } from "../pages/setup-page";

// The ready/action invariant for Setup and Upgrade cards. It combines the
// visible status text with both button states so a contradictory UI (ready but
// no action) is caught regardless of which layer drifted.
export async function assertReadyActionInvariant(setup: SetupPage) {
  const status = (await setup.status.textContent())?.trim() ?? "";
  const updateEnabled = await setup.adminUpdateButton.isEnabled();
  const continueEnabled = await setup.continueButton.isEnabled();

  const readyToProceed = /ready for the selected|can install this legacy/i.test(
    status,
  );
  const needsUpdate = /must be updated/i.test(status);

  if (readyToProceed) {
    expect(
      continueEnabled,
      `ready status but Continue disabled: "${status}"`,
    ).toBeTruthy();
    // A ready state never simultaneously demands an Admin update.
    expect(updateEnabled).toBeFalsy();
  }
  if (needsUpdate) {
    expect(updateEnabled).toBeTruthy();
    expect(continueEnabled).toBeFalsy();
  }
  // Never light up both mutually-exclusive primary actions.
  expect(updateEnabled && continueEnabled).toBeFalsy();
}
