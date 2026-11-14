// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { unreadableInput } from "../lib/forms.js";

test("a form's number the browser could not read is found, and a readable form has none", () => {
  const typo = { name: "port", validity: { badInput: true } };
  const form = (inputs) => ({ querySelectorAll: () => inputs });
  assert.equal(unreadableInput(form([{ validity: { badInput: false } }, typo])), typo);
  assert.equal(unreadableInput(form([{ validity: { badInput: false } }])), null);
  assert.equal(unreadableInput(null), null);
});
