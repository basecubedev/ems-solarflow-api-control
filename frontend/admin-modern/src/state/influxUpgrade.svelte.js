// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend } from "./auth.svelte.js";
import { reload, system } from "./system.svelte.js";
import { followJob } from "../lib/jobs.js";
import { influxView } from "../lib/influxdb.js";

const STARTS = {
  upgrade: { path: "/api/admin/maintenance/influxdb/upgrade", field: "target_image" },
  rollback: { path: "/api/admin/maintenance/influxdb/rollback", field: "image" },
};
const jobPath = (id) => `/api/admin/maintenance/influxdb/jobs/${encodeURIComponent(id)}`;
const STOP_WAITING = "/api/admin/maintenance/influxdb/stop-waiting";
const RECHECK = "/api/admin/maintenance/influxdb/recheck";
/** The job sees a stop at its next poll, READY_POLL_SECONDS on the server; the plan is read again after it. */
const STOP_SETTLE_MS = 6_000;
let asked = 0;
/** What an upgrade or its way back changes besides the version itself. */
const AFTERWARDS = ["influxdb", "overview", "plan", "backups"];

/**
 * The InfluxDB upgrade or its way back this browser started. It runs on the
 * server for a long time whatever page is open, so it is followed here rather
 * than by the Updates page: coming back finds it where it is. ``confirming``
 * is the run the reader is asked to confirm, from whichever part of the page
 * offered it. ``image`` is the image the run went for, and ``origin`` where it
 * was asked for, so a line that offered one run follows that run and no other;
 * ``endedAt`` is when this browser stopped following it, and ``readingBack``
 * holds while the plan read then is under way.
 */
class InfluxJob {
  confirming = $state("");
  askedFrom = "";
  asking = $state(false);
  checking = $state(false);
  recheck = $state.raw(null);
  stopFailed = $state(false);
  kind = $state("");
  image = $state("");
  origin = $state("");
  endedAt = $state(0);
  readingBack = $state(false);
  jobId = $state(null);
  running = $state(false);
  steps = $state.raw([]);
  lost = $state(false);
  detached = $state(false);
  stopping = $state(false);
  result = $state.raw(null);
  error = $state.raw(null);
}

export const influxJob = new InfluxJob();

export async function startInfluxJob(kind, image, origin = "") {
  if (influxJob.running || !Object.hasOwn(STARTS, kind) || !image) return;
  const { path, field } = STARTS[kind];
  influxJob.kind = kind;
  influxJob.image = image;
  influxJob.origin = origin;
  influxJob.endedAt = 0;
  influxJob.readingBack = false;
  influxJob.jobId = null;
  influxJob.steps = [];
  influxJob.result = null;
  influxJob.error = null;
  influxJob.recheck = null;
  influxJob.stopFailed = false;
  influxJob.detached = false;
  influxJob.stopping = false;
  influxJob.running = true;
  try {
    const started = await adminSend(path, { body: { confirm: true, [field]: image } });
    influxJob.jobId = started.job_id;
    influxJob.steps = started.steps || [];
    reload("influxdb");
    const final = await followJob(() => adminGet(jobPath(influxJob.jobId)), {
      onUpdate: (data) => {
        if (doneSteps(data.steps) !== doneSteps(influxJob.steps)) reload("influxdb");
        influxJob.steps = data.steps || [];
      },
      onLost: (value) => (influxJob.lost = value),
    });
    influxJob.result = final.result || {};
  } catch (error) {
    if (influxJob.jobId) detach();
    else influxJob.error = error;
  } finally {
    influxJob.lost = false;
    influxJob.endedAt = Date.now();
    influxJob.readingBack = true;
    influxJob.running = false;
    reload(...AFTERWARDS.filter((name) => name !== "influxdb"));
    reload("influxdb").finally(() => (influxJob.readingBack = false));
  }
}

const doneSteps = (steps) => (Array.isArray(steps) ? steps.filter((step) => step?.state !== "pending" && step?.state !== "running").length : 0);

/**
 * A run this page lost hold of, as when the session ran out or the network
 * went away while it waited: it goes on on the server, so it is no outcome
 * here. The plan, read on, shows where it is and how it ended.
 */
function detach() {
  influxJob.detached = true;
  influxJob.steps = [];
}

/**
 * Ask to confirm an upgrade or its way back. The confirmation names versions
 * and a backup, so it is asked against what the server says now, not against
 * what has been on screen. ``origin`` names the line that asked (startInfluxJob).
 */
export async function askInflux(kind, origin = "") {
  if (influxJob.running || influxJob.asking || !Object.hasOwn(STARTS, kind)) return;
  const ticket = ++asked;
  influxJob.asking = true;
  try {
    await reload("influxdb");
  } finally {
    influxJob.asking = false;
  }
  if (ticket !== asked) return;
  const fresh = influxView(system.influxdb.data);
  if (system.influxdb.error || !fresh) return;
  if (kind === "upgrade" && !fresh.canUpgrade) return;
  if (kind === "rollback" && !fresh.rollback?.available) return;
  influxJob.askedFrom = origin;
  influxJob.confirming = kind;
}

export function confirmInflux() {
  const kind = influxJob.confirming;
  influxJob.confirming = "";
  if (kind === "stop") {
    stopInfluxWait();
    return;
  }
  const fresh = influxView(system.influxdb.data);
  startInfluxJob(kind, kind === "upgrade" ? fresh?.targetImage : fresh?.rollback?.toImage, influxJob.askedFrom);
}

/** Forget a confirmation asked for and any still being asked, as when its page closes. */
export function cancelInflux() {
  asked += 1;
  influxJob.confirming = "";
}

/** Ask to confirm ending the wait for a start that takes long. */
export function askStopWaiting() {
  if (influxJob.asking) return;
  influxJob.confirming = "stop";
}

/**
 * End the wait for InfluxDB to answer. The run then ends as not completed and
 * its job says so. The server refuses when the wait is over already, which the
 * plan read afterwards shows; any other failure is said.
 */
async function stopInfluxWait() {
  influxJob.stopFailed = false;
  try {
    await adminSend(STOP_WAITING, { body: { confirm: true } });
    influxJob.stopping = true;
    setTimeout(() => reload("influxdb"), STOP_SETTLE_MS);
  } catch (error) {
    influxJob.stopFailed = error?.payload?.status !== "not_waiting";
  } finally {
    reload("influxdb");
  }
}

/** Check an upgrade that did not finish again; the server records it as upgraded when InfluxDB holds everything. */
export async function recheckInflux() {
  if (influxJob.running || influxJob.checking) return;
  influxJob.checking = true;
  influxJob.recheck = null;
  try {
    influxJob.recheck = await adminSend(RECHECK, { body: {} });
    if (influxJob.recheck?.ok) {
      influxJob.result = null;
      influxJob.error = null;
      influxJob.steps = [];
      influxJob.stopFailed = false;
    }
  } catch (error) {
    influxJob.recheck = { ok: false, message: error?.payload?.message || "" };
  } finally {
    influxJob.checking = false;
    reload(...AFTERWARDS);
  }
}
