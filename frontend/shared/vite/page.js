// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What both Modern front ends do to their page at build time: the tab icon,
 * the inline icon sprite and the licence texts of the font and the icons.
 * One build recipe, so the two apps cannot drift apart in how they ship.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { markSvg } from "../brand/mark.js";

const SHARED = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const ROOT = resolve(SHARED, "../..");
const ICON_DIR = resolve(ROOT, "node_modules/lucide-static/icons");
const TOKENS = resolve(SHARED, "styles/tokens.css");
const SHARED_ICONS = resolve(SHARED, "icons.json");
const NOTICES = {
  "Inter.LICENSE": resolve(ROOT, "node_modules/@fontsource-variable/inter/LICENSE"),
  "Lucide.LICENSE": resolve(ROOT, "node_modules/lucide-static/LICENSE"),
};

/**
 * The browser tab's icon: the side menu's mark in the default scheme's
 * colours, read from tokens.css. It is a data: URI, which both servers' CSP
 * admits for images, so neither needs a new file type.
 */
function favicon() {
  const css = readFileSync(TOKENS, "utf8");
  const start = css.indexOf(":root {");
  const root = css.slice(start, css.indexOf("}", start));
  const token = (name) => {
    const match = root.match(new RegExp(`${name}:\\s*([^;]+);`));
    if (!match) throw new Error(`tokens.css has no ${name} in its first :root block`);
    return match[1].trim();
  };
  const svg = markSvg({
    "--pv": token("--pv"),
    "--battery": token("--battery"),
    "--output": token("--output"),
    "--grid": token("--grid"),
    tile: token("--m-surface-2"),
    line: token("--m-line-strong"),
    bolt: token("--m-text"),
  });
  return `<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,${encodeURIComponent(svg)}">`;
}

const SHAPE = /<(?:path|circle|ellipse|line|polygon|polyline|rect)\b[^<>]*\/>/g;

/** The shared parts' icons and the app's own, each once, in a stable order. */
export function iconNames(appIcons) {
  const shared = JSON.parse(readFileSync(SHARED_ICONS, "utf8"));
  const own = JSON.parse(readFileSync(appIcons, "utf8"));
  return [...new Set([...shared, ...own])].sort();
}

/**
 * The icons the UI uses, as one inline sprite: no icon font, no request, no
 * innerHTML. Only the drawing elements are taken over, by allow-list rather
 * than by stripping what is not wanted, and an icon that holds anything else
 * fails the build instead of reaching the page.
 */
function iconSprite(appIcons) {
  const symbols = iconNames(appIcons).map((name) => {
    const svg = readFileSync(resolve(ICON_DIR, `${name}.svg`), "utf8");
    const open = svg.indexOf("<svg");
    const close = svg.lastIndexOf("</svg>");
    const inner = svg.slice(svg.indexOf(">", open) + 1, close);
    const shapes = inner.match(SHAPE) || [];
    if (!shapes.length || inner.replace(SHAPE, "").trim()) {
      throw new Error(`icon ${name} holds more than drawing elements`);
    }
    const body = shapes.map((shape) => shape.replace(/\s+/g, " ")).join("");
    return `<symbol id="i-${name}" viewBox="0 0 24 24">${body}</symbol>`;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" class="icon-sprite" aria-hidden="true">${symbols.join("")}</svg>`;
}

/**
 * The page plugin: fills <!--favicon--> and <!--icons-->, and <!--vendored-->
 * with what the app passes. The font and the icon paths are copied, not
 * bundled, so their licence texts ship beside them as files; Vite's
 * licenses.json covers bundled packages only.
 */
export function modernPage({ name, icons, vendored = "" }) {
  return {
    name,
    generateBundle() {
      for (const [fileName, path] of Object.entries(NOTICES)) {
        this.emitFile({ type: "asset", fileName, source: readFileSync(path, "utf8") });
      }
    },
    transformIndexHtml: {
      order: "post",
      handler(html) {
        return html
          .replace("<!--favicon-->", favicon())
          .replace("<!--icons-->", iconSprite(icons))
          .replace("<!--vendored-->", vendored);
      },
    },
  };
}

const PROJECT_BANNER = "/* SPDX-License-Identifier: AGPL-3.0-or-later */";

/**
 * The build both apps share: fixed file names, one stylesheet, a licence list.
 * An app that loads its pages as separate chunks (``split``) keeps every
 * bundled package in one ``vendor.js``, which THIRD_PARTY_LICENSES.md names,
 * and every other chunk is the project's own code and says so in its first
 * line; chunk names follow the pages, so a list of them would drift.
 */
export function modernBuild({ outDir, entry, split = false }) {
  const chunking = split
    ? {
        manualChunks: (id) => (id.includes("/node_modules/") ? "vendor" : undefined),
        postBanner: (chunk) => (chunk.name === "vendor" ? "" : PROJECT_BANNER),
      }
    : {};
  return {
    outDir,
    emptyOutDir: true,
    assetsDir: "assets",
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    reportCompressedSize: false,
    license: { fileName: "licenses.json" },
    rollupOptions: {
      output: {
        entryFileNames: `assets/${entry}.js`,
        chunkFileNames: "assets/[name].js",
        ...chunking,
        assetFileNames: (info) => {
          const file = (info.names && info.names[0]) || info.name || "";
          if (file.endsWith(".css")) return `assets/${entry}.css`;
          return "assets/[name][extname]";
        },
      },
    },
  };
}
