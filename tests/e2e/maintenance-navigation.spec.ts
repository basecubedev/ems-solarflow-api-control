import { test, expect } from "./fixtures/admin";
import type { Page } from "@playwright/test";
import { LoginPage } from "./pages/login-page";
import { MaintenancePage } from "./pages/maintenance-page";
import { holdRead } from "./helpers/held-read";

// Two navigation promises the console did not keep. A bookmarked address was
// dropped on a cold load, because the hash router only ever ran on a hashchange
// and never once the start gate revealed the workspace. And every page switch
// left keyboard focus on a control that had just been hidden, so a keyboard or
// screen-reader owner heard nothing and restarted from the document top.

/**
 * Record the status page's text each time it reads idle. A page that reads idle
 * and then still changes was not done, which no single look at the attribute
 * can tell while other reads are in flight.
 */
async function recordIdleText(page: Page) {
  await page.evaluate(() => {
    const panel = document.getElementById("maintenance-status-panel")!;
    const idle: string[] = [];
    (window as unknown as { idleText: string[] }).idleText = idle;
    new MutationObserver(() => {
      if (panel.getAttribute("aria-busy") === "false") idle.push(panel.textContent ?? "");
    }).observe(panel, { attributes: true, attributeFilter: ["aria-busy"] });
  });
}

test.describe("Maintenance: navigation", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
  });

  // A bookmark visit is a fresh document. Navigating from "/" to "/#..." is a
  // same-document hash change that never re-boots the app, so the reload is
  // what makes this a cold load rather than a hashchange in disguise.
  const coldLoad = async (page: Page, hash: string) => {
    await page.goto("/" + hash);
    await page.reload();
  };

  test("a bookmarked settings tab opens that tab on a cold load", async ({
    page,
  }) => {
    await coldLoad(page, "#maintenance-settings-safety");
    await expect(page.locator("#maintenance-settings-panel")).toBeVisible();
    await expect(page.locator('[data-settings-pane="safety"]')).toBeVisible();
    await expect(page.locator("#view-start")).toBeHidden();
    expect(new URL(page.url()).hash).toBe("#maintenance-settings-safety");
  });

  test("a bookmarked status page opens on a cold load", async ({ page }) => {
    await coldLoad(page, "#maintenance-manual");
    await expect(page.locator("#maintenance-status-panel")).toBeVisible();
    await expect(page.locator("#view-start")).toBeHidden();
  });

  test("an address that names no workspace view still shows the start gate", async ({
    page,
  }) => {
    await coldLoad(page, "#not-a-view");
    await expect(page.locator("#view-start")).toBeVisible();
    await expect(page.locator("#maintenance-status-panel")).toBeHidden();
  });

  // Guided Setup is a workflow, and its truth is the durable transition. An
  // address must not resurrect an unconfirmed wizard; only a server-side
  // transition resumes it.
  test("a setup address does not open the wizard from the gate", async ({
    page,
  }) => {
    await coldLoad(page, "#setup");
    await expect(page.locator("#view-start")).toBeVisible();
    await expect(page.locator('[data-admin-view-panel="setup"]')).toBeHidden();
  });

  // The gate is showing and the owner pastes a bookmarked address: no document
  // load happens, only a hashchange, and that has to open the page too.
  test("typing a maintenance address at the start gate opens that page", async ({
    page,
  }) => {
    await expect(page.locator("#view-start")).toBeVisible();
    await page.evaluate(() => {
      window.location.hash = "maintenance-settings-expert";
    });
    await expect(page.locator("#maintenance-settings-panel")).toBeVisible();
    await expect(page.locator('[data-settings-pane="expert"]')).toBeVisible();
  });

  test("opening a door moves focus to the heading of the page it opened", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openStatus();
    await expect(page.locator("#maintenance-status-panel h2")).toBeFocused();
  });

  test("going back to the hub moves focus to the hub heading", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings();
    await page.locator("#maintenance-back-settings").click();
    await expect(page.locator("#maintenance-hub")).toBeVisible();
    await expect(page.locator("#maintenance-hub h2")).toBeFocused();
  });

  // The page renders one read at a time and moves while it does, so a press
  // made mid-load can start on one control and end on another. The status page
  // says when it is still moving, and it says so until each read has rendered.
  for (const [name, path] of [
    ["overview", "/api/admin/maintenance/overview"],
    ["config", "/api/admin/maintenance/config"],
    ["container plan", "/api/admin/maintenance/containers/plan"],
    ["MQTT telemetry", "/api/admin/maintenance/zendure-mqtt/runtime-status"],
    ["MQTT migration", "/api/admin/maintenance/zendure-mqtt/migration-review"],
    ["workflow recovery", "/api/admin/workflow-lifecycle/recovery/preview"],
  ]) {
    test(`the status page reads busy until its ${name} read has returned`, async ({
      page,
    }) => {
      const maintenance = new MaintenancePage(page);
      await maintenance.enterMaintenance();
      await expect(page.locator("#maintenance-hub-backup-state")).not.toHaveText("…");
      await recordIdleText(page);
      const read = await holdRead(page, path);
      await maintenance.goTo("status");
      await read.requested;
      await expect(page.locator("#maintenance-workflow-recovery-summary")).toHaveText(
        "Not loaded yet.",
      );
      await expect(page.locator("#maintenance-status-panel")).toHaveAttribute(
        "aria-busy",
        "true",
      );
      read.release();
      await maintenance.waitForStatusSettled();

      const { idle, now } = await page.evaluate(() => ({
        idle: (window as unknown as { idleText: string[] }).idleText,
        now: document.getElementById("maintenance-status-panel")!.textContent,
      }));
      expect(idle.length).toBeGreaterThan(0);
      expect(idle[0]).toBe(now);
    });
  }

  // The page was idle when it was left; on the way back it must read busy from
  // the moment it is shown, or a press made then lands on a moving page and
  // waitForStatusSettled returns on the idle mark of the last visit.
  test("a second visit to the status page reads busy from the moment it is shown", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openStatus();
    await page.locator("#maintenance-back-hub").click();
    await expect(page.locator("#maintenance-hub")).toBeVisible();
    await page.evaluate(() => {
      const panel = document.getElementById("maintenance-status-panel")!;
      const seen: string[] = [];
      (window as unknown as { busyWhenShown: string[] }).busyWhenShown = seen;
      new MutationObserver((records) => {
        if (records.some((record) => record.attributeName === "hidden") && !panel.hidden) {
          seen.push(String(panel.getAttribute("aria-busy")));
        }
      }).observe(panel, { attributes: true, attributeFilter: ["hidden", "aria-busy"] });
    });
    const read = await holdRead(page, "/api/admin/maintenance/overview");
    await maintenance.goTo("status");
    await read.requested;
    await expect(page.locator("#maintenance-status-panel")).toHaveAttribute(
      "aria-busy",
      "true",
    );
    read.release();
    await maintenance.waitForStatusSettled();

    const seen = await page.evaluate(
      () => (window as unknown as { busyWhenShown: string[] }).busyWhenShown,
    );
    expect(seen).toEqual(["true"]);
  });

  // A recheck and a refresh overlap; the page reads idle once, after both.
  test("overlapping status loads read idle once, when the last one is done", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openStatus();
    await maintenance.openStatusCard("maintenance-containers");
    await page.evaluate(() => {
      const panel = document.getElementById("maintenance-status-panel")!;
      const marks: string[] = [];
      (window as unknown as { busyMarks: string[] }).busyMarks = marks;
      new MutationObserver(() => {
        marks.push(String(panel.getAttribute("aria-busy")));
      }).observe(panel, { attributes: true, attributeFilter: ["aria-busy"] });
    });
    const plan = await holdRead(page, "/api/admin/maintenance/containers/plan");
    await page.locator("#maintenance-runtime-containers-recheck").click();
    await plan.requested;
    const recovery = await holdRead(page, "/api/admin/workflow-lifecycle/recovery/preview");
    await page.locator("#maintenance-refresh").click();
    await recovery.requested;
    const planDone = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/admin/maintenance/containers/plan",
    );
    plan.release();
    await planDone;
    await expect(page.locator("#maintenance-status-panel")).toHaveAttribute(
      "aria-busy",
      "true",
    );
    recovery.release();
    await maintenance.waitForStatusSettled();

    const marks = await page.evaluate(
      () => (window as unknown as { busyMarks: string[] }).busyMarks,
    );
    expect(marks.filter((mark) => mark === "false")).toHaveLength(1);
  });

  // The config read also fills the safety section above the cards, and the
  // settings page starts one of its own. The status page must not read idle
  // while that one is still out: the section fills, and moves the cards, later.
  test("the status page stays busy while a config read the settings page started is out", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    const panel = page.locator("#maintenance-status-panel");
    await maintenance.enterMaintenance();
    const read = await holdRead(page, "/api/admin/maintenance/config");
    await maintenance.goTo("settings");
    await read.requested;
    await maintenance.goTo("status");
    await expect(page.locator("#maintenance-workflow-recovery-summary")).not.toHaveText(
      "Not loaded yet.",
    );
    await expect(panel).toHaveAttribute("aria-busy", "true");
    await expect(page.locator("#maintenance-control-transports")).toBeEmpty();

    read.release();

    await maintenance.waitForStatusSettled();
    await expect(page.locator("#maintenance-control-transports")).not.toBeEmpty();
  });
});

// The landing states what this host has. It was read once at bootstrap and
// never again, so a finished Guided Setup left it still insisting that nothing
// was installed — the one screen whose whole job is to be current.
test.describe("Landing: what this host has", { tag: ["@setup"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("coming back to the landing reads the install state again", async ({
    page,
  }) => {
    const reads: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/admin/install-state")) {
        reads.push(request.url());
      }
    });
    await page.locator('[data-start-path="manage_existing"]').click();
    await expect(page.locator("#maintenance-hub")).toBeVisible();
    const before = reads.length;
    await page.locator('#maintenance-hub [data-back="landing"]').click();
    await expect(page.locator("#view-start")).toBeVisible();
    await expect.poll(() => reads.length).toBeGreaterThan(before);
  });

  test("returning to the landing moves focus to its heading", async ({
    page,
  }) => {
    await page.locator('[data-start-path="manage_existing"]').click();
    await expect(page.locator("#maintenance-hub")).toBeVisible();
    await page.locator('#maintenance-hub [data-back="landing"]').click();
    await expect(page.locator("#view-start h2")).toBeFocused();
  });
});

// The hub used to print "Recommended path" on the Guided upgrade card whatever
// the system said — including for an installation it could not see running.
test.describe("Maintenance: hub recommendation", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("an installation that needs a look recommends the status page", async ({
    page,
  }) => {
    await page.locator('[data-start-path="manage_existing"]').click();
    await expect(page.locator("#maintenance-hub-status-badge")).toBeVisible();
    await expect(page.locator("#maintenance-hub-upgrade-badge")).toBeHidden();
    await expect(page.locator("#maintenance-open-status")).toHaveClass(
      /is-primary/,
    );
    await expect(page.locator("#maintenance-open-upgrade")).not.toHaveClass(
      /is-primary/,
    );
  });
});

// The status page used to open with a red banner that named a problem and said
// in the same breath that nothing could be done about it.
test.describe("Maintenance: what is wrong", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("the status page leads with a ranked answer and a next step", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openStatus();
    const findings = page.locator("#maintenance-findings");
    await expect(findings).toBeVisible();
    await expect(page.locator("#maintenance-findings-headline")).toContainText(
      /needs? your attention|could not be read/,
    );
    // Scoped to this page's list: the landing renders its own findings with the
    // same class, and a bare selector picks up that hidden list first.
    const first = page.locator("#maintenance-findings-list .maintenance-finding").first();
    await expect(first).toBeVisible();
    await expect(first.locator(".maintenance-finding-next")).not.toBeEmpty();
    // Worst first: no finding may outrank the one above it.
    const order = await page
      .locator("#maintenance-findings-list .maintenance-finding")
      .evaluateAll((items) =>
        items.map((item) =>
          ["error", "warning", "info"].indexOf(
            (item as HTMLElement).dataset.severity || "info",
          ),
        ),
      );
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });
});
