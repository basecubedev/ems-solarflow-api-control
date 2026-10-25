// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the consoles' assistants share: the states a step can be in and the
 * mark each wears, the per-browser choice whether a console opens on its
 * assistant, and how an opened console is told from a page asked for by its
 * address. Pure, so each app's node tests can walk it.
 */

/**
 * A step is done, still to do, waits for an earlier one, or cannot be told
 * yet; a last step that leads somewhere, as to a dashboard, is ready, or off
 * while the settings switch it off.
 */
export const ASSISTANT_STATES = Object.freeze(["done", "todo", "later", "unknown", "ready", "off"]);

/** Each state's mark beside its word. */
export const STATE_LOOKS = Object.freeze({
  done: { icon: "circle-check", tone: "c-ok" },
  todo: { icon: "circle", tone: "c-acc" },
  later: { icon: "clock", tone: "c-idle" },
  unknown: { icon: "circle-help", tone: "c-idle" },
  ready: { icon: "arrow-up-right", tone: "c-ok" },
  off: { icon: "circle-minus", tone: "c-idle" },
});

/** A step that waits, cannot be told or is off wears no colour of its own. */
export function markTone(step) {
  return ["later", "unknown", "off"].includes(step?.state) ? "c-idle" : step?.tone || "c-idle";
}

/** The per-browser choice whether the console opens here; "off" is stored only once the reader unticks it. */
export const WELCOME_KEY = "ems-modern.welcome";
export const WELCOME_CHOICES = Object.freeze(["on", "off"]);

/** An address with no page in it: the console was opened, not a page of it. */
export function bareAddress(hash) {
  return /^#?\/?$/.test(String(hash ?? ""));
}

export function stepProgress(steps) {
  return { done: steps.filter((step) => step.state === "done").length, total: steps.length };
}
