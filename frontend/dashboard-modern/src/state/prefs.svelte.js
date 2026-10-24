// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The dashboard's own choices beside the shared appearance: how the energy
 * flow is drawn, and the way to the deprecated Classic dashboard.
 */
import { readStored, writeStored } from "../../../shared/state/prefs.svelte.js";

export * from "../../../shared/state/prefs.svelte.js";

export const FLOW_MODES = Object.freeze(["system", "devices"]);
const FLOW_KEY = "ems-modern.flow";

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
 * Classic answers under /classic/ and is only ever opened on purpose: nothing
 * is remembered, the root stays Modern. The view key is Classic's own,
 * written only to open the same page there.
 */
export function chooseClassic(view) {
  if (view) writeStored("dashboard.flowView", view);
  globalThis.location.assign("/classic/");
}
