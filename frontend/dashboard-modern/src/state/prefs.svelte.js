// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The dashboard's own choices beside the shared appearance: how the energy
 * flow is drawn, and the browser's choice between Classic and Modern.
 */
import { readStored, writeStored } from "../../../shared/state/prefs.svelte.js";

export * from "../../../shared/state/prefs.svelte.js";

export const FLOW_MODES = Object.freeze(["system", "devices"]);
const FLOW_KEY = "ems-modern.flow";
export const UI_CHOICE_KEY = "ems-dashboard-ui";

class FlowPref {
  mode = $state(readStored(FLOW_KEY, FLOW_MODES, "system"));
}

export const flowPref = new FlowPref();

export function setFlowMode(value) {
  if (!FLOW_MODES.includes(value)) return;
  flowPref.mode = value;
  writeStored(FLOW_KEY, value);
}

/**
 * The choice is per browser and Classic's theme.js reads it before its first
 * paint. ?ui=classic carries it even where storage is blocked. The view key is
 * Classic's own, written only to open the same page there.
 */
export function chooseClassic(view) {
  writeStored(UI_CHOICE_KEY, "classic");
  if (view) writeStored("dashboard.flowView", view);
  globalThis.location.assign("/?ui=classic");
}

export function rememberModern() {
  writeStored(UI_CHOICE_KEY, "modern");
}
