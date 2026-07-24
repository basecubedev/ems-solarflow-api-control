import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

// Smoke: Fresh Install exposes the promoted control-tuning knobs as primary
// (normal-level) fields, so they render outside "Advanced settings". This drives
// the real Admin over the browser session and checks the exact catalog endpoint
// the setup config step renders from (GET /api/setup/config/catalog): normal
// fields render inline, advanced/expert fields collapse into details blocks.

const PROMOTED_PRIMARY = [
  "system.max_total_power",
  "system.loop_interval",
  "system.output_control.target_deadband_w",
  "system.output_control.ramp_up_w_per_cycle",
  "system.output_control.ramp_down_w_per_cycle",
  "system.deadband",
  "system.output_control.device_ramp_up_w_per_cycle",
  "system.output_control.device_ramp_down_w_per_cycle",
];

const PROMOTED_LABELS = [
  "Maximum system output",
  "Loop interval",
  "System deadband",
  "System ramp up",
  "System ramp down",
  "Device deadband",
  "Device ramp up",
  "Device ramp down",
];

test.describe("Fresh Install — promoted control tuning", () => {
  test("promoted system and device tuning fields are primary, not Advanced-only", async ({
    page,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();

    const res = await page.request.get("/api/setup/config/catalog");
    expect(res.ok()).toBeTruthy();
    const catalog = await res.json();

    const system = catalog.sections.find((s: any) => s.id === "system");
    expect(system, "setup catalog exposes the system section").toBeTruthy();

    const byPath = new Map<string, any>(
      system.fields.map((f: any) => [f.path, f]),
    );
    for (const path of PROMOTED_PRIMARY) {
      const field = byPath.get(path);
      expect(field, `promoted field ${path} is present`).toBeTruthy();
      expect(field.level, `${path} renders as a primary field`).toBe("normal");
    }

    const labels = new Set<string>(system.fields.map((f: any) => f.label));
    for (const label of PROMOTED_LABELS) {
      expect(labels.has(label), `promoted label "${label}" is present`).toBe(
        true,
      );
    }
  });
});
