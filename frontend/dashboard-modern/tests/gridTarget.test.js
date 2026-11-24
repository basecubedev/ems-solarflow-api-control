// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { gridTargetMeaning, gridTargetParts, gridTargetText, gridTargetValue } from "../src/lib/gridTarget.js";

test("a stored target shows as a direction and positive watts", () => {
  assert.deepEqual(gridTargetParts(10, "export"), { direction: "import", watts: 10 });
  assert.deepEqual(gridTargetParts(-25, "import"), { direction: "export", watts: 25 });
  assert.deepEqual(gridTargetParts("-25", "import"), { direction: "export", watts: 25 });
});

test("0 W and an empty field keep the direction last chosen", () => {
  assert.deepEqual(gridTargetParts(0, "export"), { direction: "export", watts: 0 });
  assert.deepEqual(gridTargetParts("", "export"), { direction: "export", watts: "" });
  assert.deepEqual(gridTargetParts(null, "import"), { direction: "import", watts: "" });
});

test("a direction and watts give the signed value, export negative", () => {
  assert.equal(gridTargetValue("import", "10"), 10);
  assert.equal(gridTargetValue("export", "10"), -10);
  assert.equal(gridTargetValue("export", "-10"), -10);
  assert.equal(gridTargetValue("import", "-10"), 10);
  assert.equal(gridTargetValue("export", ""), "");
});

test("switching the direction keeps the watts", () => {
  const { watts } = gridTargetParts(-40, "import");
  assert.equal(gridTargetValue("import", watts), 40);
});

test("the meaning names the phase and the direction, and 0 W has its own", () => {
  assert.deepEqual(gridTargetMeaning("discharge", 10), { key: "gridTarget.meaning.discharge.import", watts: 10 });
  assert.deepEqual(gridTargetMeaning("discharge", -10), { key: "gridTarget.meaning.discharge.export", watts: 10 });
  assert.deepEqual(gridTargetMeaning("charge", 15), { key: "gridTarget.meaning.charge.import", watts: 15 });
  assert.deepEqual(gridTargetMeaning("charge", -15), { key: "gridTarget.meaning.charge.export", watts: 15 });
  assert.deepEqual(gridTargetMeaning("charge", 0), { key: "gridTarget.meaning.zero", watts: 0 });
});

test("an empty field means its default", () => {
  assert.deepEqual(gridTargetMeaning("charge", "", -5), { key: "gridTarget.meaning.charge.export", watts: 5 });
  assert.deepEqual(gridTargetMeaning("discharge", ""), { key: "gridTarget.meaning.zero", watts: 0 });
});

test("an active target reads as import or export in words", () => {
  const t = (key, params) => `${key}:${params.w}`;
  const f = { power: (value) => `${value} W` };
  assert.equal(gridTargetText(t, f, 10), "gridTarget.importValue:10 W");
  assert.equal(gridTargetText(t, f, -10), "gridTarget.exportValue:10 W");
});
