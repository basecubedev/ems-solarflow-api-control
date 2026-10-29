// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Moves one run of flow dashes with a transform animation from
 * element.animate() and literal keyframes. Transform stays on the compositor,
 * and literal keyframes cost no style recalculation per frame — the two
 * findings of the dashboard performance studies that Classic's flow renderer
 * is built on. The dashes are Classic's, with round ends, scaled to Modern's
 * shorter and finer wires: 18 px long and 12 px apart. They are drawn into the layer's
 * own background, so the layer is painted once and then only moved.
 */

export const DASH_PX = 18;
export const DASH_GAP_PX = 12;
export const DASH_PERIOD_PX = DASH_PX + DASH_GAP_PX;
/** animation_mode "reduced" keeps the flow moving at half speed, as Classic slows its pipes from about 1.5 s to 3 s a cycle. */
const REDUCED_FACTOR = 2;

/** The dash pattern for a pipe of the given thickness, in the colour of the run's --c. */
export function dashTexture(thickness) {
  const r = thickness / 2;
  const edge = Math.max(0, r - 0.75);
  const cap = (x) => `radial-gradient(circle at ${x}px 50%, var(--c) ${edge}px, transparent ${r}px)`;
  const body = `linear-gradient(90deg, transparent ${r}px, var(--c) ${r}px ${DASH_PX - r}px, transparent ${DASH_PX - r}px)`;
  return { image: `${cap(r)}, ${cap(DASH_PX - r)}, ${body}`, size: `${DASH_PERIOD_PX}px 100%` };
}

/** Where the pattern starts in a run that begins ``before`` pixels along its pipe. */
export function dashPhase(before) {
  return `${-(before % DASH_PERIOD_PX)}px 0`;
}

function periodsPerSecond(params) {
  return params.speed / DASH_PERIOD_PX / (params.slow ? REDUCED_FACTOR : 1);
}

function retime(animation, rate) {
  if (typeof animation.updatePlaybackRate === "function") animation.updatePlaybackRate(rate);
  else animation.playbackRate = rate;
}

export function flowDashes(node, params) {
  let animation = null;
  let current = {};

  function apply(next) {
    const restart = next.active !== current.active || next.reverse !== current.reverse || next.animate !== current.animate;
    const faster = next.speed !== current.speed || next.slow !== current.slow;
    current = { ...next };
    if (!restart) {
      if (faster && animation) retime(animation, periodsPerSecond(next));
      return;
    }
    animation?.cancel();
    animation = null;
    if (!next.active || !next.animate || typeof node.animate !== "function") return;
    const from = next.reverse ? "translateX(0px)" : `translateX(-${DASH_PERIOD_PX}px)`;
    const to = next.reverse ? `translateX(-${DASH_PERIOD_PX}px)` : "translateX(0px)";
    animation = node.animate([{ transform: from }, { transform: to }], { duration: 1000, iterations: Infinity, easing: "linear" });
    animation.playbackRate = periodsPerSecond(next);
  }

  apply(params);
  return {
    update: apply,
    destroy() {
      animation?.cancel();
    },
  };
}
