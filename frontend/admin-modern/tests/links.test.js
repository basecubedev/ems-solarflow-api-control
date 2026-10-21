// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { dashboardHref } from "../src/lib/links.js";

test("the dashboard is reached on the host this browser used, with the overview's scheme and port", () => {
  assert.equal(dashboardHref("http://localhost:8080", { hostname: "ems-solarflow.local" }), "http://ems-solarflow.local:8080/");
  assert.equal(dashboardHref("https://localhost:8443", { hostname: "192.168.1.20" }), "https://192.168.1.20:8443/");
  assert.equal(dashboardHref("http://localhost", { hostname: "pi" }), "http://pi/");
  assert.equal(dashboardHref("http://localhost:8080", { hostname: "[fd00::1]" }), "http://[fd00::1]:8080/");
  assert.equal(dashboardHref("http://localhost:8080", { hostname: "fd00::1" }), "http://[fd00::1]:8080/");
});

test("anything that is not an http address gives no link", () => {
  for (const value of [null, "", "javascript:alert(1)", "ftp://localhost:21", "not a url", 42]) {
    assert.equal(dashboardHref(value, { hostname: "pi" }), null, String(value));
  }
  assert.equal(dashboardHref("http://localhost:8080", {}), null);
});
