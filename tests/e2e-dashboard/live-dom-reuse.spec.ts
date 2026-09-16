// SPDX-License-Identifier: AGPL-3.0-or-later
// A live snapshot arrives every couple of seconds, for as long as the cockpit
// is open. What it costs the browser depends entirely on whether it replaces
// the nodes it renders into or updates them.
//
// Replacing them costs the layout and the rasterised tiles of everything below
// the change, which a person notices as sections that have to be drawn again
// while they scroll -- including on the way back up, where nothing is new.
//
// These tests watch the DOM itself through a MutationObserver: an update shows
// up as characterData and attribute records, a rebuild as childList records
// carrying removed nodes. The counters separate the two without asking the
// implementation anything.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream.
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

type Mutations = {
  childList: number;
  removedElements: number;
  addedElements: number;
  characterData: number;
  attributes: number;
};

// Tap the live stream: count what arrives, keep the last payload, and keep a
// handle on the source so a test can deliver one of its own through the same
// path the server uses. Nothing is written into the DOM by hand.
async function tapLiveStream(page: Page) {
  await page.addInitScript(() => {
    const Original = window.EventSource;
    const shared = window as unknown as {
      __snapshots: number;
      __lastSnapshot: unknown;
      __streams: EventSource[];
    };
    shared.__snapshots = 0;
    shared.__lastSnapshot = null;
    shared.__streams = [];
    class CountingEventSource extends Original {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        shared.__streams.push(this);
        this.addEventListener("telemetry", (event) => {
          shared.__snapshots += 1;
          try {
            shared.__lastSnapshot = JSON.parse((event as MessageEvent).data);
          } catch (_) {
            /* a non-JSON keepalive is not a snapshot */
          }
        });
      }
    }
    window.EventSource = CountingEventSource as unknown as typeof EventSource;
  });
}

async function snapshotCount(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as { __snapshots: number }).__snapshots);
}

async function waitForSnapshots(page: Page, count: number) {
  await page.waitForFunction(
    (n) => (window as unknown as { __snapshots: number }).__snapshots >= n,
    count,
    { timeout: 25_000 }
  );
}

async function watch(page: Page, selector: string) {
  await page.evaluate((sel) => {
    const host = document.querySelector(sel);
    if (!host) throw new Error(`no element for ${sel}`);
    const counters: Mutations = {
      childList: 0,
      removedElements: 0,
      addedElements: 0,
      characterData: 0,
      attributes: 0,
    };
    (window as unknown as { __mutations: Mutations }).__mutations = counters;
    const elements = (nodes: NodeList) =>
      Array.from(nodes).filter((node) => node.nodeType === Node.ELEMENT_NODE).length;
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === "childList") {
          counters.childList += 1;
          counters.removedElements += elements(record.removedNodes);
          counters.addedElements += elements(record.addedNodes);
        } else if (record.type === "characterData") {
          counters.characterData += 1;
        } else if (record.type === "attributes") {
          counters.attributes += 1;
        }
      }
    });
    observer.observe(host, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
    });
  }, selector);
}

async function mutations(page: Page): Promise<Mutations> {
  return page.evaluate(() => (window as unknown as { __mutations: Mutations }).__mutations);
}

// Deliver a snapshot of the test's own making through the live stream, so the
// cockpit takes exactly the path the server's own messages take.
async function deliverSnapshot(page: Page, mutate: (snapshot: Record<string, unknown>) => void) {
  await page.evaluate((fnSource) => {
    const shared = window as unknown as {
      __lastSnapshot: Record<string, unknown>;
      __streams: EventSource[];
    };
    const snapshot = JSON.parse(JSON.stringify(shared.__lastSnapshot));
    // eslint-disable-next-line no-new-func
    new Function("snapshot", `(${fnSource})(snapshot)`)(snapshot);
    snapshot.timestamp = new Date(Date.now() + 120_000).toISOString();
    shared.__streams[0].dispatchEvent(
      new MessageEvent("telemetry", { data: JSON.stringify(snapshot) })
    );
  }, mutate.toString());
}

test.describe("live rendering reuses its DOM @smoke", () => {
  test("device cards are updated by a snapshot, not replaced", async ({ page }) => {
    await tapLiveStream(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.click('[data-flow-view="devices"]');
    await expect(page.locator("#deviceGrid .device-card").first()).toBeVisible();
    await waitForSnapshots(page, 1);

    // A property on the live node survives an update and not a rebuild, so it
    // tells the two apart even when the values happen to stay the same.
    await page.evaluate(() => {
      document.querySelectorAll("#deviceGrid .device-card").forEach((card, index) => {
        (card as unknown as { __probe: number }).__probe = index + 1;
      });
    });

    const cardCount = await page.locator("#deviceGrid .device-card").count();
    await watch(page, "#deviceGrid");
    // Change a value the cards display, so the run has real work to do.
    await deliverSnapshot(page, (snapshot) => {
      const devices = snapshot.devices as Record<string, Record<string, unknown>>;
      for (const device of Object.values(devices)) {
        device.pv_input_w = 1234;
      }
    });
    await expect(page.locator("#deviceGrid .device-card").first()).toContainText("1.23 kW");

    const probes = await page.$$eval("#deviceGrid .device-card", (cards) =>
      cards.map((card) => (card as unknown as { __probe?: number }).__probe ?? 0)
    );
    // Every card is the node that was already there.
    expect(probes).toEqual(Array.from({ length: cardCount }, (_, index) => index + 1));

    const seen = await mutations(page);
    expect(seen.removedElements).toBe(0);
    // And the update really happened in the DOM rather than nowhere.
    expect(seen.characterData + seen.attributes).toBeGreaterThan(0);
  });

  test("the rule list is updated by a snapshot, not replaced", async ({ page }) => {
    await tapLiveStream(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#rulesList .rule-row").first()).toBeVisible();

    const before = await snapshotCount(page);
    await watch(page, "#rulesList");
    await waitForSnapshots(page, before + 3);

    const seen = await mutations(page);
    expect(seen.removedElements).toBe(0);
  });

  test("a device that disappears takes its card with it", async ({ page }) => {
    // Reuse must not become "never remove anything": a device dropped from the
    // snapshot has to lose its card, or the cockpit shows hardware that is gone.
    await tapLiveStream(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.click('[data-flow-view="devices"]');
    await expect(page.locator("#deviceGrid .device-card").first()).toBeVisible();
    await waitForSnapshots(page, 1);

    const names = await page.$$eval("#deviceGrid .device-name", (els) =>
      els.map((el) => el.textContent || "")
    );
    expect(names.length).toBeGreaterThan(1);

    await deliverSnapshot(page, (snapshot) => {
      const devices = snapshot.devices as Record<string, unknown>;
      const first = Object.keys(devices)[0];
      snapshot.devices = { [first]: devices[first] };
    });

    await expect(page.locator("#deviceGrid .device-card")).toHaveCount(1);
    await expect(page.locator("#deviceGrid .device-name")).toHaveText(names[0]);
  });

  test("a device that appears gets a card without disturbing the others", async ({ page }) => {
    await tapLiveStream(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.click('[data-flow-view="devices"]');
    await expect(page.locator("#deviceGrid .device-card").first()).toBeVisible();
    await waitForSnapshots(page, 1);

    const countBefore = await page.locator("#deviceGrid .device-card").count();
    await watch(page, "#deviceGrid");
    await deliverSnapshot(page, (snapshot) => {
      const devices = snapshot.devices as Record<string, unknown>;
      const first = Object.keys(devices)[0];
      devices["Probe device"] = JSON.parse(JSON.stringify(devices[first]));
    });

    await expect(page.locator("#deviceGrid .device-card")).toHaveCount(countBefore + 1);
    const seen = await mutations(page);
    // One card arrives; none of the existing ones are thrown away for it.
    expect(seen.removedElements).toBe(0);
  });
  test("the energy board is updated by a snapshot, not replaced", async ({ page }) => {
    await tapLiveStream(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.click('[data-flow-view="energy"]');
    await expect(page.locator("#energyStats")).toBeVisible();
    await waitForSnapshots(page, 1);

    await watch(page, "#energyStats");
    await deliverSnapshot(page, (snapshot) => {
      const stats = snapshot.energy_stats as Record<string, unknown>;
      if (stats) stats.today = { output_kwh: 42.5, savings: 7.5 };
    });
    await waitForSnapshots(page, 3);

    const seen = await mutations(page);
    expect(seen.removedElements).toBe(0);
  });

  test("the control board is updated by a snapshot, not replaced", async ({ page }) => {
    await tapLiveStream(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.click('[data-flow-view="control"]');
    await expect(page.locator("#controlExplainMount")).toBeVisible();
    await waitForSnapshots(page, 1);

    await watch(page, "#controlExplainMount");
    await waitForSnapshots(page, 4);

    const seen = await mutations(page);
    expect(seen.removedElements).toBe(0);
  });
});
