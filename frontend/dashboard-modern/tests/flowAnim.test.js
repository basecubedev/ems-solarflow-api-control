// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { flowDots } from "../src/lib/flowAnim.js";

function fakeNode() {
  const calls = [];
  return {
    calls,
    animate(keyframes, options) {
      const animation = { options, cancelled: false, cancel() { this.cancelled = true; } };
      calls.push(animation);
      return animation;
    },
  };
}

test("animation_mode reduced moves the dots at half speed and switching it restarts them", () => {
  const node = fakeNode();
  const action = flowDots(node, { active: true, reverse: false, animate: true, slow: false });
  const normal = node.calls[0].options.duration;
  action.update({ active: true, reverse: false, animate: true, slow: true });
  assert.equal(node.calls.length, 2);
  assert.equal(node.calls[0].cancelled, true);
  assert.equal(node.calls[1].options.duration, normal * 2);
  action.update({ active: true, reverse: false, animate: true, slow: true });
  assert.equal(node.calls.length, 2);
});

test("an idle flow or motion switched off runs no animation", () => {
  const node = fakeNode();
  const action = flowDots(node, { active: false, reverse: false, animate: true, slow: false });
  assert.equal(node.calls.length, 0);
  action.update({ active: true, reverse: false, animate: false, slow: false });
  assert.equal(node.calls.length, 0);
});
