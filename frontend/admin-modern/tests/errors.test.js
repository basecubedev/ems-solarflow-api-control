// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../shared/lib/api.js";
import { errorKey, errorText, errorView, noAnswer } from "../src/lib/errors.js";

const t = (key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key);

test("a code this console knows is said in the reader's language, before the server's English", () => {
  assert.equal(errorText(new ApiError(403, { error: "invalid_password", message: "Wrong" }), t), "error.invalid_password");
  assert.equal(errorText({ error: "the backup password is incorrect" }, t), "error.backup_password_incorrect");
  assert.equal(errorText({ reason: { code: "compose_port_conflict", message: "x" } }, t), "error.compose_port_conflict");
});

test("otherwise the server's sentence, then a code that is a sentence, then the fallback", () => {
  assert.equal(errorText(new ApiError(409, { error: "odd_code", message: "Server says so." }), t), "Server says so.");
  assert.equal(errorText({ error: "something went sideways" }, t), "something went sideways");
  assert.equal(errorText({ error: "odd_code" }, t, "Fallback"), "Fallback");
  assert.equal(errorText(new ApiError(500, null), t), 'error.generic:{"code":"http_500"}');
  assert.equal(errorText(null, t), 'error.generic:{"code":"unknown"}');
});

test("a request that never reached the server gets the console's words, not the browser's", () => {
  assert.equal(errorText(new TypeError("Failed to fetch"), t, "Fallback"), "error.noResponse");
  assert.equal(errorText(new TypeError("NetworkError when attempting to fetch resource."), t), "error.noResponse");
  assert.equal(errorText(new ApiError(502, null), t, "Fallback"), "error.noResponse");
});

test("only a request that got no answer at all has an unknown outcome", () => {
  assert.equal(noAnswer(new TypeError("Failed to fetch")), true);
  assert.equal(noAnswer(new ApiError(500, { error: "boom" })), false);
  assert.equal(noAnswer(new ApiError(409, { error: "busy" })), false);
  assert.equal(noAnswer(Object.assign(new Error("stop"), { name: "AbortError" })), false);
  assert.equal(noAnswer({ error: "busy" }), false);
  assert.equal(noAnswer(new ApiError(502, null)), true);
  assert.equal(noAnswer(new ApiError(503, { error: "docker_daemon_unreachable" })), false);
  assert.equal(noAnswer(null), false);
});

test("an error's translation key comes from its code or a known server sentence", () => {
  assert.equal(errorKey(new ApiError(400, { error: "unknown backup id" })), "error.backup_gone");
  assert.equal(errorKey(new ApiError(403, { error: "csrf_failed" })), "error.csrf_failed");
  assert.equal(errorKey(new ApiError(400, { error: "something new" })), null);
  assert.equal(errorKey(new TypeError("Failed to fetch")), null);
});

test("a server's own sentence is marked English; the console's words are not", () => {
  assert.deepEqual(errorView(new ApiError(500, { error: "x", message: "Could not recreate the EMS container." }), t), { text: "Could not recreate the EMS container.", lang: "en" });
  assert.deepEqual(errorView(new ApiError(403, { error: "csrf_failed" }), t), { text: "error.csrf_failed", lang: undefined });
  assert.deepEqual(errorView(new ApiError(400, { error: "no such thing here" }), t), { text: "no such thing here", lang: "en" });
  assert.deepEqual(errorView(new TypeError("Failed to fetch"), t, "Fallback"), { text: "error.noResponse", lang: undefined });
});
