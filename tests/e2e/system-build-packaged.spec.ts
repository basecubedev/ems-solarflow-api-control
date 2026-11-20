// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test, expect } from "./fixtures/admin";
import { ModernSetup } from "./helpers/modern-setup";

const PACKAGED_DEVELOPMENT_TAG = "dev-development-deadbee-100-1";

test.describe("system_build_browser_gate packaged Admin", { tag: ["@system-build"] }, () => {
  test("production catalogue labels immutable Development builds newest first", async ({ page }) => {
    const setup = new ModernSetup(page);
    await setup.open();
    const options = setup.select.locator("option[value^='dev-development-']");
    await expect(options).toHaveCount(2);
    await expect(options.nth(0)).toHaveAttribute("value", PACKAGED_DEVELOPMENT_TAG);
    await expect(options.nth(0)).toContainText("system-build-action-gating · deadbee");
    await expect(options.nth(1)).toContainText("system-build-action-gating · cafebad");
    await expect(setup.option("dev-development")).toHaveCount(0);
    await expect(setup.option("dev-development-feedbad-98-1")).toHaveCount(0);
  });

  test("Latest produces and executes a valid packaged action", async ({ page }) => {
    const setup = new ModernSetup(page);
    await setup.open();
    const validation = await setup.verify("latest");
    expect(validation.action_state.selected_build.tag).toBe("latest");
    await setup.continueToDevices();
  });

  test("Development consumes its packaged descriptor and advances", async ({ page }) => {
    const setup = new ModernSetup(page);
    await setup.open();
    const validation = await setup.verify(PACKAGED_DEVELOPMENT_TAG);
    expect(validation.action_state.selected_build.tag).toBe(PACKAGED_DEVELOPMENT_TAG);
    expect(validation.action_state.selected_build.channel).toBe("development");
    expect(validation.action_state.selected_build.build_id).toBe(PACKAGED_DEVELOPMENT_TAG);
    expect(validation.action_state.resource_strategy).toBe("embedded");
    expect(validation.action_state.alignment_state).toBe("aligned");
    await setup.continueToDevices();
  });

  test("v0.7.0 keeps the packaged modern Admin and advances", async ({ page }) => {
    const setup = new ModernSetup(page);
    await setup.open();
    const validation = await setup.verify("v0.7.0");
    expect(validation.action_state.compatibility_mode).toBe("legacy_release");
    expect(validation.action_state.resource_strategy).toBe("release_archive");
    expect(validation.action_state.admin_update_allowed).toBe(false);
    await setup.continueToDevices();
  });
});
