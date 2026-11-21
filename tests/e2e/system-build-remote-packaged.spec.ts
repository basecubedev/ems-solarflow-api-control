// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test, expect } from "@playwright/test";
import { ModernSetup } from "./helpers/modern-setup";

const TAG = process.env.CANARY_TAG as string;
const REVISION = process.env.CANARY_REVISION as string;
const BUILD_ID = process.env.CANARY_BUILD_ID as string;
const DISPLAY_NAME = process.env.CANARY_DISPLAY_NAME as string;
const ADMIN_DIGEST = process.env.CANARY_ADMIN_DIGEST as string;
const EMS_DIGEST = process.env.CANARY_EMS_DIGEST as string;

test("remote packaged Admin selects and executes the exact catalogue pair", { tag: ["@system-build"] }, async ({
  page,
}) => {
  for (const [name, value] of Object.entries({
    TAG,
    REVISION,
    BUILD_ID,
    DISPLAY_NAME,
    ADMIN_DIGEST,
    EMS_DIGEST,
  })) {
    expect(value, `${name} is required`).toBeTruthy();
  }

  const setup = new ModernSetup(page);
  await setup.open();

  const option = setup.option(TAG);
  await expect(option).toHaveCount(1);
  await expect(option).toContainText(`${DISPLAY_NAME} · ${REVISION.slice(0, 7)}`);

  const releases = await page.request.get("/api/setup/releases");
  expect(releases.ok()).toBe(true);
  const catalogue = await releases.json();
  const selected = catalogue.releases.find((entry: { tag: string }) => entry.tag === TAG);
  expect(selected).toMatchObject({
    tag: TAG,
    channel: "development",
    revision: REVISION,
    build_id: BUILD_ID,
    admin_digest: ADMIN_DIGEST,
    ems_digest: EMS_DIGEST,
    installable: true,
    selectable: true,
  });

  const validation = await setup.verify(TAG);
  expect(validation.system_build).toMatchObject({
    canonical_tag: TAG,
    channel: "development",
    revision: REVISION,
    build_id: BUILD_ID,
    admin_image: `ghcr.io/basecubedev/ems-solarflow-admin:${TAG}`,
    admin_digest: ADMIN_DIGEST,
    ems_image: `ghcr.io/basecubedev/ems-solarflow-api-control:${TAG}`,
    ems_digest: EMS_DIGEST,
  });
  expect(validation.action_state.resource_strategy).toBe("embedded");
  expect(validation.action_state.continue_allowed).toBe(true);
  if (process.env.PUBLIC_CATALOGUE_READ_ONLY === "1") return;
  await setup.continueToDevices();
});
