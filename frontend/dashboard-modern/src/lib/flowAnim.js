// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Moves one run of flow dots with a transform animation from element.animate()
 * and literal keyframes. Transform stays on the compositor, and literal
 * keyframes cost no style recalculation per frame — the two findings of the
 * dashboard performance studies that Classic's flow renderer is built on.
 */

export const DOT_PERIOD_PX = 20;
const PIXELS_PER_SECOND = 26;
/** animation_mode "reduced" keeps the flow moving at half speed, as Classic slows its pipes from about 1.5 s to 3 s a cycle. */
const REDUCED_FACTOR = 2;

export function flowDots(node, params) {
  let animation = null;
  let current = {};

  function apply(next) {
    const changed =
      next.active !== current.active || next.reverse !== current.reverse || next.animate !== current.animate || next.slow !== current.slow;
    current = { ...next };
    if (!changed) return;
    animation?.cancel();
    animation = null;
    if (!next.active || !next.animate || typeof node.animate !== "function") return;
    const from = next.reverse ? "translateX(0px)" : `translateX(-${DOT_PERIOD_PX}px)`;
    const to = next.reverse ? `translateX(-${DOT_PERIOD_PX}px)` : "translateX(0px)";
    animation = node.animate([{ transform: from }, { transform: to }], {
      duration: (DOT_PERIOD_PX / PIXELS_PER_SECOND) * 1000 * (next.slow ? REDUCED_FACTOR : 1),
      iterations: Infinity,
      easing: "linear",
    });
  }

  apply(params);
  return {
    update: apply,
    destroy() {
      animation?.cancel();
    },
  };
}
