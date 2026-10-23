// SPDX-License-Identifier: AGPL-3.0-or-later
import { applianceGet } from "./session.svelte.js";
import { currentOperation, settledSince } from "../lib/operations.js";
import { logPath } from "../lib/logs.js";
import { ADMIN_CHOICES, MANAGER_CHOICES, forget } from "./forms.svelte.js";

/** Operations are polled for responsiveness; the host state costs a docker inspect and a statvfs, so it joins every fifth tick. */
export const POLL_MS = 2000;
export const STATUS_EVERY_TICKS = 5;

/**
 * What this console knows about the host, read from the agent. undefined is
 * "not asked yet", null "being read", { error } "could not be read". The
 * agent is the authority for every value; nothing here is written back.
 */
class Host {
  status = $state.raw(undefined);
  manager = $state.raw(undefined);
  operation = $state.raw(null);
  readAt = $state(0);
  releases = $state.raw(undefined);
  managerSources = $state.raw(undefined);
  backup = $state.raw(undefined);
  settings = $state.raw(undefined);
  wifi = $state.raw(undefined);
  log = $state.raw(undefined);
  logFailed = $state.raw(null);
  logLoading = $state("");
  /** A held listing as last read, kept while a settled operation has it read again. */
  previous = $state.raw({});
  /** Held listings wait for the host read a settle or a Refresh starts, so they are read against the version installed now. */
  rereading = $state(false);
}

export const host = new Host();

/**
 * A held listing to show: what was read, or while it is being read again
 * after an invalidation, what was read before -- so a page keeps its
 * controls, and a focus on one of them, instead of a loading line. A failed
 * read shows as failed; whether to read is still decided by host[key].
 */
export function held(key) {
  const value = host[key];
  return value === undefined || value === null ? host.previous[key] : value;
}

/**
 * Fetched on the first page that needs them and then held: the package index
 * is a round trip to a remote host and the backup state a statvfs per
 * exported path, so a tick must not re-read them. A settled operation drops
 * them, because it may have changed what they say.
 */
const PATHS = Object.freeze({
  releases: "/api/admin/releases",
  managerSources: "/api/manager/sources",
  backup: "/api/backup",
  settings: "/api/settings",
});
export const HELD = Object.freeze(Object.keys(PATHS));

let generation = 0;
let sessionEpoch = 0;
let live = () => true;
const listings = {};
const readAgainst = {};
const seenVersions = {};
let seen = { count: 0, finishedAt: 0 };
let hostRead = null;
let timer = null;
let ticks = 0;
let operationWatchers = [];
let logRequest = 0;

function failure(error) {
  return { error: (error && error.code) || "unreachable", message: (error && error.payload && error.payload.message) || "" };
}

function installedVersion(key) {
  if (key === "releases") return String(((host.status && host.status.admin) || {}).version || "");
  if (key === "managerSources") return host.manager && !host.manager.error ? String(host.manager.installed_version || "") : "";
  return "";
}

/**
 * Re-reads in progress: a settle and the owner's Refresh can overlap, and the
 * held listings wait until the last of them has read the host afresh.
 */
let rereads = 0;

function beginReread() {
  rereads += 1;
  host.rereading = true;
  const mine = sessionEpoch;
  return () => {
    if (mine !== sessionEpoch) return;
    rereads = Math.max(0, rereads - 1);
    host.rereading = rereads > 0;
  };
}

/** Asks for a held listing once; a page calls this whenever it needs one, and it is read again only after a drop. */
export function ensure(key) {
  if (host.rereading || !(key in PATHS) || host[key] !== undefined) return;
  host[key] = null;
  readAgainst[key] = installedVersion(key);
  load(key);
}

/**
 * An answer from before the last invalidation would pin a pre-operation
 * listing under a key nothing reads again, so a read checks both generations
 * before it stores anything.
 */
async function load(key) {
  const mine = generation;
  const listing = listings[key] || 0;
  const current = () => mine === generation && listing === (listings[key] || 0);
  try {
    const payload = await applianceGet(PATHS[key]);
    if (current()) answered(key, payload);
  } catch (error) {
    if (current()) answered(key, failure(error));
  }
}

function answered(key, value) {
  host[key] = value;
  forgetPrevious(key);
}

function forgetPrevious(key) {
  if (key in host.previous) host.previous = Object.fromEntries(Object.entries(host.previous).filter(([name]) => name !== key));
}

/** Its own generation: the global one would strand every other read at null. */
function dropListing(key) {
  listings[key] = (listings[key] || 0) + 1;
  host[key] = undefined;
  forgetPrevious(key);
}

/**
 * Choices go when the installed version moves. A listing is read again when
 * the version it was requested against is not the one installed now -- Admin
 * replacing itself or a Manager revert changes that with no operation settling
 * here -- and a failed read is retried on the next host read. An empty version
 * (a failed inspect, an unlabelled image) is never taken for a change.
 */
function reconcile(key, choices) {
  const now = installedVersion(key);
  if (!now) return;
  const before = seenVersions[key];
  seenVersions[key] = now;
  if (before && before !== now) {
    forget(...choices);
    forgetPrevious(key);
  }
  const listing = host[key];
  const stale = listing === null ? Boolean(readAgainst[key]) && readAgainst[key] !== now : listing && (listing.error || readAgainst[key] !== now);
  if (stale) dropListing(key);
}

/** Dropped rather than emptied, so the pages ask again; the generation retires whatever is in flight. */
export function invalidateHeld() {
  generation += 1;
  const kept = { ...host.previous };
  HELD.forEach((key) => {
    if (host[key] && !host[key].error) kept[key] = host[key];
    host[key] = undefined;
  });
  host.previous = kept;
  forget("adminReinstall");
}

/**
 * The status and the manager state, read together and coalesced: the tick, an
 * explicit refresh and a settled operation all want them, and a settle that
 * lands during a refresh must not order the same docker inspect twice.
 */
/** A host read that starts after this moment: one already in flight may have been asked before an operation finished. */
function freshHostRead() {
  return hostRead ? hostRead.then(() => readHostState()) : readHostState();
}

export function readHostState() {
  if (hostRead) return hostRead;
  const mine = sessionEpoch;
  const read = Promise.all([
    applianceGet("/api/status").catch(failure),
    applianceGet("/api/manager").catch(failure),
  ]).then(([status, manager]) => {
    if (hostRead === read) hostRead = null;
    if (mine !== sessionEpoch) return;
    host.status = status;
    host.manager = manager;
    host.readAt = Date.now();
    reconcile("releases", ADMIN_CHOICES);
    reconcile("managerSources", MANAGER_CHOICES);
  });
  hostRead = read;
  return read;
}

/** Called with every operations payload; the plan a lost answer belonged to is picked up here. */
export function watchOperations(watcher) {
  operationWatchers = [...operationWatchers, watcher];
  return () => {
    operationWatchers = operationWatchers.filter((item) => item !== watcher);
  };
}

export async function pollOperations() {
  const mine = sessionEpoch;
  let payload;
  try {
    payload = await applianceGet("/api/operations");
  } catch {
    return null;
  }
  if (mine !== sessionEpoch) return null;
  host.operation = currentOperation(payload);
  const next = settledSince(payload, seen);
  seen = { count: next.count, finishedAt: next.finishedAt };
  if (next.settled) {
    const done = beginReread();
    invalidateHeld();
    freshHostRead().finally(done);
  }
  operationWatchers.forEach((watcher) => watcher(payload.active || null));
  return payload;
}

/**
 * The tick runs only while this predicate holds -- signed in, and not waiting
 * for a restarting host -- so a refresh that resolves after the session ended
 * cannot start it again.
 */
export function pollWhile(predicate) {
  live = predicate;
}

function tick() {
  if (!live()) {
    stopPolling();
    return;
  }
  ticks += 1;
  if (ticks % STATUS_EVERY_TICKS === 0) readHostState();
  pollOperations();
}

export function startPolling() {
  stopPolling();
  ticks = 0;
  timer = setInterval(tick, POLL_MS);
}

export function stopPolling() {
  if (timer) clearInterval(timer);
  timer = null;
}

/** Reads the host and the operations now and restarts the tick from here. */
export async function refresh() {
  await Promise.all([readHostState(), pollOperations()]);
  if (live()) startPolling();
}

/** The one refresh that means "read what is held again too": the owner's own Refresh. */
export async function refreshEverything() {
  const done = beginReread();
  invalidateHeld();
  try {
    await Promise.all([freshHostRead(), pollOperations()]);
  } finally {
    done();
  }
  if (live()) startPolling();
}

/** A new session starts from nothing: what an earlier one read may be hours old. */
export function resetHost() {
  generation += 1;
  sessionEpoch += 1;
  for (const key of ["status", "manager", ...HELD, "wifi", "log"]) host[key] = undefined;
  host.previous = {};
  rereads = 0;
  host.rereading = false;
  host.logFailed = null;
  host.operation = null;
  host.readAt = 0;
  seen = { count: 0, finishedAt: 0 };
  hostRead = null;
}

/** A per-click read, not a held listing: only a new session retires it. */
export async function scanWifi() {
  const mine = sessionEpoch;
  host.wifi = null;
  try {
    const payload = await applianceGet("/api/network/wifi/scan");
    if (mine === sessionEpoch) host.wifi = payload;
  } catch (error) {
    if (mine === sessionEpoch) host.wifi = failure(error);
  }
}

/**
 * The failure belongs to the source it was read for, so another log's panel
 * does not wear it; only the newest request may write, so a slow earlier one
 * cannot replace the log the reader asked for last.
 */
export async function loadLog(source) {
  logRequest += 1;
  const mine = logRequest;
  host.logLoading = source;
  host.logFailed = null;
  try {
    const log = await applianceGet(logPath(source));
    if (mine === logRequest) host.log = log;
  } catch (error) {
    if (mine === logRequest) host.logFailed = { source, error };
  } finally {
    if (mine === logRequest) host.logLoading = "";
  }
}
