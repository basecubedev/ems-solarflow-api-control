// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The shared appearance, whether the console opens on the assistant, and the
 * way to the deprecated Classic console.
 */
import { readStored, writeStored } from "../../../shared/state/prefs.svelte.js";
import { WELCOME_CHOICES, WELCOME_KEY } from "../lib/assistant.js";

export * from "../../../shared/state/prefs.svelte.js";

/** This browser's choice, "on" until the reader unticks it on the assistant page. */
class Welcome {
  choice = $state(readStored(WELCOME_KEY, WELCOME_CHOICES, "on"));
}

export const welcome = new Welcome();

export function setWelcome(show) {
  welcome.choice = show ? "on" : "off";
  writeStored(WELCOME_KEY, welcome.choice);
}

/**
 * Classic answers under /classic/ and is only ever opened on purpose: nothing
 * is remembered, the root stays Modern. The page opens the same page there.
 */
export function chooseClassic(classicPage) {
  globalThis.location.assign(`/classic/${classicPage ? `#${classicPage}` : ""}`);
}
