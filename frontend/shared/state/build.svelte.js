// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The build file the image wrote beside the bundle, read once per page load
 * and shared by the menu's version line and the About page.
 */
import { BUILD_FILE, buildFacts } from "../lib/about.js";

class Build {
  facts = $state.raw(buildFacts(null));
  loaded = $state(false);
}

export const build = new Build();

let reading = null;

/**
 * Reads the build file the first time it is asked for; a missing or unreadable
 * one names no build, and the next page that asks reads it again.
 */
export function loadBuild() {
  reading ??= fetch(BUILD_FILE, { credentials: "same-origin", cache: "no-store" })
    .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
    .then(
      (value) => {
        build.facts = buildFacts(value);
        build.loaded = true;
      },
      () => {
        reading = null;
      },
    );
  return reading;
}
