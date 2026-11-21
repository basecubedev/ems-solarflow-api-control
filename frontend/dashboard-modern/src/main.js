// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import "../../shared/styles/tokens.css";
import "../../shared/styles/base.css";
import { mount } from "svelte";
import App from "./App.svelte";
import Signpost from "./app/Signpost.svelte";
import { getJson, watchRefusals } from "../../shared/lib/api.js";
import { applyDocumentPrefs, watchScreenWidth, watchSystemMotion } from "./state/prefs.svelte.js";
import { startLive } from "./state/live.svelte.js";
import { setConsoleRecord } from "./state/console.svelte.js";
import { loadAuth, watchActivity, watchSession } from "./state/auth.svelte.js";
import { watchHash } from "./app/router.svelte.js";

applyDocumentPrefs();
watchSystemMotion();
watchScreenWidth();

const BOOT_TIMEOUT_MS = 5000;

/**
 * While the Admin console runs, the EMS's own address only shows the way
 * there (``signpost`` in /api/ui-config): then nothing that reads the EMS's
 * data or its session starts. A page that opened while the console was down
 * starts over on the first answer that says the console took over.
 */
async function boot() {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), BOOT_TIMEOUT_MS);
  const config = await getJson("/api/ui-config", { signal: abort.signal }).catch(() => null);
  clearTimeout(timer);
  const target = document.getElementById("app");
  if (config?.signpost && config.admin_console) {
    setConsoleRecord(config.admin_console);
    mount(Signpost, { target });
    return;
  }
  watchRefusals((error) => {
    if (error.status === 410 && error.code === "moved_to_console") globalThis.location.reload();
  });
  watchHash();
  loadAuth();
  watchSession();
  watchActivity();
  startLive(config);
  mount(App, { target });
}

boot();
