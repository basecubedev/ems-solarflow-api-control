// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * EMS SolarFlow's mark: the inverter's bolt in a ring of the four energies,
 * each quarter where the flow diagram puts it -- solar top left, home top
 * right, grid bottom right, battery bottom left. One drawing for the side
 * menu, the phone's top bar and the browser tab; the tab's copy is built from
 * it at build time with the colours of tokens.css.
 */

export const MARK_VIEWBOX = "0 0 36 36";
export const MARK_RING = Object.freeze([
  Object.freeze({ token: "--output", d: "M18 7A11 11 0 0 1 29 18" }),
  Object.freeze({ token: "--grid", d: "M29 18A11 11 0 0 1 18 29" }),
  Object.freeze({ token: "--battery", d: "M18 29A11 11 0 0 1 7 18" }),
  Object.freeze({ token: "--pv", d: "M7 18A11 11 0 0 1 18 7" }),
]);
/** Each quarter stops short of both ends, so the four read as four flows. */
export const MARK_ARC = Object.freeze({ width: 3, length: 100, dash: "70 100", offset: -15 });
export const MARK_BOLT = "M19.4 11.5L14.2 19.2H18.2L16.6 24.5L21.8 16.8H17.8Z";

/** The mark as a standalone SVG on its own tile, for the browser tab. */
export function markSvg(colors) {
  const arcs = MARK_RING.map(
    (arc) =>
      `<path d="${arc.d}" stroke="${colors[arc.token]}" pathLength="${MARK_ARC.length}" stroke-dasharray="${MARK_ARC.dash}" stroke-dashoffset="${MARK_ARC.offset}"/>`,
  ).join("");
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${MARK_VIEWBOX}">` +
    `<rect x="0.5" y="0.5" width="35" height="35" rx="10.5" fill="${colors.tile}" stroke="${colors.line}"/>` +
    `<g fill="none" stroke-width="${MARK_ARC.width}" stroke-linecap="round">${arcs}</g>` +
    `<path d="${MARK_BOLT}" fill="${colors.bolt}"/></svg>`
  );
}
