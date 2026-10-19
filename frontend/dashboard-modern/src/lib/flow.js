// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Energy-flow semantics shared with Classic: the 8 W / 3 W activity hysteresis,
 * the scale ladder that follows the installation, and thickness as magnitude.
 */

export const FLOW_ACTIVATE_W = 8;
export const FLOW_DEACTIVATE_W = 3;
export const FLOW_SCALE_LADDER = Object.freeze([250, 500, 1000, 2000, 3000, 5000, 8000, 12000]);
export const RIBBON_IDLE_PX = 1.5;
export const RIBBON_MIN_PX = 3;
export const RIBBON_MAX_PX = 11;

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
      return { charging, watts: charging ? charge : output };
    },
  };
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

/** What the AC charger takes off the meter has its own grid → inverter ribbon, so it is not drawn twice. */
export function gridHomeWatts(gridW, chargeW) {
  const grid = Number(gridW) || 0;
  if (grid <= 0) return Math.abs(grid);
  return Math.max(0, grid - magnitude(chargeW));
}
