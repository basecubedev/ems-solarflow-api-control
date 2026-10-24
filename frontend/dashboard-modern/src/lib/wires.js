// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Geometry of the energy-flow wires: orthogonal polylines between measured
 * node boxes, a rounded SVG path for the track, and straight runs for the
 * moving dashes. Pure, so the layout can be tested without a browser.
 */

export function box(rect, origin) {
  const l = rect.left - origin.left;
  const t = rect.top - origin.top;
  return { l, t, r: l + rect.width, b: t + rect.height, cx: l + rect.width / 2, cy: t + rect.height / 2 };
}

/** Horizontal, vertical, horizontal: the elbow the flow diagrams use. */
export function elbow(x1, y1, x2, y2) {
  if (Math.abs(y2 - y1) < 0.5) return [[x1, y1], [x2, y2]];
  const xm = Math.round((x1 + x2) / 2);
  return [[x1, y1], [xm, y1], [xm, y2], [x2, y2]];
}

export function vertical(x, y1, y2) {
  return [[x, y1], [x, y2]];
}

export const CORNER_RADIUS = 12;

function cornerRadius(points, i, radius) {
  const [px, py] = points[i - 1];
  const [x, y] = points[i];
  const [nx, ny] = points[i + 1];
  return Math.min(radius, Math.hypot(x - px, y - py) / 2, Math.hypot(nx - x, ny - y) / 2);
}

export function roundedPath(points, radius = CORNER_RADIUS) {
  if (!points.length) return "";
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 1; i < points.length; i += 1) {
    const [x, y] = points[i];
    const next = points[i + 1];
    if (!next) {
      d += ` L${x},${y}`;
      break;
    }
    const [px, py] = points[i - 1];
    const inLength = Math.hypot(x - px, y - py);
    const outLength = Math.hypot(next[0] - x, next[1] - y);
    const r = Math.min(radius, inLength / 2, outLength / 2);
    if (r < 0.5) {
      d += ` L${x},${y}`;
      continue;
    }
    const ax = x - ((x - px) / inLength) * r;
    const ay = y - ((y - py) / inLength) * r;
    const bx = x + ((next[0] - x) / outLength) * r;
    const by = y + ((next[1] - y) / outLength) * r;
    d += ` L${ax},${ay} Q${x},${y} ${bx},${by}`;
  }
  return d;
}

/**
 * Straight runs of a polyline, each with its length and angle, for the
 * transform-animated dash layers. A run stops where the track starts to bend,
 * so no dash sits outside a rounded corner. ``before`` is how far along the
 * rounded track the run starts, so the dashes keep their spacing round a
 * corner.
 */
export function runs(points, radius = CORNER_RADIUS) {
  const out = [];
  const last = points.length - 1;
  let travelled = 0;
  for (let i = 1; i < points.length; i += 1) {
    const [x1, y1] = points[i - 1];
    const [x2, y2] = points[i];
    const full = Math.hypot(x2 - x1, y2 - y1);
    if (full < 1) continue;
    const head = i - 1 > 0 ? cornerRadius(points, i - 1, radius) : 0;
    const tail = i < last ? cornerRadius(points, i, radius) : 0;
    const length = full - head - tail;
    if (length >= 1) {
      const ux = (x2 - x1) / full;
      const uy = (y2 - y1) / full;
      out.push({ x: x1 + ux * head, y: y1 + uy * head, length, angle: (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI, before: travelled });
    }
    travelled += Math.max(0, length) + (Math.PI / 2) * tail;
  }
  return out;
}
