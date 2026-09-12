// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The browser half of scripts/capture_appearance_gallery.py. It signs in where
// a surface needs it, then photographs the same region of the same page once
// per variant, changing exactly one axis between shots.
//
// The axes are attributes on <html>, so a variant is one setAttribute and a
// repaint -- no reload, which is what keeps the three columns of a strip
// identical apart from the thing being shown.
//
// Never `waitUntil: "networkidle"` on the cockpit: /api/events is an open SSE
// stream, so the network is never idle.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import path from "node:path";

const [surface, port, outDir] = process.argv.slice(2);
const BASE = `http://127.0.0.1:${port}`;
const PASSWORD = "appearance-gallery-1";

const AXES = {
  palettes: [{ theme: "signal" }, { theme: "void" }, { theme: "copper" }],
  styles: [{ style: "glass" }, { style: "console" }, { style: "brutal" }],
  density: [{ density: "compact" }, { density: "normal" }, { density: "roomy" }],
};

// Where to point the camera. Each surface names an element whose box is the
// crop: a region with a card family in it, so a change of fill, frame, corner
// or spacing is visible without the reader hunting for it.
// A strip of three full-width pages is six times wider than it is tall, which
// a document renders at about 150 pixels high -- too small to see the thing it
// exists to show. Each column is therefore a window on the page rather than
// the whole of it: wide enough for a couple of cards, tall enough that the
// spacing between them reads.
const COLUMN = { width: 560, height: 520 };

// The height is per surface because the pages are: the Admin landing is two
// choices and stops, so the shared 520 would frame a third of a page and two
// thirds of background.
// The subject has to be a region the axis can actually change. The cockpit's
// first screen is mostly the Live Flow diagram, and that is exactly the wrong
// thing to photograph: it is drawn as SVG with pill-shaped nodes, and the
// object style deliberately leaves pills alone. Three columns of it are three
// identical pictures. The Control view is forty cards instead.
const SUBJECT = {
  // The landing page is two cards and a paragraph, so a style strip of it is
  // mostly unchanged background -- measured at 12% of pixels differing, which
  // is a picture that does not show its own subject. Guided Setup is six.
  admin: { after: "Guided setup", clip: "#view-setup, .admin-shell", top: 120, height: 430 },
  appliance: { clip: "#shell, .gate-panel", top: 150, height: 560 },
  dashboard: { path: "/preview/control", clip: ".control-pipeline, .shell", top: 120 },
};

async function signIn(page) {
  if (surface === "admin") {
    const create = page.locator("#auth-create-password");
    if (await create.count()) {
      await create.fill(PASSWORD);
      const confirm = page.locator("#auth-create-confirm");
      if (await confirm.count()) await confirm.fill(PASSWORD);
      await page.locator("#auth-create-form button[type=submit]").click();
      await page.waitForTimeout(1200);
    }
  }
  if (surface === "appliance") {
    const gate = page.locator("#gate-password");
    if (await gate.count()) {
      await gate.fill(PASSWORD);
      const confirm = page.locator("#gate-confirm");
      if (await confirm.count()) await confirm.fill(PASSWORD);
      await page.locator("#gate-form button[type=submit]").click();
      await page.waitForTimeout(1200);
    }
  }
}

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1180, height: 900 },
  deviceScaleFactor: 1,
});
await page.goto(`${BASE}${SUBJECT[surface].path ?? "/"}`, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1200);
await signIn(page);
await page.waitForTimeout(800);

// Some surfaces only become card-dense one click in.
if (SUBJECT[surface].after) {
  const entry = page.locator(`text=${SUBJECT[surface].after}`).first();
  if (await entry.count()) {
    await entry.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(2000);
  }
}

mkdirSync(outDir, { recursive: true });

const subject = SUBJECT[surface];
const target = page.locator(subject.clip.split(", ").find(Boolean));

for (const [axis, variants] of Object.entries(AXES)) {
  for (const [index, variant] of variants.entries()) {
    await page.evaluate((v) => {
      const root = document.documentElement;
      root.setAttribute("data-theme", v.theme ?? "signal");
      root.setAttribute("data-style", v.style ?? "glass");
      root.setAttribute("data-density", v.density ?? "normal");
    }, variant);
    await page.waitForTimeout(350);

    let box = null;
    for (const selector of subject.clip.split(", ")) {
      const found = page.locator(selector).first();
      if (await found.count()) {
        box = await found.boundingBox();
        if (box && box.width > 200) break;
      }
    }
    const top = Math.max(0, Math.round((box?.y ?? 0) + (subject.top ?? 0)));
    const clip = box
      ? {
          x: Math.max(0, Math.round(box.x)),
          y: top,
          width: Math.round(Math.min(box.width, COLUMN.width)),
          height: Math.round(Math.min(subject.height ?? COLUMN.height, 900 - top)),
        }
      : undefined;
    await page.screenshot({ path: path.join(outDir, `${axis}-${index}.png`), clip });
  }
}

await browser.close();
