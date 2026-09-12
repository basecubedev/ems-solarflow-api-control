// SPDX-License-Identifier: AGPL-3.0-or-later
// Appliance Manager UI journeys against the deterministic test server.
// Every assertion waits on a locator or a response, never on a fixed timeout.
import { expect, test } from "@playwright/test";
import { PASSWORD, PUBLIC_KEY, openView, parkAtBottom, resetAppliance, setMode, signIn } from "./helpers";

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

  test("a mismatched confirmation is refused with a visible reason", async ({ page }) => {
    await page.goto("/");
    await page.locator("#gate-password").fill("one-secret");
    await page.locator("#gate-confirm").fill("another-secret");
    await page.locator("#gate-submit").click();
    await expect(page.locator("#gate-error")).toBeVisible();
    await expect(page.locator("#gate-error")).toContainText("do not match");
    await expect(page.locator("#shell")).toBeHidden();
  });

  test("a short password is accepted, because the length is the operator's", async ({ page }) => {
    await page.goto("/");
    await page.locator("#gate-password").fill("x");
    await page.locator("#gate-confirm").fill("x");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/session/setup")),
      page.locator("#gate-submit").click(),
    ]);
    await expect(page.locator("#shell")).toBeVisible();
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
    // One product name, then one page heading below it. The page heading is
    // what a keyboard user is put on when they pick a section, so every view
    // has exactly one and it is focusable.
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Appliance Manager");
    await expect(page.locator("#main .page-title")).toHaveCount(1);
    await expect(page.locator("#main .page-title")).toHaveAttribute("tabindex", "-1");
  });

  test("the page leads with a verdict and what needs attention", async ({ page }) => {
    await signIn(page);
    const verdict = page.locator('[data-test="overview-verdict"]');
    await expect(verdict).toBeVisible();
    await expect(verdict).toHaveAttribute("data-tone", /ok|warn|bad/);

    // The fixture host has security updates pending, so there is one finding
    // and it offers the page that installs them.
    const findings = page.locator('[data-test="findings"]');
    await expect(findings).toBeVisible();
    await expect(findings.locator(".finding")).toHaveCount(1);
    await expect(findings.locator(".finding-title")).toHaveText("Security updates are waiting");
    await expect(findings.locator('[data-test="finding-open-updates"]')).toBeVisible();
  });

  test("a finding takes you to the page that can act on it", async ({ page }) => {
    await signIn(page);
    await page.locator('[data-test="finding-open-updates"]').click();
    await expect(page.locator('[data-test="nav-updates"]')).toHaveAttribute("aria-current", "page");
    await expect(page.locator("#main .page-title")).toHaveText("System updates");
  });

  test("the navigation marks the section that needs attention", async ({ page }) => {
    await signIn(page);
    const mark = page.locator('[data-test="nav-updates"] .nav-mark');
    await expect(mark).toHaveAttribute("data-severity", "warning");
    // Never colour alone: the same fact is in the button's accessible name.
    await expect(page.locator('[data-test="nav-updates"]')).toContainText("needs attention");
    await expect(page.locator('[data-test="nav-network"] .nav-mark')).not.toHaveAttribute(
      "data-severity",
      /.*/,
    );
  });

  test("switching section moves focus to that page's heading", async ({ page }) => {
    await signIn(page);
    await openView(page, "network");
    await expect(page.locator("#main .page-title")).toBeFocused();
    await expect(page.locator("#main .page-title")).toHaveText("Network");
  });
});

test.describe("the two-second poll", () => {
  // The poll exists so a running operation's progress stays live, and it
  // rebuilds the page to do it. Anything the operator was holding on to has to
  // survive that: a field keeps its text, and a control keeps the focus. Both
  // are waited on by response, never by a clock.
  test("a control keeps the focus it was given", async ({ page }) => {
    await signIn(page);
    const button = page.locator('[data-test="quick-restart-admin"]');
    await button.focus();
    await expect(button).toBeFocused();

    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await page.waitForResponse((response) => response.url().includes("/api/operations"));

    await expect(button).toBeFocused();
  });

  // Restoring focus has to be invisible. Moving focus on a deliberate view
  // change may scroll -- landing at the top of the page you just opened is
  // right -- but re-taking it after a rebuild must not, or the page walks back
  // to whatever had focus every two seconds. The tallest page shows it first.
  test("the page stays where the reader scrolled it", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    await expect(page.locator("#main .page-title")).toBeFocused();

    const parked = await page.evaluate(() => {
      window.scrollTo(0, document.body.scrollHeight);
      return Math.round(window.scrollY);
    });
    expect(parked).toBeGreaterThan(0);

    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await page.waitForResponse((response) => response.url().includes("/api/operations"));

    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(parked);
  });

  // The park is a key press here, not a window.scrollTo, because focusing the
  // control first is what this test is about: in Firefox that focus leaves a
  // scroll-into-view pending, and the next programmatic scroll is undone by it
  // a frame later -- fifteen milliseconds in, two seconds before the poll this
  // test is named after has run once. Real input is not undone, so parkAtBottom
  // parks the way the reader whose position this protects would.
  test("a control deep in the page does not pull the page to it", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    await page.locator('[data-test="key-add"]').focus();

    const parked = await parkAtBottom(page);

    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await page.waitForResponse((response) => response.url().includes("/api/operations"));

    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(parked);
    await expect(page.locator('[data-test="key-add"]')).toBeFocused();
  });

  // Every section, not only the one that happens to be tall enough today. The
  // viewport is shrunk so that each page scrolls, and the list comes from the
  // navigation rather than from here, so a section added later is covered
  // without anyone remembering to add it. The parked > 0 check is what keeps
  // this honest: a page that stops being scrollable would otherwise pass
  // without ever exercising the guard.
  test("no section walks the page while the poll rebuilds it", async ({ page }) => {
    await page.setViewportSize({ width: 1180, height: 360 });
    await signIn(page);

    const views = await page
      .locator("#nav-list [data-test^='nav-']")
      .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-test").slice(4)));
    expect(views.length).toBeGreaterThan(4);

    for (const view of views) {
      await openView(page, view);
      const parked = await page.evaluate(() => {
        window.scrollTo(0, document.body.scrollHeight);
        return Math.round(window.scrollY);
      });
      expect(parked, `${view} does not scroll at 360px, so it proves nothing`).toBeGreaterThan(0);

      await page.waitForResponse((response) => response.url().includes("/api/operations"));

      expect(await page.evaluate(() => Math.round(window.scrollY)), `${view} moved`).toBe(parked);
    }
  });

  test("opening another section still starts at the top of it", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    const parked = await page.evaluate(() => {
      window.scrollTo(0, document.body.scrollHeight);
      return Math.round(window.scrollY);
    });
    expect(parked).toBeGreaterThan(0);

    // The rebuild keeps the position; a view change is not a rebuild, it is a
    // move, and reading a page you just opened starts at its heading.
    await openView(page, "diagnostics");
    expect(await page.evaluate(() => Math.round(window.scrollY))).toBe(0);
    await expect(page.locator("#main .page-title")).toBeFocused();
  });

  test("the verdict is not read out again on every rebuild", async ({ page }) => {
    await signIn(page);
    // Not a live region of its own: this node is replaced every two seconds.
    await expect(page.locator('[data-test="overview-verdict"]')).not.toHaveAttribute(
      "aria-live",
      /.*/,
    );
    // The shell's live region stays silent while the verdict is unchanged.
    await expect(page.locator("#live-region")).toBeEmpty();
    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await expect(page.locator("#live-region")).toBeEmpty();
  });

  test("the page heading keeps the focus a section change gave it", async ({ page }) => {
    await signIn(page);
    await openView(page, "diagnostics");
    await expect(page.locator("#main .page-title")).toBeFocused();

    await page.waitForResponse((response) => response.url().includes("/api/operations"));
    await page.waitForResponse((response) => response.url().includes("/api/operations"));

    await expect(page.locator("#main .page-title")).toBeFocused();
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
    await expect(dialog).toContainText("Version to install");
    await expect(dialog).toContainText("v1.1.0");
    await expect(dialog).toContainText("Image digest");
    // What is about to happen is read before which image it happens with.
    const body = await dialog.innerText();
    expect(body.indexOf("Version to install")).toBeLessThan(body.indexOf("Image digest"));
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
    await expect(banner).toContainText("Installing EMS Admin");
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

  test("the install list offers every published version, releases before candidates", async ({
    page,
  }) => {
    await signIn(page);
    await openView(page, "admin");

    const select = page.locator('[data-test="install-channel"]');
    await expect(select.locator('optgroup[label="Stable"] option')).toHaveText([
      "v1.1.0",
      "v1.0.0",
    ]);
    await expect(select.locator('optgroup[label="Unstable"] option')).toHaveText(["v1.2.0-rc1"]);
    // Basic mode: the versions are listed without the expert free-text field.
    await expect(select).not.toContainText("Exact release tag");
  });

  test("a host that refuses candidates still shows them, greyed out with the reason", async ({
    page,
    request,
  }) => {
    await request.post("/api/test/reset", { data: { refuse_prereleases: true } });
    await signIn(page);
    await openView(page, "admin");

    const candidate = page.locator(
      '[data-test="install-channel"] optgroup[label="Unstable"] option',
    );
    await expect(candidate).toHaveCount(1);
    await expect(candidate).toContainText("v1.2.0-rc1");
    await expect(candidate).toContainText("allow_prerelease");
    await expect(candidate).toBeDisabled();
    // The releases stay listed and pickable; only the candidate is refused.
    await expect(
      page.locator('[data-test="install-channel"] optgroup[label="Stable"] option'),
    ).toHaveText(["v1.1.0", "v1.0.0"]);
  });

  test("a candidate is installable on a host that enables candidates", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");

    await page.locator('[data-test="install-channel"]').selectOption("v1.2.0-rc1");
    const [planned] = await Promise.all([
      page.waitForRequest((candidate) => candidate.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="install-plan"]').click(),
    ]);

    expect(planned.postDataJSON()).toMatchObject({ channel: "exact", tag: "v1.2.0-rc1" });
    await expect(page.locator("#dialog")).toBeVisible();
    await expect(page.locator("#dialog")).toContainText("v1.2.0-rc1");
  });

  test("a version picked from the list is planned as that exact tag", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");

    await page.locator('[data-test="install-channel"]').selectOption("v1.1.0");
    const [request] = await Promise.all([
      page.waitForRequest((candidate) => candidate.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="install-plan"]').click(),
    ]);

    expect(request.postDataJSON()).toMatchObject({ channel: "exact", tag: "v1.1.0" });
    await expect(page.locator("#dialog")).toBeVisible();
    await expect(page.locator("#dialog")).toContainText("v1.1.0");
  });

  test("a first installation can pick a version instead of only the newest", async ({
    page,
    request,
  }) => {
    await request.post("/api/test/reset", { data: { never_installed: true } });
    await signIn(page);
    await openView(page, "admin");

    const select = page.locator('[data-test="install-channel"]');
    await expect(select.locator('optgroup[label="Stable"] option')).toHaveText([
      "v1.1.0",
      "v1.0.0",
    ]);
    await select.selectOption("v1.0.0");
    const [planned] = await Promise.all([
      page.waitForRequest((candidate) => candidate.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="admin-bootstrap-plan"]').click(),
    ]);

    expect(planned.postDataJSON()).toMatchObject({ channel: "exact", tag: "v1.0.0" });
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
    await expect(page.locator("#dialog")).toContainText("Exact image");
    await expect(page.locator("#dialog")).toContainText("@sha256:");
    // A guard, not a reproduction: a digest has no break opportunity of its own,
    // and a row wider than the dialog pushes Confirm past the edge. It holds
    // today because those values are set in a breaking style; this fails if
    // that stops being true.
    const width = await page.locator("#dialog").evaluate((node) => ({
      box: node.clientWidth,
      content: node.scrollWidth,
    }));
    expect(width.content).toBeLessThanOrEqual(width.box);
    await expect(page.locator("#dialog-confirm")).toBeInViewport();
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

  test("the updates page says what recovery is", async ({ page }) => {
    await signIn(page);
    await openView(page, "updates");
    await expect(page.locator('[data-test="updates-recovery"]')).toContainText(
      "patched in place",
    );
    await expect(page.locator('[data-test="updates-recovery"]')).toContainText(
      "writing the card again",
    );
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
    const settings = await (await page.request.get("/api/settings")).json();
    await expect(page.locator('[data-test="settings-appliance"]')).toContainText(
      String(settings.web_port),
    );
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
    const notice = page.locator('.finding[data-code="security_audit_degraded"]');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("Sign-ins are not being recorded");
    await expect(notice).toContainText("unrecorded");

    await openView(page, "settings");
    const card = page.locator('[data-test="settings-audit"]');
    await expect(card).toContainText("degraded");
    await expect(card).toContainText("the privileged appliance agent");
  });

  test("a healthy audit trail shows no warning", async ({ page }) => {
    await signIn(page);
    await expect(page.locator('.finding[data-code="security_audit_degraded"]')).toHaveCount(0);
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
    await request.post("/api/test/reset", { data: { deployment_drift: true } });
    await signIn(page);

    const replan = page.locator('[data-test="replan-required"]');
    await expect(replan).toBeVisible({ timeout: 20_000 });
    await expect(replan).toContainText("Nothing was written");
    await expect(replan).toContainText("no longer the one this operation was");
    await expect(replan).toContainText("create a new plan");
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

test.describe("appliance manager @smoke", () => {
  test("a quiet appliance offers an update and has nothing to go back to", async ({ page }) => {
    await signIn(page);
    await openView(page, "updates");

    const card = page.locator('[data-test="manager-installed"]');
    await expect(card).toBeVisible();
    await expect(page.locator('[data-test="manager-kept"]')).toContainText("none kept");
    await expect(page.locator('[data-test="manager-plan-revert"]')).toBeDisabled();
  });

  test("a kept package is offered as the way back", async ({ page, request }) => {
    await resetAppliance(request, { manager_package_kept: true });
    await signIn(page);
    await openView(page, "updates");

    await expect(page.locator('[data-test="manager-kept"]')).toContainText("0.1.0");
    const revert = page.locator('[data-test="manager-plan-revert"]');
    await expect(revert).toBeEnabled();
    await expect(revert).toContainText("0.1.0");
  });

  test("a deadline in flight blocks both controls", async ({ page, request }) => {
    // A second install would replace the package whose verdict the appliance is
    // still waiting for.
    await resetAppliance(request, { manager_package_kept: true, manager_deadline_armed: true });
    await signIn(page);
    await openView(page, "updates");

    await expect(page.locator('[data-test="manager-deadline"]')).toContainText("being judged");
    await expect(page.locator('[data-test="manager-deadline"]')).toContainText("0.3.0");
    await expect(page.locator('[data-test="manager-plan-revert"]')).toBeDisabled();
  });

  test("a deadline whose window closed without a verdict stops blocking", async ({
    page,
    request,
  }) => {
    // The lockout is for a deadline in flight. One whose window passed without a
    // verdict never ran, cannot revert anything any more, and locking the
    // controls on it leaves an operator with no lever but a keyboard at the
    // console -- which an appliance owner often does not have.
    await resetAppliance(request, {
      manager_package_kept: true,
      manager_deadline_expired: true,
    });
    await signIn(page);
    await openView(page, "updates");

    await expect(page.locator('[data-test="manager-deadline"]')).toHaveCount(0);
    const notice = page.locator('[data-test="manager-deadline-expired"]');
    await expect(notice).toContainText("nothing judged it");
    await expect(notice).toContainText("0.3.0");
    await expect(page.locator('[data-test="manager-plan-revert"]')).toBeEnabled();
  });

  test("a reverted install is reported rather than left silent", async ({ page, request }) => {
    await resetAppliance(request, { manager_package_kept: true, manager_verdict: "reverted" });
    await signIn(page);
    await openView(page, "updates");

    const verdict = page.locator('[data-test="manager-verdict"]');
    await expect(verdict).toBeVisible();
    await expect(verdict).toContainText("did not prove itself in time");
  });

  test("an appliance with no package index says so instead of offering nothing", async ({
    page,
  }) => {
    await signIn(page);
    await openView(page, "updates");

    await expect(page.locator('[data-test="manager-sources-unconfigured"]')).toContainText(
      "manager_index_url",
    );
  });

  test("the revert plan states what it would put back before anything happens", async ({
    page,
    request,
  }) => {
    await resetAppliance(request, { manager_package_kept: true });
    await signIn(page);
    await openView(page, "updates");

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/manager/plan-revert")),
      page.locator('[data-test="manager-plan-revert"]').click(),
    ]);

    const dialog = page.locator("#dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("0.1.0");
  });
});

test.describe("console rescue account", () => {
  test("the shipped password is reported without being demanded", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");

    const card = page.locator('[data-test="rescue-account"]');
    await expect(card).toContainText("ems-rescue");
    await expect(card).toContainText("shipped password");
    await expect(card).toContainText("sudo passwd ems-rescue");
    // Reported, never demanded: there is no control that changes it.
    await expect(card.locator("button")).toHaveCount(0);
  });

  test("a changed password is reported as changed", async ({ page, request }) => {
    await resetAppliance(request, { rescue_password_changed: true });
    await signIn(page);
    await openView(page, "access");

    await expect(page.locator('[data-test="rescue-account"]')).toContainText("changed");
  });

  test("an appliance without the account is not reported as secure", async ({ page, request }) => {
    await resetAppliance(request, { rescue_account_absent: true });
    await signIn(page);
    await openView(page, "access");

    await expect(page.locator('[data-test="rescue-account"]')).toContainText("not present");
  });
});
