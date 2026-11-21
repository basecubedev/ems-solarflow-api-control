// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { consoleTarget } from "../src/lib/console.js";

const at = (protocol, hostname, hash = "") => ({ protocol, hostname, hash });

test("the console is the same page on its port, on the host this browser used", () => {
  assert.deepEqual(consoleTarget({ port: 8090, https_port: null }, at("http:", "ems-solarflow.local", "#/energy")), { href: "http://ems-solarflow.local:8090/#/energy", automatic: true });
  assert.deepEqual(consoleTarget({ port: 8090, https_port: 8091 }, at("https:", "192.168.1.20")), { href: "https://192.168.1.20:8091/", automatic: true });
  assert.equal(consoleTarget({ port: 8090 }, at("http:", "fd00::1")).href, "http://[fd00::1]:8090/");
});

test("a console without the browser's scheme is offered over the other one, never opened by itself", () => {
  assert.deepEqual(consoleTarget({ port: 8090, https_port: null }, at("https:", "pi")), { href: "http://pi:8090/", automatic: false });
});

test("an address the console does not have, or a hash that names no page, gives nothing extra", () => {
  assert.equal(consoleTarget(null, at("http:", "pi")), null);
  assert.equal(consoleTarget({ port: "8090" }, at("http:", "pi")), null);
  assert.equal(consoleTarget({ port: 8090 }, at("http:", "")), null);
  assert.equal(consoleTarget({ port: 8090 }, at("http:", "pi", "#/x?y=<b>")).href, "http://pi:8090/");
});
