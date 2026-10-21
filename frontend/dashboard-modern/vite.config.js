// SPDX-License-Identifier: AGPL-3.0-or-later
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { ROOT, modernBuild, modernPage } from "../shared/vite/page.js";

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * uPlot is the vendored copy Classic already serves and the inventory hash-pins,
 * so it is referenced, never bundled; the tags are added after Vite has
 * resolved the page so it does not try to.
 */
const UPLOT = '<link rel="stylesheet" href="/uPlot.min.css">\n    <script src="/uPlot.iife.min.js"></script>';

export default defineConfig({
  root: HERE,
  base: "/modern/",
  publicDir: false,
  plugins: [svelte(), modernPage({ name: "ems-modern-page", icons: resolve(HERE, "src/icons.json"), vendored: UPLOT })],
  build: modernBuild({ outDir: resolve(ROOT, "dashboard/static/modern"), entry: "modern" }),
  server: {
    fs: { allow: [ROOT] },
    proxy: {
      "/api": { target: "http://127.0.0.1:8767", changeOrigin: false },
      "/uPlot.iife.min.js": "http://127.0.0.1:8767",
      "/uPlot.min.css": "http://127.0.0.1:8767",
    },
  },
});
