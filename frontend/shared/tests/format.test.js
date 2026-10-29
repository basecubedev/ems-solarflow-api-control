// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { createFormat, MISSING } from "../lib/format.js";

test("a value that is not a number renders as a dash, never as zero", () => {
  const f = createFormat("en");
  for (const value of [null, undefined, "", "n/a", Number.NaN]) {
    assert.equal(f.power(value), MISSING);
    assert.equal(f.energy(value), MISSING);
    assert.equal(f.percent(value), MISSING);
    assert.equal(f.money(value), MISSING);
  }
  assert.equal(f.power(0), "0\u00a0W");
});

test("power switches to kilowatts on the rounded figure", () => {
  const f = createFormat("en");
  assert.equal(f.power(999.4), "999\u00a0W");
  assert.equal(f.power(999.6), "1.00\u00a0kW");
  assert.equal(f.power(1850), "1.85\u00a0kW");
  assert.equal(f.power(12500), "12.5\u00a0kW");
  assert.equal(f.power(-5, { signed: true }), "−5\u00a0W");
  assert.equal(f.power(880, { signed: true }), "+880\u00a0W");
});

test("energy under one kilowatt-hour is shown in watt-hours", () => {
  const f = createFormat("en");
  assert.equal(f.energy(0.078), "78\u00a0Wh");
  assert.equal(f.energy(0.9996), "1.0\u00a0kWh");
  assert.equal(f.energy(3.24), "3.2\u00a0kWh");
  assert.equal(f.energy(2070), "2,070\u00a0kWh");
  assert.equal(f.energy(-0.2, { signed: true }), "−200\u00a0Wh");
});

test("numbers follow the selected language", () => {
  assert.equal(createFormat("de").power(1850), "1,85\u00a0kW");
  assert.equal(createFormat("nl").energy(3.24), "3,2\u00a0kWh");
  assert.match(createFormat("fr").power(1850), /^1,85\skW$/);
  assert.match(createFormat("de").money(1.12, "EUR"), /1,12\s€/);
});

test("a calendar date does not shift with the time zone", () => {
  const f = createFormat("en");
  assert.equal(f.date("2026-06-14"), "14 June 2026");
  assert.equal(f.date("2026-06-14", { short: true }), "14 Jun");
});

test("relative times use the largest unit that keeps the number small", () => {
  const now = Date.UTC(2026, 9, 21, 12, 0, 0);
  const en = createFormat("en");
  assert.equal(en.relative(now - 20 * 1000, now), "now");
  assert.equal(en.relative(now - 5 * 60 * 1000, now), "5 minutes ago");
  assert.equal(en.relative(now - 3 * 3600 * 1000, now), "3 hours ago");
  assert.equal(en.relative(now - 26 * 3600 * 1000, now), "yesterday");
  assert.equal(en.relative(now - 40 * 86400 * 1000, now), "last month");
  assert.equal(en.relative(new Date(now - 400 * 86400 * 1000).toISOString(), now), "last year");
  assert.equal(createFormat("de").relative(now - 3 * 3600 * 1000, now), "vor 3 Stunden");
  assert.equal(en.relative(null, now), MISSING);
  assert.equal(en.relative("not a date", now), MISSING);
});

test("file sizes take the largest unit under 1024 and the reader's decimal mark", () => {
  const de = createFormat("de");
  assert.equal(de.bytes(512), "512\u00a0B");
  assert.equal(de.bytes(1536), "1,5\u00a0KB");
  assert.equal(de.bytes(48213), "47,1\u00a0KB");
  assert.equal(createFormat("en").bytes(5 * 1024 * 1024), "5.0\u00a0MB");
  assert.equal(de.bytes(null), MISSING);
  assert.equal(de.bytes(-1), MISSING);
});

test("a moment names its year once it is not this year's", () => {
  const f = createFormat("en");
  const now = new Date();
  const thisYear = new Date(now.getFullYear(), 2, 4, 9, 5);
  const lastYear = new Date(now.getFullYear() - 1, 2, 4, 9, 5);
  assert.ok(!f.dateTime(thisYear).includes(String(now.getFullYear())));
  assert.ok(f.dateTime(lastYear).includes(String(now.getFullYear() - 1)));
});

test("a moment asked for with its seconds has them, this year and any other", () => {
  const f = createFormat("en");
  const now = new Date();
  const thisYear = new Date(now.getFullYear(), 2, 4, 9, 5, 37);
  const lastYear = new Date(now.getFullYear() - 1, 2, 4, 9, 5, 37);
  assert.ok(f.dateTime(thisYear, { seconds: true }).includes("09:05:37"));
  assert.ok(!f.dateTime(thisYear, { seconds: true }).includes(String(now.getFullYear())));
  assert.ok(f.dateTime(lastYear, { seconds: true }).includes("09:05:37"));
  assert.ok(f.dateTime(lastYear, { seconds: true }).includes(String(now.getFullYear() - 1)));
  assert.ok(!f.dateTime(thisYear).includes(":37"));
});

test("a moment from today is a clock time, any other has its day", () => {
  const f = createFormat("en");
  const today = new Date();
  today.setHours(9, 5, 0, 0);
  const earlier = new Date(today.getTime() - 3 * 86400000);
  assert.equal(f.when(today), f.time(today));
  assert.equal(f.when(earlier), f.dateTime(earlier));
  assert.equal(f.when(null), MISSING);
});
