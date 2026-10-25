// SPDX-License-Identifier: AGPL-3.0-or-later
import { readStored, writeStored } from "../state/prefs.svelte.js";
import { WELCOME_CHOICES, WELCOME_KEY } from "./assistant.js";

/** This browser's choice, "on" until the reader unticks it on the assistant page. */
class Welcome {
  choice = $state(readStored(WELCOME_KEY, WELCOME_CHOICES, "on"));
}

export const welcome = new Welcome();

export function setWelcome(show) {
  welcome.choice = show ? "on" : "off";
  writeStored(WELCOME_KEY, welcome.choice);
}
