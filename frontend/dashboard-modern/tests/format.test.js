// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { createFormat, MISSING } from "../src/lib/format.js";

test("a value that is not a number renders as a dash, never as zero", () => {
  const f = createFormat("en");
  for (const value of [null, undefined, "", "n/a", Number.NaN]) {
    assert.equal(f.power(value), MISSING);
    assert.equal(f.energy(value), MISSING);
    assert.equal(f.percent(value), MISSING);
    assert.equal(f.money(value), MISSING);
  }
  assert.equal(f.power(0), "0 W");
});

test("power switches to kilowatts on the rounded figure", () => {
  const f = createFormat("en");
  assert.equal(f.power(999.4), "999 W");
  assert.equal(f.power(999.6), "1.00 kW");
  assert.equal(f.power(1850), "1.85 kW");
  assert.equal(f.power(12500), "12.5 kW");
  assert.equal(f.power(-5, { signed: true }), "−5 W");
  assert.equal(f.power(880, { signed: true }), "+880 W");
});

test("energy under one kilowatt-hour is shown in watt-hours", () => {
  const f = createFormat("en");
  assert.equal(f.energy(0.078), "78 Wh");
  assert.equal(f.energy(0.9996), "1.0 kWh");
  assert.equal(f.energy(3.24), "3.2 kWh");
  assert.equal(f.energy(2070), "2,070 kWh");
  assert.equal(f.energy(-0.2, { signed: true }), "−200 Wh");
});

test("numbers follow the selected language", () => {
  assert.equal(createFormat("de").power(1850), "1,85 kW");
  assert.equal(createFormat("nl").energy(3.24), "3,2 kWh");
  assert.match(createFormat("fr").power(1850), /^1,85\skW$/);
  assert.match(createFormat("de").money(1.12, "EUR"), /1,12\s€/);
});

test("a calendar date does not shift with the time zone", () => {
  const f = createFormat("en");
  assert.equal(f.date("2026-06-14"), "14 June 2026");
  assert.equal(f.date("2026-06-14", { short: true }), "14 Jun");
});
