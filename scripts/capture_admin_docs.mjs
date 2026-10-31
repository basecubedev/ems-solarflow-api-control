// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every documentation screenshot of the Admin Console, all of them of the
// Modern console: the PNGs under docs/assets/screenshots/admin/ that the
// guides in docs/user/admin/ and docs/user/admin-*.md embed. One entry in
// SHOTS per file; tests/test_docs_user_guides.py reads their `file:` names as
// the manifest, and docs/assets/screenshots/admin/README.md lists every file.
//
// It starts the Admin docs preview (scripts/serve_admin_docs_preview.py) on a
// free loopback port, which serves the real admin/static/modern/ build at the
// root with the demo payloads of tests/fixtures/admin_docs/, and drives it
// with Playwright Chromium. A shot names the installation it shows with the
// preview's ?scenario= (a fresh host, a signed-out browser, an update that
// runs, ...); see SCENARIOS in the preview. No hardware, Docker, config.json
// or password is involved, and nothing is written outside the PNGs.
//
//   node scripts/capture_admin_docs.mjs                       # every file
//   node scripts/capture_admin_docs.mjs admin-overview.png    # some of them
//
// A guide PNG is the 1440x900 window (390x844 on a phone), or the window
// grown to the whole page at that width where a shot says `full`. The browser
// runs in English in Europe/Berlin with the clock fixed to the demo
// installation's afternoon, so "started 6 hours ago" and the backup ages read
// the same on every capture.
// The appearance strips are the same region of the same page photographed
// once per value of one axis and laid side by side on a blank page, so
// nothing but that axis changes.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "docs", "assets", "screenshots", "admin");
const FIXTURES = path.join(ROOT, "tests", "fixtures", "admin_docs");
const DEMO_NOW = new Date("2026-07-05T13:30:00Z");
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };
const STRIP = { width: 714, height: 560, gap: 12 };
const DEFAULT_PREFS = {
  "ems-modern.lang": "en",
  "ems-modern.theme": "graphite",
  "ems-modern.density": "normal",
  "ems-modern.motion": "off",
};

const upgradeDemo = JSON.parse(readFileSync(path.join(FIXTURES, "guided_upgrade_demo.json"), "utf8"));

/** The record a tab keeps of the update it started, so the page follows that run as its own. */
function upgradeMemo() {
  const memo = upgradeDemo.upgrade_memo;
  return { "ems-admin.upgrade": JSON.stringify({ ...memo, since: Date.parse(memo.since) }) };
}

async function painted(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function settled(locator) {
  await locator.evaluate((node) => Promise.all(node.getAnimations({ subtree: true }).map((animation) => animation.finished)));
}

async function scrollTo(page, locator, margin = 24) {
  await locator.first().evaluate((node, gap) => {
    const bar = document.querySelector(".topbar")?.getBoundingClientRect().bottom ?? 0;
    window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - bar - gap);
  }, margin);
  await painted(page);
}

/** The window grown to the page's own height, so a tall page is photographed whole with its menu beside it. */
async function wholePage(page) {
  const { width } = page.viewportSize();
  let height = 0;
  for (let next = DESKTOP.height; next !== height; ) {
    height = next;
    await page.setViewportSize({ width, height });
    await painted(page);
    next = Math.max(DESKTOP.height, await page.evaluate(() => document.documentElement.scrollHeight));
  }
}

async function visible(locator) {
  await locator.first().waitFor({ state: "visible", timeout: 15000 });
}

async function appReady(page) {
  await page.locator(".app[data-landed]").waitFor({ timeout: 15000 });
  await visible(page.locator('[data-testid="health-state"]'));
}

/** The Overview with every tile answered, so no tile is photographed before its data arrives. */
async function overviewReady(page) {
  await appReady(page);
  await visible(page.locator('[data-testid="health-state"] .dot.ok'));
  await visible(page.getByTestId("tile-analytics").getByText("Running"));
  await visible(page.getByTestId("tile-control").getByText("May control"));
  await visible(page.getByTestId("tile-backup").getByText("4 hours ago"));
  await painted(page);
}

async function pickAppearance(page, label) {
  await page.getByTestId("appearance-button").click();
  const menu = page.getByTestId("appearance");
  await visible(menu);
  await menu.getByRole("button", { name: label, exact: true }).click();
  await page.keyboard.press("Escape");
  await menu.waitFor({ state: "hidden" });
  await painted(page);
}

async function column(page, x) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await painted(page);
  return page.screenshot({ clip: { x, y: 0, width: STRIP.width, height: STRIP.height }, animations: "disabled" });
}

async function strip(context, file, columns) {
  const sheet = await context.newPage();
  try {
    await sheet.setViewportSize({ width: columns.length * STRIP.width + (columns.length - 1) * STRIP.gap, height: STRIP.height });
    const images = columns.map((png) => `<img alt="" src="data:image/png;base64,${png.toString("base64")}">`).join("");
    await sheet.setContent(`<body style="margin:0;display:flex;gap:${STRIP.gap}px;background:#05070b">${images}</body>`);
    await sheet.waitForFunction(() => [...document.images].every((image) => image.complete && image.naturalWidth > 0));
    await sheet.screenshot({ path: path.join(OUT, file) });
  } finally {
    await sheet.close();
  }
}

const SHOTS = [
  {
    file: "admin-first-start-password.png",
    scenario: "first-start",
    async prepare(page) {
      await visible(page.locator('[data-testid="sign-in-screen"][data-mode="create"]'));
    },
  },
  {
    file: "admin-login.png",
    scenario: "signed-out",
    async prepare(page) {
      await visible(page.locator('[data-testid="sign-in-screen"][data-mode="login"]'));
    },
  },
  {
    file: "admin-assistant-fresh.png",
    scenario: "fresh",
    hash: "",
    async prepare(page) {
      await visible(page.locator('[data-testid="assistant-welcome"][data-kind="none"]'));
      await visible(page.locator('[data-step="install"][data-state="todo"]'));
    },
  },
  {
    file: "admin-assistant.png",
    scenario: "no-backups",
    hash: "#/assistant",
    full: true,
    async prepare(page) {
      await visible(page.locator('[data-testid="assistant-welcome"][data-kind="installed"]'));
      await visible(page.locator('[data-step="backup"][data-state="todo"]'));
      await visible(page.locator('[data-step="dashboard"][data-state="ready"]'));
    },
  },
  {
    file: "admin-guided-setup-begin.png",
    scenario: "fresh",
    hash: "#/setup",
    async prepare(page) {
      await appReady(page);
      await visible(page.getByTestId("setup-begin"));
    },
  },
  {
    file: "admin-guided-setup-start.png",
    scenario: "setup",
    hash: "#/setup",
    prefs: { "ems-admin-setup-step": "release" },
    async prepare(page) {
      await appReady(page);
      await visible(page.getByTestId("setup-release"));
      await visible(page.locator('[data-testid="setup-checks"] li[data-passed="yes"]'));
    },
  },
  {
    file: "admin-discovery-preview.png",
    scenario: "setup",
    hash: "#/setup",
    prefs: { "ems-admin-setup-step": "devices" },
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="setup-step-devices"][aria-selected="true"]'));
      await page.locator('[data-testid="setup-unified"] li[data-role="inverter"]').nth(1).waitFor();
      await visible(page.getByTestId("setup-discovery-progress").getByText("on", { exact: true }));
    },
  },
  {
    file: "admin-guided-setup-config-preview.png",
    scenario: "setup",
    hash: "#/setup",
    prefs: { "ems-admin-setup-step": "config" },
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="setup-step-config"][aria-selected="true"]'));
      await visible(page.getByTestId("setup-verdict-state").getByText("Ready", { exact: true }));
      await visible(page.getByTestId("setup-inverters").getByText("INV_2", { exact: false }));
    },
  },
  {
    file: "admin-setup-deployment.png",
    scenario: "setup",
    hash: "#/setup",
    prefs: { "ems-admin-setup-step": "deployment" },
    async prepare(page) {
      await appReady(page);
      await visible(page.getByTestId("setup-deploy-status").getByText("The deployment is ready", { exact: false }));
    },
  },
  {
    file: "admin-setup-start.png",
    scenario: "setup",
    hash: "#/setup",
    prefs: { "ems-admin-setup-step": "start" },
    async prepare(page) {
      await appReady(page);
      await visible(page.getByTestId("setup-start-success"));
    },
  },
  {
    file: "admin-overview.png",
    hash: "#/overview",
    async prepare(page) {
      await overviewReady(page);
    },
  },
  {
    file: "admin-overview-phone.png",
    hash: "#/overview",
    viewport: PHONE,
    async prepare(page) {
      await overviewReady(page);
      await visible(page.locator(".bottom-nav"));
    },
  },
  {
    file: "admin-phone-more.png",
    hash: "#/overview",
    viewport: PHONE,
    async prepare(page) {
      await appReady(page);
      await page.getByTestId("nav-more").click();
      const sheet = page.locator("dialog.sheet[open]");
      await visible(sheet);
      await settled(sheet);
    },
  },
  {
    file: "admin-updates.png",
    hash: "#/updates",
    full: true,
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="releases"] [data-tag="v0.8.16"]'));
    },
  },
  {
    file: "admin-upgrade-verifying.png",
    hash: "#/updates",
    async prepare(page) {
      await appReady(page);
      await page.route("**/api/admin/maintenance/upgrade/validate", () => {});
      await page.getByTestId("start-upgrade").click();
      await visible(page.locator('[data-testid="upgrade"][data-stage="verifying"]'));
      await page.clock.setFixedTime(new Date(DEMO_NOW.getTime() + 47_000));
      await visible(page.getByTestId("upgrade-clock").getByText("0:47"));
      await page.evaluate(() => window.scrollTo(0, 0));
    },
  },
  {
    file: "admin-guided-upgrade-plan.png",
    hash: "#/updates",
    full: true,
    async prepare(page) {
      await appReady(page);
      await page.getByTestId("start-upgrade").click();
      await visible(page.locator('[data-testid="upgrade"][data-stage="ready"]'));
      await page.evaluate(() => window.scrollTo(0, 0));
    },
  },
  {
    file: "admin-upgrade-running.png",
    scenario: "upgrade-running",
    hash: "#/updates",
    session: upgradeMemo(),
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="upgrade"][data-stage="running"]'));
      await visible(page.locator('[data-phase="install"][data-state="running"]'));
    },
  },
  {
    file: "admin-upgrade-completed.png",
    scenario: "upgrade-done",
    hash: "#/updates",
    session: upgradeMemo(),
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="upgrade"][data-stage="succeeded"]'));
    },
  },
  {
    file: "admin-admin-update-reconnect.png",
    scenario: "upgrade-reconnect",
    hash: "#/updates",
    session: upgradeMemo(),
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="upgrade"][data-stage="replacing"]'));
      await page.route("**/api/admin/auth/status", (route) => route.abort("connectionrefused"));
      await visible(page.getByTestId("reconnecting"));
    },
  },
  {
    file: "admin-settings-features.png",
    hash: "#/settings/features",
    async prepare(page) {
      await appReady(page);
      const winter = page.locator('[data-section="winter"]');
      await visible(winter);
      await winter.locator("button.summary").click();
      await visible(winter.locator('[data-path="winter.winter_min_soc"]'));
    },
  },
  {
    file: "admin-settings-safety.png",
    hash: "#/settings/safety",
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="settings-safety"] [data-path="system.allow_hardware_writes"]'));
    },
  },
  {
    file: "admin-settings-devices.png",
    hash: "#/settings/devices",
    full: true,
    async prepare(page) {
      await appReady(page);
      await visible(page.getByTestId("discovery-start"));
      await visible(page.getByText("INV_2").first());
    },
  },
  {
    file: "admin-settings-device.png",
    hash: "#/settings/devices",
    async prepare(page) {
      await appReady(page);
      const editor = page.getByTestId("devices-editor");
      await visible(editor);
      const card = editor.locator("section.device").filter({ hasText: "INV_1" }).first();
      await card.locator("button.summary").click();
      await visible(card.locator(".body input").first());
      await scrollTo(page, card, 16);
    },
  },
  {
    file: "admin-mqtt-broker.png",
    hash: "#/settings/addons",
    async prepare(page) {
      await appReady(page);
      await visible(page.getByTestId("mqtt-broker-accounts"));
    },
  },
  {
    file: "admin-mqtt-connect.png",
    hash: "#/settings/addons",
    async prepare(page) {
      await appReady(page);
      await page.getByTestId("mqtt-connect").click();
      const guide = page.getByTestId("mqtt-connect-guide");
      await visible(guide);
      await scrollTo(page, guide, 16);
    },
  },
  {
    file: "admin-mqtt-question.png",
    hash: "#/settings/devices",
    async prepare(page) {
      await appReady(page);
      const mapping = page.getByTestId("mqtt-mapping");
      await visible(mapping);
      await mapping.getByTestId("mqtt-topics-start").click();
      await visible(page.getByTestId("mqtt-topic-groups"));
      await mapping.locator('li[data-label="shelly_gen2"]').getByTestId("mqtt-map-group").click();
      const wizard = page.getByTestId("mqtt-wizard");
      await wizard.getByTestId("mqtt-kind-balcony").check();
      await wizard.getByTestId("mqtt-wizard-name").fill("Balcony South");
      await visible(wizard.getByTestId("mqtt-wizard-turned"));
      await scrollTo(page, wizard, 16);
    },
  },
  {
    file: "admin-mqtt-mapping.png",
    hash: "#/settings/devices",
    async prepare(page) {
      await appReady(page);
      const mapping = page.getByTestId("mqtt-mapping");
      await visible(mapping);
      await mapping.getByTestId("mqtt-topics-start").click();
      await visible(page.getByTestId("mqtt-topic-groups"));
      await scrollTo(page, page.locator("section.card").filter({ has: mapping }), 16);
    },
  },
  {
    file: "admin-mqtt-mapping-editor.png",
    hash: "#/settings/devices",
    async prepare(page) {
      await appReady(page);
      const mapping = page.getByTestId("mqtt-mapping");
      await visible(mapping);
      await mapping.getByTestId("mqtt-topics-start").click();
      await visible(page.getByTestId("mqtt-topic-groups"));
      await mapping.getByTestId("mqtt-mapped-devices").getByRole("button", { name: "Edit" }).first().click();
      const editor = page.getByTestId("mqtt-mapping-editor");
      await visible(editor);
      await editor.getByTestId("mqtt-device-preview").click();
      await visible(page.getByTestId("mqtt-preview-state"));
      await scrollTo(page, editor, 16);
    },
  },
  {
    file: "admin-settings-search.png",
    hash: "#/settings/devices",
    async prepare(page) {
      await appReady(page);
      await page.getByTestId("discovery-start").click();
      await visible(page.getByTestId("discovery-summary"));
      await scrollTo(page, page.getByTestId("discovery-summary"), 16);
    },
  },
  {
    file: "admin-settings-review.png",
    hash: "#/settings/features",
    async prepare(page) {
      await appReady(page);
      const winter = page.locator('[data-section="winter"]');
      await winter.locator("button.summary").click();
      await winter.locator('[data-path="winter.winter_min_soc"] input').fill("30");
      await page.getByTestId("review").click();
      await visible(page.getByTestId("settings-review"));
      await visible(page.getByTestId("apply"));
    },
  },
  {
    file: "admin-services.png",
    scenario: "settings-saved",
    hash: "#/services",
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-service="influxdb"][data-state="running"]'));
      await visible(page.getByText("newer than the running EMS"));
      await page.locator('[data-testid="sync"]:not([disabled])').waitFor();
      await visible(page.locator('[data-testid="health-state"] .dot.acc'));
    },
  },
  {
    file: "admin-backups.png",
    hash: "#/backups",
    full: true,
    async prepare(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="backup-list"] [data-kind="set"]'));
    },
  },
  {
    file: "admin-backup-restore.png",
    hash: "#/backups",
    async prepare(page) {
      await appReady(page);
      await page.locator('[data-kind="archive"][data-type="config"]').first().getByTestId("restore-open").click();
      await visible(page.getByTestId("restore-summary"));
      await scrollTo(page, page.locator(".restore"));
    },
  },
  {
    file: "admin-diagnostics.png",
    hash: "#/diagnostics",
    full: true,
    async prepare(page) {
      await appReady(page);
      await page.getByTestId("run-checks").click();
      await visible(page.getByTestId("diagnostics-summary"));
    },
  },
  {
    file: "admin-system.png",
    scenario: "mqtt",
    hash: "#/system",
    full: true,
    async prepare(page) {
      await appReady(page);
      await visible(page.getByText("local_mqtt").first());
      await visible(page.getByTestId("system-setup"));
    },
  },
  {
    file: "admin-system-recovery.png",
    scenario: "recovery",
    hash: "#/system",
    async prepare(page) {
      await appReady(page);
      const card = page.locator(".card", { hasText: "Unfinished" }).first();
      await visible(card);
      await scrollTo(page, card);
    },
  },
  {
    file: "admin-appearance-menu.png",
    hash: "#/overview",
    async prepare(page) {
      await overviewReady(page);
      await page.getByTestId("appearance-button").click();
      const menu = page.getByTestId("appearance");
      await visible(menu);
      await settled(menu);
    },
  },
  {
    file: "admin-appearance-themes.png",
    hash: "#/overview",
    async capture(page) {
      await overviewReady(page);
      const graphite = await column(page, 0);
      await pickAppearance(page, "Midnight");
      await page.locator('html[data-m-theme="midnight"]').waitFor({ state: "attached" });
      const midnight = await column(page, 0);
      await strip(page.context(), this.file, [graphite, midnight]);
    },
  },
  {
    file: "admin-appearance-density.png",
    hash: "#/backups",
    async capture(page) {
      await appReady(page);
      await visible(page.locator('[data-testid="backup-list"] [data-kind="set"]'));
      const x = Math.round((await page.locator(".sidebar").boundingBox()).width);
      const normal = await column(page, x);
      await pickAppearance(page, "Compact");
      await page.locator('html[data-density="compact"]').waitFor({ state: "attached" });
      const compact = await column(page, x);
      await strip(page.context(), this.file, [normal, compact]);
    },
  },
];

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

async function waitFor(url) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // The preview is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the Admin docs preview did not answer at ${url}`);
}

async function take(browser, base, shot) {
  const viewport = shot.viewport || DESKTOP;
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    locale: "en-US",
    timezoneId: "Europe/Berlin",
  });
  try {
    await context.addInitScript(
      ({ local, session }) => {
        for (const [key, value] of Object.entries(local)) window.localStorage.setItem(key, value);
        for (const [key, value] of Object.entries(session)) window.sessionStorage.setItem(key, value);
      },
      { local: { ...DEFAULT_PREFS, ...(shot.prefs || {}) }, session: shot.session || {} },
    );
    await context.clock.setFixedTime(DEMO_NOW);
    const page = await context.newPage();
    const query = shot.scenario ? `?scenario=${shot.scenario}` : "";
    await page.goto(`${base}/${query}${shot.hash ?? "#/overview"}`);
    await page.evaluate(() => document.fonts.ready);
    if (shot.capture) {
      await shot.capture(page);
    } else {
      await shot.prepare(page);
      if (shot.full) await wholePage(page);
      await painted(page);
      await page.screenshot({ path: path.join(OUT, shot.file), animations: "disabled" });
    }
    console.log(`wrote ${path.relative(ROOT, path.join(OUT, shot.file))}`);
  } finally {
    await context.close();
  }
}

const wanted = process.argv.slice(2);
const unknown = wanted.filter((name) => !SHOTS.some((shot) => shot.file === name));
if (unknown.length) throw new Error(`no such screenshot: ${unknown.join(", ")}`);
const port = await freePort();
const preview = spawn("python3", [path.join(ROOT, "scripts", "serve_admin_docs_preview.py"), "--port", String(port)], { stdio: "ignore" });
const base = `http://127.0.0.1:${port}`;
const browser = await chromium.launch();
try {
  await waitFor(`${base}/api/admin/auth/status`);
  for (const shot of SHOTS.filter((item) => !wanted.length || wanted.includes(item.file))) await take(browser, base, shot);
} finally {
  await browser.close();
  const stopped = new Promise((resolve) => preview.once("exit", resolve));
  preview.kill("SIGINT");
  await stopped;
}
