// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError } from "../../../shared/lib/api.js";

export const JOB_POLL_INTERVAL_MS = 1200;
export const JOB_RETRY_INTERVAL_MS = 3000;
export const JOB_MAX_MISSES = 20;

function wait(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const stop = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", stop);
      resolve();
    }, ms);
    signal?.addEventListener("abort", stop, { once: true });
  });
}

/** A read that may succeed if tried again: no answer at all, or a server that failed to give one. */
export function isTransient(error) {
  return !(error instanceof ApiError) || error.status >= 500;
}

/**
 * Follow a server-side job until it has finished. The job keeps running on
 * the server whatever this page does, so a read that gets no answer is
 * retried for a while (``onLost`` says so) rather than reported as a failure;
 * an answer that refuses is final.
 */
export async function followJob(read, { onUpdate, onLost, signal } = {}) {
  let misses = 0;
  for (;;) {
    let data;
    try {
      data = await read();
    } catch (error) {
      if (!isTransient(error) || misses >= JOB_MAX_MISSES || signal?.aborted) throw error;
      misses += 1;
      onLost?.(true);
      await wait(JOB_RETRY_INTERVAL_MS, signal);
      continue;
    }
    if (signal?.aborted) throw signal.reason;
    if (data?.ok === false) throw new ApiError(409, data);
    if (misses) onLost?.(false);
    misses = 0;
    onUpdate?.(data);
    if (data?.status !== "running") return data;
    await wait(JOB_POLL_INTERVAL_MS, signal);
  }
}
