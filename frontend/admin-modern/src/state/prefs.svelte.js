// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The shared appearance, whether the console opens on the assistant, and the
 * way to the deprecated Classic console.
 */
export * from "../../../shared/state/prefs.svelte.js";
export { welcome, setWelcome } from "../../../shared/assistant/welcome.svelte.js";

/**
 * Classic answers under /classic/ and is only ever opened on purpose: nothing
 * is remembered, the root stays Modern. The page opens the same page there.
 */
export function chooseClassic(classicPage) {
  globalThis.location.assign(`/classic/${classicPage ? `#${classicPage}` : ""}`);
}
