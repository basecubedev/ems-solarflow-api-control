// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { createLiveTransport, TELEMETRY_TIMEOUT_MS, POLL_INTERVAL_MS, PROMOTION_INTERVAL_MS, HIDDEN_RELEASE_MS } from "../src/lib/transport.js";

/** Manual timers: a test advances time explicitly, so no ordering depends on real sleeps. */
function fakeTimers() {
  let now = 0;
  let next = 1;
  const tasks = new Map();
  return {
    setTimeout(fn, ms) { const id = next++; tasks.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { tasks.delete(id); },
    setInterval(fn, ms) { const id = next++; tasks.set(id, { fn, at: now + ms, every: ms }); return id; },
    clearInterval(id) { tasks.delete(id); },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...tasks.entries()].filter(([, task]) => task.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, task] = due;
        now = task.at;
        if (task.every) task.at += task.every; else tasks.delete(id);
        task.fn();
      }
      now = end;
    },
  };
}

function fakeSources() {
  const created = [];
  return {
    created,
    create(url) {
      const listeners = {};
      const source = {
        url,
        closed: false,
        onerror: null,
        addEventListener(name, fn) { listeners[name] = fn; },
        close() { source.closed = true; },
        emit(data) { listeners.telemetry({ data: JSON.stringify(data) }); },
      };
      created.push(source);
      return source;
    },
  };
}

function setup() {
  const timers = fakeTimers();
  const sources = fakeSources();
  const snapshots = [];
  const states = [];
  let polls = 0;
  const transport = createLiveTransport({
    onSnapshot: (snapshot) => snapshots.push(snapshot),
    onState: (state) => states.push(state),
    fetchLive: async () => { polls += 1; return { timestamp: `poll-${polls}` }; },
    createEventSource: (url) => sources.create(url),
    timers,
    supportsEventSource: true,
  });
  return { timers, sources, snapshots, states, transport, polls: () => polls };
}

test("one stream on /api/events delivers telemetry and the tab is live", () => {
  const { transport, sources, snapshots, states } = setup();
  transport.start();
  assert.equal(sources.created.length, 1);
  assert.equal(sources.created[0].url, "/api/events");
  sources.created[0].emit({ timestamp: "a" });
  assert.deepEqual(snapshots.at(-1), { timestamp: "a" });
  assert.equal(states.at(-1), "live");
  assert.equal(transport.transport, "sse");
});

test("no telemetry within the timeout falls back to polling, and a retried stream promotes back", async () => {
  const { transport, sources, timers, polls } = setup();
  transport.start();
  timers.advance(TELEMETRY_TIMEOUT_MS);
  assert.equal(sources.created[0].closed, true);
  assert.equal(transport.transport, "polling");
  const before = polls();
  timers.advance(POLL_INTERVAL_MS * 2);
  await Promise.resolve();
  assert.ok(polls() >= before + 2);
  timers.advance(PROMOTION_INTERVAL_MS - POLL_INTERVAL_MS * 2);
  const retried = sources.created.at(-1);
  assert.notEqual(retried, sources.created[0]);
  retried.emit({ timestamp: "b" });
  assert.equal(transport.transport, "sse");
});

test("an error before the first frame (for example 429) falls back at once", () => {
  const { transport, sources } = setup();
  transport.start();
  sources.created[0].onerror();
  assert.equal(sources.created[0].closed, true);
  assert.equal(transport.transport, "polling");
});

test("a tab hidden long enough releases its stream and reconnects when shown", () => {
  const { transport, sources, timers } = setup();
  transport.start();
  sources.created[0].emit({ timestamp: "a" });
  transport.setVisible(false);
  timers.advance(HIDDEN_RELEASE_MS - 1);
  assert.equal(sources.created[0].closed, false);
  timers.advance(1);
  assert.equal(sources.created[0].closed, true);
  assert.equal(transport.transport, "paused");
  transport.setVisible(true);
  assert.equal(sources.created.length, 2);
  assert.equal(sources.created[1].closed, false);
});

test("a short hide keeps the stream", () => {
  const { transport, sources, timers } = setup();
  transport.start();
  sources.created[0].emit({ timestamp: "a" });
  transport.setVisible(false);
  timers.advance(HIDDEN_RELEASE_MS / 2);
  transport.setVisible(true);
  timers.advance(HIDDEN_RELEASE_MS);
  assert.equal(sources.created.length, 1);
  assert.equal(sources.created[0].closed, false);
});
