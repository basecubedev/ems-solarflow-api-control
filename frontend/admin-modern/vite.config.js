// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { ROOT, modernBuild, modernPage } from "../shared/vite/page.js";

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * fallback.js runs first and sends a browser too old for the app to the page
 * that says so. The console carries the dashboard's pages, so it loads the
 * dashboard's vendored uPlot as the dashboard does, from the root, and draws
 * the dashboard's icons as well as its own.
 */
const VENDORED = '<script src="/fallback.js"></script>\n    <link rel="stylesheet" href="/uPlot.min.css">\n    <script src="/uPlot.iife.min.js"></script>';
const ICONS = [resolve(HERE, "src/icons.json"), resolve(ROOT, "frontend/dashboard-modern/src/icons.json")];

export default defineConfig({
  root: HERE,
  base: "/",
  publicDir: false,
  plugins: [svelte(), modernPage({ name: "ems-admin-modern-page", icons: ICONS, vendored: VENDORED, unsupported: true })],
  build: modernBuild({ outDir: resolve(ROOT, "admin/static/modern"), entry: "admin", split: true }),
  server: {
    fs: { allow: [ROOT] },
    proxy: { "/api": { target: "http://127.0.0.1:8123", changeOrigin: false } },
  },
});
