// SPDX-License-Identifier: AGPL-3.0-or-later
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { markSvg } from "./src/brand/mark.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../..");
const OUT_DIR = resolve(ROOT, "dashboard/static/modern");
const ICON_DIR = resolve(ROOT, "node_modules/lucide-static/icons");
const NOTICES = {
  "Inter.LICENSE": resolve(ROOT, "node_modules/@fontsource-variable/inter/LICENSE"),
  "Lucide.LICENSE": resolve(ROOT, "node_modules/lucide-static/LICENSE"),
};

const TOKENS = resolve(HERE, "src/styles/tokens.css");

/**
 * The browser tab's icon: the side menu's mark in the default scheme's
 * colours, read from tokens.css. It is a data: URI, which the dashboard's CSP
 * admits for images, so the server needs no new file type.
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

/**
 * The icons the UI uses, as one inline sprite: no icon font, no request, no
 * innerHTML. Only the drawing elements are taken over, by allow-list rather
 * than by stripping what is not wanted, and an icon that holds anything else
 * fails the build instead of reaching the page.
 */
function iconSprite() {
  const names = JSON.parse(readFileSync(resolve(HERE, "src/icons.json"), "utf8"));
  const symbols = names.map((name) => {
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
 * uPlot is the vendored copy Classic already serves and the inventory hash-pins,
 * so it is referenced, never bundled; the tags are added after Vite has
 * resolved the page so it does not try to. The font and the icon paths are
 * copied, not bundled, so their licence texts ship beside them as files, the
 * way uPlot.LICENSE does; Vite's licenses.json covers bundled packages only.
 */
function emsPage() {
  return {
    name: "ems-modern-page",
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
          .replace("<!--icons-->", iconSprite())
          .replace(
            "<!--vendored-->",
            '<link rel="stylesheet" href="/uPlot.min.css">\n    <script src="/uPlot.iife.min.js"></script>',
          );
      },
    },
  };
}

export default defineConfig({
  root: HERE,
  base: "/modern/",
  publicDir: false,
  plugins: [svelte(), emsPage()],
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    assetsDir: "assets",
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    reportCompressedSize: false,
    license: { fileName: "licenses.json" },
    rollupOptions: {
      output: {
        entryFileNames: "assets/modern.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: (info) => {
          const name = (info.names && info.names[0]) || info.name || "";
          if (name.endsWith(".css")) return "assets/modern.css";
          return "assets/[name][extname]";
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8767", changeOrigin: false },
      "/uPlot.iife.min.js": "http://127.0.0.1:8767",
      "/uPlot.min.css": "http://127.0.0.1:8767",
    },
  },
});
