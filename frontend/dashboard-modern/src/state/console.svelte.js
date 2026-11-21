// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { consoleTarget } from "../lib/console.js";

export const PROBE_MS = 3000;

/**
 * Where the Admin console answers, as the EMS last named it (it names one
 * only while that console answers as one that carries the dashboard), and
 * whether this browser reaches it.
 */
class ConsoleAddress {
  record = $state.raw(null);
  answers = $state(false);
}

export const consoleAddress = new ConsoleAddress();

let probed = "";

/**
 * The console the EMS names, and whether this browser reaches it: the EMS
 * reached it, but a reader behind a proxy, on an IPv6 address or past a
 * firewall may not. The page asks the console once per address it names
 * (the EMS's page policy allows exactly that address) and the reader is
 * taken there only once the answer came back; otherwise it is offered.
 */
export function setConsoleRecord(record) {
  consoleAddress.record = record;
  const target = consoleTarget(record);
  const address = target?.automatic ? target.href.replace(/\/(#.*)?$/, "") : "";
  if (address === probed) return;
  probed = address;
  consoleAddress.answers = false;
  if (!address) return;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), PROBE_MS);
  fetch(`${address}/api/console`, { mode: "no-cors", cache: "no-store", credentials: "omit", signal: abort.signal })
    .then(
      () => {
        if (probed === address) consoleAddress.answers = true;
      },
      () => {},
    )
    .finally(() => clearTimeout(timer));
}
