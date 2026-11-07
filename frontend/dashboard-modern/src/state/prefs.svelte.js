// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The dashboard's own choices beside the shared appearance: how the energy
 * flow is drawn, how much the energy pages tell, the order consumers are
 * listed in, and the way to the deprecated Classic dashboard.
 */
import { readStored, writeStored } from "../../../shared/state/prefs.svelte.js";
import { CONSUMER_ORDERS } from "../lib/consumers.js";

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

export const ENERGY_DETAILS = Object.freeze(["basic", "expert"]);
const ENERGY_DETAIL_KEY = "ems-modern.energyDetail";

class EnergyDetailPref {
  detail = $state(readStored(ENERGY_DETAIL_KEY, ENERGY_DETAILS, "basic"));
}

/** Basic or Expert, one choice for the Energy and the Consumers page. */
export const energyDetail = new EnergyDetailPref();

export function setEnergyDetail(value) {
  if (!ENERGY_DETAILS.includes(value)) return;
  energyDetail.detail = value;
  writeStored(ENERGY_DETAIL_KEY, value);
}

export { CONSUMER_ORDERS };
const CONSUMER_ORDER_KEY = "ems-modern.consumer-order";

class ConsumerOrderPref {
  order = $state(readStored(CONSUMER_ORDER_KEY, CONSUMER_ORDERS, "rank"));
}

/** How the consumers page lists them: the highest first, or by name. */
export const consumerOrder = new ConsumerOrderPref();

export function setConsumerOrder(value) {
  if (!CONSUMER_ORDERS.includes(value)) return;
  consumerOrder.order = value;
  writeStored(CONSUMER_ORDER_KEY, value);
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
