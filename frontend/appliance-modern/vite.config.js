// SPDX-License-Identifier: AGPL-3.0-or-later
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { ROOT, modernBuild, modernPage } from "../shared/vite/page.js";

const HERE = dirname(fileURLToPath(import.meta.url));

/** public/fallback.js runs first and sends ?ui=classic and a browser too old for Modern to /classic/. */
const VENDORED = '<script src="/fallback.js"></script>';

export default defineConfig({
  root: HERE,
  base: "/",
  publicDir: resolve(HERE, "public"),
  plugins: [svelte(), modernPage({ name: "ems-appliance-modern-page", icons: resolve(HERE, "src/icons.json"), vendored: VENDORED })],
  build: modernBuild({ outDir: resolve(ROOT, "appliance/static/modern"), entry: "appliance" }),
  server: {
    fs: { allow: [ROOT] },
    proxy: { "/api": { target: "http://127.0.0.1:8124", changeOrigin: false } },
  },
});
