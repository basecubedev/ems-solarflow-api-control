// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// The Admin console's own pages in the one app at the root. It is driven here
// against the deterministic test-mode Admin: the sign-in, the language, and the
// pages whose writes it runs itself -- a backup from creation through details,
// restore and deletion.
import { readFileSync } from "node:fs";
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page, Route } from "@playwright/test";
import { post } from "./helpers/setup-authority";
import { E3DC_SYSTEM, OPENDTU_INVERTER, apiInverter } from "./helpers/discovery-state";
import { routeRunnablePlan } from "./helpers/container-plan";

/** The sign-in, opened from the top bar where the address showed the dashboard's pages. */
async function signIn(page: Page, password = ADMIN_PASSWORD) {
  const screen = page.getByTestId("sign-in-screen");
  await expect(screen.or(page.getByTestId("sign-in"))).toBeVisible();
  if (!(await screen.isVisible())) await page.getByTestId("sign-in").click();
  await expect(screen).toBeVisible();
  await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
  await page.fill("#admin-password", password);
  await page.getByTestId("sign-in-submit").click();
}

async function openModern(page: Page, hash = "") {
  await page.goto(`/${hash}`);
  await signIn(page);
  await expect(page.locator(".app")).toBeVisible();
  if (!hash) await expect(page).toHaveURL(/\/#\/assistant$/);
}

test.describe("Modern Admin: signing in", { tag: ["@smoke"] }, () => {
  test("a wrong password is refused in words, the right one opens the console, signing out leads to the Overview", async ({ page }) => {
    await page.goto("/");
    await signIn(page, "not-the-password");
    await expect(page.getByRole("alert")).toHaveText("Wrong password.");
    await expect(page.locator(".app")).toHaveCount(0);

    await page.fill("#admin-password", ADMIN_PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Assistant");

    await page.evaluate(() => {
      const seen = { signIn: false };
      (globalThis as unknown as { signInSeen: typeof seen }).signInSeen = seen;
      new MutationObserver((records) => {
        for (const record of records) {
          for (const node of record.addedNodes) {
            if (node instanceof Element && (node.matches('[data-testid="sign-in-screen"]') || node.querySelector('[data-testid="sign-in-screen"]'))) seen.signIn = true;
          }
        }
      }).observe(document.body, { childList: true, subtree: true });
    });
    const entries = await page.evaluate(() => history.length);
    await page.getByTestId("sign-out").click();
    await expect(page).toHaveURL(/#\/overview$/);
    await expect(page.locator('aside.sidebar a[data-page="assistant"] .lock')).toBeVisible();
    expect(await page.evaluate(() => history.length)).toBe(entries);
    await expect(page.getByTestId("sign-in")).toBeFocused();
    const status = await (await page.request.get("/api/admin/auth/status")).json();
    expect(status.authenticated).toBe(false);
    await expect(page.getByTestId("sign-in-screen")).toHaveCount(0);
    expect(await page.evaluate(() => (globalThis as unknown as { signInSeen: { signIn: boolean } }).signInSeen.signIn)).toBe(false);
  });

  test("a session that ended elsewhere brings the sign-in back", async ({ page }) => {
    await openModern(page);
    await page.request.post("/api/admin/auth/logout");
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
  });

  test("a run the ended session refused is not left behind as a failure once signed in again", async ({ page }) => {
    await openModern(page, "#/diagnostics");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Diagnostics");
    await page.request.post("/api/admin/auth/logout");
    await page.getByTestId("run-checks").click();
    await signIn(page);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Diagnostics");
    await expect(page.getByTestId("run-checks")).toBeEnabled();
    await expect(page.getByRole("alert")).toHaveCount(0);
  });

  test("a sign-out the server refuses leaves the console signed in and says so", async ({ page }) => {
    await openModern(page);
    await page.route("**/api/admin/auth/logout", (route: Route) => route.fulfill({ status: 500, json: { error: "internal_error" } }));
    await page.getByTestId("sign-out").click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.locator(".app")).toBeVisible();
    await expect(page.getByTestId("sign-in-screen")).toHaveCount(0);
    await expect(page).toHaveURL(/#\/assistant$/);
  });

  test("a password file the console cannot read shows the repair, never a password form", async ({ page }) => {
    await page.route("**/api/admin/auth/status", (route: Route) =>
      route.fulfill({ json: { auth_configured: true, authenticated: false, requires_initial_password: false, recovery_required: true, error: "auth_file_invalid", message: "The password file cannot be read." } }),
    );
    await page.goto("/#/status");
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "recovery");
    await expect(page.getByTestId("recovery-retry")).toBeVisible();
    await expect(page.locator("#admin-password")).toHaveCount(0);
  });

  test("the language chosen at the sign-in carries into the console", async ({ page }) => {
    await page.goto("/#/status");
    await page.getByTestId("sign-in-screen").getByRole("button", { name: "DE" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Anmelden");
    await signIn(page);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Status");
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
  });
});

async function withoutBackups(page: Page) {
  await page.route("**/api/admin/maintenance/backups", async (route: Route) => {
    const response = await route.fetch().catch(() => null);
    const body = await response?.json().catch(() => null);
    if (!response || !body) return;
    await route.fulfill({ response, json: { ...body, backups: [], sets: [], summary: { ...body.summary, total: 0, latest_created_at: null } } }).catch(() => {});
  });
}

async function installedAs(page: Page, ems: { running: boolean; found?: boolean }) {
  await page.route("**/api/admin/maintenance/overview", async (route: Route) => {
    const response = await route.fetch().catch(() => null);
    const body = await response?.json().catch(() => null);
    if (!response || !body) return;
    body.install_state = { ...body.install_state, state: "standard_install" };
    body.docker = { ...body.docker, available: true };
    body.containers = { ...body.containers, ems: { ...body.containers?.ems, found: ems.found ?? true, running: ems.running, status: ems.running ? "running" : "exited" } };
    body.links = { ...body.links, dashboard_url: "http://127.0.0.1:9/" };
    await route.fulfill({ response, json: body }).catch(() => {});
  });
}

async function landedAt(page: Page, address: string) {
  await page.goto("about:blank");
  await page.goto(address);
  await expect(page.locator(".app[data-landed]")).toBeVisible();
}

async function opens(page: Page, task: string, address: RegExp, target: string) {
  await page.goto("/#/assistant");
  await page.getByTestId("assistant-tasks").locator(`[data-task="${task}"]`).click();
  await expect(page).toHaveURL(address);
  await expect(page.getByTestId(target)).toBeVisible();
}

test.describe("Modern Admin: the assistant", { tag: ["@smoke"] }, () => {
  test("with nothing installed the app opens on it, and the guided setup comes first", async ({ page }) => {
    await openModern(page);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Assistant");
    const steps = page.getByTestId("assistant-steps");
    await expect(steps.locator('[data-step="install"]')).toHaveAttribute("data-state", "todo");
    await expect(steps.locator('[data-step="install"]')).toHaveClass(/next/);
    await expect(steps.locator('[data-step="install"]')).toHaveAttribute("aria-current", "step");
    await expect(steps.locator('[data-step="devices"]')).not.toHaveAttribute("aria-current", "step");
    await expect(page.getByTestId("assistant-setup")).toHaveAttribute("href", "#/setup");
    await expect(page.getByTestId("assistant-setup")).not.toHaveClass(/secondary/);
    await expect(page.getByTestId("assistant-restore")).toHaveAttribute("href", "#/backups/restore");
    await expect(steps.locator('[data-step="devices"]')).toHaveAttribute("data-state", "later");
    await expect(steps.locator('[data-step="backup"]')).toHaveAttribute("data-state", "later");
    await expect(steps.locator('[data-step="dashboard"]')).toHaveAttribute("data-state", "later");
    await expect(page.getByTestId("assistant-tasks")).toHaveCount(0);
    await expect(page.getByTestId("assistant-attention")).toHaveCount(0);
    await expect(page.locator(".sidebar a[href='#/assistant']")).toBeVisible();
    await expect(page.getByTestId("nav-mark-assistant")).toHaveClass(/warning/);

    await landedAt(page, "/");
    await expect(page).toHaveURL(/\/#\/assistant$/);
    await page.goBack();
    await expect(page).toHaveURL("about:blank");
    await page.goto("/#/assistant");

    await page.getByTestId("assistant-restore").click();
    await expect(page).toHaveURL(/\/#\/backups\/restore$/);
    await expect(page.getByTestId("bring-back")).toBeFocused();
    await page.goto("/#/assistant");
    await page.getByTestId("assistant-setup").click();
    await expect(page).toHaveURL(/\/#\/setup$/);
    await expect(page.getByTestId("setup-begin")).toBeEnabled();
  });

  test("an unfinished guided setup opens in place of it, at the bare address and at its own", async ({ page }) => {
    const transition = { mode: "fresh_install", stage: "failed_recoverable", system_tag: "latest", operation_id: "op-stuck", resume_available: true, cancel_available: true, worker_active: false, worker_status_available: true };
    await page.route("**/api/admin/system-alignment/status", async (route: Route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...(await response.json()), transition } });
    });
    await page.goto("/");
    await signIn(page);
    await expect(page).toHaveURL(/\/#\/setup$/);
    await expect(page.getByTestId("setup-recovery")).toBeVisible();
    await landedAt(page, "/#/assistant");
    await expect(page).toHaveURL(/\/#\/setup$/);
    await landedAt(page, "/#/updates");
    await expect(page).toHaveURL(/\/#\/updates$/);
  });

  test("on an installed system it lists what is left and the ways to the rest, and the app opens on the dashboard", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mixed_transports");
    await installedAs(page, { running: true });
    await withoutBackups(page);
    await landedAt(page, "/");
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator('.sidebar a[data-page="overview"]')).toHaveAttribute("aria-current", "page");
    await landedAt(page, "/#/assistant");
    const steps = page.getByTestId("assistant-steps");
    await expect(steps.locator('[data-step="install"]')).toHaveAttribute("data-state", "done");
    await expect(steps.locator('[data-step="devices"]')).toHaveAttribute("data-state", "done");
    await expect(steps.locator('[data-step="devices"]')).toContainText("3 devices are set up.");
    await expect(steps.locator('[data-step="backup"]')).toHaveAttribute("data-state", "todo");
    await expect(steps.locator('[data-step="backup"]')).toHaveClass(/next/);
    await expect(page.getByTestId("assistant-backup")).toHaveAttribute("href", "#/backups/create");
    await expect(page.getByTestId("assistant-dashboard")).toHaveAttribute("href", "#/overview");
    await expect(page.getByTestId("assistant-attention")).toHaveAttribute("href", "#/status");
    await expect(page.getByTestId("assistant-attention")).toContainText(/needs? your attention/);
    const tasks = page.getByTestId("assistant-tasks");
    await expect(tasks.locator("[data-task]")).toHaveCount(6);
    await expect(tasks.locator('[data-task="reinstall"]')).toContainText("make a backup first");
    await tasks.locator('[data-task="device"]').click();
    await expect(page).toHaveURL(/\/#\/settings\/devices$/);
    await expect(page.getByTestId("devices-editor")).toBeVisible();

    await page.goto("/#/assistant");
    await page.getByTestId("assistant-backup").click();
    await expect(page).toHaveURL(/\/#\/backups\/create$/);
    await expect(page.getByTestId("create-backup")).toBeFocused();
    await opens(page, "update", /\/#\/updates$/, "releases");
    await opens(page, "settings", /\/#\/settings$/, "settings-features");
    await opens(page, "backup", /\/#\/backups$/, "create-backup");
    await opens(page, "check", /\/#\/diagnostics$/, "run-checks");
    await opens(page, "reinstall", /\/#\/setup$/, "setup-begin");
    await opens(page, "device", /\/#\/settings\/devices$/, "discovery-start");

    await landedAt(page, "/#/services");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Services");
    await expect(page.getByTestId("nav-mark-assistant")).toHaveClass(/info/);
  });

  test("settings without an EMS container leave the installation to do, with the way to Updates", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mixed_transports");
    await installedAs(page, { running: false, found: false });
    await landedAt(page, "/#/assistant");
    await expect(page.getByTestId("assistant-welcome")).toContainText("EMS itself is not installed yet");
    await expect(page.getByTestId("assistant-welcome")).not.toContainText("Your EMS is installed");
    const install = page.getByTestId("assistant-steps").locator('[data-step="install"]');
    await expect(install).toHaveAttribute("data-state", "todo");
    await expect(install).toContainText("the EMS container does not exist");
    await expect(page.getByTestId("nav-mark-assistant")).toHaveClass(/warning/);
    await page.getByTestId("assistant-updates").click();
    await expect(page).toHaveURL(/\/#\/updates$/);
    await expect(page.getByTestId("releases")).toBeVisible();
  });

  test("an overview that cannot be read again is said, with the time of the last answer", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mixed_transports");
    await installedAs(page, { running: true });
    await landedAt(page, "/#/assistant");
    await expect(page.getByTestId("assistant-attention")).toBeVisible();
    await expect(page.getByTestId("assistant-unreadable")).toHaveCount(0);
    await page.route("**/api/admin/maintenance/overview", (route: Route) => route.fulfill({ status: 500, json: { ok: false, error: "internal_error" } }));
    await page.goto("/#/status");
    await page.getByTestId("refresh").click();
    await expect(page.getByTestId("overview-hero")).toHaveAttribute("data-level", "unreadable");
    await page.goto("/#/assistant");
    await expect(page.getByTestId("assistant-unreadable")).toBeVisible();
    await expect(page.getByTestId("assistant-stale")).toContainText("last readable answer");
    await expect(page.getByTestId("assistant-attention")).toBeVisible();
  });

  test("the dashboard waits while EMS is stopped, and the page speaks the reader's language", async ({ page, seedAdminScenario }) => {
    await page.addInitScript(() => window.localStorage.setItem("ems-modern.lang", "de"));
    await openModern(page);
    await seedAdminScenario("mixed_transports");
    await installedAs(page, { running: false });
    await landedAt(page, "/#/assistant");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Assistent");
    await expect(page.getByTestId("assistant-welcome")).toContainText("Dein EMS ist installiert.");
    await expect(page.getByTestId("assistant-steps").locator('[data-step="dashboard"]')).toHaveAttribute("data-state", "later");
    await expect(page.getByTestId("assistant-steps").locator('[data-step="dashboard"]')).toContainText("Öffnet sich, sobald das EMS läuft.");
    await expect(page.getByTestId("assistant-dashboard")).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 2, name: "Was möchtest du tun?" })).toBeVisible();
    await expect(page.getByTestId("assistant-tasks").locator('[data-task="reinstall"]')).toContainText("Neu einrichten");
  });
});

// The seed hook runs behind the page's own session, so the console is signed
// in first and read again once the scenario is in place.
async function seededAt(page: Page, seed: (scenario: string) => Promise<void>, hash: string) {
  await openModern(page);
  await seed("mixed_transports");
  await page.goto(`/${hash}`);
  await page.reload();
  await expect(page.locator(".app")).toBeVisible();
}

test.describe("Modern Admin: overview and status", { tag: ["@smoke"] }, () => {

  test("an incomplete installation is named, with the way out of it", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/status");
    const hero = page.getByTestId("overview-hero");
    await expect(hero).toHaveAttribute("data-level", "error");
    const finding = page.locator('[data-code="installation_incomplete"]');
    await expect(finding).toContainText("Settings without container setup");
    await expect(finding).toContainText("Missing here: Container setup");
    await expect(finding.getByRole("link", { name: "Set up a new system" })).toHaveAttribute("href", "#/setup");
    await expect(page.getByTestId("health-state")).toHaveText("Needs attention");
    await expect(page.getByTestId("tile-control")).toContainText("Calculates only");
  });

  test("the restart saved settings wait for runs in place from the overview's finding: a failure says why and offers it again, a success ends the finding, and a later save raises it afresh", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/status");
    await expect(page.getByTestId("overview-hero")).toHaveAttribute("data-level", "error");
    let restarted = false;
    let rewritten = false;
    let attempts = 0;
    await routeRunnablePlan(page, () => ({ state: restarted && !rewritten ? "current" : "pending" }));
    await page.route("**/api/admin/maintenance/overview", async (route) => {
      const response = await route.fetch();
      const overview = await response.json();
      if (!restarted || rewritten) {
        const waiting = { code: "settings_await_restart", severity: "info", title: "Saved settings are newer than the running EMS", message: "", next: "" };
        overview.health = { ...overview.health, findings: [...(overview.health?.findings || []), waiting] };
      }
      await route.fulfill({ response, json: overview });
    });
    await page.route("**/api/admin/maintenance/containers/sync", (route) => {
      attempts += 1;
      if (attempts === 1) return route.fulfill({ status: 500, json: { ok: false, status: "error", error: "sync_failed", message: "docker compose up failed" } });
      restarted = true;
      return route.fulfill({ json: { ok: true, status: "completed", steps: [{ service: "ems", action: "recreate", status: "ok" }], warnings: [] } });
    });
    await page.getByTestId("refresh").click();

    const finding = page.locator('[data-code="settings_await_restart"]');
    await expect(finding).toBeInViewport();
    await expect(finding).toContainText("Restart EMS to apply the saved settings.");
    const confirmation = page.getByRole("dialog", { name: "Restart EMS now?" });
    await finding.getByTestId("restart-ems").click();
    await confirmation.getByTestId("confirm").click();
    await expect(finding.getByTestId("services-result")).toHaveClass(/fault/);
    await expect(finding.getByTestId("restart-ems")).toBeEnabled();

    const reread = page.waitForResponse("**/api/admin/maintenance/overview");
    await finding.getByTestId("restart-ems").click();
    await confirmation.getByTestId("confirm").click();
    await reread;
    await expect(finding).toHaveCount(0);
    await expect(page).toHaveURL(/#\/status$/);
    expect(attempts).toBe(2);

    rewritten = true;
    await page.getByTestId("refresh").click();
    await expect(finding).toBeVisible();
    await expect(finding.getByTestId("services-result")).toHaveCount(0);
    await expect(finding.getByTestId("restart-ems")).toBeEnabled();
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("an inverter just added stays open when the next one is added", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const inverters = page.locator('section.device[data-device^="inverter-"]');
    await page.getByTestId("add-inverter").click();
    const first = inverters.last();
    const firstId = await first.getAttribute("data-device");
    await expect(first.locator("button.summary")).toHaveAttribute("aria-expanded", "true");

    await page.getByTestId("add-inverter").click();
    await expect(inverters.last()).not.toHaveAttribute("data-device", firstId as string);
    await expect(page.locator(`section.device[data-device="${firstId}"] button.summary`)).toHaveAttribute("aria-expanded", "true");
  });

  test("services show what should run and refuse a restart they cannot do", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/services");
    await expect(page.locator('[data-service="ems"]')).toContainText("Not set up");
    await expect(page.getByTestId("sync")).toBeDisabled();
    await expect(page.getByText("Docker or the container setup is not available")).toBeVisible();
  });

  test("the bundled InfluxDB is offered under Updates, lit in the menu, upgraded behind its backup and rolled back", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_available");
    await page.goto("/#/status");
    await page.reload();
    await expect(page.getByTestId("nav-mark-updates")).toHaveClass(/info/);
    await page.locator('.sidebar a[data-page="updates"]').click();
    const row = page.getByTestId("update-influxdb");
    await expect(row).toHaveAttribute("data-state", "available");
    await expect(row).toContainText("The update may take a very long time. Do not restart the system while it runs.");
    await expect(page.getByTestId("influx-state")).toHaveCount(0);
    const offer = (await row.locator(".name .num").textContent()) || "";
    expect(offer).toMatch(/^InfluxDB 2\.7 → 2\.\d+\.\d+$/);
    const target = offer.split("→")[1].trim();

    await page.getByTestId("influx-upgrade").click();
    const upgrade = page.getByRole("dialog", { name: `Upgrade InfluxDB to ${target}?` });
    await expect(upgrade).toContainText("A full InfluxDB backup is made first and checked");
    await expect(upgrade).toContainText("Do not restart the system while it runs.");
    await expect(upgrade).toContainText("2.7 cannot open the converted data");
    await upgrade.getByTestId("confirm").click();

    await expect(page.getByTestId("influx-outcome")).toContainText("InfluxDB is upgraded.");
    await expect(page.getByRole("list", { name: "Upgrade progress" }).locator('li[data-state="done"]')).toHaveCount(7);
    await expect(page.getByTestId("influx-current")).toHaveText(target);
    await expect(page.getByTestId("influx-last")).toContainText(`Upgraded from 2.7 to ${target}`);
    await expect(row).toHaveAttribute("data-state", "current");
    await expect(row).toContainText("Up to date");
    await expect(page.getByTestId("influx-upgrade")).toHaveCount(0);
    await expect(page.getByTestId("nav-mark-updates")).toHaveCount(0);

    await page.getByTestId("influx-rollback").click();
    const rollback = page.getByRole("dialog", { name: "Roll InfluxDB back to 2.7?" });
    await expect(rollback).toContainText("set aside (not deleted)");
    await rollback.getByTestId("confirm").click();

    await expect(page.getByTestId("influx-outcome")).toContainText("InfluxDB is back on the previous version.");
    await expect(page.getByTestId("influx-current")).toHaveText("2.7");
    await expect(page.getByTestId("influx-last")).toContainText("Rolled back to 2.7");
    await expect(page.getByTestId("influx-rollback")).toHaveCount(0);
    await expect(page.getByTestId("influx-upgrade")).toBeVisible();
    await expect(page.getByTestId("nav-mark-updates")).toHaveClass(/info/);
  });

  test("an InfluxDB upgrade says in its own line which step runs, and there how it ended, with the new version up to date", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_available");
    let held = true;
    await page.route("**/api/admin/maintenance/influxdb/jobs/*", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (held) {
        body.status = "running";
        body.steps = body.steps.map((step: Record<string, unknown>, index: number) => ({ ...step, state: index < 3 ? "done" : index === 3 ? "running" : "pending" }));
        delete body.result;
      }
      await route.fulfill({ response, json: body });
    });
    await page.goto("/#/updates");
    await page.reload();
    const row = page.getByTestId("update-influxdb");
    await expect(row).toHaveAttribute("data-state", "available");
    await page.getByTestId("influx-upgrade").click();
    await page.getByRole("dialog", { name: /Upgrade InfluxDB to/ }).getByTestId("confirm").click();

    await expect(row).toHaveAttribute("data-run", "running");
    await expect(row).toContainText("Step 4 of 7 · Back up InfluxDB and check the backup");
    await expect(row.locator(".status")).toHaveText("Upgrading");
    await expect(page.getByTestId("influx-upgrade")).toHaveCount(0);

    held = false;
    await expect(row).toHaveAttribute("data-run", "done");
    await expect(row.getByTestId("influx-line-outcome")).toHaveText("InfluxDB is upgraded.");
    await expect(page.getByTestId("update-influxdb-version")).toHaveText(/^InfluxDB 2\.\d+\.\d+$/);
    await expect(row.locator(".status")).toHaveText("Up to date");
    const running = page.locator(".fact", { has: page.getByTestId("influx-current") });
    await expect(running.locator(".status")).toHaveText("Up to date");
    await expect(page.getByTestId("influx-outcome")).toContainText("InfluxDB is upgraded.");
  });

  test("an InfluxDB upgrade the device lacks the memory for is shown with its figures and not offered", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_low_memory");
    await page.goto("/#/status");
    await page.reload();
    await expect(page.getByTestId("nav-mark-updates")).toHaveClass(/info/);
    await page.locator('.sidebar a[data-page="updates"]').click();
    const row = page.getByTestId("update-influxdb");
    await expect(row).toHaveAttribute("data-state", "low_memory");
    await expect(row.locator(".name .num")).toHaveText(/^InfluxDB 2\.7 → 2\.\d+\.\d+$/);
    await expect(row).toContainText("it needs about 124 MB free, 100 MB of memory and swap are free");
    await expect(page.getByTestId("influx-upgrade")).toHaveCount(0);
    await expect(page.getByTestId("influx-state")).toContainText("Switch on swap, shorten the retention under Settings");
  });

  test("a start that takes long is stopped from the card, and the run that did not finish is checked again", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_available");
    const record = { from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "ems-influxdb-b.tar.gz", started_at: "2026-11-18T10:00:00Z" };
    const base = { ok: true, current_image: "influxdb:2.9.1", target_image: "influxdb:2.9.1", current_version: "2.9.1", target_version: "2.9.1", container: { name: "ems-influxdb", running: true, found: true, image: "influxdb:2.9.1" } };
    const plans = {
      slow: { ...base, state: "busy", slow: true, live: true, last: { ...record, state: "running", finished_at: null }, rollback: null, recheck: null },
      stopped: {
        ...base, state: "current", last: { ...record, state: "failed", finished_at: "2026-11-18T10:40:00Z", message: "Waiting was stopped on request; InfluxDB may still be starting." },
        rollback: { available: true, to_image: "influxdb:2.7", backup: record.backup, since: record.started_at, reason: null }, recheck: { available: true, reason: null },
      },
      done: { ...base, state: "current", last: { ...record, state: "upgraded", finished_at: "2026-11-18T11:00:00Z" }, rollback: { available: true, to_image: "influxdb:2.7", backup: record.backup, since: record.started_at, reason: null }, recheck: null },
    };
    let plan = plans.slow;
    let readsSinceStop = -1;
    const sent: Record<string, unknown>[] = [];
    await page.route("**/api/admin/maintenance/influxdb", (route: Route) => {
      if (readsSinceStop >= 0 && ++readsSinceStop >= 2) {
        plan = plans.stopped;
        readsSinceStop = -1;
      }
      return route.fulfill({ json: plan });
    });
    await page.route("**/api/admin/maintenance/influxdb/stop-waiting", (route: Route) => {
      sent.push({ stop: route.request().postDataJSON() });
      readsSinceStop = 0;
      return route.fulfill({ status: 202, json: { ok: true, status: "stopping" } });
    });
    await page.route("**/api/admin/maintenance/influxdb/recheck", (route: Route) => {
      sent.push({ recheck: route.request().postDataJSON() });
      plan = plans.done;
      return route.fulfill({ json: { ok: true, status: "completed", message: "InfluxDB runs influxdb:2.9.1 with every bucket and task." } });
    });
    await page.goto("/#/updates");
    await page.reload();

    await expect(page.getByTestId("influx-last")).toContainText("has been running since");
    await page.getByTestId("influx-stop-waiting").click();
    const stop = page.getByRole("dialog", { name: "Stop waiting for InfluxDB?" });
    await expect(stop).toContainText("The run ends as not completed.");
    await expect(stop).toContainText("Roll back then restores the backup made first");
    await stop.getByTestId("confirm").click();

    await expect(page.getByTestId("influx-stop-waiting")).toBeDisabled();
    await expect(page.getByTestId("influx-stop-waiting")).toContainText("Stopping the wait");
    await expect(page.getByTestId("update-influxdb")).toContainText("Look at it", { timeout: 15_000 });
    await expect(page.getByTestId("nav-mark-updates")).toHaveClass(/warning/);
    await expect(page.getByTestId("influx-stop-waiting")).toHaveCount(0);
    await expect(page.getByTestId("influx-rollback")).toBeVisible();
    await page.getByTestId("influx-recheck").click();

    await expect(page.getByTestId("influx-recheck-outcome")).toContainText("the upgrade is complete");
    await expect(page.getByTestId("update-influxdb")).toContainText("Up to date");
    await expect(page.getByTestId("influx-recheck")).toHaveCount(0);
    await expect(page.getByTestId("nav-mark-updates")).toHaveCount(0);
    expect(sent).toEqual([{ stop: { confirm: true } }, { recheck: {} }]);
  });

  test("a run this page loses hold of is followed from the server, never reported as not completed", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_available");
    const record = { from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b.tar.gz", started_at: "2026-11-18T10:00:00Z" };
    const base = { ok: true, target_image: "influxdb:2.9.1", target_version: "2.9.1", rollback: null, recheck: null };
    let plan: Record<string, unknown> = { ...base, state: "available", current_image: "influxdb:2.7", current_version: "2.7", last: null };
    await page.route("**/api/admin/maintenance/influxdb", (route: Route) => route.fulfill({ json: plan }));
    await page.route("**/api/admin/maintenance/influxdb/upgrade", (route: Route) => {
      plan = { ...base, state: "busy", live: true, slow: false, current_image: "influxdb:2.9.1", current_version: "2.9.1", last: { ...record, state: "running", finished_at: null } };
      return route.fulfill({ status: 202, json: { ok: true, job_id: "gone", status: "running", steps: [{ key: "preflight", label: "Check", state: "running", message: null }] } });
    });
    await page.route("**/api/admin/maintenance/influxdb/jobs/gone", (route: Route) => route.fulfill({ status: 404, json: { ok: false, error: "job not found" } }));
    await page.goto("/#/updates");
    await page.reload();

    await page.getByTestId("influx-upgrade").click();
    await page.getByRole("dialog", { name: "Upgrade InfluxDB to 2.9.1?" }).getByTestId("confirm").click();

    await expect(page.getByTestId("influx-last")).toContainText("has been running since");
    await expect(page.getByTestId("influx-detached")).toContainText("the run goes on on the server");
    await expect(page.getByTestId("influx-lost")).toHaveCount(0);
    await expect(page.getByTestId("influx-outcome")).toHaveCount(0);
    plan = { ...base, state: "current", current_image: "influxdb:2.9.1", current_version: "2.9.1", last: { ...record, state: "upgraded", finished_at: "2026-11-18T11:00:00Z" } };
    await page.getByTestId("influx-state").locator("xpath=ancestor::section[1]").getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByTestId("influx-last")).toContainText("Upgraded from 2.7 to 2.9.1");
    await expect(page.getByTestId("update-influxdb")).toContainText("Up to date");
    await expect(page.getByTestId("influx-outcome")).toHaveCount(0);
    await expect(page.getByTestId("influx-detached")).toHaveCount(0);
  });

  test("stopping a way back that waits long says the way back ends and InfluxDB is put back", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_available");
    await page.route("**/api/admin/maintenance/influxdb", (route: Route) => route.fulfill({ json: {
      ok: true, state: "busy", slow: true, live: true, current_image: "influxdb:2.7", target_image: "influxdb:2.9.1",
      last: { state: "rolling_back", from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b.tar.gz", started_at: "2026-11-01T10:00:00Z", finished_at: "2026-11-01T10:20:00Z", rollback_started_at: "2026-11-18T09:00:00Z" },
      rollback: null, recheck: null,
    } }));
    await page.goto("/#/updates");
    await page.reload();

    await expect(page.getByTestId("influx-last")).toContainText("The way back to 2.7 has been running since");
    await page.getByTestId("influx-stop-waiting").click();
    const stop = page.getByRole("dialog", { name: "Stop waiting for InfluxDB?" });
    await expect(stop).toContainText("The way back ends as not completed.");
    await expect(stop).toContainText("The way back then puts InfluxDB back as it was before it.");
    await stop.getByRole("button", { name: "Cancel" }).click();
    await expect(stop).toHaveCount(0);
  });

  test("the services page leaves the InfluxDB version to the Updates page", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("influxdb_upgrade_available");
    await page.goto("/#/services");
    await page.reload();
    await expect(page.locator('[data-service="influxdb"]')).toBeVisible();
    await expect(page.getByTestId("influx-upgrade")).toHaveCount(0);
    await expect(page.getByTestId("update-influxdb")).toHaveCount(0);
  });

  test("an installation without the bundled InfluxDB lists no InfluxDB under Updates", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/updates");
    await expect(page.getByTestId("update-release")).toBeVisible();
    await expect(page.getByTestId("update-influxdb")).toHaveCount(0);
    await expect(page.getByTestId("influx-state")).toHaveCount(0);
  });

  test("the settings edit the devices here, with the network search beside them and no way to Classic", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    await expect(page.getByTestId("settings-features")).toContainText("Winter mode");
    await page.getByRole("button", { name: "Devices", exact: true }).click();
    const editor = page.getByTestId("devices-editor");
    await expect(editor.locator("[data-device^='inverter-'], [data-device^='mqtt-']")).toHaveCount(3);
    await expect(editor).toContainText("Zendure via the Zendure cloud");
    await expect(editor.locator('[data-device="zendure-broker"]')).toContainText("read only");
    await expect(page.getByTestId("discovery-start")).toBeVisible();
    await expect(page.locator(".content a[href^='/#']")).toHaveCount(0);
  });

  test("the checks say plainly when there is no EMS to check", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/diagnostics");
    await page.getByTestId("run-checks").click();
    await expect(page.getByTestId("diagnostics-summary")).toContainText("Not available");
    await expect(page.getByText("No running EMS container and no emsctl.py were found")).toBeVisible();
  });

  test("the system page lists the files and stays quiet about a workflow that is idle", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/system");
    await expect(page.getByText("Settings file", { exact: true })).toBeVisible();
    await expect(page.getByText("local_mixed")).toBeVisible();
    await expect(page.getByTestId("recovery-summary")).toHaveCount(0);
  });
});

test.describe("Modern Admin: settings", { tag: ["@smoke"] }, () => {
  test("the settings catalogue and its choices are said in the reader's language", async ({ page, seedAdminScenario }) => {
    await page.addInitScript(() => window.localStorage.setItem("ems-modern.lang", "de"));
    await seededAt(page, seedAdminScenario, "#/settings/expert");
    const expert = page.getByTestId("settings-expert");
    await expert.getByRole("button", { name: /Systemgrundlagen/ }).click();
    const level = expert.locator('[data-path="system.log_level"]');
    await expect(level).toContainText("Protokollstufe");
    await expect(level).toContainText("Legt fest, wie ausführlich das EMS protokolliert.");
    await expect(level.locator("option")).toContainText(["Debug", "Info", "Warnung", "Fehler"]);
    await expect(level.locator(".label")).not.toHaveAttribute("lang", "en");
    await page.getByTestId("settings-search").fill("Schleifenintervall");
    await expect(expert.locator('[data-path="system.loop_interval"]')).toBeVisible();
    await expect(expert.locator('[data-path="system.log_level"]')).toHaveCount(0);
    await page.getByTestId("settings-search").fill("");
    await page.goto("/#/settings/safety");
    await expect(page.getByTestId("settings-safety")).toContainText("Was das EMS ändern darf");

    await page.goto("/#/settings/devices");
    const editor = page.getByTestId("devices-editor");
    await expect(editor).toContainText("Ältere Zendure-Hub-/Hyper-Generation");
    await expect(editor).not.toContainText("Older Zendure Hub / Hyper generation");
    const meter = editor.locator('[data-device="grid-meter"]');
    await meter.getByRole("button", { name: /Netzzähler/ }).click();
    await editor.locator("#grid-type").selectOption("e3dc_modbus");
    await expect(meter).toContainText("E3/DC-Speichersystem über Modbus TCP");
    const port = editor.locator('[data-path="grid_meter.port"]');
    await expect(port.locator(".desc")).toHaveText("Modbus-TCP-Port. Beim E3/DC standardmäßig 502.");
    await expect(port.locator(".label")).not.toHaveAttribute("lang", "en");
  });

  test("a feature is switched on, checked and saved, and the saved settings say so", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    const winter = page.locator('[data-section="winter"]');
    const toggle = winter.getByRole("checkbox", { name: "Winter mode on or off" });
    const before = await toggle.isChecked();
    await toggle.click();
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");

    await page.getByTestId("review").click();
    const review = page.getByTestId("settings-review");
    await expect(review).toContainText("Settings are valid");
    await expect(review).toContainText("winter.enabled");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toContainText("Saved, with a backup of the previous settings:");

    await page.reload();
    await expect(page.locator('[data-section="winter"]').getByRole("checkbox", { name: "Winter mode on or off" })).toBeChecked({ checked: !before });
    await expect(page.getByTestId("settings-changes")).toHaveCount(0);
  });

  test("Saved shows only once the saved settings have been read back", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    await page.locator('[data-section="winter"]').getByRole("checkbox", { name: "Winter mode on or off" }).click();
    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    let release;
    const held = new Promise((resolve) => (release = resolve));
    let readBack;
    const reading = new Promise((resolve) => (readBack = resolve));
    let answered = false;
    page.on("response", (response) => {
      if (response.url().endsWith("/api/admin/maintenance/config/apply")) answered = true;
    });
    const config = /\/api\/admin\/maintenance\/config(\?.*)?$/;
    const hold = async (route) => {
      if (route.request().method() === "GET") {
        if (answered) readBack();
        await held;
      }
      await route.fallback();
    };
    await page.route(config, hold);
    try {
      await page.getByTestId("apply").click();
      await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
      await reading;
      await expect(page.getByTestId("apply")).toHaveText(/Saving…/);
      await expect(page.getByTestId("settings-applied")).toHaveCount(0);
    } finally {
      release();
    }
    await expect(page.getByTestId("settings-applied")).toContainText("Saved, with a backup of the previous settings:");
    await page.unroute(config, hold);
  });

  test("battery care is a feature right after winter mode, with its own name, mark, switch and every setting in view", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    const sections = await page.locator("[data-section]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-section")));
    expect(sections.indexOf("battery_care")).toBe(sections.indexOf("winter") + 1);
    const care = page.locator('[data-section="battery_care"]');
    await expect(care.getByRole("checkbox", { name: "Battery care on or off" })).toBeVisible();
    await expect(care.locator(".summary .name")).toHaveText("Battery care");
    await expect(care.locator(".summary .icon-tile use")).toHaveAttribute("href", "#i-battery-low");

    await care.locator(".summary").click();
    for (const [key, shipped] of [["summer_full_points", "3"], ["winter_full_points", "4"], ["pv_only_points", "1"], ["min_discharge_w", "50"], ["pv_margin_percent", "20"], ["pv_margin_min_w", "35"]]) {
      const input = care.locator(`[data-path="battery_care.${key}"] input`);
      await expect(input).toBeVisible();
      await expect(input).toHaveAttribute("placeholder", `${shipped} (default)`);
    }
    await expect(care.locator("details.level")).toHaveCount(0);
  });

  test("a value away from its default names the default beside it, and an emptied one names it grey in the field", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/safety");
    const minimum = page.locator('[data-path="system.min_output_limit"]');
    const input = minimum.locator("input");
    await input.fill("40");
    await expect(minimum.locator(".badge.default")).toHaveText("Default: 35");
    await input.pressSequentially("e");
    await expect(minimum.locator(".form-error")).toHaveText("Not a number");
    await expect(minimum.locator(".badge.default")).toHaveText("Default: 35");
    await page.getByTestId("review").click();
    await expect(page.getByTestId("unreadable-number")).toBeVisible();
    await expect(page.getByTestId("settings-review")).toHaveCount(0);
    await input.fill("");
    await expect(input).toHaveAttribute("placeholder", "35 (default)");
    await expect(minimum.locator(".badge.default")).toHaveCount(0);
    await input.fill("35");
    await expect(minimum.locator(".badge.default")).toHaveCount(0);

    await page.goto("/#/settings/expert");
    const system = page.locator('[data-section="system"]');
    await system.locator(".summary").click();
    await system.locator("details.level summary").first().click();
    const level = system.locator('[data-path="system.log_level"]');
    await level.locator("select").selectOption("debug");
    await expect(level.locator('option[value="info"]')).toHaveText(/\(default\)$/);
    await expect(level.locator("option", { hasText: "(default)" })).toHaveCount(1);
    await expect(level.locator(".badge.default")).toHaveText(/^Default: /);
  });

  test("an emptied value is saved as the value it named grey, on a setting and on a device", async ({ page, seedAdminScenario }) => {
    const save = async () => {
      await page.getByTestId("review").click();
      await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
      await page.getByTestId("apply").click();
      await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
      await expect(page.getByTestId("settings-applied")).toBeVisible();
      await page.reload();
    };
    await seededAt(page, seedAdminScenario, "#/settings/safety");
    const minimum = page.locator('[data-path="system.min_output_limit"]');
    await minimum.locator("input").fill("40");
    await page.goto("/#/settings/devices");
    const inverter = page.getByTestId("devices-editor").locator('[data-device="inverter-0"]');
    await inverter.locator(".summary").click();
    await inverter.locator('[data-path="devices[].min_soc"] input').fill("20");
    await inverter.locator('[data-path="devices[].max_power"] input').fill("700");
    await save();

    await inverter.locator(".summary").click();
    const soc = inverter.locator('[data-path="devices[].min_soc"] input');
    const ceiling = inverter.locator('[data-path="devices[].max_power"] input');
    await expect(soc).toHaveValue("20");
    await expect(ceiling).toHaveValue("700");
    await soc.fill("");
    await expect(soc).toHaveAttribute("placeholder", "15 (default)");
    await ceiling.fill("");
    await expect(ceiling).toHaveAttribute("placeholder", "800 (default)");
    await page.goto("/#/settings/safety");
    await expect(minimum.locator(".badge.default")).toHaveText("Default: 35");
    await minimum.locator("input").fill("");
    await expect(minimum.locator("input")).toHaveAttribute("placeholder", "35 (default)");
    await save();

    await expect(minimum.locator("input")).toHaveValue("35");
    await expect(minimum.locator(".badge.default")).toHaveCount(0);
    await page.goto("/#/settings/devices");
    await inverter.locator(".summary").click();
    await expect(soc).toHaveValue("15");
    await expect(ceiling).toHaveValue("800");
  });

  test("the Customized tab lists each saved value away from its default and leads to where it is set", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/defaults");
    await expect(page.getByRole("button", { name: "Customized (0)", exact: true })).toBeVisible();
    await expect(page.getByText("Every setting follows its default.")).toBeVisible();

    await page.goto("/#/settings/safety");
    await page.locator('[data-path="system.min_output_limit"] input').fill("40");
    await page.getByRole("button", { name: "Customized (0)", exact: true }).click();
    await expect(page.getByTestId("defaults-unsaved")).toBeVisible();
    await expect(page.getByTestId("settings-defaults")).toHaveCount(0);
    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    const row = page.getByTestId("settings-defaults").locator('[data-path="system.min_output_limit"]');
    await expect(page.getByRole("button", { name: "Customized (1)", exact: true })).toBeVisible();
    await expect(row.locator(".badge.saved")).toHaveText("Saved: 40");
    await expect(row.locator(".badge.default")).toHaveText("Default: 35");
    await expect(row.locator(".badge.live")).toHaveCount(0);
    await expect(page.getByTestId("defaults-unsaved")).toHaveCount(0);

    await row.getByRole("button", { name: "Show" }).click();
    await expect(page).toHaveURL(/#\/settings\/safety$/);
    await expect(page.getByTestId("settings-search")).toHaveValue("system.min_output_limit");
    await expect(page.locator('[data-path="system.min_output_limit"] input')).toHaveValue("40");
  });

  test("Show on a device's departure opens that device's card at the field, folded group included, after an unsaved removal too", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const editor = page.getByTestId("devices-editor");
    const field = '[data-path="devices[].pv_priority_factor"]';
    const hyper = editor.locator('[data-device="mqtt-1"]');
    await hyper.locator(".summary").click();
    await hyper.locator("details.level", { has: page.locator(field) }).locator("summary").click();
    await hyper.locator(`${field} input`).fill("2");
    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    await expect(page.getByRole("button", { name: "Customized (1)", exact: true })).toBeVisible();
    await editor.locator('[data-device="inverter-0"]').getByTestId("device-remove").click();
    await expect(page.getByTestId("change-count")).toContainText("unsaved change");
    await expect(editor.locator('[data-device="mqtt-0"]')).toContainText("Local MQTT inverter");
    await page.getByRole("button", { name: "Customized (1)", exact: true }).click();
    const group = page.getByTestId("settings-defaults").locator('[data-group="device:1"]');
    await expect(group.locator("h3")).toContainText("Local MQTT inverter");
    const row = group.locator(`${field}`);
    await expect(row.locator(".badge.saved")).toHaveText("Saved: 2");
    await expect(row.locator(".badge.default")).toHaveText("Default: 1");
    await page.getByTestId("settings-search").fill("local mqtt");
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: /^Show / }).click();

    await expect(page).toHaveURL(/#\/settings\/devices$/);
    const moved = editor.locator(`[data-device="mqtt-0"] ${field} input`);
    await expect(moved).toBeVisible();
    await expect(moved).toHaveValue("2");
    await expect(moved).toBeFocused();
    await expect(editor.locator(`[data-device="mqtt-1"] ${field}`)).toBeHidden();
  });

  test("the overview's Control & safety card opens the safety settings", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/status");
    await page.getByRole("link", { name: "Change", exact: true }).click();
    await expect(page).toHaveURL(/#\/settings\/safety$/);
    await expect(page.getByTestId("settings-safety")).toContainText("Allow hardware writes");
  });

  test("a device is changed, another added, both checked and saved", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const editor = page.getByTestId("devices-editor");
    const first = editor.locator('[data-device="inverter-0"]');
    await first.locator(".summary").click();
    await first.locator('[data-path="devices[].max_power"] input').fill("700");
    await page.getByTestId("add-e3dc").click();
    const e3dc = editor.locator('[data-device="e3dc-3"]');
    await expect(e3dc).toContainText("E3DC");
    await e3dc.locator('[data-path="e3dcHost"] input').fill("192.168.50.30");
    await expect(page.getByTestId("change-count")).toHaveText("2 unsaved changes");

    await page.getByTestId("review").click();
    const review = page.getByTestId("settings-review");
    await expect(review).toContainText("Settings are valid");
    await expect(review).toContainText("devices");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    await page.reload();
    await expect(editor.locator('[data-device="inverter-0"]')).toContainText("700 W");
    await expect(editor.locator('[data-device="e3dc-3"]')).toContainText("192.168.50.30");
  });

  test("an inverter an OpenDTU reads is added read only, checked and saved into the config", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const editor = page.getByTestId("devices-editor");
    await page.getByTestId("add-opendtu").click();
    const card = editor.locator('[data-device="opendtu-3"]');
    await expect(card).toContainText("Inverter read by an OpenDTU");
    await expect(card).toContainText("OpenDTU, read only");
    await expect(card.locator('[data-path="name"] input')).toHaveValue("OpenDTU");
    await expect(card.locator('[data-path="opendtuPort"] input')).toHaveAttribute("placeholder", "80 (default)");
    await expect(card.locator('[data-path="opendtuMaxAge"] input')).toHaveAttribute("placeholder", "60 (default)");
    await card.locator('[data-path="name"] input').fill("Balcony");
    await card.locator('[data-path="opendtuHost"] input').fill("192.168.50.70");
    await card.locator('[data-path="opendtuSerial"] input').fill("116491234567");
    await card.locator('[data-path="opendtuMaxAge"] input').fill("90");

    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    const saved = await (await page.request.get("/api/admin/maintenance/config")).json();
    const written = JSON.parse(readFileSync(saved.config_path, "utf8"));
    expect(written.devices.filter((device: { type?: string }) => device.type === "opendtu_http")).toEqual([
      { name: "Balcony", type: "opendtu_http", ip: "192.168.50.70", serial: "116491234567", max_age_seconds: 90, enabled: true },
    ]);
    await page.reload();
    await expect(editor.locator('[data-device="opendtu-3"]')).toContainText("Balcony · 192.168.50.70 · SN 116491234567 · read only");
  });

  test("an input the EMS saw dark for days is marked empty from its hint, another by its box, checked and saved", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mixed_transports");
    await seedAdminScenario("ems_started_pv_hint");
    await page.goto("/#/settings/devices");
    await page.reload();
    const card = page.getByTestId("devices-editor").locator('[data-device="inverter-0"]');
    await expect(card.locator(".side")).toContainText("Check PV inputs");
    await card.locator(".summary").click();
    const hint = card.getByTestId("pv-hint");
    await expect(hint).toContainText("PV 1 has delivered nothing for 3 days while the other inputs did.");

    await hint.getByTestId("pv-hint-apply").click();
    await expect(card.getByTestId("pv-hint")).toHaveCount(0);
    await expect(card.locator(".side")).not.toContainText("Check PV inputs");
    await card.locator("details.level").filter({ hasText: "Advanced settings" }).locator("summary").click();
    await expect(card.getByTestId("pv-input-1")).not.toBeChecked();
    await expect(card.getByTestId("pv-input-4")).toBeChecked();
    await card.getByTestId("pv-input-4").uncheck();

    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    const saved = await (await page.request.get("/api/admin/maintenance/config")).json();
    const written = JSON.parse(readFileSync(saved.config_path, "utf8"));
    expect(written.devices[0].unused_pv_inputs).toEqual([1, 4]);
    await page.reload();
    await expect(card.locator(".side")).not.toContainText("Check PV inputs");
  });

  test("the grid meter's type changes its fields, and removing it leaves none", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const meter = page.getByTestId("devices-editor").locator('[data-device="grid-meter"]');
    await meter.locator(".summary").click();
    await expect(meter.locator('[data-path="grid_meter.ip"] input')).toHaveValue("192.168.50.2");
    await page.getByTestId("grid-type").selectOption("zendure_smartmeter_d0");
    await expect(meter.locator('[data-path="d0Serial"]')).toBeVisible();
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");
    await meter.getByTestId("device-remove").click();
    await expect(meter).toContainText("No grid meter configured");
  });

  test("the grid meter becomes a paired HomeWizard P1 Meter at the same address, checked and saved", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const meter = page.getByTestId("devices-editor").locator('[data-device="grid-meter"]');
    await meter.locator(".summary").click();
    await page.getByTestId("grid-type").selectOption("homewizard_p1");
    await expect(meter).toContainText("HomeWizard P1 Meter");
    await expect(meter.locator('[data-path="grid_meter.ip"] input')).toHaveValue("192.168.50.2");
    const pairing = meter.getByTestId("homewizard-pairing");
    await expect(pairing.getByTestId("homewizard-state")).toHaveText("Not paired yet: the EMS reads the meter only after pairing.");

    await pairing.getByTestId("homewizard-pair").click();
    await expect(pairing).toContainText("Press the button on the P1 Meter now.");
    await expect(pairing.getByTestId("homewizard-state")).toHaveText("Paired with P1 Meter 0a1b2c3d4e5f. Save the settings to keep the pairing.");

    await page.getByTestId("review").click();
    const review = page.getByTestId("settings-review");
    await expect(review).toContainText("Settings are valid");
    await expect(review).toContainText("grid_meter");
    await expect(review).not.toContainText("0123456789ABCDEF0123456789ABCDEF");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    await page.reload();
    await expect(page.getByTestId("grid-type")).toHaveValue("homewizard_p1");
    await expect(page.getByTestId("homewizard-state")).toHaveText("Paired with P1 Meter 0a1b2c3d4e5f.");
    await expect(page.getByTestId("settings-changes")).toHaveCount(0);

    await meter.locator(".summary").click();
    await page.getByTestId("homewizard-pair").click();
    await expect(page.getByTestId("homewizard-pairing")).toContainText("Pairing again ends the current pairing at once");
    await page.getByTestId("homewizard-pairing").getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByTestId("homewizard-state")).toHaveText("Paired with P1 Meter 0a1b2c3d4e5f.");
  });

  test("an edit survives a reload, the search finds it, and discard leaves the file alone", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    await page.getByRole("button", { name: "Expert", exact: true }).click();
    await page.getByTestId("settings-search").fill("loop interval");
    const field = page.locator('[data-path="system.loop_interval"] input');
    await expect(field).toBeVisible();
    await field.fill("7");
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");

    await page.reload();
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");
    await page.getByTestId("discard").click();
    await expect(page.getByTestId("settings-changes")).toHaveCount(0);
    await page.getByRole("button", { name: "Expert", exact: true }).click();
    await page.getByTestId("settings-search").fill("loop interval");
    await expect(page.locator('[data-path="system.loop_interval"] input')).not.toHaveValue("7");
  });
});

test.describe("Modern Admin: older MQTT setup", { tag: ["@smoke"] }, () => {
  test("the migration is reviewed and applied here, without showing a secret", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.goto("/#/system");
    await page.reload();
    await expect(page.getByTestId("migration-devices").locator("li").first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText("e2e-super-secret-broker-password");
    await expect(page.getByTestId("migration-backup")).toBeChecked();
    const request = page.waitForRequest((req) => req.url().endsWith("/zendure-mqtt/migration-apply"));
    const devices = await page.getByTestId("migration-devices").locator("li").count();
    await page.getByTestId("migration-apply").click();
    const dialog = page.getByRole("dialog", { name: "Apply the migration?" });
    await expect(dialog).toContainText(devices === 1 ? "1 device is set up anew." : `${devices} devices are set up anew.`);
    await expect(dialog).toContainText("A backup of the current settings is made first.");
    await dialog.getByTestId("confirm").click();
    const sent = await request;
    expect(sent.headers()["x-csrf-token"]).toBeTruthy();
    expect(sent.postDataJSON()).toMatchObject({ confirm: true, backup: true });
    await expect(page.getByTestId("migration-result")).toHaveText("The MQTT setup is up to date.");
    await expect(page.getByTestId("migration-apply")).toHaveCount(0);
  });
});

const EXTERNAL = "E2EMODERN0001";

function externalBroker() {
  const host = "192.168.100.72";
  return {
    host,
    port: 1883,
    devices: [
      {
        broker_id: `mqtt:${host}:1883`,
        broker_host: host,
        broker_port: 1883,
        source_type: "local_mqtt",
        topic_family: "ems_solarflow",
        device_id: EXTERNAL,
        serial_number: null,
        display_name: "External device",
        metrics_seen: ["electricLevel", "outputHomePower"],
        topics_seen: [`ems-solarflow/${EXTERNAL}/inverterPower`, `ems-solarflow/${EXTERNAL}/batterySoc`],
      },
    ],
  };
}

async function quietNetwork(page: Page) {
  const reply = (body: unknown) => (route: import("@playwright/test").Route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/discovery/networks**", reply({ networks: [] }));
  await page.route("**/api/discovery/scan**", reply({ scan_id: "e2e-scan" }));
  await page.route("**/api/discovery/result/**", reply({ status: "complete", devices: [] }));
  await page.route("**/api/discovery/mqtt-brokers/refresh**", reply({ ok: true }));
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings**", reply({ token_saved: false, tls_mode: "system_ca" }));
}

async function searched(page: Page, seedAdminScenario: (scenario: string) => Promise<void>, seedDiscoveryState: (state: object) => Promise<void>) {
  await quietNetwork(page);
  await openModern(page);
  await seedAdminScenario("mixed_transports");
  await seedDiscoveryState({ local_api_devices: [apiInverter("E2ENEWINV01", "192.168.50.41"), E3DC_SYSTEM], local_mqtt_brokers: [externalBroker()] });
  await page.goto("/#/settings/devices");
  await page.reload();
  await page.getByTestId("discovery-start").click();
  await expect(page.getByTestId("discovery-outcome")).toContainText("The search finished");
  return page.getByTestId("discovery-items");
}

test.describe("Modern Admin: network search", { tag: ["@smoke"] }, () => {
  test("found devices are added from the search, checked and saved", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    test.setTimeout(120_000);
    const items = await searched(page, seedAdminScenario, seedDiscoveryState);
    const inverter = items.locator("li").filter({ hasText: "192.168.50.41" });
    await inverter.getByRole("button", { name: "Add inverter" }).click();
    await expect(inverter.getByRole("button", { name: "Added" })).toBeDisabled();
    await items.locator('li[data-role="read_only_device"]').getByRole("button", { name: "Add as read-only device" }).click();
    await items.locator("li").filter({ hasText: EXTERNAL }).getByRole("button", { name: "Add inverter" }).click();
    await expect(items.locator('li[data-role="read_only_device"]').getByRole("button", { name: "Added" })).toBeDisabled();
    const editor = page.getByTestId("devices-editor");
    await expect(editor).toContainText("192.168.50.41");
    await expect(editor).toContainText(`${E3DC_SYSTEM.ip}:502`);
    await expect(editor).toContainText("added from discovery");
    await expect(page.getByTestId("change-count")).toHaveText("3 unsaved changes");

    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("devices-editor")).toContainText("192.168.50.41");
    await expect(page.getByTestId("devices-editor")).toContainText(`${E3DC_SYSTEM.ip}:502`);
  });

  test("an inverter an OpenDTU reads is found, added read only with its serial, saved and found in the settings", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    test.setTimeout(120_000);
    await quietNetwork(page);
    await openModern(page);
    await seedAdminScenario("mixed_transports");
    await seedDiscoveryState({ local_api_devices: [OPENDTU_INVERTER] });
    await page.goto("/#/settings/devices");
    await page.reload();
    await page.getByTestId("discovery-start").click();
    await expect(page.getByTestId("discovery-outcome")).toContainText("The search finished");
    const found = page.getByTestId("discovery-items").locator("li").filter({ hasText: `SN ${OPENDTU_INVERTER.serial}` });
    await expect(found).toHaveAttribute("data-role", "read_only_device");
    await expect(found).toContainText("an inverter this OpenDTU reads");
    await expect(found.getByRole("button", { name: "Add inverter" })).toHaveCount(0);
    await found.getByRole("button", { name: "Add as read-only device" }).click();
    await expect(found.getByRole("button", { name: "Added" })).toBeDisabled();
    const editor = page.getByTestId("devices-editor");
    await expect(editor).toContainText(`SN ${OPENDTU_INVERTER.serial}`);
    await expect(editor).toContainText(OPENDTU_INVERTER.ip);
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");

    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("devices-editor")).toContainText(`SN ${OPENDTU_INVERTER.serial}`);
    await page.getByTestId("discovery-start").click();
    await expect(page.getByTestId("discovery-outcome")).toContainText("The search finished");
    const again = page.getByTestId("discovery-items").locator("li").filter({ hasText: `SN ${OPENDTU_INVERTER.serial}` });
    await expect(again.getByRole("button", { name: "In the settings" })).toBeDisabled();
  });

  test("a found device is ignored for this search, and clearing leaves the changes alone", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    test.setTimeout(120_000);
    const items = await searched(page, seedAdminScenario, seedDiscoveryState);
    const inverter = items.locator("li").filter({ hasText: "192.168.50.41" });
    await inverter.getByTestId("discovery-ignore").click();
    await expect(inverter.getByRole("button", { name: "Ignored" })).toBeDisabled();
    await items.locator('li[data-role="read_only_device"]').getByRole("button", { name: "Add as read-only device" }).click();
    await page.getByTestId("discovery-clear").click();
    await expect(page.getByTestId("discovery-items")).toHaveCount(0);
    await expect(page.getByTestId("discovery-outcome")).toContainText("Results cleared");
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");
  });
});

test.describe("Modern Admin: what the search may use", { tag: ["@smoke"] }, () => {
  test("a broker credential is saved and removed, and the password never comes back", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("credential-label").fill("garage broker");
    await page.getByTestId("credential-username").fill("garage-user");
    await page.getByTestId("credential-password").fill("e2e-modern-secret");
    await page.getByTestId("credential-save").click();
    const list = page.getByTestId("credentials");
    await expect(list).toContainText("garage broker");
    await expect(page.getByTestId("credential-password")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText("e2e-modern-secret");
    await list.locator("li").filter({ hasText: "garage broker" }).getByRole("button", { name: "Remove" }).click();
    await page.getByRole("dialog", { name: "Remove this credential?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("sources-message")).toContainText("Credential removed.");
    await expect(page.getByTestId("search-sources")).not.toContainText("garage broker");
  });

  test("a Tibber bridge password is saved by name, the grid meter picks it, and the password in use stays", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const bridges = page.getByTestId("bridge-passwords");
    await page.getByTestId("bridge-label").fill("basement bridge");
    await page.getByTestId("bridge-password").fill("E2E1-TIBR");
    await page.getByTestId("bridge-save").click();
    await expect(bridges).toContainText("basement bridge");
    await expect(page.getByTestId("bridge-password")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText("E2E1-TIBR");

    const meter = page.getByTestId("devices-editor").locator('[data-device="grid-meter"]');
    await meter.locator(".summary").click();
    await page.getByTestId("grid-type").selectOption("tibber_pulse_bridge");
    await meter.locator('[data-path="grid_meter.ip"] input').fill("192.168.50.70");
    await meter.locator('[data-path="grid_meter.credentials_ref"] select').selectOption({ label: "basement bridge" });
    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    await page.reload();
    await meter.locator(".summary").click();
    await expect(meter.locator('[data-path="grid_meter.credentials_ref"] select')).toHaveValue("basement-bridge");
    await bridges.locator("li").filter({ hasText: "basement bridge" }).getByRole("button", { name: "Remove" }).click();
    await page.getByRole("dialog", { name: "Remove this bridge password?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("sources-message")).toContainText("The bridge password could not be removed.");
    await expect(bridges).toContainText("basement bridge");
  });

  test("the Zendure cloud key is saved, tested, used and forgotten", async ({ page, seedAdminScenario }) => {
    let saved = false;
    const sent: string[] = [];
    const reply = (route: import("@playwright/test").Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/api/discovery/zendure-cloud-mqtt/**", async (route) => {
      const url = route.request().url();
      const method = route.request().method();
      sent.push(`${method} ${url.replace(/^.*zendure-cloud-mqtt/, "")}`);
      if (url.endsWith("/settings")) return reply(route, { token_saved: saved, tls_mode: "system_ca", last_broker: saved ? "mqtt.zen-iot.com" : "", last_status: saved ? "ok" : "" });
      if (url.endsWith("/token") && method === "POST") {
        saved = true;
        return reply(route, { ok: true });
      }
      if (url.endsWith("/token") && method === "DELETE") {
        saved = false;
        return reply(route, { ok: true });
      }
      if (url.endsWith("/test")) return reply(route, { ok: true, devices_found: 2, broker: "mqtt.zen-iot.com", tls_mode: "system_ca" });
      return reply(route, { ok: true, candidates: [{ display_name: "SolarFlow 800 Pro 2", serial_number: "CLOUDSN1" }], device_list_count: 1, mqtt_observed_count: 1 });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await expect(page.getByTestId("cloud-key-state")).toHaveText("Not saved");
    await page.getByTestId("cloud-key").fill("e2e-cloud-key");
    await page.getByTestId("cloud-save").click();
    await expect(page.getByTestId("cloud-key-state")).toHaveText("Saved");
    await page.getByTestId("cloud-test").click();
    await expect(page.getByTestId("sources-message")).toContainText("The key works: 2 devices via mqtt.zen-iot.com");
    await page.getByTestId("cloud-refresh").click();
    await expect(page.getByTestId("search-sources")).toContainText("CLOUDSN1");
    await page.getByTestId("cloud-forget").click();
    await page.getByRole("dialog", { name: "Forget the Zendure cloud key?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("cloud-key-state")).toHaveText("Not saved");
    expect(sent).toEqual(expect.arrayContaining(["POST /token", "POST /test", "POST /refresh", "DELETE /token"]));
  });

  test("an inverter over MQTT is added by hand on a typed local broker, its password kept out of the draft", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("add-mqtt-inverter").click();
    const form = page.getByTestId("mqtt-form");
    await form.locator('[data-path="broker.name"] input').fill("garage");
    await form.locator('[data-path="host"] input').fill("192.168.50.60");
    await form.locator('[data-path="username"] input').fill("garage-user");
    await page.getByTestId("mqtt-form-add").click();
    await expect(form).toContainText("Enter both a username and a password, or neither.");
    await form.locator('[data-path="broker.password"] input').fill("e2e-broker-secret");
    await page.getByTestId("mqtt-form-add").click();
    await expect(form).toHaveCount(0);
    await expect(page.getByTestId("devices-editor").locator('[data-device="mqtt-3"]')).toBeVisible();
    await expect(page.getByTestId("credentials")).toContainText("local_mqtt_garage");
    const stored = await page.evaluate(() => sessionStorage.getItem("ems-admin.settings-hardware") || "");
    expect(stored).toContain("local_mqtt_garage");
    expect(stored).not.toContain("e2e-broker-secret");
  });
});

test.describe("Modern Admin: backups", { tag: ["@smoke"] }, () => {
  test("a backup is created, inspected, restored and deleted from the console", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/backups");
    const names = page.getByTestId("backup-list").locator(".name");
    await expect(page.getByTestId("create-backup")).toBeEnabled();
    const before = new Set(await names.allTextContents());

    await page.locator('[data-scope="config"]').click();
    await page.getByTestId("create-backup").click();
    await expect(page.getByTestId("create-outcome")).toHaveText("The backup was created and verified.");
    await expect(names).toHaveCount(before.size + 1);
    const created = (await names.allTextContents()).find((name) => !before.has(name)) as string;
    expect(created).toMatch(/^ems-config-manual-/);
    const row = page.getByTestId("backup-list").locator("li").filter({ hasText: created });
    await expect(row).toContainText("Settings");

    await row.getByRole("button", { name: "Details" }).click();
    const details = page.getByRole("dialog");
    await expect(details).toContainText("config/config.json");
    await expect(details).toContainText("This backup holds passwords or keys.");
    await details.getByRole("button", { name: "Close" }).click();
    await expect(details).toBeHidden();

    const exported = page.waitForRequest((request) => request.url().endsWith("/api/admin/maintenance/backups/export"));
    const downloaded = page.waitForEvent("download");
    await row.getByRole("button", { name: `Download ${created}` }).click();
    const request = await exported;
    expect(request.method()).toBe("POST");
    expect(request.headers()["x-csrf-token"]).toBeTruthy();
    expect((await downloaded).suggestedFilename()).toBe(created);

    await row.getByTestId("restore-open").click();
    await expect(page.getByTestId("restore-summary")).toContainText("Files unchanged");
    await page.getByTestId("restore-execute").click();
    const settingsReread = page.waitForResponse((response) => response.url().endsWith("/api/admin/maintenance/config") && response.request().method() === "GET");
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("restore-outcome")).toHaveText("Restored. Restart EMS so it uses the restored files.");
    expect((await settingsReread).ok()).toBeTruthy();
    await expect(names.filter({ hasText: /^ems-config-rollback-/ })).not.toHaveCount(0);

    const count = await names.count();
    await row.getByRole("button", { name: `Delete ${created}` }).click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(names).toHaveCount(count - 1);
    await expect(page.getByTestId("backup-list").locator("li").filter({ hasText: created })).toHaveCount(0);
  });

  test("an encrypted archive asks for its password before the server previews its restore", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/backups");
    await page.locator('[data-scope="config"]').click();
    await page.getByTestId("create-backup").click();
    await expect(page.getByTestId("create-outcome")).toHaveText("The backup was created and verified.");
    await page.route("**/api/admin/maintenance/backups", async (route: Route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, backups: (body.backups || []).map((backup: Record<string, unknown>) => ({ ...backup, locked: true })) } });
    });
    const previews: string[] = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/api/admin/maintenance/backups/restore/preview")) previews.push(request.url());
    });
    await page.reload();
    await page.getByTestId("backup-list").locator('li[data-kind="archive"]').first().getByTestId("restore-open").click();
    await expect(page.locator("form.unlock")).toBeVisible();
    await expect(page.getByTestId("restore-summary")).toHaveCount(0);
    expect(previews).toHaveLength(0);
  });

  test("a restore the server's preview blocks cannot be started", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/backups");
    await page.locator('[data-scope="config"]').click();
    await page.getByTestId("create-backup").click();
    await expect(page.getByTestId("create-outcome")).toHaveText("The backup was created and verified.");
    await page.route("**/api/admin/maintenance/backups/restore/preview", async (route: Route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...(await response.json()), blocked: true, block_reason: "restore_blocked" } });
    });
    await page.getByTestId("backup-list").locator('li[data-kind="archive"]').first().getByTestId("restore-open").click();
    await expect(page.getByTestId("restore-summary")).toBeVisible();
    await expect(page.getByTestId("restore-execute")).toBeDisabled();
  });

  test("a restore whose preview was used up elsewhere is refused, and a fresh preview takes its place", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/backups");
    await page.locator('[data-scope="config"]').click();
    await page.getByTestId("create-backup").click();
    await expect(page.getByTestId("create-outcome")).toHaveText("The backup was created and verified.");

    const previewed = page.waitForResponse((response) => response.url().endsWith("/api/admin/maintenance/backups/restore/preview"));
    await page.getByTestId("backup-list").locator('li[data-kind="archive"]').first().getByTestId("restore-open").click();
    const plan = await (await previewed).json();
    await expect(page.getByTestId("restore-summary")).toBeVisible();

    const used = await post(page, "/api/admin/maintenance/backups/restore/execute", { plan_id: plan.plan_id, confirm: true });
    expect(used.status, JSON.stringify(used.body)).toBeLessThan(300);
    await expect
      .poll(async () => (await (await page.request.get(`/api/admin/maintenance/backups/jobs/${used.body.job_id}`)).json()).status)
      .not.toBe("running");

    await page.getByTestId("restore-execute").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("restore-refused")).toContainText("The restore did not start. This preview has expired.");
    await expect(page.getByTestId("restore-summary")).toBeVisible();
    await expect(page.getByTestId("restore-execute")).toBeEnabled();
    await expect(page.getByTestId("restore-outcome")).toHaveCount(0);
  });
});

// A guided workflow that can no longer finish, put in place by the gated seed
// hook the Classic recovery tests use, then resolved from Modern's System page.
async function stuckAt(page: Page, scenarios: string[]) {
  await openModern(page);
  for (const scenario of scenarios) {
    const seeded = await post(page, "/api/admin/test/seed", { scenario });
    expect(seeded.status, JSON.stringify(seeded.body)).toBe(200);
  }
  await page.goto("/#/system");
  await page.reload();
  await expect(page.locator(".app")).toBeVisible();
}

const lifecycle = async (page: Page) => (await page.request.get("/api/admin/workflow-lifecycle")).json();

test.describe("Modern Admin: an unfinished workflow", { tag: ["@smoke", "@workflow"] }, () => {
  test("is named on every page and reset from the System page after one confirmation", async ({ page }) => {
    await stuckAt(page, ["setup_cleanup_pending"]);
    const banner = page.getByTestId("workflow-banner");
    await expect(banner).toContainText("Guided setup is in progress.");
    await expect(banner.getByTestId("workflow-continue")).toHaveAttribute("href", "#/setup");
    await expect(page.getByTestId("recovery-summary")).toContainText("Guided setup is stuck: Cleanup unfinished.");

    const executed = page.waitForResponse((response) => response.url().endsWith("/api/admin/workflow-lifecycle/recovery") && response.request().method() === "POST");
    await page.getByTestId("recovery-safe").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    expect((await executed).ok()).toBeTruthy();
    await expect(page.getByText("The unfinished workflow was cancelled.")).toBeVisible();
    expect((await lifecycle(page)).switchable).toBe(true);
    await expect(banner).toBeHidden();
  });

  test("leftover records are cleared only after a second, last confirmation", async ({ page }) => {
    await stuckAt(page, ["installed_system_artifacts", "workflow_state_corrupt"]);
    await expect(page.getByTestId("recovery-summary")).toBeVisible();
    await page.getByRole("button", { name: "Force-clear leftover records" }).click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    const last = page.getByRole("dialog", { name: "This is the last confirmation" });
    await expect(last).toBeVisible();
    await last.getByTestId("confirm").click();
    await expect(page.getByText("The leftover records were backed up and removed.")).toBeVisible();
    expect((await lifecycle(page)).state).toBe("idle");
  });

  test("a state that changed after the confirmation is refused, not acted on", async ({ page }) => {
    await stuckAt(page, ["installed_system_artifacts", "setup_cleanup_pending"]);
    await page.getByTestId("recovery-safe").click();
    await expect(page.getByRole("dialog")).toBeVisible();
    const changed = await post(page, "/api/admin/test/seed", { scenario: "workflow_state_corrupt" });
    expect(changed.status, JSON.stringify(changed.body)).toBe(200);
    const executed = page.waitForResponse((response) => response.url().endsWith("/api/admin/workflow-lifecycle/recovery") && response.request().method() === "POST");
    await page.getByRole("dialog").getByTestId("confirm").click();
    expect((await executed).ok()).toBe(false);
    await expect(page.locator(".recovery .outcome.fault")).toHaveText("The workflow changed since this page read it. Look again, then decide.");
    expect((await lifecycle(page)).state).not.toBe("idle");
  });
});

test.describe("Modern Admin: on a phone", { tag: ["@smoke"] }, () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the versions are grouped stable first, and with no newer stable release nothing is proposed", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.goto("/#/updates");
    await page.reload();
    const groups = page.getByTestId("releases").locator("[data-group]");
    await expect(groups.first()).toHaveAttribute("data-group", "stable");
    await expect(page.locator('[data-group="latest"] [data-tag="latest"]')).toBeVisible();
    await expect(page.locator('[data-group="stable"] .choice.recommended')).toHaveCount(1);
    await expect(page.getByTestId("start-upgrade")).toBeVisible();

    await page.route("**/api/setup/releases?flow=upgrade", async (route: Route) => {
      const response = await route.fetch();
      const body = await response.json();
      const releases = body.releases.map((release: { tag: string; selectable: boolean; active: boolean }) => (release.tag === "v9.9.11" ? { ...release, active: true, selectable: false } : release));
      await route.fulfill({ response, json: { ...body, releases, default_release: null, latest_stable: null } });
    });
    await page.reload();
    await expect(page.getByTestId("releases")).toBeVisible();
    await expect(page.getByTestId("no-recommendation")).toContainText("You run a stable release");
    await expect(page.getByTestId("start-upgrade")).toHaveCount(0);
    await expect(page.locator(".choice.recommended")).toHaveCount(0);
  });

  test("on the rolling build nothing is proposed at the top, and latest is still chosen by hand", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.route("**/api/setup/releases?flow=upgrade", async (route: Route) => {
      const response = await route.fetch();
      const body = await response.json();
      const releases = body.releases.map((release: { active: boolean }) => ({ ...release, active: false }));
      await route.fulfill({ response, json: { ...body, releases, active_release: null, running_rolling: true, default_release: null } });
    });
    await page.goto("/#/updates");
    await page.reload();

    await expect(page.getByTestId("rolling-no-recommendation")).toContainText("Only stable releases are suggested here");
    await expect(page.getByTestId("start-upgrade")).toHaveCount(0);
    await expect(page.locator(".choice.recommended")).toHaveCount(0);
    await expect(page.locator('[data-group="latest"] [data-tag="latest"] [data-testid="choose-release"]')).toBeEnabled();
  });

  test("the channel filter shows one channel at a time, says when none of it can be installed, and is remembered", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.route("**/api/setup/releases?flow=upgrade", async (route: Route) => {
      const response = await route.fetch();
      const body = await response.json();
      const candidate = { tag: "v9.9.12-rc1", name: "v9.9.12-rc1", channel: "rc", selectable: false, upgrade_state: "downgrade_blocked" };
      await route.fulfill({ response, json: { ...body, releases: [...body.releases, candidate] } });
    });
    await page.goto("/#/updates");
    await page.reload();
    const filter = page.getByTestId("release-filter");
    const groups = page.getByTestId("releases").locator("[data-group]");
    await expect(filter.getByRole("button", { name: /^All \(\d+\)$/ })).toHaveAttribute("aria-pressed", "true");
    await expect(groups.first()).toHaveAttribute("data-group", "stable");

    await filter.getByRole("button", { name: /^Rolling/ }).click();
    await expect(groups).toHaveCount(1);
    await expect(groups.first()).toHaveAttribute("data-group", "latest");
    expect(await page.evaluate(() => localStorage.getItem("ems-admin.releaseChannel"))).toBe("latest");

    await filter.getByRole("button", { name: "Release candidate (0)" }).click();
    await expect(groups).toHaveCount(0);
    await expect(page.getByTestId("none-in-channel")).toHaveText("No version in this channel can be installed right now.");
    await page.getByTestId("show-all-releases").click();
    await expect(page.locator('[data-group="rc"] [data-tag="v9.9.12-rc1"]')).toBeVisible();
    await expect(page.getByTestId("none-in-channel")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    await page.reload();
    await expect(filter.getByRole("button", { name: "Release candidate (0)" })).toHaveAttribute("aria-pressed", "true");
    await filter.getByRole("button", { name: /^All/ }).click();
    await expect(groups.first()).toHaveAttribute("data-group", "stable");
    expect(await page.evaluate(() => localStorage.getItem("ems-admin.releaseChannel"))).toBe("all");
  });

  test("a version tapped in the list opens the guided upgrade here and verifies it", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.goto("/#/updates");
    await page.reload();
    await expect(page.getByTestId("releases")).toBeVisible();
    const validated = page.waitForResponse((response) => response.url().endsWith("/api/admin/maintenance/upgrade/validate"));
    await page.locator('[data-tag="v9.9.10"]').getByTestId("choose-release").click();
    expect((await validated).ok()).toBeTruthy();
    await expect(page.getByTestId("upgrade")).toHaveAttribute("data-stage", "ready");
    await expect(page).toHaveURL(/\/#\/updates$/);
    await expect(page.getByRole("heading", { name: "Update to v9.9.10" })).toBeInViewport();
    await expect(page.getByTestId("upgrade-migration")).toContainText("format. 1 device loses");
  });

  test("the bar holds four pages and More holds the rest", async ({ page }) => {
    await openModern(page);
    const bar = page.locator(".bottom-nav");
    await expect(bar).toBeVisible();
    await expect(bar.locator("a")).toHaveCount(4);
    await expect(bar.locator("a .slot-label")).toHaveText(["Overview", "Energy", "Status", "Updates"]);
    await page.getByTestId("nav-more").click();
    const sheet = page.getByRole("dialog", { name: "More" });
    await expect(sheet.getByRole("link", { name: "Diagnostics" })).toBeVisible();
    await sheet.getByRole("link", { name: "System" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("System");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});

test.describe("Modern Admin: settings in the old place", { tag: ["@smoke"] }, () => {
  test("are moved to config/config.json after a confirmation, and a different one there is replaced only after a second", async ({ page }) => {
    let moved = false;
    await page.route("**/api/admin/maintenance/overview", async (route: Route) => {
      const data = await route
        .fetch()
        .then((response) => response.json())
        .catch(() => null);
      if (!data) return;
      data.install_state = { ...(data.install_state || {}), state: moved ? "configured" : "legacy_root_config", legacy_migration_available: !moved, paths: { legacy_config: "/srv/ems/config.json", standard_config: "/srv/ems/config/config.json" } };
      await route.fulfill({ json: data }).catch(() => undefined);
    });
    const asked: unknown[] = [];
    await page.route("**/api/admin/config/migrate-legacy", async (route: Route) => {
      const body = route.request().postDataJSON();
      asked.push(body);
      if (!body.overwrite) return route.fulfill({ status: 409, json: { ok: false, reason: "target_exists", message: "config/config.json already exists and differs from the legacy config. Confirm the overwrite to replace it." } });
      moved = true;
      return route.fulfill({ json: { ok: true, migrated: true, backup_path: "/srv/ems/data/admin/backups/config/root-config-legacy.json", backups: ["/srv/ems/data/admin/backups/config/root-config-legacy.json", "/srv/ems/data/admin/backups/config/standard-config-before-migrate.json"] } });
    });
    await openModern(page, "#/system");
    const legacy = page.getByTestId("legacy-config");
    await expect(legacy).toContainText("/srv/ems/config.json");
    await legacy.getByTestId("legacy-move").click();
    await page.getByRole("dialog", { name: "Move the settings to config/config.json?" }).getByTestId("confirm").click();
    await expect(legacy).toContainText("The file there already exists and differs. Confirm to replace it.");
    await legacy.getByTestId("legacy-replace").click();
    await page.getByRole("dialog", { name: "Replace the existing config/config.json?" }).getByTestId("confirm").click();
    await expect(legacy).toContainText("The settings were moved to config/config.json.");
    await expect(legacy).toContainText("standard-config-before-migrate.json");
    await expect(legacy).not.toContainText("still in the old place");
    await expect(legacy.getByTestId("legacy-move")).toHaveCount(0);
    expect(asked).toEqual([{}, { overwrite: true }]);
  });
});

test.describe("Modern Admin: About & licenses", () => {
  test("the sign-in screen opens the same page without a password", async ({ page }) => {
    await page.goto("/#/status");
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
    await page.getByTestId("sign-in-about").click();
    const dialog = page.getByTestId("about-dialog");
    await expect(dialog.getByTestId("about-community")).toContainText("it is not a Zendure product");
    await expect(dialog.getByTestId("about-notice").filter({ hasText: "svelte" })).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
  });

  test("the page names the license, the community notice and every component it is built from", async ({ page }) => {
    await openModern(page);
    await page.getByTestId("nav-about").click();
    await expect(page).toHaveURL(/#\/about$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("About & licenses");
    await expect(page.getByTestId("about-license")).toContainText("WITHOUT ANY WARRANTY");
    await expect(page.getByTestId("about-community")).toContainText("it is not a Zendure product");
    await expect(page.getByTestId("about-discussions")).toHaveAttribute("href", "https://github.com/basecubedev/ems-solarflow-api-control/discussions");
    await expect(page.getByTestId("about-issues")).toHaveAttribute("href", "https://github.com/basecubedev/ems-solarflow-api-control/issues");
    await expect(page.getByTestId("about-device-report")).toHaveAttribute("href", "https://github.com/basecubedev/ems-solarflow-api-control/issues/new?template=device_compatibility_report.yml");
    await expect(page.getByTestId("about-supported-setups")).toHaveAttribute("href", "https://basecubedev.github.io/ems-solarflow-api-control/user/supported-setups.html");
    const notices = page.getByTestId("about-notices");
    for (const name of ["clsx", "svelte", "Inter", "Lucide", "Vite"]) {
      await expect(notices.getByTestId("about-notice").filter({ hasText: name })).toHaveCount(1);
    }
    await expect(page.getByText("could not be read")).toHaveCount(0);
    await notices.getByTestId("about-notice").filter({ hasText: "Vite" }).click();
    await expect(notices.locator("details[open] pre")).toContainText("VoidZero Inc. and Vite contributors");
  });
});
