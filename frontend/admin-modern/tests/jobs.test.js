// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../shared/lib/api.js";
import { followJob, isTransient, JOB_MAX_MISSES } from "../src/lib/jobs.js";

function scripted(answers) {
  let index = 0;
  return async () => {
    const answer = answers[Math.min(index, answers.length - 1)];
    index += 1;
    if (answer instanceof Error) throw answer;
    return answer;
  };
}

test("a job is followed until it is no longer running", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const updates = [];
  const done = followJob(scripted([{ ok: true, status: "running", steps: [1] }, { ok: true, status: "succeeded", steps: [1, 2] }]), { onUpdate: (data) => updates.push(data.status) });
  await Promise.resolve();
  for (let i = 0; i < 5; i += 1) {
    t.mock.timers.tick(1200);
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal((await done).status, "succeeded");
  assert.deepEqual(updates, ["running", "succeeded"]);
});

test("a read with no answer is retried and said so; an answer that refuses is final", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const lost = [];
  const done = followJob(scripted([new TypeError("network"), { ok: true, status: "succeeded" }]), { onLost: (value) => lost.push(value) });
  for (let i = 0; i < 5; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
    t.mock.timers.tick(3000);
  }
  assert.equal((await done).status, "succeeded");
  assert.deepEqual(lost, [true, false]);
  await assert.rejects(followJob(scripted([new ApiError(404, { error: "unknown job" })])), (error) => error.status === 404);
  await assert.rejects(followJob(scripted([{ ok: false, error: "gone" }])), (error) => error.payload.error === "gone");
});

test("the retries give up after a bounded number of misses", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const done = followJob(scripted([new ApiError(502, null)]));
  const outcome = done.then(() => "resolved", (error) => error.status);
  for (let i = 0; i <= JOB_MAX_MISSES + 2; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
    t.mock.timers.tick(3000);
  }
  assert.equal(await outcome, 502);
});

test("leaving the page stops following", async () => {
  const controller = new AbortController();
  const done = followJob(scripted([{ ok: true, status: "running" }]), { signal: controller.signal });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort(new Error("left"));
  await assert.rejects(done, /left/);
});

test("a page left while a read is in flight stops after that read, and an already left one at once", async () => {
  const controller = new AbortController();
  let reads = 0;
  const done = followJob(async () => {
    reads += 1;
    controller.abort(new Error("left"));
    return { ok: true, status: "running" };
  }, { signal: controller.signal });
  await assert.rejects(done, /left/);
  assert.equal(reads, 1);

  await assert.rejects(followJob(scripted([new TypeError("network")]), { signal: controller.signal }), /network/);
});

test("no answer and a server failure are worth asking again; a refusal is not", () => {
  assert.equal(isTransient(new TypeError("Failed to fetch")), true);
  assert.equal(isTransient(new ApiError(502, null)), true);
  assert.equal(isTransient(new ApiError(404, { error: "unknown job" })), false);
  assert.equal(isTransient(new ApiError(401, { error: "not_authenticated" })), false);
});
