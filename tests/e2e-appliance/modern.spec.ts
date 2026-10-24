// SPDX-License-Identifier: AGPL-3.0-or-later
// The Modern Appliance Manager at the root, against the deterministic test
// server. Every assertion waits on a locator or a response, never on a fixed
// timeout.
import { expect, test, type Page } from "@playwright/test";
import { PASSWORD, PUBLIC_KEY, resetAppliance } from "./helpers";

test.beforeEach(async ({ request }) => {
  await resetAppliance(request);
});

async function createPassword(page: Page) {
  await page.goto("/");
  await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "create");
  await page.locator("#appliance-password").fill(PASSWORD);
  await page.locator("#appliance-password-confirm").fill(PASSWORD);
  await Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/session/setup") && response.ok()),
    page.getByTestId("sign-in-submit").click(),
  ]);
  await expect(page.getByTestId("overview-verdict")).toBeVisible();
}

async function open(page: Page, id: string) {
  await page.locator(`.sidebar a[data-page="${id}"]`).click();
  await expect(page.locator(`.sidebar a[data-page="${id}"]`)).toHaveAttribute("aria-current", "page");
}

async function confirmPlan(page: Page) {
  await Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/operations/confirm") && response.ok()),
    page.getByTestId("plan-confirm").click(),
  ]);
  await expect(page.getByTestId("plan-dialog")).toBeHidden();
}

async function settled(page: Page) {
  await expect(page.getByTestId("operation-banner")).toHaveAttribute("data-state", "succeeded", { timeout: 20_000 });
}

test.describe("Modern at the root, the deprecated Classic at /classic/", () => {
  test("the address opens Modern, Classic is one click away and not remembered, old addresses keep working", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();

    await page.getByTestId("sign-in-classic").click();
    await expect(page).toHaveURL(/\/classic\/$/);
    await expect(page.locator("#gate")).toBeVisible();
    await expect(page.locator("#classic-notice")).toBeVisible();
    await page.goto("/");
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();

    await page.goto("/?ui=classic");
    await expect(page).toHaveURL(/\/classic\/\?ui=classic$/);
    await page.goto("/modern/");
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem("ems-appliance-ui"))).toBeNull();
  });
});

test.describe("signing in", () => {
  test("the first start creates the password, and a mismatch is refused", async ({ page }) => {
    await page.goto("/");
    await page.locator("#appliance-password").fill("one-secret");
    await page.locator("#appliance-password-confirm").fill("another-secret");
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByTestId("sign-in-error")).toContainText("do not match");
    await expect(page.locator("body")).not.toContainText("Raspberry Pi 5");
    await createPassword(page);
  });

  test("a wrong password is said in the reader's language", async ({ page, context }) => {
    await createPassword(page);
    await page.getByTestId("sign-out").click();
    await context.addInitScript(() => window.localStorage.setItem("ems-modern.lang", "de"));
    await page.reload();
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
    await page.locator("#appliance-password").fill("not-it");
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByTestId("sign-in-error")).toHaveText("Das Passwort ist nicht richtig.");
  });

  test("an expired session returns to the sign-in and says why", async ({ page, request }) => {
    await createPassword(page);
    await request.post("/api/test/reset", { data: { expire_sessions: true } });
    await page.getByTestId("refresh").click();
    await expect(page.getByTestId("sign-in-notice")).toContainText("session ended", { timeout: 15_000 });
  });

  test("a deleted password file does not offer the first-time setup again", async ({ page, request }) => {
    await resetAppliance(request, { confirmed_password: "the-previous-manager-password" });
    await page.goto("/");
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "missing");
    await expect(page.getByTestId("sign-in-lead")).toContainText("password-reset");
    await expect(page.locator("#appliance-password-confirm")).toHaveCount(0);
  });
});

test.describe("the overview", () => {
  test("leads with a verdict and the finding, which opens the page that fixes it", async ({ page }) => {
    await createPassword(page);
    await expect(page.getByTestId("overview-verdict")).toHaveText("This appliance is running and needs a little attention.");
    const finding = page.locator('[data-code="security_updates_pending"]');
    await expect(finding).toContainText("2 packages have security updates available.");
    await finding.getByTestId("finding-open-updates").click();
    await expect(page.locator('.sidebar a[data-page="updates"]')).toHaveAttribute("aria-current", "page");
    await expect(page.getByTestId("health-state")).toContainText("Attention");
  });

  test("security updates are one click from their plan, and the navigation marks Updates", async ({ page }) => {
    await createPassword(page);
    await expect(page.locator('.sidebar a[data-page="updates"]')).toHaveAttribute("aria-label", "Updates, needs attention");
    await page.getByTestId("quick-security-updates").click();
    await expect(page.getByRole("dialog")).toContainText("Install security updates");
    await expect(page.getByTestId("package-table")).toContainText("openssl");
  });

  test("a page opened from a link takes the focus, as Classic moves it to the page", async ({ page }) => {
    await createPassword(page);
    await page.locator('[data-code="security_updates_pending"]').getByTestId("finding-open-updates").focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".sidebar a[data-page=\"updates\"]")).toHaveAttribute("aria-current", "page");
    await expect(page.locator("#content")).toBeFocused();
  });

  test("another Manager answering reloads the page from the address it decides on", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    await page.route("**/api/status", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, appliance_version: "9.9.9" } });
    });
    const reload = page.waitForRequest((request) => request.isNavigationRequest() && new URL(request.url()).pathname === "/");
    await page.getByTestId("refresh").click();
    await reload;
    await expect(page).toHaveURL(/\/#\/updates$/);
    await expect(page.getByTestId("manager-section")).toBeVisible();
  });

  test("a page that waited at the sign-in reloads when another Manager answers the sign-in", async ({ page }) => {
    await page.route("**/api/session", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, appliance_version: "0.1.0" } });
    });
    await createPassword(page);
    await page.getByTestId("sign-out").click();
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
    await page.route("**/api/session/login", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, appliance_version: "9.9.9" } });
    });
    const reload = page.waitForRequest((request) => request.isNavigationRequest() && new URL(request.url()).pathname === "/");
    await page.locator("#appliance-password").fill(PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await reload;
    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
  });

  test("the updates card does not call a failed update check up to date", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const updates = { ...body.updates, security_count: 0, normal_count: 0, error: "update_check_failed" };
      await route.fulfill({ response, json: { ...body, updates } });
    });
    await createPassword(page);
    const state = page.getByTestId("overview-updates-state");
    await expect(state).toHaveText("The package counts are not an answer");
    await expect(page.locator(".card", { has: page.getByTestId("card-updates") })).not.toContainText("Up to date");
  });

  test("the link to EMS Admin keeps its place and the focus while the settings are read again", async ({ page }) => {
    await createPassword(page);
    const link = page.getByTestId("open-admin");
    await expect(link).toBeVisible();
    await link.focus();
    const settings = page.waitForResponse((response) => response.url().includes("/api/settings"));
    await page.getByTestId("refresh").dispatchEvent("click");
    await settings;
    await expect(link).toBeFocused();
  });

  test("a read of the held listings again leaves the controls on screen and the focus in them", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true });
    await createPassword(page);
    await open(page, "diagnostics");
    const source = page.getByTestId("log-source");
    await source.focus();
    const settings = page.waitForResponse((response) => response.url().includes("/api/settings"));
    await page.getByTestId("refresh").dispatchEvent("click");
    await settings;
    await expect(source).toBeFocused();

    await open(page, "updates");
    const version = page.getByTestId("manager-version");
    await version.focus();
    const sources = page.waitForResponse((response) => response.url().includes("/api/manager/sources"));
    await page.getByTestId("refresh").dispatchEvent("click");
    await sources;
    await expect(version).toBeFocused();
  });

  test("what a plan keeps is the appliance's English, marked as such", async ({ page, context }) => {
    await context.addInitScript(() => window.localStorage.setItem("ems-modern.lang", "de"));
    await createPassword(page);
    await open(page, "updates");
    await page.getByTestId("update-admin-latest").click();
    const kept = page.getByTestId("plan-dialog").locator(".note", { hasText: "Bleibt erhalten:" });
    await expect(kept.locator('[lang="en"]')).toContainText("EMS");
  });

  test("after an install settles on Updates, the releases wait for the new status and are read once", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    const reads: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/admin/releases")) reads.push(request.url());
    });
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/status", async (route) => {
      await held;
      await route.continue();
    });
    await page.getByTestId("update-admin-latest").click();
    await confirmPlan(page);
    await settled(page);
    const before = reads.length;
    await page.waitForRequest((request) => request.url().includes("/api/operations"));
    expect(reads.length).toBe(before);
    const answered = page.waitForResponse((response) => response.url().includes("/api/admin/releases"));
    release();
    await answered;
    await expect(page.getByTestId("update-summary-admin-state")).toHaveText("Up to date");
    await page.waitForRequest((request) => request.url().includes("/api/operations"));
    expect(reads.length).toBe(before + 1);
  });

  test("the link to EMS Admin goes when Admin is gone, even while the settings cannot be read", async ({ page }) => {
    await createPassword(page);
    await expect(page.getByTestId("open-admin")).toBeVisible();
    await page.route("**/api/settings", (route) => route.fulfill({ status: 503, json: { error: "agent_unavailable", message: "x" } }));
    await page.route("**/api/status", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, admin: { ...body.admin, installed: false } } });
    });
    await page.getByTestId("refresh").click();
    await expect(page.getByTestId("open-admin")).toHaveCount(0);
  });

  test("a settle the owner's Refresh sees still holds the releases until the new status", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    const reads: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/admin/releases")) reads.push(request.url());
    });
    let releaseOperations: () => void = () => {};
    const operationsHeld = new Promise<void>((resolve) => (releaseOperations = resolve));
    await page.route("**/api/operations", async (route) => {
      await operationsHeld;
      await route.continue();
    });
    await page.getByTestId("update-admin-latest").click();
    await confirmPlan(page);
    await expect.poll(async () => (await (await page.request.get("/api/operations")).json()).active, { timeout: 20_000 }).toBeNull();
    const refreshed = page.waitForResponse((response) => response.url().endsWith("/api/status"));
    await page.getByTestId("refresh").click();
    await refreshed;
    let releaseStatus: () => void = () => {};
    const statusHeld = new Promise<void>((resolve) => (releaseStatus = resolve));
    await page.route("**/api/status", async (route) => {
      await statusHeld;
      await route.continue();
    });
    const before = reads.length;
    releaseOperations();
    await expect(page.getByTestId("refresh")).toBeEnabled();
    await page.waitForRequest((request) => request.url().includes("/api/operations"));
    expect(reads.length).toBe(before);
    const answered = page.waitForResponse((response) => response.url().includes("/api/admin/releases"));
    releaseStatus();
    await answered;
    await expect(page.getByTestId("update-summary-admin-state")).toHaveText("Up to date");
  });

  test("a new session does not offer the last session's link to EMS Admin", async ({ page }) => {
    await createPassword(page);
    await expect(page.getByTestId("open-admin")).toBeVisible();
    await page.getByTestId("sign-out").click();
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
    await page.route("**/api/settings", (route) => route.fulfill({ status: 503, json: { error: "agent_unavailable", message: "x" } }));
    await page.locator("#appliance-password").fill(PASSWORD);
    const settings = page.waitForResponse((response) => response.url().includes("/api/settings"));
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByTestId("overview-verdict")).toBeVisible();
    await settings;
    await expect(page.getByTestId("open-admin")).toHaveCount(0);
  });

  test("shows the host, EMS, EMS Admin, updates, Docker and network", async ({ page }) => {
    await createPassword(page);
    await expect(page.getByTestId("card-host")).toContainText("Raspberry Pi OS");
    await expect(page.getByTestId("card-admin")).toContainText("v1.0.0");
    await expect(page.getByTestId("card-updates")).toContainText("Security updates");
    await expect(page.getByTestId("card-network")).toContainText("ems-solarflow.local");
  });
});

test.describe("plan, confirm, follow, acknowledge", () => {
  test("a restart shows its plan, runs, and stays until it is acknowledged", async ({ page }) => {
    await createPassword(page);
    await page.getByTestId("quick-restart-admin").click();
    const dialog = page.getByTestId("plan-dialog");
    await expect(dialog).toContainText("Container state now");
    await expect(page.getByRole("dialog")).toContainText("Restart the EMS Admin container");
    await confirmPlan(page);
    await settled(page);
    await expect(page.getByTestId("operation-banner")).toContainText("EMS Admin container");
    await expect(page.getByTestId("operation-outcome")).toHaveText("Completed");
    await page.getByTestId("acknowledge-operation").click();
    await expect(page.getByTestId("operation-banner")).toHaveCount(0);
  });

  test("closing a plan cancels it and leaves nothing behind", async ({ page }) => {
    await createPassword(page);
    await page.getByTestId("quick-repair-admin").click();
    await expect(page.getByTestId("plan-dialog")).toBeVisible();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/acknowledge")),
      page.keyboard.press("Escape"),
    ]);
    await expect(page.getByTestId("plan-dialog")).toBeHidden();
    await expect(page.getByTestId("operation-banner")).toHaveCount(0);
  });

  test("a plan whose answer never arrived opens once the appliance has made it", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    await page.route("**/api/updates/plan", async (route) => {
      await route.fetch();
      await route.abort("timedout");
    });
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/resume-plan") && response.ok()),
      page.getByTestId("updates-install-security").click(),
    ]);
    await expect(page.getByTestId("package-table")).toContainText("openssl");
    await expect(page.getByTestId("action-notice")).toHaveCount(0);
    await confirmPlan(page);
    await settled(page);
  });

  test("a plan started with another request id is left to the banner, which can cancel it", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    await page.route("**/api/updates/plan", async (route) => {
      const body = route.request().postDataJSON();
      await route.fetch({ postData: JSON.stringify({ ...body, plan_request: "fedcba9876543210fedcba9876543210" }) });
      await route.abort("timedout");
    });
    const refused = page.waitForResponse((response) => response.url().includes("/api/operations/resume-plan"));
    await page.getByTestId("updates-install-security").click();
    expect((await refused).status()).toBe(403);
    await expect(page.getByTestId("plan-dialog")).toBeHidden();
    await expect(page.getByTestId("action-notice")).toContainText("did not ask for");
    await page.getByTestId("cancel-operation").click();
    await expect(page.getByTestId("operation-banner")).toHaveAttribute("data-state", "cancelled");
    await page.getByTestId("acknowledge-operation").click();
    await expect(page.getByTestId("operation-banner")).toHaveCount(0);
  });

  test("shutting down says to switch the Raspberry Pi back on", async ({ page }) => {
    await createPassword(page);
    await page.getByTestId("quick-shutdown").click();
    await expect(page.getByTestId("plan-confirm")).toHaveText("Shut down");
    await confirmPlan(page);
    await expect(page.getByTestId("reconnect-detail")).toContainText("switch the Raspberry Pi back on");
  });

  test("after a restart the sign-in screen asks the appliance nothing", async ({ page, context }) => {
    await page.clock.install();
    await createPassword(page);
    await page.getByTestId("quick-reboot").click();
    await confirmPlan(page);
    await expect(page.getByTestId("reconnect-screen")).toBeVisible();
    await context.clearCookies();
    await page.getByTestId("reconnect-check").click();
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
    const polled: string[] = [];
    page.on("request", (request) => {
      if (/\/api\/(operations|status|manager)$/.test(new URL(request.url()).pathname)) polled.push(request.url());
    });
    await page.clock.runFor(12_000);
    expect(polled).toEqual([]);
  });

  test("a restart of the Raspberry Pi waits for it to come back", async ({ page }) => {
    await createPassword(page);
    await page.getByTestId("quick-reboot").click();
    await expect(page.getByTestId("plan-confirm")).toHaveText("Restart");
    await confirmPlan(page);
    await expect(page.getByTestId("reconnect-detail")).toContainText("restarting");
    await page.getByTestId("reconnect-check").click();
    await expect(page.getByTestId("sign-in-screen").or(page.getByTestId("overview-verdict"))).toBeVisible();
  });
});

test.describe("updates", () => {
  test("the latest stable versions are one click from their plans", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true, manager_package_kept: true });
    await createPassword(page);
    await open(page, "updates");
    await expect(page.getByTestId("update-summary-admin-state")).toHaveText("Update available");
    await page.getByTestId("update-admin-latest").click();
    await expect(page.getByRole("dialog")).toContainText("Install EMS Admin v1.1.0");
    await page.getByTestId("plan-cancel").click();
    await expect(page.getByTestId("plan-dialog")).toBeHidden();

    const latest = page.waitForRequest((request) => request.url().includes("/api/manager/plan-update"));
    await page.getByTestId("update-manager-latest").click();
    expect((await latest).postDataJSON()).toMatchObject({ release_id: "ems-appliance-manager-0.2.0-arm64" });
    await page.getByTestId("action-notice").getByRole("button").click();

    await expect(page.getByTestId("manager-version")).toHaveValue("ems-appliance-manager-0.2.0-arm64");
    await expect(page.getByTestId("manager-version").locator("optgroup")).toHaveCount(3);
    await page.getByTestId("manager-version").selectOption("ems-appliance-manager-0.1.0-arm64");
    const plan = page.waitForRequest((request) => request.url().includes("/api/manager/plan-update"));
    await page.getByTestId("manager-plan-update").click();
    expect((await plan).postDataJSON()).toMatchObject({ release_id: "ems-appliance-manager-0.1.0-arm64" });
    // The test server's index points at example.invalid, so the agent refuses
    // the download; the refusal is the appliance's own sentence.
    await expect(page.getByTestId("action-notice")).toContainText("manifest is unreachable");
  });

  test("only the button that asked for a Manager plan says it is planning", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true });
    await createPassword(page);
    await open(page, "updates");
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/manager/plan-update", async (route) => {
      await held;
      await route.continue();
    });
    await page.getByTestId("update-manager-latest").click();
    await expect(page.getByTestId("update-manager-latest")).toHaveAttribute("aria-busy", "true");
    await expect(page.getByTestId("manager-plan-update")).not.toHaveAttribute("aria-busy", "true");
    const answer = page.waitForResponse((response) => response.url().includes("/api/manager/plan-update"));
    release();
    await answer;
  });

  test("Expert can type an exact tag, also on a fresh card, and ask for a reinstall", async ({ page, request }) => {
    await createPassword(page);
    await page.locator(".topbar .seg button", { hasText: "Expert" }).click();
    await open(page, "updates");
    await page.getByTestId("install-channel").selectOption("exact");
    await page.getByTestId("install-tag").fill("v1.1.0");
    await page.getByTestId("install-reinstall").check();
    const plan = page.waitForRequest((sent) => sent.url().includes("/api/admin/plan-install"));
    await page.getByTestId("install-plan").click();
    expect((await plan).postDataJSON()).toMatchObject({ channel: "exact", tag: "v1.1.0", reinstall: true });
    await page.getByTestId("plan-cancel").click();

    await resetAppliance(request, { never_installed: true });
    await createPassword(page);
    await open(page, "admin");
    await page.getByTestId("install-channel").selectOption("exact");
    await page.getByTestId("install-tag").fill("v1.0.0");
    const bootstrap = page.waitForRequest((sent) => sent.url().includes("/api/admin/plan-install"));
    await page.getByTestId("admin-bootstrap-plan").click();
    expect((await bootstrap).postDataJSON()).toMatchObject({ channel: "exact", tag: "v1.0.0", reinstall: false });
  });

  test("a version picked from the list is planned as that exact tag", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    await page.getByTestId("install-channel").selectOption("tag:v1.2.0-rc1");
    const plan = page.waitForRequest((request) => request.url().includes("/api/admin/plan-install"));
    await page.getByTestId("install-plan").click();
    expect((await plan).postDataJSON()).toMatchObject({ channel: "exact", tag: "v1.2.0-rc1", reinstall: false });
  });

  test("the kept Manager package is offered as the way back", async ({ page, request }) => {
    await resetAppliance(request, { manager_package_kept: true });
    await createPassword(page);
    await open(page, "updates");
    await expect(page.getByTestId("manager-kept")).toContainText("0.1.0");
    await page.getByTestId("manager-plan-revert").click();
    await expect(page.getByRole("dialog")).toContainText("Go back to Appliance Manager 0.1.0");
    await expect(page.getByTestId("plan-confirm")).toHaveClass(/danger/);
  });

  test("a plan field this build has no name for is labelled in the appliance's English", async ({ page }) => {
    await createPassword(page);
    await page.locator(".topbar .seg button", { hasText: "Expert" }).click();
    await open(page, "updates");
    await page.getByTestId("updates-install-security").click();
    const row = page.getByTestId("plan-dialog").locator(".kv > div", { hasText: "free space known" });
    await expect(row.locator("dt")).toHaveAttribute("lang", "en");
    await expect(row.locator("dd")).not.toHaveAttribute("lang", "en");
  });

  test("a repair of the package manager is planned in Expert", async ({ page }) => {
    await createPassword(page);
    await page.locator(".topbar .seg button", { hasText: "Expert" }).click();
    await open(page, "updates");
    await page.getByTestId("updates-repair-refresh_index").click();
    await expect(page.getByRole("dialog")).toContainText("Refresh package indexes");
    await expect(page.getByTestId("plan-confirm")).toHaveText("Run repair");
  });

  test("a deadline in flight blocks installing and going back", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true, manager_package_kept: true, manager_deadline_armed: true });
    await createPassword(page);
    await open(page, "updates");
    await expect(page.getByTestId("manager-deadline")).toBeVisible();
    await expect(page.getByTestId("manager-plan-update")).toBeDisabled();
    await expect(page.getByTestId("manager-plan-revert")).toBeDisabled();
  });

  test("Expert adds every update and the package-manager repairs, and is remembered", async ({ page }) => {
    await createPassword(page);
    await open(page, "updates");
    await expect(page.getByTestId("updates-install-all")).toHaveCount(0);
    await page.locator(".topbar .seg button", { hasText: "Expert" }).click();
    await expect(page.getByTestId("updates-install-all")).toBeVisible();
    await expect(page.getByTestId("updates-repair-fix_broken")).toBeVisible();
    await page.reload();
    await page.getByTestId("updates-install-all").click();
    await expect(page.getByRole("dialog")).toContainText("Install all OS updates");
    await expect(page.getByTestId("plan-dialog")).toContainText("Every available update");
  });
});

test.describe("EMS Admin", () => {
  test("the container is stopped and started through its plans", async ({ page }) => {
    await createPassword(page);
    await open(page, "admin");
    await page.getByTestId("admin-stop").click();
    await expect(page.getByRole("dialog")).toContainText("Stop the EMS Admin container");
    await confirmPlan(page);
    await settled(page);
    await page.getByTestId("acknowledge-operation").click();
    await page.getByTestId("admin-start").click();
    await confirmPlan(page);
    await settled(page);
  });

  test("an appliance that never had EMS Admin offers to install it", async ({ page, request }) => {
    await resetAppliance(request, { never_installed: true });
    await createPassword(page);
    await expect(page.getByTestId("quick-install-admin")).toBeVisible();
    await open(page, "admin");
    await expect(page.getByTestId("admin-restart")).toHaveCount(0);
    const plan = page.waitForRequest((request) => request.url().includes("/api/admin/plan-install"));
    await page.getByTestId("admin-bootstrap-plan").click();
    expect((await plan).postDataJSON()).toMatchObject({ channel: "latest_stable", reinstall: false });
  });

  test("rollback names the version it goes back to", async ({ page }) => {
    await createPassword(page);
    await open(page, "admin");
    await page.getByTestId("admin-rollback").click();
    await expect(page.getByRole("dialog")).toContainText("Roll back to the previous known-good EMS Admin");
    await expect(page.getByTestId("plan-confirm")).toHaveText("Roll back");
  });
});

test.describe("access, network and diagnostics", () => {
  test("a public key shows its fingerprint first and lands in the table", async ({ page }) => {
    await createPassword(page);
    await open(page, "access");
    await page.getByTestId("key-value").fill(PUBLIC_KEY);
    await page.getByTestId("key-add").click();
    await expect(page.getByTestId("plan-key")).toContainText("SHA256:");
    await confirmPlan(page);
    await settled(page);
    await expect(page.getByTestId("key-value")).toHaveValue("");
    await expect(page.getByTestId("ssh-key-table")).toContainText("appliance-test@example.invalid", { timeout: 15_000 });
  });

  test("a key that is in the file twice is listed twice and can be removed", async ({ page, request }) => {
    await resetAppliance(request, { authorized_keys: [PUBLIC_KEY, PUBLIC_KEY.replace("appliance-test@example.invalid", "second-copy")] });
    await createPassword(page);
    await open(page, "access");
    await expect(page.getByTestId("ssh-key-table").locator("tbody tr")).toHaveCount(2);
    await page.getByTestId("ssh-remove-key").first().click();
    await expect(page.getByTestId("plan-dialog")).toBeVisible();
  });

  test("a private key is refused with the appliance's reason", async ({ page }) => {
    await createPassword(page);
    await open(page, "access");
    await page.getByTestId("key-value").fill("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----");
    await page.getByTestId("key-add").click();
    await expect(page.getByTestId("action-notice")).toContainText("private key");
    await expect(page.getByTestId("plan-dialog")).toBeHidden();
  });

  test("a hostname change is planned with the new name", async ({ page }) => {
    await createPassword(page);
    await open(page, "network");
    await page.getByTestId("hostname-input").fill("ems-garage");
    await page.getByTestId("hostname-plan").click();
    await expect(page.getByTestId("plan-dialog")).toContainText("ems-garage");
  });

  test("a WLAN change warns about the disconnect and keeps no passphrase", async ({ page }) => {
    await createPassword(page);
    await open(page, "network");
    await page.getByTestId("wifi-ssid").fill("GuestNet");
    await page.getByTestId("wifi-pass").fill("correct-horse-battery");
    const plan = page.waitForRequest((request) => request.url().includes("/api/network/wifi/plan"));
    await page.getByTestId("wifi-plan").click();
    expect((await plan).postDataJSON()).toMatchObject({ ssid: "GuestNet", passphrase: "correct-horse-battery", hidden: false });
    await expect(page.getByTestId("plan-dialog")).toContainText("previous profile is kept");
    await page.getByTestId("plan-cancel").click();
    await expect(page.getByTestId("wifi-pass")).toHaveValue("");
    await expect(page.getByTestId("wifi-ssid")).toHaveValue("GuestNet");
  });

  test("a WLAN scan lists the networks and fills in the chosen one", async ({ page }) => {
    await createPassword(page);
    await open(page, "network");
    await page.getByTestId("wifi-scan").click();
    await expect(page.getByTestId("wifi-select").locator("option", { hasText: "HomeNet" })).toContainText("connected");
    await page.getByTestId("wifi-select").selectOption("GuestNet");
    await expect(page.getByTestId("wifi-ssid")).toHaveValue("GuestNet");
  });

  test("a scan whose answer comes after a refresh still ends", async ({ page }) => {
    await createPassword(page);
    await open(page, "network");
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/network/wifi/scan", async (route) => {
      await held;
      await route.continue();
    });
    await page.getByTestId("wifi-scan").click();
    await expect(page.getByTestId("wifi-scan")).toBeDisabled();
    await page.getByTestId("refresh").click();
    release();
    await expect(page.getByTestId("wifi-scan")).toBeEnabled();
    await expect(page.getByTestId("wifi-select")).toBeVisible();
  });

  test("a time zone change is planned with the typed zone", async ({ page }) => {
    await createPassword(page);
    await open(page, "network");
    await expect(page.getByTestId("timezone-input")).toHaveValue("UTC");
    await page.getByTestId("timezone-input").fill("Europe/Berlin");
    const plan = page.waitForRequest((request) => request.url().includes("/api/system/timezone"));
    await page.getByTestId("timezone-plan").click();
    expect((await plan).postDataJSON()).toMatchObject({ timezone: "Europe/Berlin" });
    await expect(page.getByTestId("plan-dialog")).toBeVisible();
  });

  test("the root-capable shell account is switched on only through a warned plan", async ({ page }) => {
    await createPassword(page);
    await open(page, "access");
    await expect(page.getByTestId("shell-access-disable")).toBeDisabled();
    await page.getByTestId("shell-access-enable").click();
    await expect(page.getByRole("dialog")).toContainText("Enable the root-capable shell account");
    await expect(page.getByTestId("plan-confirm")).toHaveClass(/danger/);
  });

  test("SSH is switched on and off through its plans", async ({ page }) => {
    await createPassword(page);
    await open(page, "access");
    await page.getByTestId("ssh-enable").click();
    await expect(page.getByRole("dialog")).toContainText("Switch the SSH service on");
    await confirmPlan(page);
    await settled(page);
    await page.getByTestId("acknowledge-operation").click();
    await page.getByTestId("ssh-disable").click();
    await expect(page.getByRole("dialog")).toContainText("Switch the SSH service off");
    await expect(page.getByTestId("plan-confirm")).toHaveClass(/danger/);
  });

  test("a support archive can be downloaded once it is made", async ({ page }) => {
    await createPassword(page);
    await open(page, "diagnostics");
    await page.getByTestId("support-archive").click();
    await confirmPlan(page);
    await settled(page);
    const download = page.waitForEvent("download");
    await page.getByTestId("download-support-archive").click();
    expect((await download).suggestedFilename()).toMatch(/\.tar\.gz$/);
  });

  test("a slow earlier log answer does not replace the log asked for last", async ({ page }) => {
    await createPassword(page);
    await page.locator(".topbar .seg button", { hasText: "Expert" }).click();
    await open(page, "diagnostics");
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/logs/admin_container*", async (route) => {
      await held;
      await route.continue();
    });
    await page.getByTestId("log-source").selectOption("admin_container");
    await page.getByTestId("log-load").click();
    await page.getByTestId("log-source").selectOption("appliance_web");
    await page.getByTestId("log-load").click();
    await expect(page.getByTestId("log-output")).toContainText("journal line one");
    const late = page.waitForResponse((response) => response.url().includes("/api/logs/admin_container"));
    release();
    await late;
    await expect(page.getByTestId("log-output")).toContainText("journal line one");
    await expect(page.getByTestId("log-load")).toBeEnabled();
  });

  test("a log that could not be read is said on its own panel only", async ({ page }) => {
    await createPassword(page);
    await open(page, "admin");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/logs/admin_container")),
      page.getByTestId("log-load").click(),
    ]);
    await page.route("**/api/logs/operations*", (route) => route.fulfill({ status: 503, json: { error: "agent_unavailable", message: "x" } }));
    await open(page, "diagnostics");
    await page.getByTestId("log-source").selectOption("operations");
    await page.getByTestId("log-load").click();
    await expect(page.getByTestId("log-panel")).toContainText("agent is not reachable");
    await open(page, "admin");
    await expect(page.getByTestId("log-panel")).not.toContainText("agent is not reachable");
  });

  test("a log is read on request, and one that cannot be read says so", async ({ page }) => {
    await createPassword(page);
    await open(page, "diagnostics");
    await page.getByTestId("log-source").selectOption("audit");
    const [response] = await Promise.all([
      page.waitForResponse((answer) => answer.url().includes("/api/logs/audit")),
      page.getByTestId("log-load").click(),
    ]);
    expect(response.ok()).toBeTruthy();
    await expect(page.getByTestId("log-output")).toContainText("password.change");
    await page.route("**/api/logs/operations*", (route) =>
      route.fulfill({ json: { text: "", lines: 0, truncated: false, source: "operations", unreadable: "PermissionError" } }),
    );
    await page.getByTestId("log-source").selectOption("operations");
    await page.getByTestId("log-load").click();
    await expect(page.getByTestId("log-panel")).toContainText("This log could not be read (PermissionError).");
    await expect(page.getByTestId("log-output")).toHaveCount(0);
  });
});

test.describe("settings", () => {
  test("a password set elsewhere holds SSH changes back until it is confirmed", async ({ page, request }) => {
    const previous = "the-previous-manager-password";
    await resetAppliance(request, { confirmed_password: previous, shared_password: PASSWORD });
    await page.goto("/");
    await page.locator("#appliance-password").fill(PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await expect(page.locator('[data-code="password_unconfirmed"]')).toBeVisible();

    await open(page, "settings");
    await page.getByTestId("pw-previous").fill("not-the-previous-one");
    await page.getByTestId("pw-previous-submit").click();
    await expect(page.getByTestId("pw-previous-message")).toContainText("knew before");
    await page.getByTestId("pw-previous").fill(previous);
    await page.getByTestId("pw-previous-submit").click();
    await expect(page.getByTestId("settings-password-confirm")).toHaveCount(0);
    await open(page, "overview");
    await expect(page.locator('[data-code="password_unconfirmed"]')).toHaveCount(0);
  });

  test("changing the password signs out and the new one signs in", async ({ page }) => {
    await createPassword(page);
    await open(page, "settings");
    await page.getByTestId("pw-current").fill(PASSWORD);
    await page.getByTestId("pw-new").fill("a-new-password-2");
    await page.getByTestId("pw-confirm").fill("a-new-password-2");
    await page.getByTestId("pw-submit").click();
    await expect(page.getByTestId("sign-in-notice")).toContainText("password was changed");
    await page.locator("#appliance-password").fill("a-new-password-2");
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByTestId("settings-password")).toBeVisible();
  });
});

test.describe("what an outcome says", () => {
  test("a restart that fails verification names the fact that failed", async ({ page, request }) => {
    await resetAppliance(request, { admin_unreachable: true });
    await createPassword(page);
    await page.getByTestId("quick-restart-admin").click();
    await confirmPlan(page);
    await expect(page.getByTestId("verification-reasons")).toContainText("the EMS Admin web interface did not answer", { timeout: 20_000 });
    await expect(page.getByTestId("operation-banner")).not.toHaveAttribute("data-state", "succeeded");
  });

  test("an update refused before the write says nothing was written", async ({ page, request }) => {
    await resetAppliance(request, { deployment_drift: true });
    await createPassword(page);
    await expect(page.getByTestId("replan-required")).toContainText("Nothing was written", { timeout: 20_000 });
    await expect(page.getByTestId("operation-outcome")).toHaveText("Incomplete");
  });

  test("a rollback that fails preflight says the running EMS Admin was left alone", async ({ page, request }) => {
    await resetAppliance(request, { rollback_image_missing: true });
    await createPassword(page);
    await open(page, "admin");
    await page.getByTestId("admin-rollback").click();
    await confirmPlan(page);
    await expect(page.getByTestId("admin-untouched")).toContainText("still running", { timeout: 20_000 });
  });

  test("an operation state this build has no word for keeps the appliance's English, marked as such", async ({ page, context }) => {
    await context.addInitScript(() => window.localStorage.setItem("ems-modern.lang", "de"));
    await page.route("**/api/operations", (route) =>
      route.fulfill({ json: { active: { operation_id: "op-unknown", type: "admin.reticulate", state: "paused_for_input", settled: false }, unacknowledged: [] } }),
    );
    await createPassword(page);
    const outcome = page.getByTestId("operation-outcome").locator(".status");
    await expect(outcome).toHaveText("paused for input");
    await expect(outcome).toHaveAttribute("lang", "en");
    await expect(page.getByTestId("operation-banner").locator("[role=status] span").last()).toHaveAttribute("lang", "en");
    await expect(page.locator("#operation-title")).toHaveText("admin reticulate");
    await expect(page.locator("#operation-title")).toHaveAttribute("lang", "en");
  });

  test("a host without Docker shows it as not installed and offers no EMS Admin version", async ({ page, request }) => {
    await resetAppliance(request, { docker_missing: true });
    await createPassword(page);
    await expect(page.getByTestId("card-docker")).toContainText("Docker is not installed, so containers cannot be managed here.");
    await open(page, "updates");
    await expect(page.getByTestId("admin-versions-unavailable")).toBeVisible();
    await expect(page.getByTestId("install-plan")).toHaveCount(0);
  });

  test("a reverted Manager install and an expired deadline are said, not left silent", async ({ page, request }) => {
    await resetAppliance(request, { manager_package_kept: true, manager_verdict: "reverted" });
    await createPassword(page);
    await open(page, "updates");
    await expect(page.getByTestId("manager-verdict")).toContainText("previous package was put back");
    await resetAppliance(request, { manager_package_kept: true, manager_deadline_expired: true });
    await createPassword(page);
    await open(page, "updates");
    await expect(page.getByTestId("manager-deadline-expired")).toBeVisible();
    await expect(page.getByTestId("manager-plan-revert")).toBeEnabled();
  });

  test("an export mounted read-write is never shown as read-only", async ({ page, request }) => {
    await resetAppliance(request, { export_read_write: true });
    await createPassword(page);
    await open(page, "access");
    await expect(page.getByTestId("backup-export")).toContainText("Not confined");
    await expect(page.getByTestId("backup-paths")).toContainText("Exported read-write");
    await expect(page.getByTestId("backup-paths")).not.toContainText("Read-only export");
  });
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the bar holds four pages and More reaches the rest, handing focus to the page", async ({ page }) => {
    await createPassword(page);
    const bar = page.locator(".bottom-nav");
    await expect(bar.locator("a")).toHaveCount(4);
    await page.getByTestId("nav-more").click();
    await page.locator('.sheet a[data-page="settings"]').click();
    await expect(page.getByTestId("settings-password")).toBeVisible();
    await expect(page.locator("#content")).toBeFocused();
  });

  test("a page opened through More keeps the chevron free of its attention mark", async ({ page, request }) => {
    await resetAppliance(request, { confirmed_password: "the-previous-manager-password", shared_password: PASSWORD });
    await page.goto("/");
    await page.locator("#appliance-password").fill(PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await page.getByTestId("nav-more").click();
    await page.locator('.sheet a[data-page="settings"]').click();
    const more = page.getByTestId("nav-more");
    const mark = await more.getByTestId("nav-more-mark").boundingBox();
    const chevron = await more.locator("svg.slot-badge").boundingBox();
    expect(mark && chevron).toBeTruthy();
    const apart = mark!.x + mark!.width <= chevron!.x || chevron!.x + chevron!.width <= mark!.x || mark!.y + mark!.height <= chevron!.y || chevron!.y + chevron!.height <= mark!.y;
    expect(apart).toBe(true);
  });
});
