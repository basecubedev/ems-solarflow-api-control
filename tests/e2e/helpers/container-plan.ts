import type { Page } from "@playwright/test";

// The deterministic Admin runs no Docker, so its container plan can restart
// nothing. A spec that takes a restart through the page answers the plan here
// as one that can recreate EMS, and says at each read whether the saved
// settings are in effect and when the running EMS started.

export type ConfigState = { state: "current" | "pending" | "unknown"; ems_started_at?: string | null };

export async function routeRunnablePlan(page: Page, configState: () => ConfigState) {
  await page.route("**/api/admin/maintenance/containers/plan", async (route) => {
    const response = await route.fetch();
    const plan = await response.json();
    plan.available = true;
    plan.actions = [{ service: "ems", action: "recreate", label: "Recreate EMS", reason: "The settings file changed after EMS started." }];
    plan.config_state = configState();
    await route.fulfill({ response, json: plan });
  });
}
