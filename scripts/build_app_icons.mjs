// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The app's home-screen icons, drawn from the mark (frontend/shared/brand/
// mark.js) in the default scheme's colours: the mark on its own tile at 192
// and 512 pixels, and the ring and bolt on a full-bleed tile, inside the
// maskable icon's safe circle of 80 per cent, for the maskable icon and the
// iOS home screen, which cut the shape themselves. Written as PNG beside the mark; the shared page build
// (frontend/shared/vite/page.js) ships them with the web app manifest.
// Inkscape draws them, so run it again only when the mark or its colours
// change, and commit the PNGs it writes.
//
//   node scripts/build_app_icons.mjs
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { MARK_ARC, MARK_BOLT, MARK_RING, MARK_VIEWBOX, markSvg } from "../frontend/shared/brand/mark.js";
import { APP_ICONS, markColors } from "../frontend/shared/vite/page.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BRAND = path.join(ROOT, "frontend", "shared", "brand");
const MARK_SCALE = 1.05;
const QUARTER = (Math.PI * 11) / 2;

/** The mark's dashes in lengths of the drawing itself: Inkscape does not scale a dash by ``pathLength``. */
function absoluteDashes(svg) {
  const [dash] = MARK_ARC.dash.split(" ").map(Number);
  const unit = QUARTER / MARK_ARC.length;
  return svg.replaceAll(
    `pathLength="${MARK_ARC.length}" stroke-dasharray="${MARK_ARC.dash}" stroke-dashoffset="${MARK_ARC.offset}"`,
    `stroke-dasharray="${(dash * unit).toFixed(3)} ${QUARTER.toFixed(3)}" stroke-dashoffset="${(MARK_ARC.offset * unit).toFixed(3)}"`,
  );
}

function fullBleed(colors) {
  const arcs = MARK_RING.map(
    (arc) => `<path d="${arc.d}" stroke="${colors[arc.token]}" pathLength="${MARK_ARC.length}" stroke-dasharray="${MARK_ARC.dash}" stroke-dashoffset="${MARK_ARC.offset}"/>`,
  ).join("");
  const offset = (36 * (1 - MARK_SCALE)) / 2;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${MARK_VIEWBOX}">` +
    `<rect width="36" height="36" fill="${colors.tile}"/>` +
    `<g transform="translate(${offset} ${offset}) scale(${MARK_SCALE})">` +
    `<g fill="none" stroke-width="${MARK_ARC.width}" stroke-linecap="round">${arcs}</g>` +
    `<path d="${MARK_BOLT}" fill="${colors.bolt}"/></g></svg>`
  );
}

const colors = markColors();
const drawings = { tile: absoluteDashes(markSvg(colors)), bleed: absoluteDashes(fullBleed(colors)) };
const scratch = mkdtempSync(path.join(process.env.TMPDIR || tmpdir(), "app-icons-"));
try {
  for (const [fileName, size] of Object.entries(APP_ICONS)) {
    const drawing = fileName.startsWith("app-icon-") && !fileName.includes("maskable") ? "tile" : "bleed";
    const source = path.join(scratch, `${drawing}.svg`);
    writeFileSync(source, drawings[drawing]);
    execFileSync("inkscape", [source, "--export-type=png", `--export-width=${size}`, `--export-height=${size}`, `--export-filename=${path.join(BRAND, fileName)}`], { stdio: "ignore" });
    console.log(`${fileName}  ${size}x${size}  ${drawing}`);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
