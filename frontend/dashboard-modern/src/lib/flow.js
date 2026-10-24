// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Energy-flow semantics shared with Classic: the 8 W / 3 W activity hysteresis,
 * the scale ladder that follows the installation, and thickness as magnitude.
 */

export const FLOW_ACTIVATE_W = 8;
export const FLOW_DEACTIVATE_W = 3;
export const FLOW_SCALE_LADDER = Object.freeze([250, 500, 1000, 2000, 3000, 5000, 8000, 12000]);
export const RIBBON_IDLE_PX = 1.5;
export const RIBBON_MIN_PX = 2;
export const RIBBON_MAX_PX = 6.5;

function magnitude(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.abs(number) : 0;
}

/** Remembers which flows are running, so a flow hovering at the threshold does not flicker. */
export function createFlowActivity() {
  const active = new Map();
  return {
    isActive(key, watts) {
      const value = magnitude(watts);
      const was = Boolean(active.get(key));
      const now = was ? value > FLOW_DEACTIVATE_W : value >= FLOW_ACTIVATE_W;
      active.set(key, now);
      return now;
    },
    forget(keys) {
      const keep = new Set(keys);
      for (const key of active.keys()) if (!keep.has(key)) active.delete(key);
    },
  };
}

export const HOUSE_LINE_TAKEOVER_RATIO = 2;

/**
 * Classic's speed buckets: a stronger flow moves faster and its dashes shine
 * brighter. ``speed`` is in pixels a second, Classic's 52 px dash period in
 * 1.70, 1.38 and 1.10 s; ``alpha`` is the dashes' opacity and ``glow`` the
 * strength of the halo around the pipe.
 */
export const FLOW_BUCKETS = Object.freeze({
  low: Object.freeze({ alpha: 0.48, glow: 0.16, speed: 31 }),
  medium: Object.freeze({ alpha: 0.68, glow: 0.26, speed: 38 }),
  high: Object.freeze({ alpha: 0.9, glow: 0.4, speed: 47 }),
});
export const FLOW_BUCKET_MEDIUM_W = 150;
export const FLOW_BUCKET_HIGH_W = 600;

export function flowBucket(watts) {
  const value = magnitude(watts);
  if (value >= FLOW_BUCKET_HIGH_W) return "high";
  if (value >= FLOW_BUCKET_MEDIUM_W) return "medium";
  return "low";
}

/**
 * Classic's deviceHouseLine: which way one inverter's house line runs. Each
 * side needs the activity hysteresis, and once a direction shows, the other
 * side must be twice as strong to take it over, so a mixed state does not
 * flip on every frame. A direction older than one snapshot is forgotten.
 */
export function createHouseLines(activity = createFlowActivity()) {
  const lines = new Map();
  return {
    direction(key, outputW, chargeW, sequence) {
      const output = magnitude(outputW);
      const charge = magnitude(chargeW);
      const outputActive = activity.isActive(`device:${key}:house-output`, output);
      const chargeActive = activity.isActive(`device:${key}:house-charge`, charge);
      const previous = lines.get(key);
      const shown = previous && sequence - previous.sequence <= 1 ? previous.direction : "idle";
      let charging = chargeActive;
      if (chargeActive && outputActive) {
        if (shown === "charging") charging = output < charge * HOUSE_LINE_TAKEOVER_RATIO;
        else if (shown === "feeding") charging = charge >= output * HOUSE_LINE_TAKEOVER_RATIO;
        else charging = charge > output;
      }
      lines.set(key, { direction: charging ? "charging" : outputActive ? "feeding" : "idle", sequence });
      return { charging, active: charging || outputActive, watts: charging ? charge : output };
    },
  };
}

export const FLEET_LINE_BAND_SHARE = 0.05;

/**
 * The inverters' line to the home in the System view: what they feed minus
 * what they charge, negative when charging draws from the home. A difference
 * shows once it reaches 5 % of the larger side (and the 8 W any flow needs),
 * and a shown line keeps showing until it falls below the larger of half that
 * band and the 3 W at which any flow stops. A difference on the other side
 * inside the band leaves the line at rest, so a fleet charging about as much
 * as it feeds neither flips on every snapshot nor shows a flow that is not
 * there; one beyond the band turns it straight round. A state older than one
 * snapshot is forgotten.
 */
export function createFleetLine() {
  let shown = 0;
  let seen = null;
  return {
    watts(outputW, chargeW, sequence) {
      if (seen === null || sequence - seen > 1) shown = 0;
      seen = sequence;
      const output = magnitude(outputW);
      const charge = magnitude(chargeW);
      const net = output - charge;
      const band = Math.max(output, charge) * FLEET_LINE_BAND_SHARE;
      const on = Math.max(FLOW_ACTIVATE_W, band);
      const off = Math.max(FLOW_DEACTIVATE_W, band / 2);
      if (shown !== 0 && Math.sign(net) === shown && Math.abs(net) >= off) return net;
      shown = Math.abs(net) >= on ? Math.sign(net) : 0;
      return shown === 0 ? 0 : net;
    },
  };
}

/**
 * The bus that joins the inverters' house lines on their way to the home. Each
 * segment carries what the lines beyond it bring toward the junction, and the
 * trunk carries them all: positive feeds the home, negative is AC charging
 * drawn from it. ``above`` and ``below`` list device indexes from the far end.
 */
export function busFlows(flows, { above = [], below = [] } = {}) {
  const segments = {};
  [["above", above], ["below", below]].forEach(([side, order]) => {
    let sum = 0;
    order.forEach((index, k) => {
      sum += flows[index] || 0;
      segments[`bus-${side}${k}`] = sum;
    });
  });
  return { segments, total: flows.reduce((sum, value) => sum + (value || 0), 0) };
}

/** A full scale chosen from the system's own output, with hysteresis so widths do not jump between rungs. */
export function createFlowScale(initial = 1000) {
  let reference = initial;
  return {
    get reference() {
      return reference;
    },
    update(maxWatts) {
      const value = magnitude(maxWatts);
      if (value <= reference && value >= reference * 0.5) return reference;
      let next = FLOW_SCALE_LADDER[FLOW_SCALE_LADDER.length - 1];
      for (const rung of FLOW_SCALE_LADDER) {
        if (value <= rung) {
          next = rung;
          break;
        }
      }
      reference = next;
      return reference;
    },
  };
}

export function ribbonWidth(watts, active, reference) {
  if (!active) return RIBBON_IDLE_PX;
  const share = Math.min(1, magnitude(watts) / (reference || 1000));
  const width = RIBBON_MIN_PX + (RIBBON_MAX_PX - RIBBON_MIN_PX) * share;
  return Math.round(width * 2) / 2;
}

export function batteryDirection(batteryW) {
  const value = Number(batteryW);
  if (!Number.isFinite(value) || value === 0) return "idle";
  return value > 0 ? "charging" : "discharging";
}

export function gridDirection(gridW, valid = true) {
  if (valid === false) return "offline";
  const value = Number(gridW);
  if (!Number.isFinite(value)) return "offline";
  if (value > FLOW_ACTIVATE_W) return "importing";
  if (value < -FLOW_ACTIVATE_W) return "exporting";
  return "balanced";
}
