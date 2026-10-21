// SPDX-License-Identifier: AGPL-3.0-or-later
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { ROOT, modernBuild, modernPage } from "../shared/vite/page.js";

const HERE = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: HERE,
  base: "/modern/",
  publicDir: false,
  plugins: [svelte(), modernPage({ name: "ems-admin-modern-page", icons: resolve(HERE, "src/icons.json") })],
  build: modernBuild({ outDir: resolve(ROOT, "admin/static/modern"), entry: "admin", split: true }),
  server: {
    fs: { allow: [ROOT] },
    proxy: { "/api": { target: "http://127.0.0.1:8123", changeOrigin: false } },
  },
});
