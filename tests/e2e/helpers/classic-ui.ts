// SPDX-License-Identifier: AGPL-3.0-or-later
import type { BrowserContext } from "@playwright/test";

/** The browser's choice between Classic and Modern (admin/static/admin-theme.js). */
export const ADMIN_UI_KEY = "ems-admin-ui";

/**
 * Modern is the Admin console's default view; the Classic specs choose
 * Classic first, as a reader who prefers it has. The Playwright configs put
 * this storage state in their "use", so every context a spec opens -- the
 * fixture's and browser.newContext()'s alike -- starts in Classic, and a spec
 * that switches later keeps the choice it made. The Modern specs clear it.
 */
export function classicUiStorage(baseURL: string) {
  return {
    cookies: [],
    origins: [{ origin: new URL(baseURL).origin, localStorage: [{ name: ADMIN_UI_KEY, value: "classic" }] }],
  };
}

/** The same choice for a context on another origin than the config's, such as a spec's own Admin process. */
export async function chooseClassicUi(context: BrowserContext) {
  await context.addInitScript((key) => {
    if (window.location.protocol.startsWith("http") && !window.localStorage.getItem(key)) {
      window.localStorage.setItem(key, "classic");
    }
  }, ADMIN_UI_KEY);
}
