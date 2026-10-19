// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The live feed, with Classic's transport contract:
 *
 * - one EventSource per tab, on /api/events, event "telemetry";
 * - no telemetry within 3 s, or an error before the first one: fall back to
 *   polling /api/live every 2 s and retry the stream every 30 s;
 * - a retried stream that delivers promotes the tab back to SSE.
 *
 * Added for Modern: a tab hidden for 30 s releases its stream, because the
 * server allows two per address and Classic may be holding the other one.
 */

export const TELEMETRY_TIMEOUT_MS = 3000;
export const POLL_INTERVAL_MS = 2000;
export const PROMOTION_INTERVAL_MS = 30000;
export const HIDDEN_RELEASE_MS = 30000;

export function createLiveTransport({
  onSnapshot,
  onState = () => {},
  fetchLive,
  createEventSource,
  timers = globalThis,
  supportsEventSource = typeof globalThis.EventSource === "function",
}) {
  let source = null;
  let polling = false;
  let pollTimer = null;
  let promotionTimer = null;
  let hiddenTimer = null;
  let suspended = false;
  let stopped = false;

  function setState(state) {
    onState(state);
  }

  async function pollOnce() {
    try {
      onSnapshot(await fetchLive());
      if (polling) setState("polling");
    } catch {
      setState("offline");
    }
  }

  function closeSource() {
    if (!source) return;
    try {
      source.close();
    } catch {
      /* already closed */
    }
    source = null;
  }

  function stopPolling() {
    polling = false;
    if (pollTimer) timers.clearInterval(pollTimer);
    if (promotionTimer) timers.clearInterval(promotionTimer);
    pollTimer = null;
    promotionTimer = null;
  }

  function startPolling() {
    if (polling || stopped || suspended) return;
    polling = true;
    setState("polling");
    pollTimer = timers.setInterval(pollOnce, POLL_INTERVAL_MS);
    if (supportsEventSource) {
      promotionTimer = timers.setInterval(() => {
        if (polling && !suspended) connect();
      }, PROMOTION_INTERVAL_MS);
    }
  }

  function connect() {
    closeSource();
    if (stopped || suspended) return;
    const current = createEventSource("/api/events");
    source = current;
    let received = false;
    let version = 0;
    let fellBack = false;

    const fallBack = () => {
      if (fellBack) return;
      fellBack = true;
      try {
        current.close();
      } catch {
        /* already closed */
      }
      if (source === current) source = null;
      startPolling();
    };

    const armTimeout = (armedVersion) => {
      timers.setTimeout(() => {
        if (version === armedVersion && source === current) fallBack();
      }, TELEMETRY_TIMEOUT_MS);
    };
    armTimeout(version);

    current.addEventListener("telemetry", (event) => {
      if (source !== current) return;
      received = true;
      version += 1;
      if (polling) stopPolling();
      setState("live");
      try {
        onSnapshot(JSON.parse(event.data));
      } catch {
        /* a malformed frame is skipped; the next one replaces it */
      }
    });

    current.onerror = () => {
      if (source !== current) return;
      setState("reconnecting");
      if (!received) {
        fallBack();
        return;
      }
      armTimeout(version);
    };
  }

  function start() {
    stopped = false;
    pollOnce();
    if (!supportsEventSource) {
      startPolling();
      return;
    }
    connect();
  }

  function stop() {
    stopped = true;
    closeSource();
    stopPolling();
    if (hiddenTimer) timers.clearTimeout(hiddenTimer);
    hiddenTimer = null;
  }

  /** Called on visibilitychange; hidden for HIDDEN_RELEASE_MS releases the connection. */
  function setVisible(visible) {
    if (stopped) return;
    if (!visible) {
      if (hiddenTimer || suspended) return;
      hiddenTimer = timers.setTimeout(() => {
        hiddenTimer = null;
        suspended = true;
        closeSource();
        stopPolling();
        setState("paused");
      }, HIDDEN_RELEASE_MS);
      return;
    }
    if (hiddenTimer) {
      timers.clearTimeout(hiddenTimer);
      hiddenTimer = null;
    }
    if (suspended) {
      suspended = false;
      pollOnce();
      if (supportsEventSource) connect();
      else startPolling();
    }
  }

  return {
    start,
    stop,
    setVisible,
    get transport() {
      if (suspended) return "paused";
      if (polling) return "polling";
      return source ? "sse" : "idle";
    },
  };
}
