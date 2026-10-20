// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { flowDashes, dashTexture, dashPhase, DASH_PX, DASH_PERIOD_PX } from "../src/lib/flowAnim.js";

function fakeNode() {
  const calls = [];
  return {
    calls,
    animate(keyframes, options) {
      const animation = {
        keyframes,
        options,
        cancelled: false,
        playbackRate: 1,
        cancel() { this.cancelled = true; },
        updatePlaybackRate(rate) { this.playbackRate = rate; },
      };
      calls.push(animation);
      return animation;
    },
  };
}

const moving = { active: true, reverse: false, animate: true, slow: false, speed: 47 };

test("the dashes move at the bucket's speed, and animation_mode reduced halves it without a restart", () => {
  const node = fakeNode();
  const action = flowDashes(node, moving);
  assert.equal(node.calls.length, 1);
  assert.equal(node.calls[0].options.duration, 1000);
  assert.equal(node.calls[0].playbackRate, 47 / DASH_PERIOD_PX);
  action.update({ ...moving, slow: true });
  assert.equal(node.calls.length, 1);
  assert.equal(node.calls[0].cancelled, false);
  assert.equal(node.calls[0].playbackRate, 23.5 / DASH_PERIOD_PX);
});

test("a flow that changes bucket changes speed where it is instead of starting over", () => {
  const node = fakeNode();
  const action = flowDashes(node, moving);
  action.update({ ...moving, speed: 31 });
  assert.equal(node.calls.length, 1);
  assert.equal(node.calls[0].playbackRate, 31 / DASH_PERIOD_PX);
});

test("a flow that starts in reduced motion moves at half speed from its first frame", () => {
  const node = fakeNode();
  flowDashes(node, { ...moving, slow: true });
  assert.equal(node.calls[0].playbackRate, 23.5 / DASH_PERIOD_PX);
});

test("a flow that turns round starts again in the other direction", () => {
  const node = fakeNode();
  const action = flowDashes(node, moving);
  action.update({ ...moving, reverse: true });
  assert.equal(node.calls.length, 2);
  assert.equal(node.calls[0].cancelled, true);
  assert.deepEqual(node.calls[1].keyframes, [{ transform: "translateX(0px)" }, { transform: `translateX(-${DASH_PERIOD_PX}px)` }]);
});

test("an idle flow or motion switched off runs no animation", () => {
  const node = fakeNode();
  const action = flowDashes(node, { ...moving, active: false });
  assert.equal(node.calls.length, 0);
  action.update({ ...moving, animate: false });
  assert.equal(node.calls.length, 0);
});

test("a dash has round ends the size of the pipe and repeats every period", () => {
  const { image, size } = dashTexture(6);
  assert.equal(size, `${DASH_PERIOD_PX}px 100%`);
  assert.match(image, /radial-gradient\(circle at 3px 50%, var\(--c\) 2\.25px, transparent 3px\)/);
  assert.match(image, new RegExp(`radial-gradient\\(circle at ${DASH_PX - 3}px 50%`));
  assert.match(image, new RegExp(`linear-gradient\\(90deg, transparent 3px, var\\(--c\\) 3px ${DASH_PX - 3}px, transparent ${DASH_PX - 3}px\\)`));
});

test("a run further along its pipe starts the pattern where the run before it left off", () => {
  assert.equal(dashPhase(0), "0px 0");
  assert.equal(dashPhase(DASH_PERIOD_PX + 10), "-10px 0");
});
