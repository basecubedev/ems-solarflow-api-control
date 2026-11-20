// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiError } from "../../shared/lib/api.js";
import { EMS_REFUSALS, emsRefusal } from "../src/lib/ems.js";

const FORWARDING = readFileSync(new URL("../../../admin/ems_forwarding.py", import.meta.url), "utf8");

test("every refusal the forwarding gives is read as one of the three states the pages explain", () => {
  const codes = [...FORWARDING.matchAll(/ForwardingRefusal\(\s*"([a-z_]+)"/g)].map((match) => match[1]);
  assert.ok(codes.length >= 6);
  const ems = codes.filter((code) => code.startsWith("ems_"));
  for (const code of ems) assert.ok(EMS_REFUSALS.includes(emsRefusal(new ApiError(503, { error: code }))), code);
  assert.deepEqual(EMS_REFUSALS.filter((code) => !ems.includes(code)), []);
});

test("an answer the console did not give is read as an EMS that does not answer", () => {
  assert.equal(emsRefusal(new ApiError(502, { error: "something_else" })), "ems_unreachable");
  assert.equal(emsRefusal(new TypeError("Failed to fetch")), "ems_unreachable");
  assert.equal(emsRefusal(new ApiError(503, { error: "ems_config_unreadable" })), "ems_not_configured");
  assert.equal(emsRefusal(new ApiError(503, { error: "ems_dashboard_disabled" })), "ems_dashboard_disabled");
});
