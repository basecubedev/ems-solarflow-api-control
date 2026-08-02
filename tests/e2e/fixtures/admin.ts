import {
  test as base,
  expect,
  request,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

// The shared Admin password for the deterministic test server. Auth stays fully
// real (session cookie + CSRF); this only avoids re-typing a password per test.
export const ADMIN_PASSWORD = "e2e-admin-password";

async function ensureAuthenticated(ctx: APIRequestContext): Promise<string> {
  // First run creates the password; later runs log in. Either returns a CSRF
  // token bound to the new session.
  let res = await ctx.post("/api/admin/auth/setup", {
    data: { password: ADMIN_PASSWORD, confirm_password: ADMIN_PASSWORD },
  });
  if (res.status() === 409) {
    res = await ctx.post("/api/admin/auth/login", {
      data: { password: ADMIN_PASSWORD },
    });
  }
  const body = await res.json();
  return body.csrf_token as string;
}

// Reset the deterministic Admin to a known first-run state before every test:
// no transition, no known-good, no prepared release, running Admin back to the
// aligned modern build. This uses the gated /api/admin/test/reset route (behind
// the real session + CSRF gate).
// What a spec may describe about a device the deterministic Admin should have
// discovered over the local API. It is a *description*, not an identity: the
// real mDNS merge path publishes it and admin/observation_identity.py issues
// its ids, so a spec can set up a discovery state without being able to assert
// what the backend concludes about it.
export type LocalApiDeviceSpec = {
  ip: string;
  serial?: string;
  port?: number;
  role?: "inverter" | "grid_meter";
  api_family?: string;
  device_type?: string;
  display_name?: string;
  model?: string;
  config_ready?: boolean;
};

// A local broker and the telemetry observations it published, and the Zendure
// cloud account's device list. Same idea as above: the spec describes discovery
// state, the real proposal generator turns it into candidates.
export type LocalMqttBrokerSpec = {
  host: string;
  port?: number;
  tls?: boolean;
  devices?: Record<string, unknown>[];
};

export type DiscoveryStateSpec = {
  local_api_devices?: LocalApiDeviceSpec[];
  local_mqtt_brokers?: LocalMqttBrokerSpec[];
  cloud_devices?: Record<string, unknown>[];
};

type AdminFixtures = {
  freshAdmin: void;
  seedAdminScenario: (scenario: string) => Promise<void>;
  seedLocalApiDevices: (devices: LocalApiDeviceSpec[]) => Promise<void>;
  seedDiscoveryState: (state: DiscoveryStateSpec) => Promise<void>;
};

export const test = base.extend<AdminFixtures>({
  freshAdmin: [
    async ({ baseURL }, use) => {
      const ctx = await request.newContext({ baseURL });
      const csrf = await ensureAuthenticated(ctx);
      const res = await ctx.post("/api/admin/test/reset", {
        headers: { "X-CSRF-Token": csrf },
      });
      expect(res.ok()).toBeTruthy();
      await ctx.dispose();
      await use();
    },
    { auto: true },
  ],
  seedAdminScenario: async ({ page }, use) => {
    await use(async (scenario: string) => {
      const status = await page.request.get("/api/admin/auth/status");
      const auth = await status.json();
      const response = await page.request.post("/api/admin/test/seed", {
        headers: { "X-CSRF-Token": auth.csrf_token as string },
        data: { scenario },
      });
      expect(response.ok()).toBeTruthy();
    });
  },
  seedLocalApiDevices: async ({ page }, use) => {
    await use(async (devices: LocalApiDeviceSpec[]) => {
      await seedDiscovery(page, { local_api_devices: devices });
    });
  },
  seedDiscoveryState: async ({ page }, use) => {
    await use(async (state: DiscoveryStateSpec) => {
      await seedDiscovery(page, state);
    });
  },
});

async function seedDiscovery(page: Page, state: DiscoveryStateSpec) {
  const status = await page.request.get("/api/admin/auth/status");
  const auth = await status.json();
  const response = await page.request.post("/api/admin/test/seed", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: state,
  });
  expect(response.ok()).toBeTruthy();
}

export { expect };
