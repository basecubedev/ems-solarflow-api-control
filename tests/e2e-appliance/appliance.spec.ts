// SPDX-License-Identifier: AGPL-3.0-or-later
// Appliance Manager UI journeys against the deterministic test server.
// Every assertion waits on a locator or a response, never on a fixed timeout.
import { expect, test } from "@playwright/test";
import { PASSWORD, PUBLIC_KEY, openView, resetAppliance, setMode, signIn } from "./helpers";

// Playwright gives every test its own browser context, so localStorage and
// cookies start empty; only the shared server state needs resetting.
test.beforeEach(async ({ request }) => {
  await resetAppliance(request);
});

test.describe("authentication @smoke", () => {
  test("first start requires a new appliance password", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("#gate")).toBeVisible();
    await expect(page.locator("#gate-intro")).toContainText("No appliance password exists yet");
    await expect(page.locator("#gate-confirm-field")).toBeVisible();
    await expect(page.locator("#gate-submit")).toHaveText("Create password");
    // Nothing about the host is visible before authentication.
    await expect(page.locator("#shell")).toBeHidden();
    await expect(page.locator("body")).not.toContainText("Raspberry Pi 5");
  });

  test("a short password is refused with a visible reason", async ({ page }) => {
    await page.goto("/");
    await page.locator("#gate-password").fill("short");
    await page.locator("#gate-confirm").fill("short");
    await page.locator("#gate-submit").click();
    await expect(page.locator("#gate-error")).toBeVisible();
    await expect(page.locator("#gate-error")).toContainText("at least");
  });

  test("sign in, reload and sign out", async ({ page }) => {
    await signIn(page);
    await page.reload();
    await expect(page.locator("#shell")).toBeVisible();
    await page.locator("#logout-button").click();
    await expect(page.locator("#gate")).toBeVisible();
    await expect(page.locator("#gate-submit")).toHaveText("Sign in");
  });

  test("an expired session returns to the login page", async ({ page, request }) => {
    await signIn(page);
    await request.post("/api/test/reset", { data: { expire_sessions: true } });
    await page.locator("#refresh-button").click();
    await expect(page.locator("#gate")).toBeVisible({ timeout: 15_000 });
  });
});

test.describe("overview @smoke", () => {
  test("shows host, Docker, Admin, EMS, updates and network", async ({ page }) => {
    await signIn(page);
    await expect(page.locator('[data-test="card-host"]')).toContainText("Raspberry Pi 5");
    await expect(page.locator('[data-test="card-docker"]')).toContainText("running");
    await expect(page.locator('[data-test="card-admin"]')).toContainText("v1.0.0");
    await expect(page.locator('[data-test="card-ems"]')).toBeVisible();
    await expect(page.locator('[data-test="card-updates"]')).toContainText("Security updates");
    await expect(page.locator('[data-test="card-network"]')).toContainText("ems-solarflow.local");
  });

  test("status is not communicated by colour alone", async ({ page }) => {
    await signIn(page);
    const tones = page.locator('[data-test="card-docker"] .tone');
    await expect(tones.first()).toHaveText(/running|stopped|unavailable/);
  });

  test("every section keeps its heading structure", async ({ page }) => {
    await signIn(page);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Appliance Manager");
    await expect(page.getByRole("heading", { level: 2, name: "Warnings" })).toBeVisible();
  });
});

test.describe("basic and expert mode", () => {
  test("basic mode hides image digests and raw package details", async ({ page }) => {
    await signIn(page);
    await setMode(page, "basic");
    await openView(page, "admin");
    await expect(page.locator('[data-test="admin-version"]')).toBeVisible();
    await expect(page.locator('[data-test="admin-image"]')).toHaveCount(0);
    await expect(page.locator('[data-test="install-channel"]')).not.toContainText(
      "Exact release tag",
    );
  });

  test("expert mode adds the image identity and advanced actions", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    await expect(page.locator('[data-test="admin-image"]')).toBeVisible();
    await expect(page.locator('[data-test="admin-image"]')).toContainText("sha256:");
    await expect(page.locator('[data-test="install-channel"]')).toContainText("Exact release tag");

    await openView(page, "updates");
    await expect(page.locator('[data-test="updates-stage-all"]')).toBeVisible();
    await expect(page.locator('[data-test="updates-stage-repair"]')).toBeVisible();
  });

  test("the mode preference survives a reload and stays a UI preference", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await page.reload();
    await expect(page.locator("#mode-expert")).toHaveAttribute("aria-pressed", "true");
  });

  test("basic mode still shows the same backend state", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    const expertVersion = await page.locator('[data-test="admin-version"]').innerText();
    await setMode(page, "basic");
    const basicVersion = await page.locator('[data-test="admin-version"]').innerText();
    expect(basicVersion).toContain("v1.0.0");
    expect(expertVersion).toContain("v1.0.0");
  });
});

test.describe("admin lifecycle @authority", () => {
  test("an install plan previews the target before anything changes", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");

    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v1.1.0");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="install-plan"]').click(),
    ]);

    const dialog = page.locator("#dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("target tag");
    await expect(dialog).toContainText("v1.1.0");
    await expect(dialog).toContainText("target digest");
    await expect(page.locator("#dialog-confirm")).toBeEnabled();
    await expect(page.locator('[data-test="admin-version"]')).toContainText("v1.0.0");
  });

  test("a cancelled plan changes nothing", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v1.1.0");
    await page.locator('[data-test="install-plan"]').click();
    await expect(page.locator("#dialog")).toBeVisible();
    await page.locator("#dialog-cancel").click();
    await expect(page.locator("#dialog-backdrop")).toBeHidden();
    await expect(page.locator('[data-test="admin-version"]')).toContainText("v1.0.0");
  });

  test("confirming an install runs it and reports the result", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v1.1.0");
    await page.locator('[data-test="install-plan"]').click();
    await expect(page.locator("#dialog")).toBeVisible();

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/confirm")),
      page.locator("#dialog-confirm").click(),
    ]);

    const banner = page.locator('[data-test="operation-stage"]');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("admin.install");
    await expect(banner).toContainText("succeeded", { timeout: 20_000 });
    await expect(page.locator('[data-test="acknowledge-operation"]')).toBeVisible();
  });

  test("an install error is shown with its reason", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v9.9.9");

    const dialogMessage = new Promise<string>((resolve) => {
      page.once("dialog", async (alert) => {
        const message = alert.message();
        await alert.dismiss();
        resolve(message);
      });
    });
    await page.locator('[data-test="install-plan"]').click();
    expect(await dialogMessage).toContain("v9.9.9");
  });

  test("rollback previews the previous known-good version", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");
    await expect(page.locator('[data-test="admin-known-good"]')).toContainText("v0.9.0");

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/rollback")),
      page.getByRole("button", { name: "Roll back" }).click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await expect(page.locator("#dialog-title")).toContainText("Roll back");
  });

  test("a repair that cannot fix anything is not styled as success", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { break_compose: true } });
    await signIn(page);
    await openView(page, "admin");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/repair")),
      page.getByRole("button", { name: "Preview repair" }).click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/confirm")),
      page.locator("#dialog-confirm").click(),
    ]);

    const outcome = page.locator('[data-test="operation-outcome"] .tone');
    await expect(outcome).toHaveText("manual action required", { timeout: 20_000 });
    await expect(outcome).not.toHaveClass(/tone-ok/);
    await expect(page.locator('[data-test="manual-actions"]')).toBeVisible();
    await expect(page.locator('[data-test="manual-actions"]')).toContainText(
      "install-admin-console.sh",
    );
  });

  test("an appliance that never had Admin offers to install it, not to repair it", async ({
    page,
    request,
  }) => {
    await request.post("/api/test/reset", { data: { never_installed: true } });
    await signIn(page);
    await openView(page, "admin");

    await expect(page.locator('[data-test="admin-bootstrap-install"]')).toBeVisible();
    await expect(page.locator('[data-test="admin-repair-stage"]')).toHaveCount(0);
    await expect(page.locator('[data-test="admin-rollback-stage"]')).toHaveCount(0);

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="admin-bootstrap-plan"]').click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await expect(page.locator('[data-test="plan-creates-deployment"]')).toContainText(
      "docker-compose.admin.yml",
    );
  });

  test("typing survives the two-second poll", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");

    await page.locator('[data-test="install-channel"]').selectOption("exact");
    const tag = page.locator('[data-test="install-tag"]');
    await tag.click();
    await tag.type("v1.1.0", { delay: 40 });

    // Long enough for at least two poll ticks to have rebuilt the page.
    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await page.waitForResponse((response) => response.url().includes("/api/operations"));

    await expect(tag).toHaveValue("v1.1.0");
    await expect(tag).toBeFocused();
  });

  test("a digest that cannot be resolved is reported, not worked around", async ({
    page,
    request,
  }) => {
    await request.post("/api/test/reset", { data: { break_digest: true } });
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v1.1.0");

    const refusal = new Promise<string>((resolve) => {
      page.once("dialog", async (alert) => {
        const message = alert.message();
        await alert.dismiss();
        resolve(message);
      });
    });
    await page.locator('[data-test="install-plan"]').click();
    expect(await refusal).toContain("digest");
    await expect(page.locator('[data-test="admin-version"]')).toContainText("v1.0.0");
  });

  test("an install plan shows the immutable reference in expert mode", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "admin");
    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v1.1.0");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="install-plan"]').click(),
    ]);
    await expect(page.locator("#dialog")).toContainText("target reference");
    await expect(page.locator("#dialog")).toContainText("@sha256:");
    await page.locator("#dialog-cancel").click();
  });

  test("a repair preview lists findings before anything is changed", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/repair")),
      page.getByRole("button", { name: "Preview repair" }).click(),
    ]);
    await expect(page.locator('[data-test="repair-findings"]')).toBeVisible();
    await expect(page.locator('[data-test="repair-findings"]')).toContainText("docker daemon");
  });

  test("a running operation survives a browser reload", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");
    await page.locator('[data-test="admin-restart"]').click();
    await expect(page.locator("#dialog")).toBeVisible();
    await page.locator("#dialog-cancel").click();

    await page.reload();
    await expect(page.locator('[data-test="operation-stage"]')).toBeVisible();
    await expect(page.locator('[data-test="operation-stage"]')).toContainText("awaiting");
  });

  test("a second conflicting mutation is refused", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");
    await page.locator('[data-test="admin-restart"]').click();
    await expect(page.locator("#dialog")).toBeVisible();
    await page.locator("#dialog-cancel").click();

    const conflict = new Promise<string>((resolve) => {
      page.once("dialog", async (alert) => {
        const message = alert.message();
        await alert.dismiss();
        resolve(message);
      });
    });
    await page.locator('[data-test="admin-stop"]').click();
    expect(await conflict).toContain("still active");
  });
});

test.describe("operating-system updates", () => {
  test("a security update plan lists the affected packages", async ({ page }) => {
    await signIn(page);
    await openView(page, "updates");
    await expect(page.locator('[data-test="updates-security"]')).toContainText("2");

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/updates/plan")),
      page.locator('[data-test="updates-install-security"]').click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await expect(page.locator('[data-test="package-table"]')).toContainText("openssl");
    await expect(page.locator('[data-test="package-table"]')).toContainText("security");
  });

  test("installing security updates reports a result", async ({ page }) => {
    await signIn(page);
    await openView(page, "updates");
    await page.locator('[data-test="updates-install-security"]').click();
    await expect(page.locator("#dialog")).toBeVisible();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/confirm")),
      page.locator("#dialog-confirm").click(),
    ]);
    await expect(page.locator('[data-test="operation-stage"]')).toContainText("succeeded", {
      timeout: 20_000,
    });
  });

  test("the major OS upgrade path is explained, not offered", async ({ page }) => {
    await signIn(page);
    await openView(page, "updates");
    await expect(page.locator("#main")).toContainText("flash the new supported appliance image");
  });

  // The test host is a single-slot appliance, so the page must show the package
  // mode and say what A/B would require rather than offering a conversion.
  test("a single-slot appliance says A/B needs re-imaging", async ({ page }) => {
    await signIn(page);
    await openView(page, "updates");
    await expect(page.locator('[data-test="ab-single-slot"]')).toContainText(
      "single root filesystem",
    );
    await expect(page.locator('[data-test="ab-single-slot"]')).toContainText(
      "A/B-capable appliance image",
    );
    await expect(page.locator('[data-test="ab-plan-update"]')).toHaveCount(0);
    await expect(page.locator('[data-test="ab-stage-rollback"]')).toHaveCount(0);
  });
});

test.describe("ssh and backup access", () => {
  test("deploying a public key shows its fingerprint first", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    await page.locator('[data-test="key-account"]').selectOption("ems-backup");
    await page.locator('[data-test="key-value"]').fill(PUBLIC_KEY);
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/ssh/keys")),
      page.locator('[data-test="key-add"]').click(),
    ]);
    await expect(page.locator("#dialog")).toContainText("SHA256:");

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/confirm")),
      page.locator("#dialog-confirm").click(),
    ]);
    await expect(page.locator('[data-test="operation-stage"]')).toContainText("succeeded", {
      timeout: 20_000,
    });

    await page.locator('[data-test="acknowledge-operation"]').click();
    await expect(page.locator('[data-test="ssh-key-table"]')).toContainText(
      "appliance-test@example.invalid",
    );
  });

  test("a private key is refused with an explanation", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    await page.locator('[data-test="key-value"]').fill(
      "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
    );
    const refusal = new Promise<string>((resolve) => {
      page.once("dialog", async (alert) => {
        const message = alert.message();
        await alert.dismiss();
        resolve(message);
      });
    });
    await page.locator('[data-test="key-add"]').click();
    expect(await refusal).toContain("private key");
  });

  test("password login stays disabled and sftp instructions are shown", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    await expect(page.locator('[data-test="ssh-service"]')).toContainText("no");
    // The backup account is SFTP-only, so the UI must not advertise rsync/scp.
    await expect(page.locator('[data-test="backup-example"]').first()).toContainText("sftp -r");
    await expect(page.locator("#main")).not.toContainText("rsync -a");
    await expect(page.locator('[data-test="backup-paths"]')).toContainText("read-only");
  });

  test("the backup card states the protocol and that there is no shell", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    const card = page.locator('[data-test="backup-account"]');
    await expect(card).toContainText("SFTP");
    await expect(card).toContainText("Shell access");
    await expect(page.locator('[data-test="backup-export"]')).toBeVisible();
  });
});

test.describe("network and power", () => {
  test("a WLAN change warns about the disconnect before applying", async ({ page }) => {
    await signIn(page);
    await openView(page, "network");
    await page.locator('[data-test="wifi-ssid"]').fill("GuestNet");
    await page.locator('[data-test="wifi-pass"]').fill("correct-horse-battery");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/network/wifi/plan")),
      page.locator('[data-test="wifi-plan"]').click(),
    ]);
    await expect(page.locator("#dialog")).toContainText("previous profile is kept");
    await page.locator("#dialog-cancel").click();
  });

  test("a hostname change shows the new URL", async ({ page }) => {
    await signIn(page);
    await openView(page, "network");
    await page.locator('[data-test="hostname-input"]').fill("ems-pi5");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/network/hostname")),
      page.locator('[data-test="hostname-plan"]').click(),
    ]);
    await expect(page.locator("#dialog")).toContainText("ems-pi5.local");
    await page.locator("#dialog-cancel").click();
  });

  test("reboot requires an explicit confirmation", async ({ page }) => {
    await signIn(page);
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/system/reboot")),
      page.locator('[data-test="quick-reboot"]').click(),
    ]);
    await expect(page.locator("#dialog-title")).toContainText("Restart the Raspberry Pi");
    await expect(page.locator("#dialog-confirm")).toHaveText("Restart");
    await expect(page.locator("#dialog")).toContainText("EMS control stops");
    await page.locator("#dialog-cancel").click();
    await expect(page.locator("#reconnect")).toBeHidden();
  });

  test("the confirmation dialog is usable with the keyboard", async ({ page }) => {
    await signIn(page);
    await page.locator('[data-test="quick-reboot"]').click();
    await expect(page.locator("#dialog")).toBeVisible();
    await expect(page.locator("#dialog-confirm")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.locator("#dialog-backdrop")).toBeHidden();
  });
});

test.describe("diagnostics", () => {
  test("logs are loaded on demand and stay bounded", async ({ page }) => {
    await signIn(page);
    await openView(page, "diagnostics");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/logs/")),
      page.locator('[data-test="log-load"]').click(),
    ]);
    await expect(page.locator('[data-test="log-output"]')).toBeVisible();
    await expect(page.locator('[data-test="log-output"]')).not.toContainText("supersecret");
  });

  test("a support archive states what it excludes", async ({ page }) => {
    await signIn(page);
    await openView(page, "diagnostics");
    await expect(page.locator('[data-test="diag-support"]')).toContainText("Passwords");
  });
});

test.describe("responsive navigation", () => {
  test("the phone layout keeps every section reachable", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 780 });
    await signIn(page);
    for (const view of ["admin", "updates", "network", "access", "diagnostics", "settings"]) {
      await openView(page, view);
    }
    const body = await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    );
    expect(body).toBeTruthy();
  });

  test("the desktop layout shows the host badge", async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await signIn(page);
    await expect(page.locator(".app-header .badge")).toHaveText("HOST MANAGEMENT");
    await expect(page.locator(".app-header .brand-line")).toHaveText("EMS SolarFlow");
  });
});

test.describe("settings", () => {
  test("host settings are read-only and the password can be changed", async ({ page }) => {
    await signIn(page);
    await openView(page, "settings");
    await expect(page.locator('[data-test="settings-appliance"]')).toContainText("8080");
    await expect(page.locator('[data-test="settings-updates"]')).toContainText(
      "ghcr.io/basecubedev/ems-solarflow-admin",
    );

    await page.locator('[data-test="pw-current"]').fill(PASSWORD);
    await page.locator('[data-test="pw-new"]').fill("a-second-appliance-secret");
    await page.locator('[data-test="pw-confirm"]').fill("a-second-appliance-secret");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/settings/password")),
      page.locator('[data-test="pw-submit"]').click(),
    ]);
    await expect(page.locator('[data-test="pw-message"]')).toContainText("signed out");
    await expect(page.locator("#gate")).toBeVisible({ timeout: 15_000 });
  });
});

test.describe("truthful host state @smoke", () => {
  test("a degraded security audit is stated, not implied away", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { agent_offline: true } });
    await signIn(page);

    // Authentication is a recovery path: it must still work.
    await expect(page.locator("#shell")).toBeVisible();
    const notice = page.locator('[data-test="audit-degraded"]');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("Security audit degraded");
    await expect(notice).toContainText("unrecorded");

    await openView(page, "settings");
    const card = page.locator('[data-test="settings-audit"]');
    await expect(card).toContainText("degraded");
    await expect(card).toContainText("the privileged appliance agent");
  });

  test("a healthy audit trail shows no warning", async ({ page }) => {
    await signIn(page);
    await expect(page.locator('[data-test="audit-degraded"]')).toHaveCount(0);
    await openView(page, "settings");
    await expect(page.locator('[data-test="settings-audit"]')).toContainText("healthy");
  });

  test("a lifecycle action names the fact that failed verification", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { admin_unreachable: true } });
    await signIn(page);
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/restart")),
      page.locator('[data-test="quick-restart-admin"]').click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/confirm")),
      page.locator("#dialog-confirm").click(),
    ]);

    const outcome = page.locator('[data-test="operation-outcome"] .tone');
    await expect(outcome).not.toHaveClass(/tone-ok/, { timeout: 20_000 });
    const reasons = page.locator('[data-test="verification-reasons"]');
    await expect(reasons).toBeVisible();
    await expect(reasons).toContainText("the Admin web interface did not answer");
  });

  // An OS update refused before the first destructive byte. "Incomplete" on its
  // own would send an operator looking for an outage that never happened.
  test("an os update refused before the write says nothing was written", async ({
    page,
    request,
  }) => {
    await request.post("/api/test/reset", { data: { ab_deployment_drift: true } });
    await signIn(page);

    const replan = page.locator('[data-test="ab-replan-required"]');
    await expect(replan).toBeVisible({ timeout: 20_000 });
    await expect(replan).toContainText("the inactive slot is untouched");
    await expect(replan).toContainText("boot default is unchanged");
    await expect(replan).toContainText("create a new update plan");
    await expect(page.locator('[data-test="operation-outcome"] .tone')).not.toHaveClass(
      /tone-ok/,
    );
  });

  test("a rollback that fails preflight reports that nothing was stopped", async ({
    page,
    request,
  }) => {
    await request.post("/api/test/reset", { data: { rollback_image_missing: true } });
    await signIn(page);
    await openView(page, "admin");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/rollback")),
      page.getByRole("button", { name: "Roll back" }).click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/operations/confirm")),
      page.locator("#dialog-confirm").click(),
    ]);

    const untouched = page.locator('[data-test="admin-untouched"]');
    await expect(untouched).toBeVisible({ timeout: 20_000 });
    await expect(untouched).toContainText("still running");
    await expect(page.locator('[data-test="operation-outcome"] .tone')).not.toHaveClass(/tone-ok/);
  });

  test("an export mounted read-write is never shown as read-only", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { export_read_write: true } });
    await signIn(page);
    await openView(page, "access");

    const card = page.locator('[data-test="backup-export"]');
    await expect(card).toContainText("degraded");
    await expect(card).toContainText("not confined");
    const paths = page.locator('[data-test="backup-paths"]');
    await expect(paths).toContainText("exported read-write");
    await expect(paths).not.toContainText("read-only export");
  });

  test("a confined export root is shown as confined and read-only", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");

    const card = page.locator('[data-test="backup-export"]');
    await expect(card).toContainText("configured");
    await expect(card).toContainText("confined to the export root");
    await expect(page.locator('[data-test="backup-paths"]')).toContainText("read-only export");
  });

  test("a missing optional host feature is not styled as a failure", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { docker_missing: true } });
    await signIn(page);

    const card = page.locator('[data-test="card-docker"]');
    await expect(card).toContainText("unavailable");
    await expect(card).toContainText("Docker is not installed");
    await expect(card.locator(".tone")).not.toHaveClass(/tone-bad/);
  });

  test("an sshd policy that still permits forwarding is not shown as confined", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { forwarding_allowed: true } });
    await signIn(page);
    await openView(page, "access");

    const card = page.locator('[data-test="backup-export"]');
    await expect(card).toContainText("not confined");
    await expect(card).toContainText("Not enforced by sshd");
    await expect(card).toContainText("allowtcpforwarding");
  });

  test("a refused export source is visible in the export card", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { export_source_rejected: true } });
    await signIn(page);
    await openView(page, "access");

    const card = page.locator('[data-test="backup-export"]');
    await expect(card).toContainText("Export setup");
    await expect(card).toContainText("failed");
    await expect(card).toContainText("symlink");
  });

  test("a port check that could not run is not shown as ok", async ({ page, request }) => {
    await request.post("/api/test/reset", { data: { port_check_broken: true } });
    await signIn(page);
    await openView(page, "admin");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/repair")),
      page.getByRole("button", { name: "Preview repair" }).click(),
    ]);

    const row = page.locator('[data-test="repair-findings"] tr', { hasText: "admin port" });
    await expect(row).toContainText("not checked");
    await expect(row.locator(".tone")).toHaveClass(/tone-warn/);
  });
});
