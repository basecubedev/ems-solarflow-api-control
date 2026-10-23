// SPDX-License-Identifier: AGPL-3.0-or-later

/** The browser's choice between Classic and Modern (appliance/static/theme.js). */
export const APPLIANCE_UI_KEY = "ems-appliance-ui";

/**
 * Modern is the Appliance Manager's default view; the Classic specs choose
 * Classic first, as a reader who prefers it has. The Playwright config puts
 * this storage state in its "use", so every context a spec opens starts in
 * Classic. The Modern spec clears it.
 */
export function classicUiStorage(baseURL: string) {
  return {
    cookies: [],
    origins: [{ origin: new URL(baseURL).origin, localStorage: [{ name: APPLIANCE_UI_KEY, value: "classic" }] }],
  };
}
