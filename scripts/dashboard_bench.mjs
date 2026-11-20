// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the dashboard costs a browser to keep open: frames, main-thread time
 * and memory per page, with more devices and more tabs. It needs no EMS, no
 * hardware and no network: the preview server (scripts/serve_dashboard_preview.py)
 * serves the synthetic snapshots, scaled to the device counts asked for, and
 * the pages run in real browsers through Playwright.
 *
 *   node scripts/dashboard_bench.mjs                               # the default matrix, Chromium
 *   node scripts/dashboard_bench.mjs --browser firefox --pages overview,energy
 *   node scripts/dashboard_bench.mjs --devices 2,8 --tabs 1,5 --seconds 20 --out reports/dashboard-perf/run.json
 *
 * Every row names the browser build, so a number is compared only with
 * numbers taken the same way. Headless browsers render in software: compare
 * ratios between rows of one run, never absolutes across machines, and never
 * run two benchmarks at once.
 */
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, firefox } from "playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const PAGES = ["overview", "devices", "consumers", "energy", "analytics", "control"];
const BROWSERS = { chromium, firefox };
const SETTLE_MS = 3000;

function option(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index > 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function list(value) {
  return String(value).split(",").map((item) => item.trim()).filter(Boolean);
}

export function quantile(values, q) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
}

function mean(values) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function round(value, digits = 1) {
  return value == null ? null : Math.round(value * 10 ** digits) / 10 ** digits;
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitFor(url) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the preview did not answer at ${url}`);
}

async function startPreview(devices) {
  const port = await freePort();
  const preview = spawn("python3", [path.join(ROOT, "scripts", "serve_dashboard_preview.py"), "--port", String(port), "--devices", String(devices)], { stdio: "ignore" });
  await waitFor(`http://127.0.0.1:${port}/api/ui-config`);
  return { base: `http://127.0.0.1:${port}`, stop: () => new Promise((resolve) => { preview.once("exit", resolve); preview.kill("SIGINT"); }) };
}

/** Frame intervals the page itself observes, from requestAnimationFrame, while the window runs. */
function recordFrames() {
  window.__benchFrames = [];
  let last = performance.now();
  const step = (now) => {
    window.__benchFrames.push(now - last);
    last = now;
    window.__benchFrame = requestAnimationFrame(step);
  };
  window.__benchFrame = requestAnimationFrame(step);
}

async function chromiumMetrics(session) {
  if (!session) return null;
  const { metrics } = await session.send("Performance.getMetrics");
  return Object.fromEntries(metrics.map(({ name, value }) => [name, value]));
}

async function measure(browserName, browser, base, page, devices, tabs, seconds) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  try {
    await context.addInitScript(() => {
      localStorage.setItem("ems-modern.lang", "en");
      localStorage.setItem("ems-modern.stayOnDashboard", "1");
    });
    const opened = [];
    for (let index = 0; index < tabs; index += 1) {
      const tab = await context.newPage();
      await tab.goto(`${base}/#/${page}`, { waitUntil: "load" });
      opened.push(tab);
    }
    const front = opened[opened.length - 1];
    await front.bringToFront();
    await front.waitForTimeout(SETTLE_MS);
    const session = browserName === "chromium" ? await context.newCDPSession(front) : null;
    if (session) await session.send("Performance.enable");
    const before = await chromiumMetrics(session);
    await front.evaluate(recordFrames);
    const started = Date.now();
    await front.waitForTimeout(seconds * 1000);
    const elapsed = (Date.now() - started) / 1000;
    const frames = (await front.evaluate(() => {
      cancelAnimationFrame(window.__benchFrame);
      return window.__benchFrames;
    })).slice(5);
    const after = await chromiumMetrics(session);
    const busy = (name) => (before && after ? ((after[name] - before[name]) / elapsed) * 1000 : null);
    return {
      browser: browserName,
      version: browser.version(),
      page,
      devices,
      tabs,
      seconds: round(elapsed),
      fps: round(frames.length ? 1000 / mean(frames) : null),
      frameP95Ms: round(quantile(frames, 0.95)),
      longFrames: frames.filter((delta) => delta > 50).length,
      taskMsPerSecond: round(busy("TaskDuration")),
      scriptMsPerSecond: round(busy("ScriptDuration")),
      layoutMsPerSecond: round(busy("LayoutDuration")),
      styleMsPerSecond: round(busy("RecalcStyleDuration")),
      heapMb: round(after ? after.JSHeapUsedSize / 1024 / 1024 : null),
      domNodes: after ? after.Nodes : null,
    };
  } finally {
    await context.close();
  }
}

function table(rows) {
  const columns = ["browser", "page", "devices", "tabs", "fps", "frameP95Ms", "longFrames", "taskMsPerSecond", "scriptMsPerSecond", "layoutMsPerSecond", "heapMb", "domNodes"];
  const line = (cells) => `| ${cells.join(" | ")} |`;
  return [line(columns), line(columns.map(() => "---")), ...rows.map((row) => line(columns.map((column) => row[column] ?? "–")))].join("\n");
}

async function main() {
  const browsers = list(option("browser", "chromium"));
  const pages = list(option("pages", PAGES.join(",")));
  const deviceCounts = list(option("devices", "2,8")).map(Number);
  const tabCounts = list(option("tabs", "1,3")).map(Number);
  const seconds = Number(option("seconds", "15"));
  const out = option("out", "");
  for (const name of browsers) if (!BROWSERS[name]) throw new Error(`no such browser: ${name}`);
  for (const page of pages) if (!PAGES.includes(page)) throw new Error(`no such page: ${page}`);

  const rows = [];
  for (const devices of deviceCounts) {
    const preview = await startPreview(devices);
    try {
      for (const name of browsers) {
        const browser = await BROWSERS[name].launch();
        try {
          for (const page of pages) {
            for (const tabs of tabCounts) {
              const row = await measure(name, browser, preview.base, page, devices, tabs, seconds);
              rows.push(row);
              console.error(`${name} ${page} devices=${devices} tabs=${tabs}: ${row.fps} fps, ${row.taskMsPerSecond ?? "–"} ms/s`);
            }
          }
        } finally {
          await browser.close();
        }
      }
    } finally {
      await preview.stop();
    }
  }
  const report = { taken: new Date().toISOString(), host: { platform: os.platform(), cpus: os.cpus().length, model: os.cpus()[0]?.model }, rows };
  if (out) writeFileSync(path.resolve(ROOT, out), `${JSON.stringify(report, null, 2)}\n`);
  console.log(table(rows));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
