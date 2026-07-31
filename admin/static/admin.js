// SPDX-License-Identifier: AGPL-3.0-or-later
// Vanilla admin discovery UI for retained Setup and Maintenance scan sessions.
// Every server-provided value passes through escapeHtml or text-only DOM APIs.
"use strict";

const POLL_INTERVAL_MS = 1200;
const POLL_MAX_MS = 120000;

// --- Admin auth gate --------------------------------------------------------
// One shared password protects the Admin Console and the EMS Dashboard (same
// file, separate sessions). Every workflow below stays behind this gate.
const authState = {
  adminInstanceId: null,
  configured: false,
  authenticated: false,
  requiresInitialPassword: true,
  recoveryRequired: false,
  csrfToken: null,
};

// Pollers/bootstrap loaders check this before hitting a protected API so a
// timer never hammers the backend with 401s after logout or session expiry.
function isAuthenticated() {
  return Boolean(authState && authState.authenticated);
}

// Reachable without an Admin session; every other POST needs the CSRF token.
const AUTH_PUBLIC_POST_PATHS = new Set([
  "/api/admin/auth/setup",
  "/api/admin/auth/login",
  "/api/admin/auth/logout",
]);
const AUTH_ERROR_CODES = new Set([
  "not_authenticated",
  "auth_not_configured",
  "csrf_failed",
  "auth_file_invalid",
]);
const SETUP_DISCOVERY_GATE_ERRORS = new Set([
  "setup_operation_required",
  "operation_mismatch",
  "system_alignment_incomplete",
  "system_build_mismatch",
]);

// Bypass the wrapped fetch for the public auth-status probe and login handshake,
// so those calls never try to attach a CSRF token or recurse on auth failure.
const rawFetch = window.fetch.bind(window);

// Wrap fetch once so authenticated mutating requests carry X-CSRF-Token and any
// auth failure (401/403 with an auth error) drops the UI back to the login gate.
window.fetch = function (input, init) {
  const options = init ? { ...init } : {};
  const url = typeof input === "string" ? input : (input && input.url) || "";
  const path = url.split("?", 1)[0];
  const method = (options.method || "GET").toUpperCase();
  const isApi = path.indexOf("/api/") !== -1;
  if (
    method !== "GET" &&
    authState.csrfToken &&
    isApi &&
    !AUTH_PUBLIC_POST_PATHS.has(path)
  ) {
    const headers = new Headers(options.headers || {});
    if (!headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", authState.csrfToken);
    }
    options.headers = headers;
  }
  return rawFetch(input, options).then((resp) => {
    if (
      (resp.status === 401 || resp.status === 403) &&
      isApi &&
      path !== "/api/admin/auth/status" &&
      !AUTH_PUBLIC_POST_PATHS.has(path)
    ) {
      resp
        .clone()
        .json()
        .then((data) => {
          if (data && AUTH_ERROR_CODES.has(data.error)) onAuthLost();
        })
        .catch(() => {});
    }
    return resp;
  });
};

function setupDiscoveryFetch(input, init) {
  const path = String(input || "");
  const setupPath = path.startsWith("/api/discovery")
    ? "/api/setup/discovery" + path.slice("/api/discovery".length)
    : path;
  const options = init ? { ...init } : {};
  const headers = new Headers(options.headers || {});
  if (setupOperationId) {
    headers.set("X-Setup-Operation-ID", setupOperationId);
  }
  options.headers = headers;
  return fetch(setupPath, options);
}

// One routing authority for discovery mutations shared between the workflows:
// Guided Setup speaks the operation-gated /api/setup/discovery aliases,
// Maintenance the generic authenticated /api/discovery routes. The context is
// decided before the request is sent — never probed via a Setup 409.
function discoveryFetch(input, init, context) {
  if (context === "setup") return setupDiscoveryFetch(input, init);
  if (context === "maintenance") return fetch(input, init);
  throw new Error("Discovery request context is required");
}

// The shared source-config nodes move between the Setup parking/priority slots
// and the Maintenance source rows; the node's current owner is the context.
function discoveryContextFor(node) {
  return inlineConfigMountedInMaintenance(node) ? "maintenance" : "setup";
}

const els = {
  form: document.getElementById("scan-form"),
  cidr: document.getElementById("cidr-input"),
  button: document.getElementById("scan-button"),
  status: document.getElementById("scan-status"),
  error: document.getElementById("scan-error"),
  count: document.getElementById("results-count"),
  localApiCount: document.getElementById("local-api-count"),
  empty: document.getElementById("results-empty"),
  list: document.getElementById("results-list"),
  accumulate: document.getElementById("results-accumulate"),
  networksScan: document.getElementById("networks-scan"),
  networksSummary: document.getElementById("networks-summary"),
  networksWarnings: document.getElementById("networks-warnings"),
  networksEmpty: document.getElementById("networks-empty"),
  networksList: document.getElementById("networks-list"),
  networksDockerDetails: document.getElementById("networks-docker-details"),
  networksDockerList: document.getElementById("networks-docker-list"),
  gatewayStatus: document.getElementById("gateway-probe-status"),
  mdnsState: document.getElementById("mdns-state"),
  mdnsMessage: document.getElementById("mdns-message"),
  mdnsCount: document.getElementById("mdns-count"),
  mdnsToggle: document.getElementById("mdns-toggle"),
  mdnsRefresh: document.getElementById("mdns-refresh"),
  ignoredDetails: document.getElementById("ignored-devices"),
  ignoredSummary: document.getElementById("ignored-summary"),
  ignoredList: document.getElementById("ignored-list"),
  mqttDetails: document.getElementById("mqtt-details"),
  mqttCount: document.getElementById("mqtt-count"),
  mqttMessage: document.getElementById("mqtt-message"),
  mqttRefresh: document.getElementById("mqtt-refresh"),
  mqttEmpty: document.getElementById("mqtt-empty"),
  mqttList: document.getElementById("mqtt-list"),
  mqttCredentialForm: document.getElementById("mqtt-credential-form"),
  mqttCredentialLabel: document.getElementById("mqtt-credential-label"),
  mqttCredentialUsername: document.getElementById("mqtt-credential-username"),
  mqttCredentialPassword: document.getElementById("mqtt-credential-password"),
  mqttCredentialMessage: document.getElementById("mqtt-credential-message"),
  mqttCredentialSave: document.getElementById("mqtt-credential-save"),
  mqttCredentialList: document.getElementById("mqtt-credential-list"),
  mqttCredentialEmpty: document.getElementById("mqtt-credential-empty"),
  zendureCloudDetails: document.getElementById("zendure-cloud-details"),
  zendureCloudCount: document.getElementById("zendure-cloud-count"),
  zendureCloudMessage: document.getElementById("zendure-cloud-message"),
  zendureCloudForm: document.getElementById("zendure-cloud-token-form"),
  zendureCloudTokenInput: document.getElementById("zendure-cloud-token-input"),
  zendureCloudSave: document.getElementById("zendure-cloud-save"),
  zendureCloudTest: document.getElementById("zendure-cloud-test"),
  zendureCloudRefresh: document.getElementById("zendure-cloud-refresh"),
  zendureCloudForget: document.getElementById("zendure-cloud-forget"),
  zendureCloudTokenState: document.getElementById("zendure-cloud-token-state"),
  zendureCloudTls: document.getElementById("zendure-cloud-tls"),
  zendureCloudBroker: document.getElementById("zendure-cloud-broker"),
  zendureCloudLastStatus: document.getElementById("zendure-cloud-last-status"),
  zendureCloudLastError: document.getElementById("zendure-cloud-last-error"),
  zendureCloudEmpty: document.getElementById("zendure-cloud-empty"),
  zendureCloudList: document.getElementById("zendure-cloud-list"),
  mqttProposalsCount: document.getElementById("mqtt-proposals-count"),
  mqttProposalsMessage: document.getElementById("mqtt-proposals-message"),
  mqttProposalsEmpty: document.getElementById("mqtt-proposals-empty"),
  mqttProposalsList: document.getElementById("mqtt-proposals-list"),
  summaryDevices: document.getElementById("setup-summary-devices"),
  summaryNetworks: document.getElementById("setup-summary-networks"),
  summaryMdns: document.getElementById("setup-summary-mdns"),
  summaryMqtt: document.getElementById("setup-summary-mqtt"),
  discoveryProgress: document.getElementById("setup-discovery-progress"),
  discoveryProgressBar: document.getElementById("setup-discovery-progress-bar"),
  discoveryProgressText: document.getElementById("setup-discovery-progress-text"),
  discoveryIdle: document.getElementById("setup-discovery-idle"),
  discoveryReset: document.getElementById("setup-discovery-reset"),
  discoveryRun: document.getElementById("discovery-run"),
  discoverySourceCount: document.getElementById("discovery-source-count"),
  priorityList: document.getElementById("discovery-priority-list"),
  unifiedList: document.getElementById("unified-list"),
  unifiedEmpty: document.getElementById("unified-empty"),
  unifiedCount: document.getElementById("unified-count"),
  unifiedSourceSummary: document.getElementById("unified-source-summary"),
};

// Compact Setup summary lines. Counts and fixed labels only, written with
// textContent so no server-provided value reaches the DOM as markup.
function setSummary(el, text) {
  if (el) el.textContent = text;
}

function plural(count, singular) {
  return count + " " + singular + (count === 1 ? "" : "s");
}

const MDNS_POLL_INTERVAL_MS = 20000;

let scanning = false;
let directNetworks = [];
let gatewayNetworks = [];
// mDNS is its own source: always merged, never cleared by manual scans.
const mdnsDevices = new Map();
const ignoredMdnsDevices = new Map();
const mqttBrokers = new Map();

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderCredentialRollbackWarning(payload) {
  // The backend only sets credential_rollback when rolling back staged MQTT
  // credential changes itself failed, so the operator must be told manual
  // cleanup may be needed. Returns the escaped warning HTML, or "" when there
  // is nothing to report so a normal error is left untouched.
  const rollback = payload && payload.credential_rollback;
  const refs =
    rollback && Array.isArray(rollback.failed_refs) ? rollback.failed_refs : [];
  if (!rollback || !refs.length) return "";
  const tone = rollback.severity === "high" ? "error" : "warn";
  const mark = tone === "error" ? "×" : "!";
  const items = refs.map((ref) => "<li>" + escapeHtml(ref) + "</li>").join("");
  const detail = escapeHtml(
    rollback.message ||
      "One or more credential files could not be restored automatically."
  );
  return (
    '<div class="config-validation-item config-validation-item-' +
    tone +
    '" role="alert"><span class="config-validation-icon" aria-hidden="true">' +
    mark +
    "</span><div><strong>Credential rollback " +
    "failed.</strong> " +
    detail +
    " Manual inspection may be required — check the Admin secret storage " +
    "before retrying.<br>Affected references:" +
    '<ul class="credential-rollback-refs">' +
    items +
    "</ul></div></div>"
  );
}

function showCredentialRollbackWarning(el, payload) {
  // Shared DOM setter for both apply flows: it never assembles HTML itself, so
  // Setup and Maintenance render the identical warning.
  if (!el) return;
  const html = renderCredentialRollbackWarning(payload);
  el.innerHTML = html;
  el.hidden = !html;
}

function setStatus(text, tone) {
  els.status.textContent = text;
  els.status.className = "scan-status" + (tone ? " " + tone : "");
}

function showError(text) {
  if (!text) {
    els.error.hidden = true;
    els.error.textContent = "";
    return;
  }
  els.error.hidden = false;
  els.error.textContent = text;
}

const DISCOVERY_SOURCE_LABELS = {
  mdns: "mDNS",
  http_probe: "active scan",
  network_scan: "active scan",
  active_scan: "active scan",
  manual: "manual scan",
  manual_scan: "manual scan",
  configured: "configured",
};

function createDiscoverySession(mode) {
  return {
    active: false,
    startedAt: null,
    mode,
    devices: new Map(),
    networks: new Map(),
    scanQueue: [],
    scans: [],
    scanKeys: new Set(),
    generation: 0,
    progress: { total: 0, done: 0, failed: 0, active: 0 },
  };
}

const discoverySessions = {
  setup: createDiscoverySession("setup"),
  maintenance: createDiscoverySession("maintenance"),
};
const keptDevices = discoverySessions.setup.devices;

function discoveryDeviceType(device) {
  return String(
    device.device_type || device.api_family || device.role_suggestion || "device"
  ).toLowerCase();
}

function discoveryDeviceMatch(existing, incoming) {
  const serialA = String(existing.serial_number || "").trim().toLowerCase();
  const serialB = String(incoming.serial_number || "").trim().toLowerCase();
  if (serialA && serialB) return serialA === serialB;
  if (discoveryDeviceType(existing) !== discoveryDeviceType(incoming)) return false;
  const ipA = String(existing.ip || "").trim().toLowerCase();
  const ipB = String(incoming.ip || "").trim().toLowerCase();
  if (ipA && ipB) return ipA === ipB;
  const hostA = String(existing.host || existing.hostname || "").trim().toLowerCase();
  const hostB = String(incoming.host || incoming.hostname || "").trim().toLowerCase();
  return Boolean(hostA && hostB && hostA === hostB);
}

function normalizeDiscoverySource(source) {
  const value = String(source || "network_scan").toLowerCase();
  if (value === "manual") return "manual_scan";
  if (value === "http_probe") return "active_scan";
  if (value === "network_scan") return "active_scan";
  return value;
}

function discoveryRoleClass(role) {
  return "role-" + String(role || "unknown").replace(/[^a-z_]/gi, "");
}

function mergeDiscoveryDevice(session, device, source) {
  if (!device || typeof device !== "object") return null;
  const incoming = Object.assign({}, device);
  const incomingSources = source
    ? [normalizeDiscoverySource(source)]
    : sourcesOf(incoming).map(normalizeDiscoverySource);
  let matchKey = null;
  let existing = null;
  for (const [key, candidate] of session.devices) {
    if (discoveryDeviceMatch(candidate, incoming)) {
      matchKey = key;
      existing = candidate;
      break;
    }
  }
  const sources = Array.from(
    new Set((existing ? sourcesOf(existing) : []).map(normalizeDiscoverySource).concat(incomingSources))
  );
  const merged = Object.assign({}, existing || {}, incoming, { sources });
  [
    "model",
    "device_type",
    "api_family",
    "role_suggestion",
    "host",
    "hostname",
  ].forEach((field) => {
    if (!incoming[field] && existing && existing[field]) merged[field] = existing[field];
  });
  merged.serial_number = incoming.serial_number || (existing && existing.serial_number) || null;
  merged.display_name =
    incoming.display_name || incoming.model ||
    (existing && (existing.display_name || existing.model)) || "";
  const reachableSource = incomingSources.some(
    (value) => value === "active_scan" || value === "manual_scan"
  );
  if (incoming.ip && (reachableSource || !existing || !existing.ip)) {
    merged.ip = incoming.ip;
  }
  if (!(Number(incoming.port) > 0 && Number(incoming.port) <= 65535)) {
    if (existing && Number(existing.port) > 0 && Number(existing.port) <= 65535) {
      merged.port = existing.port;
    } else {
      delete merged.port;
    }
  }
  const key = matchKey || deviceKey(merged);
  session.devices.set(key, merged);
  return merged;
}

function resetDiscoverySession(session) {
  session.generation += 1;
  session.active = false;
  session.startedAt = null;
  session.devices.clear();
  session.networks.clear();
  session.scanQueue.length = 0;
  session.scans.length = 0;
  session.scanKeys.clear();
  session.mqttProposals = [];
  session.progress = { total: 0, done: 0, failed: 0, active: 0 };
}

function validateManualScanInput(raw) {
  const input = String(raw || "").trim();
  if (!input) return { error: "Enter an IPv4 address or CIDR range." };
  const parts = input.split("/");
  if (parts.length > 2) return { error: "Enter a valid IPv4 address or CIDR range." };
  const octets = parts[0].split(".");
  if (
    octets.length !== 4 ||
    octets.some((part) => !/^\d{1,3}$/.test(part) || Number(part) > 255)
  ) {
    return { error: "Enter a valid IPv4 address or CIDR range." };
  }
  let prefix = 32;
  if (parts.length === 2) {
    if (!/^\d{1,2}$/.test(parts[1])) {
      return { error: "Enter a valid IPv4 CIDR prefix." };
    }
    prefix = Number(parts[1]);
    if (prefix < 24 || prefix > 32) {
      return { error: "Scan ranges must be /24 or smaller." };
    }
  }
  const numbers = octets.map(Number);
  const allowed =
    numbers[0] === 10 ||
    (numbers[0] === 172 && numbers[1] >= 16 && numbers[1] <= 31) ||
    (numbers[0] === 192 && numbers[1] === 168) ||
    (numbers[0] === 169 && numbers[1] === 254) ||
    numbers[0] === 127;
  if (!allowed) {
    return { error: "Only private, link-local, or loopback IPv4 ranges can be scanned." };
  }
  if (prefix < 32) {
    const value =
      (((numbers[0] << 24) >>> 0) |
        (numbers[1] << 16) |
        (numbers[2] << 8) |
        numbers[3]) >>> 0;
    const mask = (0xffffffff << (32 - prefix)) >>> 0;
    const network = (value & mask) >>> 0;
    return {
      cidr:
        [network >>> 24, (network >>> 16) & 255, (network >>> 8) & 255, network & 255]
          .join(".") +
        "/" +
        prefix,
    };
  }
  return { cidr: numbers.join(".") + "/" + prefix };
}

function scanHostFraction(scan) {
  return scan.status === "running" && scan.total_hosts > 0
    ? Math.min(1, scan.checked_hosts / scan.total_hosts)
    : 0;
}

// Fully-completed work units are counted via done/failed; a still-running
// network scan adds its partial host fraction so the bar advances mid-scan.
function discoveryProgressPercent(session) {
  const total = session.progress.total;
  if (!total) return 0;
  let completed = session.progress.done + session.progress.failed;
  (session.scans || []).forEach((scan) => {
    completed += scanHostFraction(scan);
  });
  return Math.min(100, Math.round((completed / total) * 100));
}

function activeScanHostDetail(session) {
  const scan = (session.scans || []).find(
    (item) => item.status === "running" && item.total_hosts > 0
  );
  return scan
    ? " · Current: " + scan.cidr + " " + scan.checked_hosts + "/" +
        scan.total_hosts + " hosts"
    : "";
}

async function queueDiscoveryScans(session, cidrs, source, onUpdate) {
  const queued = [];
  (cidrs || []).forEach((raw) => {
    const checked = validateManualScanInput(raw);
    if (checked.error || session.scanKeys.has(checked.cidr)) return;
    session.scanKeys.add(checked.cidr);
    const scan = {
      cidr: checked.cidr,
      source: normalizeDiscoverySource(source),
      status: "queued",
      devices: [],
      error: null,
      generation: session.generation,
    };
    session.scanQueue.push(scan);
    session.scans.push(scan);
    session.progress.total += 1;
    queued.push(scan);
  });
  if (!queued.length) {
    if (onUpdate) onUpdate(session);
    return [];
  }
  session.active = true;
  session.startedAt = session.startedAt || Date.now();
  if (onUpdate) onUpdate(session);
  const results = await Promise.all(
    queued.map(async (scan) => {
      scan.status = "running";
      session.progress.active += 1;
      if (onUpdate) onUpdate(session);
      try {
        scan.devices = await maintenanceScanNetwork(scan.cidr, (progress) => {
          if (scan.generation !== session.generation) return;
          scan.total_hosts = progress.total_hosts;
          scan.checked_hosts = progress.checked_hosts;
          scan.progress_percent = progress.percent;
          scan.found_devices = progress.found_devices;
          if (onUpdate) onUpdate(session);
        }, session);
        if (scan.generation !== session.generation) {
          scan.status = "cancelled";
          return scan;
        }
        scan.devices.forEach((device) =>
          mergeDiscoveryDevice(session, device, scan.source)
        );
        scan.status = "done";
        session.progress.done += 1;
      } catch (err) {
        if (scan.generation !== session.generation) {
          scan.status = "cancelled";
          return scan;
        }
        scan.status = "failed";
        scan.error = err.message || String(err);
        session.progress.failed += 1;
      } finally {
        if (scan.generation !== session.generation) return;
        session.progress.active -= 1;
        const index = session.scanQueue.indexOf(scan);
        if (index >= 0) session.scanQueue.splice(index, 1);
        session.active = session.progress.active > 0 || session.scanQueue.length > 0;
        if (onUpdate) onUpdate(session);
      }
      return scan;
    })
  );
  return results;
}

// --- scanning (one or several networks) ----------------------------------

function updateBusy() {
  els.button.disabled = scanning;
  els.button.textContent = scanning ? "Scanning…" : "Scan manually";
  els.button.classList.toggle("is-scanning", scanning);
  notifySetupStatus();
}

// Every detected LAN network (direct + gateway, excluding Docker) is scanned
// together by Run discovery; Docker networks keep their own per-chip button.
function lanCidrs() {
  return combinedNetworks()
    .filter((net) => !net.is_docker_like)
    .map((net) => net.cidr);
}

// After the initial network discovery finishes, kick off a scan of every
// detected LAN network not already auto-scanned this session. Manual scans and
// Docker networks are never triggered here; the same CIDR is not re-scanned.
const autoScannedCidrs = new Set();
function runInitialScan() {
  const cidrs = lanCidrs().filter((cidr) => !autoScannedCidrs.has(cidr));
  if (scanning || !cidrs.length) return;
  for (const cidr of cidrs) autoScannedCidrs.add(cidr);
  runScans(cidrs);
}

async function runScans(cidrs, source) {
  const unique = [...new Set(cidrs.filter(Boolean))];
  if (!unique.length) {
    showError("Select at least one network, or enter a CIDR.");
    return;
  }
  showError("");
  scanning = true;
  const setupSession = discoverySessions.setup;
  const generation = setupSession.generation;
  updateBusy();
  setStatus("Starting scan of " + unique.length + " network(s)…", "is-running");
  renderAggregate();
  probeMqttNetworks(unique);
  const beforeFailed = setupSession.progress.failed;
  const scans = await queueDiscoveryScans(
    setupSession,
    unique,
    source || "active_scan",
    () => {
      renderSetupDiscoveryProgress();
      renderAggregate();
    }
  );
  scanning = setupSession.active;
  updateBusy();
  if (generation !== setupSession.generation) return;
  const failed = setupSession.progress.failed - beforeFailed;
  setStatus(
    "Discovery completed" + (failed ? " with warnings" : "") + ": " +
      setupSession.devices.size + " device(s) retained.",
    failed ? null : "is-done"
  );
  if (failed) {
    const first = scans.find((scan) => scan.status === "failed");
    showError(
      failed + " network(s) failed" + (first && first.error ? ": " + first.error : ".")
    );
  }
  refreshUnifiedDevices();
  runInitialScan();
}

function deviceKey(device) {
  if (device.serial_number) {
    return (device.api_family || "device") + ":" + device.serial_number;
  }
  if (device.id) return device.id;
  return (
    (device.source || "unknown") +
    ":" +
    (device.ip || "unknown") +
    ":" +
    (device.port || 80)
  );
}

function sourcesOf(device) {
  if (Array.isArray(device.sources) && device.sources.length) return device.sources;
  return [device.source || "network_scan"];
}

// `fresh` marks a record that an active network scan just confirmed online.
// Scan results carry no liveness field, so without this an old mDNS `stale`
// marker would leak onto a device the scan just reached. mDNS-only devices keep
// their own staleness — a scan never marks anything stale, only clears it.
function mergeDevice(map, device, fresh) {
  const key = deviceKey(device);
  const existing = map.get(key);
  if (!existing) {
    const entry = Object.assign({}, device, { sources: sourcesOf(device) });
    if (fresh) entry.stale = false;
    map.set(key, entry);
    return;
  }
  const sources = existing.sources.slice();
  for (const source of sourcesOf(device)) {
    if (!sources.includes(source)) sources.push(source);
  }
  // A verified/newer record wins for the mutable fields; keep the union of sources.
  const merged = Object.assign({}, existing, device, { sources });
  merged.verified = existing.verified || device.verified;
  if (fresh) merged.stale = false;
  map.set(key, merged);
}

function aggregateDevices() {
  const seen = new Map();
  for (const device of mdnsDevices.values()) mergeDevice(seen, device);
  for (const device of discoverySessions.setup.devices.values()) {
    mergeDevice(seen, device, true);
  }
  return [...seen.values()];
}

function commitDevices(devices, source) {
  (devices || []).forEach((device) =>
    mergeDiscoveryDevice(discoverySessions.setup, device, source || "active_scan")
  );
}

// Fingerprint of the discovery state that actually affects the Config draft.
// Volatile fields (last_seen, confidence, scan progress) are excluded so routine
// mDNS polling does not re-render the draft while the user is editing it.
function buildDiscoverySignature(devices, ignored) {
  const entries = devices
    .map((device) => ({
      id: deviceKey(device),
      role: String(device.role_suggestion || ""),
      ip: device.ip || "",
      port: device.port || "",
      serial_number: device.serial_number || "",
      device_type: device.device_type || "",
      api_family: device.api_family || "",
      verified: device.verified !== false,
      usable: isAutoConfigReady(device),
      stale: Boolean(device.stale),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const skipped = (ignored || [])
    .map((device) => ({
      id: deviceKey(device),
      reason: device.reason || "",
      device_type: device.device_type || "",
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  return JSON.stringify({ devices: entries, ignored: skipped });
}

let lastDiscoverySignature = null;

function activeScanLabel() {
  if (unifiedRunActive) return "Device scan";
  if (networkScanActive) return "Network scan";
  return "Discovery";
}

function renderSetupDiscoveryProgress() {
  const session = discoverySessions.setup;
  if (!els.discoveryProgress) return;
  const progress = session.progress;
  const completed = progress.done + progress.failed;
  const idle = progress.total === 0;
  els.discoveryProgress.hidden = idle;
  if (els.discoveryIdle) els.discoveryIdle.hidden = !idle;
  els.discoveryProgressBar.style.width = discoveryProgressPercent(session) + "%";
  els.discoveryProgressText.textContent =
    activeScanLabel() + ": " + completed + " of " + progress.total +
    " scans checked · Active: " + progress.active +
    activeScanHostDetail(session) + " · Found: " + session.devices.size +
    " · Failed: " + progress.failed;
}

function renderAggregate() {
  const devices = aggregateDevices();
  els.count.textContent = devices.length + " found";
  if (els.localApiCount) els.localApiCount.textContent = plural(devices.length, "device");
  updateDeviceSummary(devices);
  // The Config tab consumes the same aggregated devices, but its draft holds
  // live inputs: only re-sync when the discovered device set meaningfully
  // changed, otherwise an unchanged poll would reset the user's editing.
  const signature = buildDiscoverySignature(
    devices.filter(isConfigCandidate),
    Array.from(ignoredMdnsDevices.values())
  );
  if (signature !== lastDiscoverySignature) {
    lastDiscoverySignature = signature;
    syncConfigFromDiscovery();
  }
  const running = discoverySessions.setup.active;
  if (running) {
    setStatus(
      "Scanning… " + devices.length + " found",
      "is-running"
    );
  }

  if (!devices.length) {
    els.list.hidden = true;
    els.list.innerHTML = "";
    els.empty.hidden = false;
    els.empty.textContent = running
      ? "Please wait for scanning to finish. Scan results will appear here."
      : "No supported devices were found.";
    return;
  }
  els.empty.hidden = true;
  els.list.hidden = false;
  els.list.innerHTML = devices.map(renderDeviceCard).join("");
}

function updateDeviceSummary(devices) {
  const inverters = devices.filter(
    (d) => String(d.role_suggestion) === "inverter"
  ).length;
  const meters = devices.filter(
    (d) => String(d.role_suggestion) === "grid_meter"
  ).length;
  setSummary(
    els.summaryDevices,
    inverters || meters
      ? plural(inverters, "inverter") + ", " + plural(meters, "grid meter")
      : "none yet"
  );
  notifySetupStatus();
}

const SOURCE_LABELS = DISCOVERY_SOURCE_LABELS;

function sourceBadges(device) {
  return sourcesOf(device)
    .map((source) => {
      const normalized = normalizeDiscoverySource(source);
      const label = SOURCE_LABELS[normalized] || source;
      const cls = source === "mdns" ? "source-mdns" : "source-scan";
      return '<span class="source-badge ' + cls + '">' + escapeHtml(label) + "</span>";
    })
    .join("");
}

function renderDeviceCard(device) {
  const role = String(device.role_suggestion || "unknown");
  const roleClass = discoveryRoleClass(role);
  const serial = device.serial_number
    ? '<span class="v">' + escapeHtml(device.serial_number) + "</span>"
    : '<span class="v missing">missing</span>';
  const ready =
    device.usable_for_config !== undefined
      ? device.usable_for_config
      : device.config_ready;
  const confidence = Math.round((Number(device.confidence) || 0) * 100);
  const stale = device.stale
    ? '<span class="stale-badge">stale</span>'
    : "";

  return (
    '<article class="device-card">' +
    '<div class="device-card-head">' +
    '<span class="device-name">' +
    escapeHtml(device.display_name || device.device_type || "Device") +
    "</span>" +
    '<span class="device-role ' +
    escapeHtml(roleClass) +
    '">' +
    escapeHtml(role) +
    "</span>" +
    "</div>" +
    '<div class="device-sources">' +
    sourceBadges(device) +
    stale +
    "</div>" +
    '<div class="device-facts">' +
    fact("IP", escapeHtml(device.ip)) +
    fact("Serial", serial, true) +
    fact("API family", escapeHtml(device.api_family)) +
    fact("Type", escapeHtml(device.device_type)) +
    "</div>" +
    '<div class="device-card-foot">' +
    '<span class="readiness ' +
    (ready ? "ready" : "not-ready") +
    '">' +
    (ready ? "Config ready" : "Needs info") +
    "</span>" +
    '<span class="confidence">' +
    confidence +
    "% match</span>" +
    "</div>" +
    "</article>"
  );
}

function fact(label, valueHtml, rawHtml) {
  const inner = rawHtml ? valueHtml : '<span class="v">' + valueHtml + "</span>";
  return (
    '<div class="device-fact"><span class="k">' +
    escapeHtml(label) +
    "</span>" +
    inner +
    "</div>"
  );
}

// --- hardware-card role vocabulary ---------------------------------------
// One normalized hardware role decides the card colour for every discovery
// source. The transport a device was found over never changes it, and a role
// the backend did not positively identify stays neutral.

function hardwareCardKindForRole(role) {
  const kinds = { inverter: "inverter", grid_meter: "grid-meter" };
  return kinds[String(role || "").toLowerCase()] || null;
}

function hardwareCardClass(role) {
  const kind = hardwareCardKindForRole(role);
  return kind ? "hardware-card hardware-card-" + kind : "hardware-card";
}

// --- network suggestions -------------------------------------------------

async function loadNetworks() {
  networkDetectionActive = true;
  els.networksEmpty.hidden = false;
  els.networksEmpty.textContent = "Detecting local networks…";
  els.networksList.hidden = true;
  gatewayNetworks = [];
  try {
    try {
      const res = await fetch("/api/discovery/networks");
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data && data.error ? data.error : "detection failed");
      }
      renderNetworks(data);
    } catch (err) {
      renderNetworks({ networks: [], warnings: [err.message || String(err)] });
    }
    // Common router gateways are probed automatically alongside direct-route
    // detection, and any reachable network merges into the same list.
    await loadGatewayNetworks();
  } finally {
    networkDetectionActive = false;
  }
}

function renderNetworks(data) {
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  if (warnings.length) {
    els.networksWarnings.hidden = false;
    els.networksWarnings.innerHTML = warnings
      .map((w) => '<p class="network-warning">' + escapeHtml(w) + "</p>")
      .join("");
  } else {
    els.networksWarnings.hidden = true;
    els.networksWarnings.innerHTML = "";
  }

  directNetworks = Array.isArray(data.networks) ? data.networks : [];
  renderNetworkList();
}

// Direct-route networks plus gateway-probe networks in one deduplicated list;
// direct networks keep their backend order and win over a gateway duplicate.
function combinedNetworks() {
  const seen = new Set();
  const combined = [];
  for (const net of directNetworks.concat(gatewayNetworks)) {
    if (seen.has(net.cidr)) continue;
    seen.add(net.cidr);
    combined.push(net);
  }
  return combined;
}

function updateNetworkSummary() {
  const lan = combinedNetworks().filter((net) => !net.is_docker_like).length;
  const gateways = gatewayNetworks.length;
  setSummary(
    els.summaryNetworks,
    lan + " detected" +
      (gateways ? ", " + plural(gateways, "reachable gateway") : "")
  );
  // Compact pill for the global Detected networks / gateways section.
  const parts = [plural(lan, "network")];
  if (gateways) parts.push(plural(gateways, "gateway"));
  setSummary(els.networksSummary, lan || gateways ? parts.join(" · ") : "none yet");
}

function renderNetworkList() {
  updateNetworkSummary();
  const networks = combinedNetworks();
  if (!networks.length) {
    els.networksList.hidden = true;
    els.networksList.innerHTML = "";
    els.networksDockerDetails.hidden = true;
    els.networksDockerList.innerHTML = "";
    els.networksEmpty.hidden = false;
    els.networksEmpty.textContent =
      "No local networks or gateways detected yet.";
    return;
  }

  // Docker/container bridge networks are rarely useful for Zendure hardware, so
  // keep them out of the primary list and collapsed under an advanced section.
  const lan = networks.filter((net) => !net.is_docker_like);
  const docker = networks.filter((net) => net.is_docker_like);

  els.networksEmpty.hidden = lan.length > 0;
  if (!lan.length) {
    els.networksEmpty.hidden = false;
    els.networksEmpty.textContent =
      "No LAN networks detected yet. See advanced/container networks below.";
  }
  els.networksList.hidden = lan.length === 0;
  // LAN chips are info-only; Run discovery scans every detected LAN network
  // together (see runInitialScan).
  els.networksList.innerHTML = lan.map((net) => renderNetworkRow(net, false)).join("");

  els.networksDockerDetails.hidden = docker.length === 0;
  // Docker networks keep a per-chip Scan button (opt-in, not part of Run discovery).
  els.networksDockerList.innerHTML = docker
    .map((net) => renderNetworkRow(net, true))
    .join("");
}

function renderNetworkRow(net, withScanButton) {
  const isGateway = net.source === "gateway";
  const recommended = Boolean(net.scan_recommended) && !isGateway;
  const cidr = escapeHtml(net.cidr);

  let badgeClass = "badge-advanced";
  let badgeText = "Advanced";
  if (isGateway) {
    badgeClass = "badge-gateway";
    badgeText = "Gateway";
  } else if (recommended) {
    badgeClass = "badge-recommended";
    badgeText = "Recommended";
  }

  const meta = isGateway
    ? "via gateway " + escapeHtml(net.gateway_candidate) + " · not directly on this host"
    : escapeHtml(net.interface) + " · " + escapeHtml(net.address);
  const original =
    !isGateway && net.original_cidr && net.original_cidr !== net.cidr
      ? " · was " + escapeHtml(net.original_cidr)
      : "";
  const highlight = recommended || isGateway;
  const scanButton = withScanButton
    ? '<button type="button" class="primary-button compact network-scan" data-cidr="' +
      cidr +
      '">Scan</button>'
    : "";

  // Compact chip: CIDR + badge and a short meta line. The full reason is a hover
  // title so chips stay small side by side. LAN chips have no button (scanned
  // together by Run discovery); Docker chips carry their own opt-in Scan button.
  return (
    '<div class="network-chip' +
    (highlight ? " is-recommended" : "") +
    '" role="listitem" title="' +
    escapeHtml(net.reason) +
    '">' +
    '<div class="network-row-top">' +
    '<span class="network-cidr">' +
    cidr +
    "</span>" +
    '<span class="network-badge ' +
    badgeClass +
    '">' +
    badgeText +
    "</span>" +
    "</div>" +
    '<div class="network-meta">' +
    meta +
    original +
    "</div>" +
    scanButton +
    "</div>"
  );
}

function triggerScan(cidr) {
  els.cidr.value = cidr;
  runScans([cidr]);
}

// --- gateway candidate probe ---------------------------------------------

function signalLabel(signal) {
  const match = /^tcp_(\d+)_(open|refused)$/.exec(String(signal));
  if (!match) return String(signal);
  return "TCP/" + match[1] + (match[2] === "refused" ? " (refused)" : "");
}

function gatewayReason(cand) {
  const signals = Array.isArray(cand.signals) ? cand.signals : [];
  const ports = signals.map(signalLabel).join(", ");
  const base = ports ? "Responded on " + ports : "Responded";
  return base + " · discovered indirectly, not directly seen by this host";
}

// Reachable gateway candidates become normal selectable network entries so they
// merge into the same multi-select list as directly detected networks.
function normalizeGatewayCandidate(cand) {
  return {
    cidr: cand.network,
    source: "gateway",
    gateway_candidate: cand.gateway_candidate,
    signals: cand.signals,
    scan_recommended: Boolean(cand.scan_supported),
    reason: gatewayReason(cand),
    is_docker_like: false,
  };
}

async function loadGatewayNetworks() {
  els.gatewayStatus.textContent = "Probing common router gateways…";
  try {
    const res = await setupDiscoveryFetch("/api/discovery/gateway-probe", {
      method: "POST",
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data && data.error ? data.error : "gateway probe failed");
    }
    const candidates = Array.isArray(data.candidates) ? data.candidates : [];
    const reachable = candidates.filter((c) => c.status === "reachable");
    gatewayNetworks = reachable.map(normalizeGatewayCandidate);
    els.gatewayStatus.textContent = reachable.length
      ? "Added " +
        reachable.length +
        " reachable gateway network(s), discovered indirectly (not directly on this host)."
      : "No additional networks found via common router gateways.";
    renderNetworkList();
  } catch (err) {
    gatewayNetworks = [];
    els.gatewayStatus.textContent =
      "Gateway probe unavailable: " + (err.message || String(err));
    renderNetworkList();
  }
}

// --- events --------------------------------------------------------------

function handleScanButtonClick(event) {
  const button = event.target.closest(".network-scan");
  if (!button) return;
  const cidr = button.getAttribute("data-cidr");
  if (cidr) triggerScan(cidr);
}

els.networksList.addEventListener("click", handleScanButtonClick);
els.networksDockerList.addEventListener("click", handleScanButtonClick);

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const checked = validateManualScanInput(els.cidr.value);
  if (checked.error) {
    showError(checked.error);
    return;
  }
  runScans([checked.cidr], "manual_scan");
});

if (els.discoveryReset) {
  els.discoveryReset.addEventListener("click", () => {
    resetDiscoverySession(discoverySessions.setup);
    keptDevices.clear();
    mdnsDevices.clear();
    autoScannedCidrs.clear();
    lastDiscoverySignature = null;
    showError("");
    setStatus("Discovery results reset.", "is-done");
    renderSetupDiscoveryProgress();
    renderAggregate();
  });
}

// --- Unified discovery preparation ------------------------------------
// Admin-only setup orchestration: pick which sources to scan and the order
// used to select a device found through more than one source. Never writes the
// EMS config and never changes how EMS runs.

const DISCOVERY_SOURCE_META = {
  local_api: { label: "Local API", detail: "local-api-details" },
  local_mqtt: { label: "Local MQTT", detail: "mqtt-details" },
  zendure_mqtt: { label: "Zendure MQTT", detail: "zendure-cloud-details" },
};
const DEFAULT_DISCOVERY_PRIORITY = ["local_api", "local_mqtt", "zendure_mqtt"];

let discoveryPreparation = {
  discovery_priority: DEFAULT_DISCOVERY_PRIORITY.slice(),
  sources: {
    local_api: { enabled: true },
    local_mqtt: { enabled: true },
    zendure_mqtt: { enabled: true },
  },
};
// A unified "Run discovery" and a standalone "Scan networks" run are mutually
// exclusive and both drive the shared setup discovery session. `scanCancelRequested`
// is the single cancel flag both drivers watch; `networkDetectionActive` is true
// while network detection (direct routes + the gateway probe) is still in flight,
// so a device scan can start on the first network yet stay busy until the gateway
// probe finishes and every LAN network has been scanned.
let unifiedRunActive = false;
let networkScanActive = false;
let scanCancelRequested = false;
let networkDetectionActive = false;
// Once discovery has completed at least once, the single primary action reads
// "Run discovery again" so a repeat scan is clearly a rescan, not a first run.
let unifiedDiscoveryHasRun = false;
let openInlineConfigSource = null;
let lastUnifiedDetails = {};
let lastUnifiedData = null;

function discoverySourceLabel(source) {
  const meta = DISCOVERY_SOURCE_META[source];
  return meta ? meta.label : source;
}

function discoverySourceEnabled(source) {
  const entry = discoveryPreparation.sources && discoveryPreparation.sources[source];
  return !(entry && entry.enabled === false);
}

function normalizePreparation(data) {
  const priority = [];
  ((data && data.discovery_priority) || []).forEach((source) => {
    if (DISCOVERY_SOURCE_META[source] && priority.indexOf(source) === -1) {
      priority.push(source);
    }
  });
  DEFAULT_DISCOVERY_PRIORITY.forEach((source) => {
    if (priority.indexOf(source) === -1) priority.push(source);
  });
  const sources = {};
  priority.forEach((source) => {
    const entry = ((data && data.sources) || {})[source];
    sources[source] = { enabled: !(entry && entry.enabled === false) };
  });
  return { discovery_priority: priority, sources };
}

async function loadDiscoveryPreparation() {
  try {
    const res = await fetch("/api/discovery/preparation");
    if (res.ok) discoveryPreparation = normalizePreparation(await res.json());
  } catch (err) {
    /* keep defaults on failure */
  }
  renderDiscoveryPreparation();
  loadMqttCredentials();
}

async function persistDiscoveryPreparation() {
  renderDiscoveryPreparation();
  try {
    const res = await setupDiscoveryFetch("/api/discovery/preparation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(discoveryPreparation),
    });
    if (res.ok) discoveryPreparation = normalizePreparation(await res.json());
  } catch (err) {
    /* the UI already reflects the intended change */
  }
  renderDiscoveryPreparation();
  refreshUnifiedDevices();
  // A priority change recalculates automatic transport selections (manual ones
  // stay) and invalidates the Config preview so it regenerates.
  syncConfigFromDiscovery();
}

function reassertPriorityOverManualTransport() {
  let draftChanged = false;
  for (const item of configDraftItems) {
    if (item.role === "inverter" && item.auto_added === false) {
      item.auto_added = true;
      draftChanged = true;
    }
  }
  if (draftChanged) saveConfigDraft();
  let mqttChanged = false;
  for (const entry of zendureMqttPreviewProposals.values()) {
    if (entry.selection_origin === "manual") {
      entry.selection_origin = "priority";
      mqttChanged = true;
    }
  }
  if (mqttChanged) saveMqttPreviewProposals();
}

function moveDiscoverySource(source, delta) {
  const priority = discoveryPreparation.discovery_priority.slice();
  const index = priority.indexOf(source);
  const next = index + delta;
  if (index === -1 || next < 0 || next >= priority.length) return;
  priority.splice(index, 1);
  priority.splice(next, 0, source);
  discoveryPreparation.discovery_priority = priority;
  reassertPriorityOverManualTransport();
  persistDiscoveryPreparation();
}

function toggleDiscoverySource(source, enabled) {
  if (!discoveryPreparation.sources[source]) discoveryPreparation.sources[source] = {};
  discoveryPreparation.sources[source].enabled = enabled;
  persistDiscoveryPreparation();
}

function openSourceDetail(source) {
  const meta = DISCOVERY_SOURCE_META[source];
  const el = meta && document.getElementById(meta.detail);
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderDiscoveryPreparation() {
  if (!els.priorityList) return;
  // Return any live config node to its hidden parking slot before the list HTML
  // is wiped, otherwise the moved node would be detached and lost on re-render.
  parkInlineConfigs();
  const priority = discoveryPreparation.discovery_priority;
  els.priorityList.innerHTML = priority
    .map((source, index) => renderPrioritySourceRow(source, index, priority.length))
    .join("");
  if (openInlineConfigSource) mountInlineConfig(openInlineConfigSource);
  updateDiscoverySourceCount();
}

// The source configuration controls live as stable nodes (with fixed IDs and
// bound handlers) in a hidden parking container. Configure moves the matching
// node into the open priority row's slot; closing or re-rendering parks it again.
function parkInlineConfigs() {
  const parking = document.getElementById("inline-config-parking");
  if (!parking) return;
  Object.keys(DISCOVERY_SOURCE_META).forEach((source) => {
    const node = document.querySelector('[data-inline-config="' + source + '"]');
    if (!node || node.parentElement === parking) return;
    // A node open in a Maintenance source row belongs to that view; the setup
    // flow's re-renders must not steal it out from under the operator.
    if (inlineConfigMountedInMaintenance(node)) return;
    parking.appendChild(node);
  });
}

function mountInlineConfig(source) {
  if (!els.priorityList) return;
  const node = document.querySelector('[data-inline-config="' + source + '"]');
  const slot = els.priorityList.querySelector('[data-inline-slot="' + source + '"]');
  if (node && slot) slot.appendChild(node);
}

// Maintenance "Add more devices" reuses the same parked source-config nodes
// (local MQTT credential pool, Zendure cloud API key) instead of duplicating
// the forms: opening a source row moves the node into its slot, closing the
// row or leaving the maintenance view parks it again.
function inlineConfigMountedInMaintenance(node) {
  return Boolean(node && node.closest("#maintenance-add-devices"));
}

function mountMaintenanceSourceConfig(source) {
  const node = document.querySelector('[data-inline-config="' + source + '"]');
  const slot = document.querySelector(
    '[data-maintenance-source-slot="' + source + '"]'
  );
  if (!node || !slot) return;
  slot.appendChild(node);
  if (source === "local_mqtt") loadMqttCredentials();
  if (source === "zendure_mqtt") loadZendureCloudSettings();
}

function parkMaintenanceSourceConfig(source) {
  const parking = document.getElementById("inline-config-parking");
  const node = document.querySelector('[data-inline-config="' + source + '"]');
  if (parking && node && inlineConfigMountedInMaintenance(node)) {
    parking.appendChild(node);
  }
}

function parkMaintenanceSourceConfigs() {
  document.querySelectorAll("[data-maintenance-source]").forEach((row) => {
    row.open = false;
    parkMaintenanceSourceConfig(row.getAttribute("data-maintenance-source"));
  });
}

// Passive status text next to the single Run discovery action, so the operator
// sees how many sources that one button will scan without offering per-source
// scan buttons in the header.
function updateDiscoverySourceCount() {
  if (!els.discoverySourceCount) return;
  const enabled = discoveryPreparation.discovery_priority.filter(discoverySourceEnabled);
  els.discoverySourceCount.textContent = plural(enabled.length, "source") + " enabled";
}

function renderPrioritySourceRow(source, index, total) {
  const enabled = discoverySourceEnabled(source);
  const label = escapeHtml(discoverySourceLabel(source));
  const safeSource = escapeHtml(source);
  const upDisabled = index === 0 ? "disabled" : "";
  const downDisabled = index === total - 1 ? "disabled" : "";
  const configuring = openInlineConfigSource === source;
  return (
    '<li class="prep-source-item' + (configuring ? " is-configuring" : "") +
      '" data-source="' + safeSource + '">' +
      '<div class="prep-source-row' + (enabled ? "" : " is-disabled") + '">' +
        '<span class="prep-source-rank">' + (index + 1) + "</span>" +
        '<label class="prep-source-toggle">' +
          '<input type="checkbox" data-prep-toggle ' + (enabled ? "checked" : "") + ">" +
          '<span class="prep-source-label">' + label + "</span>" +
        "</label>" +
        (enabled ? "" : '<span class="prep-source-off">disabled</span>') +
        '<span class="prep-source-actions">' +
          '<button type="button" class="secondary-button compact" data-prep-up ' +
            upDisabled + ' aria-label="Move ' + label + ' up">↑</button>' +
          '<button type="button" class="secondary-button compact" data-prep-down ' +
            downDisabled + ' aria-label="Move ' + label + ' down">↓</button>' +
          '<button type="button" class="secondary-button compact" data-prep-configure' +
            ' aria-expanded="' + (configuring ? "true" : "false") + '">' +
            (configuring ? "Close" : "Configure") + "</button>" +
        "</span>" +
      "</div>" +
      (configuring ? renderInlineConfig(source) : "") +
    "</li>"
  );
}

// Compact per-source controls that expand directly under the priority row so the
// user never has to scroll to the detail panels below to adjust one source.
function renderInlineConfig(source) {
  const enabled = discoverySourceEnabled(source);
  const label = escapeHtml(discoverySourceLabel(source));
  const safeSource = escapeHtml(source);
  const networksLink = source === "local_api"
    ? '<button type="button" class="secondary-button compact" data-prep-networks>' +
      "Detected networks</button>"
    : "";
  return (
    '<div class="prep-source-config" data-prep-config>' +
      '<div class="prep-config-status">' +
        '<span class="prep-config-state">' + (enabled ? "Enabled" : "Disabled") + "</span>" +
        '<span class="prep-config-detail">' + inlineConfigStatus(source) + "</span>" +
      "</div>" +
      '<div class="prep-config-slot" data-inline-slot="' + safeSource + '"></div>' +
      '<div class="prep-config-actions">' +
        networksLink +
        '<button type="button" class="primary-button compact" data-prep-rescan>' +
          "Rescan " + label + "</button>" +
        '<button type="button" class="secondary-button compact" data-prep-open-details>' +
          "Open results</button>" +
      "</div>" +
    "</div>"
  );
}

function inlineConfigStatus(source) {
  const detail = lastUnifiedDetails[source] || {};
  const count = Number(detail.device_count || 0);
  if (source === "zendure_mqtt") {
    const credentialState = zendureCloudTokenSaved
      ? "credential saved"
      : "no credential saved";
    return escapeHtml(credentialState + " · " + plural(count, "device") + " found");
  }
  return escapeHtml(plural(count, "device") + " found");
}

function toggleInlineConfig(source) {
  openInlineConfigSource = openInlineConfigSource === source ? null : source;
  renderDiscoveryPreparation();
}

async function rescanSource(source) {
  const jobs = [];
  if (source === "local_api") {
    await loadNetworks();
    runInitialScan();
    jobs.push(refreshMdns());
  } else if (source === "local_mqtt") {
    jobs.push(refreshMqttBrokers());
  } else if (source === "zendure_mqtt") {
    jobs.push(refreshZendureCloudDiscovery());
  }
  await Promise.all(jobs.map((job) => Promise.resolve(job).catch(() => {})));
  await refreshUnifiedDevices();
}

if (els.priorityList) {
  els.priorityList.addEventListener("click", (event) => {
    const row = event.target.closest("[data-source]");
    if (!row) return;
    const source = row.getAttribute("data-source");
    if (event.target.closest("[data-prep-up]")) moveDiscoverySource(source, -1);
    else if (event.target.closest("[data-prep-down]")) moveDiscoverySource(source, 1);
    else if (event.target.closest("[data-prep-configure]")) toggleInlineConfig(source);
    else if (event.target.closest("[data-prep-rescan]")) rescanSource(source);
    else if (event.target.closest("[data-prep-open-details]")) openSourceDetail(source);
    else if (event.target.closest("[data-prep-networks]")) {
      const networks = document.getElementById("discovery-networks");
      if (networks) networks.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  els.priorityList.addEventListener("change", (event) => {
    const toggle = event.target.closest("[data-prep-toggle]");
    const row = event.target.closest("[data-source]");
    if (toggle && row) toggleDiscoverySource(row.getAttribute("data-source"), toggle.checked);
  });
}

// With refreshSources the backend orchestrates the full fresh-install run:
// it refreshes every enabled source exactly once (failures isolated per
// source) before unifying. Without it this stays the read-only unify used
// after rescans and on initial load.
async function refreshUnifiedDevices(refreshSources) {
  try {
    const init = { method: "POST" };
    if (refreshSources) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify({ refresh: true });
    }
    const res = await setupDiscoveryFetch("/api/discovery/run", init);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (
        err &&
        (err.error === "system_build_alignment_required" ||
          SETUP_DISCOVERY_GATE_ERRORS.has(err.error))
      ) {
        returnToSystemBuildStep(err.message);
      }
      return;
    }
    const data = await res.json();
    renderUnifiedDevices(data);
    if (refreshSources) syncSourcePanels(data);
  } catch (err) {
    /* leave the previous render in place */
  }
}

function returnToSystemBuildStep(message) {
  // Ignore a stale discovery gate error that arrives after the user already
  // returned to Step 1 (build reselected/cancelled); it must not hijack it.
  if (setupState.activeStep === "release") return;
  // The server refused a discovery action until the selected System Build is
  // aligned. Return to Step 1; discovery never auto-continues from here.
  setActiveStep("release");
  if (setupSystemBuildEls.error) {
    setupSystemBuildEls.error.hidden = false;
    setupSystemBuildEls.error.textContent =
      message || "Align the selected System Build before running discovery.";
  }
}

// Re-read the per-source detail panels after a backend-orchestrated refresh.
// Pure display sync: the returned details already carry the fresh state, and
// the remaining loaders are read-only status endpoints.
function syncSourcePanels(data) {
  const details = (data && data.details) || {};
  const zendure = details.zendure_mqtt || {};
  zendureCloudDevices.length = 0;
  for (const device of Array.isArray(zendure.candidates) ? zendure.candidates : []) {
    zendureCloudDevices.push(device);
  }
  renderZendureCloudDevices();
  pollMdns().catch(() => {});
  loadMqttBrokers().catch(() => {});
  loadZendureCloudSettings().catch(() => {});
}

function renderUnifiedDevices(data) {
  lastUnifiedData = data || null;
  const devices = Array.isArray(data && data.devices) ? data.devices : [];
  lastUnifiedDetails = (data && data.details) || {};
  if (els.unifiedCount) els.unifiedCount.textContent = plural(devices.length, "device");
  renderUnifiedSourceSummary(lastUnifiedDetails);
  if (openInlineConfigSource) renderDiscoveryPreparation();
  if (!els.unifiedList || !els.unifiedEmpty) return;
  if (!devices.length) {
    els.unifiedList.hidden = true;
    els.unifiedList.innerHTML = "";
    els.unifiedEmpty.hidden = false;
    els.unifiedEmpty.textContent =
      "No devices detected yet. Open a source below to check its status or credentials.";
    return;
  }
  els.unifiedEmpty.hidden = true;
  els.unifiedList.hidden = false;
  els.unifiedList.innerHTML = devices.map(renderUnifiedDeviceCard).join("");
}

// The setup unified overview reuses the Maintenance "Configuration & Hardware"
// collapsible hardware-card list (`renderConfigAvailableCard` /
// `renderHardwareCard`) so both flows read the same. The one setup-specific
// extra kept here is the "Selected by priority: …" line that explains why a
// source was chosen; the overview is read-only, so it carries no actions.
function renderUnifiedDeviceCard(device) {
  const sourceId = String(
    device.id || device.serial_number || device.display_name || "device"
  );
  const role = String(device.role || "unknown");
  const isGridMeter = role === "grid_meter";
  const hardwareRole = isGridMeter ? "grid_meter" : "inverter";
  const id = escapeHtml(sourceId);
  const safe = sourceId.replace(/[^a-z0-9]/gi, "-");
  const endpoint = String(device.ip || "");
  const meta = [
    endpoint,
    device.serial_number ? "SN " + device.serial_number : "SN missing",
    device.api_family,
    device.device_type,
  ]
    .filter(Boolean)
    .map((part) => escapeHtml(String(part)))
    .join(" · ");
  const title = isGridMeter
    ? "Grid meter"
    : role === "inverter"
    ? "Inverter"
    : "Device";
  const model =
    device.display_name || device.model_hint || device.device_type || "Device";
  const open = openHardwareCards.has(sourceId);
  const status = device.confidence === "low" ? "Low confidence" : "Detected";

  const badges = (Array.isArray(device.sources) ? device.sources : [])
    .map((source) => {
      const selected = source === device.selected_source ? " is-selected" : "";
      return (
        '<span class="source-badge source-unified' + selected + '">' +
        escapeHtml(discoverySourceLabel(source)) + "</span>"
      );
    })
    .join("");
  const selectedLine = device.selected_source
    ? '<span class="unified-selected">Selected by priority: ' +
      escapeHtml(discoverySourceLabel(device.selected_source)) + "</span>"
    : "";

  const body =
    '<div class="device-facts">' +
    fact("IP", escapeHtml(endpoint)) +
    fact(
      "Serial",
      device.serial_number
        ? '<span class="v">' + escapeHtml(device.serial_number) + "</span>"
        : '<span class="v missing">missing</span>',
      true
    ) +
    fact("API family", escapeHtml(String(device.api_family || ""))) +
    fact("Type", escapeHtml(String(device.device_type || ""))) +
    '<div class="device-sources">' + badges + selectedLine + "</div>" +
    "</div>";

  return (
    '<article class="' + hardwareCardClass(hardwareRole) +
    '" data-source-id="' + id + '"' +
    (open ? ' data-open="true"' : "") + ">" +
    '<div class="hardware-card-head">' +
    '<button type="button" class="hardware-card-summary" data-unified-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") + '"' +
    ' aria-controls="unified-body-' + safe + '">' +
    '<span class="hardware-card-title">' + escapeHtml(title) + "</span>" +
    '<span class="hardware-card-model">' + escapeHtml(String(model)) + "</span>" +
    '<span class="hardware-card-meta">' + meta + "</span>" +
    "</button>" +
    '<div class="hardware-card-actions">' +
    '<span class="hardware-card-status">' + escapeHtml(status) + "</span>" +
    '<button type="button" class="hardware-card-toggle" data-unified-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") +
    '" aria-controls="unified-body-' + safe +
    '" aria-label="' + (open ? "Collapse " : "Expand ") + escapeHtml(title) + '">' +
    '<span aria-hidden="true">' + (open ? "▾" : "▸") + "</span>" +
    "</button>" +
    "</div>" +
    "</div>" +
    '<div class="hardware-card-body" id="unified-body-' + safe + '"' +
    (open ? "" : " hidden") + ">" +
    (open ? body : "") +
    "</div>" +
    "</article>"
  );
}

function renderUnifiedSourceSummary(details) {
  if (!els.unifiedSourceSummary) return;
  const chips = DEFAULT_DISCOVERY_PRIORITY.map((source) => {
    const detail = details[source] || {};
    const count = Number(detail.device_count || 0);
    const state = discoverySourceEnabled(source)
      ? plural(count, "device") + " found"
      : "disabled";
    return '<button type="button" class="prep-source-chip" data-source-chip="' +
      escapeHtml(source) + '">' +
      '<span class="prep-chip-label">' + escapeHtml(discoverySourceLabel(source)) + "</span>" +
      '<span class="prep-chip-count">' + escapeHtml(state) + "</span>" +
      "</button>";
  }).join("");
  els.unifiedSourceSummary.hidden = false;
  els.unifiedSourceSummary.innerHTML = chips;
}

if (els.unifiedSourceSummary) {
  els.unifiedSourceSummary.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-source-chip]");
    if (chip) openSourceDetail(chip.getAttribute("data-source-chip"));
  });
}

// Expand/collapse the hardware-card rows in the unified overview, reusing the
// shared openHardwareCards open-state so the layout matches the config list.
if (els.unifiedList) {
  els.unifiedList.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-unified-toggle]");
    if (!toggle) return;
    const sourceId = toggle.getAttribute("data-unified-toggle");
    if (openHardwareCards.has(sourceId)) openHardwareCards.delete(sourceId);
    else openHardwareCards.add(sourceId);
    renderUnifiedDevices(lastUnifiedData);
  });
}

function discoveryRunLabel() {
  return unifiedDiscoveryHasRun ? "Run discovery again" : "Run discovery";
}

// Both primary actions are single toggle buttons: idle they start a run, busy
// they cancel it. While one run is active the other button is disabled so the
// two drivers never fight over the shared session.
function updateScanButtons() {
  if (els.discoveryRun) {
    if (unifiedRunActive) {
      els.discoveryRun.textContent = "Cancel discovery";
      els.discoveryRun.classList.add("is-scanning", "is-cancel");
      els.discoveryRun.disabled = false;
    } else {
      els.discoveryRun.textContent = discoveryRunLabel();
      els.discoveryRun.classList.remove("is-scanning", "is-cancel");
      els.discoveryRun.disabled = networkScanActive;
    }
  }
  if (els.networksScan) {
    if (networkScanActive) {
      els.networksScan.textContent = "Cancel scan";
      els.networksScan.classList.add("is-scanning", "is-cancel");
      els.networksScan.disabled = false;
    } else {
      els.networksScan.textContent = "Scan networks";
      els.networksScan.classList.remove("is-scanning", "is-cancel");
      els.networksScan.disabled = unifiedRunActive;
    }
  }
}

// Cancel any in-flight scan without discarding devices already found: bumping the
// session generation makes queued/running network scans abandon their results and
// the runScans loop return early, while settleNetworkScans unblocks on the flag.
function cancelActiveScans(message) {
  scanCancelRequested = true;
  const session = discoverySessions.setup;
  session.generation += 1;
  session.scanQueue.length = 0;
  session.progress.active = 0;
  session.active = false;
  scanning = false;
  networkDetectionActive = false;
  updateBusy();
  renderSetupDiscoveryProgress();
  updateScanButtons();
  if (message) setStatus(message, "is-done");
}

// Resolve once network detection has finished (gateway probe included) and the
// device-scan queue has drained. Device scans are launched for LAN networks as
// they appear, so a scan can start on the first network yet keep running until
// the gateway probe adds any further networks and they too have been scanned.
function settleNetworkScans() {
  return new Promise((resolve) => {
    const tick = () => {
      if (scanCancelRequested) return resolve();
      const pending = lanCidrs().filter((cidr) => !autoScannedCidrs.has(cidr));
      if (!scanning && pending.length) runInitialScan();
      if (!networkDetectionActive && !scanning && !pending.length) return resolve();
      window.setTimeout(tick, 200);
    };
    tick();
  });
}

// Detect networks and scan every LAN network, awaited to full completion. `rescan`
// clears the per-session scan memory so already-scanned networks are scanned again.
async function detectAndScanNetworks(rescan) {
  if (rescan) {
    autoScannedCidrs.clear();
    discoverySessions.setup.scanKeys.clear();
  }
  // Detection runs alongside the device scan rather than blocking it: loadNetworks
  // renders direct networks immediately, then awaits the gateway probe; settle
  // keeps the run busy until both have finished.
  loadNetworks().catch(() => {});
  await settleNetworkScans();
}

async function networkScanRun(rescan) {
  if (unifiedRunActive || networkScanActive) return;
  networkScanActive = true;
  scanCancelRequested = false;
  updateScanButtons();
  try {
    await detectAndScanNetworks(rescan);
    await refreshUnifiedDevices();
  } finally {
    networkScanActive = false;
    updateScanButtons();
  }
}

async function runUnifiedDiscovery() {
  if (unifiedRunActive || networkScanActive) return;
  unifiedRunActive = true;
  scanCancelRequested = false;
  updateScanButtons();
  try {
    const enabled = discoveryPreparation.discovery_priority.filter(discoverySourceEnabled);
    if (enabled.indexOf("local_api") !== -1 || enabled.indexOf("local_mqtt") !== -1) {
      await detectAndScanNetworks(false);
    }
    // The backend owns the per-source refresh fan-out (enabled sources only,
    // one refresh per source, failures isolated); the UI only triggers it. A
    // cancelled run just re-renders the already-collected state read-only.
    if (scanCancelRequested) {
      await refreshUnifiedDevices();
    } else {
      await refreshUnifiedDevices(true);
      unifiedDiscoveryHasRun = true;
    }
  } finally {
    unifiedRunActive = false;
    updateScanButtons();
  }
}

if (els.discoveryRun) {
  els.discoveryRun.addEventListener("click", () => {
    if (unifiedRunActive) cancelActiveScans("Discovery cancelled.");
    else runUnifiedDiscovery();
  });
}

if (els.networksScan) {
  els.networksScan.addEventListener("click", () => {
    if (networkScanActive) cancelActiveScans("Network scan cancelled.");
    else networkScanRun(true);
  });
}

// --- live mDNS discovery -------------------------------------------------

const MDNS_STATE_TEXT = {
  running_with_devices: "enabled",
  running_no_devices: "enabled",
  disabled: "disabled",
  unavailable_dependency: "unavailable",
  unavailable_runtime: "unavailable",
};

function renderMdnsStatus(status) {
  const state = String(
    status.state || (status.available ? "disabled" : "unavailable_dependency")
  );
  els.mdnsState.textContent = MDNS_STATE_TEXT[state] || state;
  els.mdnsState.className =
    "network-badge " +
    (state.indexOf("running_") === 0 ? "badge-recommended" : "badge-advanced");
  els.mdnsMessage.textContent =
    status.message || "Automatic mDNS discovery is unavailable in this runtime.";
  const count = Number(status.verified_count) || 0;
  els.mdnsCount.textContent = count + " found";
  notifySetupStatus();
  const unavailable = state.indexOf("unavailable_") === 0;
  els.mdnsToggle.disabled = unavailable;
  els.mdnsRefresh.disabled = unavailable;
  els.mdnsToggle.textContent = status.enabled ? "Disable" : "Enable";
  els.mdnsToggle.setAttribute("aria-pressed", status.enabled ? "true" : "false");
  const summary =
    state.indexOf("running_") === 0
      ? "running"
      : state.indexOf("unavailable_") === 0
      ? "unavailable"
      : state;
  setSummary(els.summaryMdns, summary);
}

function renderIgnoredDevices() {
  const devices = Array.from(ignoredMdnsDevices.values());
  notifySetupStatus();
  els.ignoredDetails.hidden = devices.length === 0;
  els.ignoredSummary.textContent = "Ignored devices (" + devices.length + ")";
  els.ignoredList.innerHTML = devices
    .map((device) => {
      const endpoint = device.ip
        ? String(device.ip) + (device.port ? ":" + String(device.port) : "")
        : "No IP address";
      const hints = [device.vendor, device.model_hint].filter(Boolean);
      const timestamps = [
        device.last_seen ? "seen: " + String(device.last_seen) : "",
        device.last_verify_attempt
          ? "verified: " + String(device.last_verify_attempt)
          : "",
      ].filter(Boolean);
      return (
        '<div class="ignored-row">' +
        '<div class="ignored-row-head"><span>' +
        escapeHtml(device.service_name || device.display_name || "mDNS service") +
        "</span>" +
        (device.stale ? '<span class="stale-badge">stale</span>' : "") +
        "</div>" +
        '<div class="ignored-meta">' +
        [device.source_detail, endpoint, hints.join(" · ")].concat(timestamps)
          .filter(Boolean)
          .map(escapeHtml)
          .join(" · ") +
        "</div>" +
        '<div class="ignored-reason">' +
        escapeHtml(device.reason || "Not supported by EMS") +
        "</div></div>"
      );
    })
    .join("");
}

async function pollMdns() {
  // Recurring interval poller: no-op while unauthenticated so a logged-out or
  // expired session never keeps calling the protected discovery API.
  if (!isAuthenticated()) return;
  try {
    const [statusRes, devicesRes] = await Promise.all([
      fetch("/api/discovery/mdns/status"),
      fetch("/api/discovery/devices"),
    ]);
    const status = await statusRes.json();
    const result = await devicesRes.json();
    if (!statusRes.ok || !devicesRes.ok) {
      throw new Error(status.last_error || result.error || "discovery status failed");
    }
    renderMdnsStatus(status);
    for (const device of Array.isArray(result.devices) ? result.devices : []) {
      mdnsDevices.set(deviceKey(device), device);
      mergeDiscoveryDevice(discoverySessions.setup, device, "mdns");
    }
    ignoredMdnsDevices.clear();
    for (const device of Array.isArray(result.ignored_devices) ? result.ignored_devices : []) {
      ignoredMdnsDevices.set(deviceKey(device), device);
    }
    renderIgnoredDevices();
    if (!scanning) renderAggregate();
  } catch (err) {
    renderMdnsStatus({
      state: "unavailable_runtime",
      message: "Automatic mDNS discovery is unavailable in this runtime.",
      last_error: err.message || String(err),
    });
  }
}

async function toggleMdns() {
  const enable = els.mdnsToggle.getAttribute("aria-pressed") !== "true";
  els.mdnsToggle.disabled = true;
  try {
    const res = await setupDiscoveryFetch(
      enable ? "/api/discovery/mdns/enable" : "/api/discovery/mdns/disable",
      { method: "POST" }
    );
    const status = await res.json();
    if (!res.ok) throw new Error(status.last_error || "discovery update failed");
    renderMdnsStatus(status);
    await pollMdns();
  } catch (err) {
    renderMdnsStatus({
      state: "unavailable_runtime",
      message: "Automatic mDNS discovery is unavailable in this runtime.",
      last_error: err.message || String(err),
    });
  }
}

async function refreshMdns() {
  els.mdnsRefresh.disabled = true;
  els.mdnsMessage.textContent = "Refreshing mDNS discovery…";
  try {
    const res = await setupDiscoveryFetch("/api/discovery/mdns/refresh", {
      method: "POST",
    });
    const status = await res.json();
    if (!res.ok) throw new Error(status.last_error || "mDNS refresh failed");
    renderMdnsStatus(status);
    await pollMdns();
  } catch (err) {
    renderMdnsStatus({
      state: "unavailable_runtime",
      message: "Automatic mDNS discovery is unavailable in this runtime.",
      last_error: err.message || String(err),
    });
  }
}

function mqttSourceLabel(source) {
  if (source === "mdns") return "mDNS";
  if (source === "network_probe") return "Network probe";
  return source ? String(source) : "—";
}

function renderMqttDeviceCard(device) {
  const idLabel = device.serial_number || device.device_id || "unknown";
  const metrics = Array.isArray(device.metrics_seen) ? device.metrics_seen : [];
  const topics = Array.isArray(device.topics_seen) ? device.topics_seen : [];
  const confidence = Math.round((Number(device.confidence) || 0) * 100);
  const modelHtml = device.model_hint
    ? fact("Model", escapeHtml(device.model_hint))
    : "";
  const metricsHtml = metrics.length
    ? fact("Metrics seen", escapeHtml(metrics.slice(0, 8).join(", ")))
    : "";
  const topicsHtml = topics.length
    ? fact("Topics seen", escapeHtml(topics.slice(0, 4).join(", ")))
    : "";
  return (
    '<article class="mqtt-device-card">' +
    '<div class="mqtt-device-head">' +
    '<span class="mqtt-device-title">' +
    escapeHtml(device.display_name || "Zendure MQTT device") +
    "</span>" +
    '<span class="pill muted">' +
    confidence +
    "% match</span></div>" +
    '<div class="device-facts">' +
    fact("Device/SN", escapeHtml(idLabel)) +
    fact("Topic family", escapeHtml(device.topic_family || "unknown")) +
    modelHtml +
    metricsHtml +
    topicsHtml +
    "</div></article>"
  );
}

function mqttAttemptStatusLabel(status) {
  return (
    {
      tcp_open: "TCP open",
      mqtt_connected: "connected",
      mqtt_listened_no_topics: "listened, no hardware topics yet",
      topics_seen: "topics seen",
      auth_failed: "auth failed",
      tls_failed: "TLS failed",
      connection_failed: "connection failed",
      timeout: "timed out",
    }[status] || String(status || "unknown").replace(/_/g, " ")
  );
}

function renderMqttAttemptRow(attempt) {
  const label = attempt.label || (attempt.credential_ref || "anonymous");
  const count = Number(attempt.device_count) || 0;
  let detail = mqttAttemptStatusLabel(attempt.status);
  if (count > 0) detail += ", " + plural(count, "device");
  return (
    '<li class="mqtt-attempt-row">' +
    '<span class="mqtt-attempt-label">' +
    escapeHtml(String(label)) +
    "</span>: " +
    '<span class="mqtt-attempt-status">' +
    escapeHtml(detail) +
    "</span></li>"
  );
}

function renderMqttBrokerCard(broker) {
  const endpoint = String(broker.host || "") + ":" + String(broker.port || "");
  const source = mqttSourceLabel(broker.source);
  const status = String(
    broker.status || (broker.reachable ? "reachable" : "candidate")
  ).replace(/_/g, " ");
  const transport = broker.transport === "tls" ? "TLS" : "plain";
  const devices = Array.isArray(broker.devices) ? broker.devices : [];
  const hasDevices = devices.length > 0;
  const attempts = Array.isArray(broker.attempts) ? broker.attempts : [];
  const attemptsHtml = attempts.length
    ? '<div class="mqtt-broker-attempts"><span class="mqtt-attempts-title">Attempts</span>' +
      "<ul>" +
      attempts.map(renderMqttAttemptRow).join("") +
      "</ul></div>"
    : "";
  const devicesHtml = hasDevices
    ? devices.map(renderMqttDeviceCard).join("")
    : '<p class="mqtt-broker-empty">No hardware topics found on this broker.</p>';
  return (
    '<article class="device-card mqtt-card mqtt-broker-card' +
    (hasDevices ? " has-devices" : "") +
    '">' +
    '<div class="device-card-head">' +
    '<span class="device-name">Broker ' +
    escapeHtml(endpoint) +
    "</span>" +
    '<span class="device-role role-unknown">' +
    escapeHtml(status) +
    "</span></div>" +
    '<div class="device-facts">' +
    fact("Source", escapeHtml(source)) +
    fact("Transport", escapeHtml(transport)) +
    fact("Hostname", escapeHtml(broker.hostname || "—")) +
    fact("Devices found", escapeHtml(String(devices.length))) +
    fact("Last seen", escapeHtml(broker.last_seen || "—")) +
    "</div>" +
    attemptsHtml +
    '<div class="mqtt-broker-devices">' +
    devicesHtml +
    "</div></article>"
  );
}

function renderMqttBrokers() {
  const brokers = Array.from(mqttBrokers.values());
  const deviceCount = brokers.reduce(
    (total, broker) =>
      total + (Array.isArray(broker.devices) ? broker.devices.length : 0),
    0
  );
  els.mqttCount.textContent =
    plural(brokers.length, "broker") + " / " + plural(deviceCount, "device");
  setSummary(els.summaryMqtt, plural(brokers.length, "broker"));
  notifySetupStatus();
  const hasAny = brokers.length > 0 || deviceCount > 0;
  els.mqttEmpty.hidden = hasAny;
  els.mqttList.hidden = !hasAny;
  // Detail panels stay collapsed by default; the unified overview is the
  // primary result view. The user opens a source panel only to inspect it.
  els.mqttList.innerHTML = brokers.map(renderMqttBrokerCard).join("");
}

async function loadMqttBrokers() {
  if (!isAuthenticated()) return;
  try {
    const res = await fetch("/api/discovery/mqtt-brokers");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "broker discovery failed");
    mqttBrokers.clear();
    for (const broker of Array.isArray(data.candidates) ? data.candidates : []) {
      mqttBrokers.set(String(broker.host) + ":" + String(broker.port), broker);
    }
    renderMqttBrokers();
    loadMqttProposals();
  } catch (err) {
    els.mqttMessage.textContent =
      "MQTT broker discovery unavailable: " + (err.message || String(err));
  }
}

function renderMqttProposalPill(label) {
  return '<span class="pill proposal-safety-pill">' + escapeHtml(label) + "</span>";
}

// User-facing Zendure hardware generation for a proposal; the raw topic family
// never appears in normal UI copy.
function mqttGenerationLabel(proposal) {
  const label =
    proposal.hardware_generation_label ||
    generationLabel(proposal.hardware_generation) ||
    "Zendure MQTT";
  return proposal.alternative_layout
    ? label + " · alternative topic layout detected"
    : label;
}

// A D0 MQTT grid-meter proposal targets the central grid_meter, not devices[].
function isMqttGridMeterProposal(proposal) {
  return (
    !!proposal &&
    String(proposal.target || "device").toLowerCase() === "grid_meter" &&
    !!proposal.grid_meter_fragment
  );
}

// Hardware role of an MQTT proposal, from the backend target/role_hint only.
// Display name, model, serial, topic text and connection source are never
// evidence; anything not positively classified stays "unknown" and neutral.
function mqttProposalHardwareRole(proposal) {
  if (!proposal) return "unknown";
  const roleHint = String(proposal.role_hint || "").toLowerCase();
  if (
    isMqttGridMeterProposal(proposal) ||
    String(proposal.target || "").toLowerCase() === "grid_meter" ||
    roleHint === "grid_meter_candidate"
  ) {
    return "grid_meter";
  }
  return roleHint === "battery_inverter_candidate" ? "inverter" : "unknown";
}

function mqttGridMeterProposalTopic(proposal) {
  const fragment = proposal && proposal.grid_meter_fragment;
  const mqtt = fragment && fragment.mqtt;
  return mqtt && typeof mqtt.topic === "string" ? mqtt.topic : "";
}

// The broker reference a trusted proposal fragment names, falling back to the
// proposal's own ref. One normalization for every consumer of a proposal.
function mqttProposalBrokerRef(proposal, mqtt) {
  const fragmentRef = mqtt && mqtt.broker_ref;
  return String(fragmentRef || (proposal && proposal.broker_ref) || "").trim();
}

// The one proposal → broker-profile mapping. Inverter drafts and grid-meter
// adoption both carry it, so a broker discovered in this session is provisioned
// by the shared server-side resolver instead of staying an unknown broker_ref.
// Non-secret connection metadata only; credentials travel as a reference.
function mqttProposalBrokerProfile(proposal, mqtt) {
  return {
    ref: mqttProposalBrokerRef(proposal, mqtt),
    host: proposal.broker_host || "",
    port: proposal.broker_port == null ? null : proposal.broker_port,
    tls: proposal.broker_tls === true,
    tls_insecure: proposal.broker_tls_insecure === true,
    tls_mode: proposal.broker_tls_mode || "",
    credentials_ref: proposal.credentials_ref || "",
    source: proposal.connection_source || proposal.source || "",
  };
}

// The one proposal → grid_meter config mapping, shared by Guided Setup and
// Maintenance and identical for Local and Zendure MQTT. Only the backend-minted
// fragment is trusted, so a grid-meter role alone never yields a mapping. The
// broker reference is consumed from the shared normalization; no broker
// transport is rebuilt here.
function mqttGridMeterConfigFromProposal(proposal) {
  const fragment = (proposal && proposal.grid_meter_fragment) || null;
  const type = fragment ? String(fragment.type || "").trim() : "";
  if (!type || !mqttGridMeterProposalTopic(proposal)) return null;
  const mqtt = Object.assign({}, fragment.mqtt);
  const brokerRef = mqttProposalBrokerRef(proposal, mqtt);
  if (brokerRef) mqtt.broker_ref = brokerRef;
  return { present: true, type, mqtt };
}

// Exactly one central grid meter exists, so an already chosen one is never
// silently replaced — Setup and Maintenance ask the same question.
function confirmGridMeterReplacement() {
  return typeof window !== "undefined" && typeof window.confirm === "function"
    ? window.confirm(
        "A grid meter is already selected. Replace it with this MQTT grid meter?"
      )
    : false;
}

// Preview-only proposal card. It never renders credentials/tokens and never
// offers a config-apply/write action beyond the read-only grid-meter mapping.
// Local or Zendure cloud MQTT connection label for a proposal.
function mqttTransportLabel(proposal) {
  return connectionLabelFor(
    mqttSourceOfConnection(proposal && proposal.connection_source)
  );
}

// User-facing labels for internal write-protocol and capability-reason values;
// the raw enum names never render directly.
function mqttWriteProtocolLabel(protocol) {
  const labels = {
    legacy_properties_write: "Properties write",
    custom_properties_write: "Properties write (custom topic)",
  };
  return labels[String(protocol || "")] || "Verified write protocol";
}

function mqttControlReasonLabel(reason) {
  const labels = {
    output_control_not_observed: "No output control observed in telemetry yet",
    // Route-addressing and identity blocks: keep the user-facing reason aligned
    // with why control is actually blocked. No raw product key or route id is
    // exposed.
    write_target_missing:
      "No complete MQTT write route: an MQTT device ID (and product key) is required",
    mqtt_device_id_missing:
      "MQTT device ID is missing: the physical serial cannot be used as the MQTT route",
    identity_route_product_conflict:
      "This physical inverter carries two MQTT product routes — output control is blocked",
    identity_route_serial_conflict:
      "Two physical serials share one MQTT route — output control is blocked",
    identity_conflict: "Identity conflict for this MQTT route — output control is blocked",
    hardware_profile_conflict:
      "Conflicting hardware-model evidence — select the exact model to enable control",
  };
  return (
    labels[String(reason || "")] ||
    "No verified write protocol for this topic family"
  );
}

// When output control is blocked the visible reason must describe the actual
// block. control_block_reason is the machine-readable block cause (route
// conflict, missing write route, model conflict); output_control_reason is only
// the capability/write-protocol name and is used when nothing blocks.
function mqttProposalControlReason(proposal) {
  if (!proposal) {
    return "";
  }
  return proposal.control_block_reason || proposal.output_control_reason || "";
}

function mqttProposalWriteProtocol(proposal) {
  const fragment = proposal && proposal.config_fragment;
  const mqtt = fragment && fragment.mqtt;
  return (
    (mqtt && mqtt.write_protocol) ||
    (proposal && proposal.output_control_reason) ||
    ""
  );
}

function renderMqttProposalCard(proposal) {
  const isGrid = isMqttGridMeterProposal(proposal);
  // Capability-based: a supported inverter is controllable; other families are
  // telemetry-only. MQTT control itself is never presented as experimental.
  const controllable = !isGrid && !!proposal.output_control_supported;
  const idLabel =
    proposal.serial_number || proposal.device_id || proposal.id || "unknown";
  const capabilities = Array.isArray(proposal.capabilities)
    ? proposal.capabilities
    : [];
  const metrics = Array.isArray(proposal.metrics) ? proposal.metrics : [];
  const warnings = Array.isArray(proposal.warnings) ? proposal.warnings : [];
  const capabilitiesHtml = capabilities.length
    ? fact("Capabilities", escapeHtml(capabilities.join(", ")))
    : "";
  const metricsHtml = metrics.length
    ? fact("Metrics seen", escapeHtml(metrics.slice(0, 8).join(", ")))
    : "";
  const warningsHtml = warnings.length
    ? '<div class="proposal-warnings">' +
      warnings.map((w) => '<span class="pill proposal-warning-pill">' + escapeHtml(String(w)) + "</span>").join("") +
      "</div>"
    : "";
  const fragmentSource = isGrid
    ? proposal.grid_meter_fragment
    : proposal.config_fragment;
  const fragmentHtml = fragmentSource
    ? '<details class="proposal-fragment"><summary>Config fragment (preview)</summary>' +
      "<pre>" +
      escapeHtml(JSON.stringify(fragmentSource, null, 2)) +
      "</pre></details>"
    : "";
  const proposalId = String(proposal.id || "");
  const selected = isMqttPreviewProposalSelected(proposalId);
  const canSelect = proposalId && fragmentSource;
  const actionHtml = canSelect
    ? '<div class="proposal-action">' +
      '<button type="button" class="secondary-button compact mqtt-proposal-add' +
      (selected ? " is-added" : "") +
      '"' +
      (selected ? ' data-selected="true"' : "") +
      ' data-proposal-id="' +
      escapeHtml(proposalId) +
      '">' +
      (isGrid
        ? selected
          ? "Selected as grid meter"
          : "Use as grid meter"
        : selected
          ? "Added to preview"
          : "Add to config preview") +
      "</button>" +
      (selected
        ? renderMqttProposalPill(
            isGrid
              ? "Grid meter — read only"
              : controllable
                ? "Output control enabled"
                : "Telemetry only — output write disabled"
          )
        : "") +
      "</div>"
    : "";
  const safetyPills = isGrid
    ? renderMqttProposalPill("Grid meter") + renderMqttProposalPill("Read only")
    : controllable
      ? renderMqttProposalPill("Output control available") +
        renderMqttProposalPill(mqttTransportLabel(proposal))
      : renderMqttProposalPill("Telemetry only") +
        renderMqttProposalPill(
          "Output control not available for this topic family"
        );
  const gridDetailsHtml = isGrid
    ? fact("Role", "Grid meter") +
      fact("Transport", "Local MQTT") +
      fact("Broker", "Local MQTT") +
      fact("Topic", escapeHtml(mqttGridMeterProposalTopic(proposal) || "unknown"))
    : fact("Transport", escapeHtml(mqttTransportLabel(proposal))) +
      fact("Output control", controllable ? "Supported" : "Not available") +
      (controllable
        ? fact(
            "Write protocol",
            escapeHtml(mqttWriteProtocolLabel(mqttProposalWriteProtocol(proposal)))
          )
        : fact(
            "Reason",
            escapeHtml(mqttControlReasonLabel(mqttProposalControlReason(proposal)))
          )) +
      fact("Role hint", escapeHtml(proposal.role_hint || "unknown"));
  return (
    '<article class="' +
    hardwareCardClass(mqttProposalHardwareRole(proposal)) +
    ' mqtt-proposal-card">' +
    '<div class="mqtt-device-head">' +
    '<span class="mqtt-device-title">' +
    escapeHtml(proposal.display_name || "Zendure MQTT proposal") +
    "</span>" +
    '<span class="pill muted">' +
    escapeHtml(String(proposal.confidence || "unknown")) +
    " confidence</span></div>" +
    '<div class="proposal-safety">' +
    safetyPills +
    "</div>" +
    '<div class="device-facts">' +
    fact("Device/SN", escapeHtml(idLabel)) +
    fact("Hardware generation", escapeHtml(mqttGenerationLabel(proposal))) +
    gridDetailsHtml +
    capabilitiesHtml +
    metricsHtml +
    "</div>" +
    warningsHtml +
    actionHtml +
    fragmentHtml +
    "</article>"
  );
}

function renderMqttProposals(proposals) {
  const list = Array.isArray(proposals) ? proposals : [];
  latestMqttProposals = list;
  els.mqttProposalsCount.textContent = plural(list.length, "proposal");
  const hasAny = list.length > 0;
  els.mqttProposalsEmpty.hidden = hasAny;
  els.mqttProposalsList.hidden = !hasAny;
  els.mqttProposalsList.innerHTML = list.map(renderMqttProposalCard).join("");
}

async function loadMqttProposals() {
  if (!isAuthenticated()) return;
  // Stale-response guard: a slow older rescan must not clobber a newer one.
  const requestId = ++mqttProposalsRequest;
  const generation = guidedSetupGeneration;
  try {
    const res = await fetch("/api/discovery/mqtt-proposals");
    const data = await res.json();
    if (requestId !== mqttProposalsRequest || generation !== guidedSetupGeneration) {
      return;
    }
    if (!res.ok) throw new Error(data.error || "proposal discovery failed");
    els.mqttProposalsMessage.hidden = true;
    renderMqttProposals(data.proposals);
    // Proposals may arrive after HTTP auto-add; reconcile the draft so a
    // prioritized MQTT device wins over an already auto-added HTTP one.
    syncConfigFromDiscovery();
  } catch (err) {
    if (requestId !== mqttProposalsRequest || generation !== guidedSetupGeneration) {
      return;
    }
    els.mqttProposalsMessage.hidden = false;
    els.mqttProposalsMessage.textContent =
      "Config proposals unavailable: " + (err.message || String(err));
    renderMqttProposals([]);
  }
}

// Selected Zendure MQTT proposals. Kept separate from the discovered
// inverter/grid-meter draft (configDraftItems): these proposal entries ride
// alongside the draft into preview and export/apply/write.
const CONFIG_MQTT_PREVIEW_STORAGE_KEY = "ems-admin-config-mqtt-preview";
let latestMqttProposals = [];
let mqttProposalsRequest = 0;
const transportInverterNames = new Map();
const zendureMqttPreviewProposals = loadMqttPreviewProposals();

function loadMqttPreviewProposals() {
  const map = new Map();
  try {
    const raw = window.localStorage.getItem(CONFIG_MQTT_PREVIEW_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (Array.isArray(parsed)) {
      for (const entry of parsed) {
        if (entry && typeof entry === "object" && entry.id != null) {
          map.set(String(entry.id), entry);
        }
      }
    }
  } catch (err) {
    /* localStorage may be unavailable; selection still lives in memory. */
  }
  return map;
}

function saveMqttPreviewProposals() {
  try {
    window.localStorage.setItem(
      CONFIG_MQTT_PREVIEW_STORAGE_KEY,
      JSON.stringify(Array.from(zendureMqttPreviewProposals.values()))
    );
  } catch (err) {
    /* localStorage may be unavailable; selection still lives in memory. */
  }
}

function isMqttPreviewProposalSelected(proposalId) {
  return zendureMqttPreviewProposals.has(String(proposalId));
}

function hasMqttPreviewProposals() {
  return zendureMqttPreviewProposals.size > 0;
}

// The single canonical serializer for a selected MQTT proposal. It preserves the
// minimum trusted, secret-free metadata the backend needs to resolve and
// re-validate the proposal (identity, topic family, broker ref, observed topics,
// broker endpoint); the backend never trusts these blindly — it re-sanitizes the
// fragment and re-validates family/topic/broker before any preview is produced.
// Both storage and the preview payload go through this one helper so a future
// change can never drop a required field from only one path.
// The durable selection is resolved server-side from id + broker_ref: an exact
// current hit needs no token, while the opaque identity token is required only to
// remap a stale/alias id and is validated whenever supplied. The backend then
// ignores every mutable discovery echo below (topic_family, seen_topics,
// device_id, broker endpoint); those fields are browser display/grid-meter hints,
// never a security assertion — the server is authoritative.
function serializeMqttProposalSelection(
  proposal,
  { target, replaceGridMeter, configValues, enabled } = {}
) {
  const resolvedTarget = String(
    target || proposal.target || "device"
  ).toLowerCase();
  const isGrid = resolvedTarget === "grid_meter";
  const hasConfigName = Object.prototype.hasOwnProperty.call(proposal, "config_name");
  // Common EMS values and the enabled state belong to the logical inverter, not
  // to the connection, so they survive a connection switch and a reload.
  const values = configValues !== undefined ? configValues : proposal.config_values;
  const keptValues =
    !isGrid && values && typeof values === "object" && Object.keys(values).length
      ? values
      : undefined;
  const resolvedEnabled = enabled !== undefined ? enabled : proposal.enabled;
  return {
    id: String(proposal.id || ""),
    target: resolvedTarget,
    config_name: isGrid
      ? undefined
      : hasConfigName
      ? String(proposal.config_name == null ? "" : proposal.config_name).trim()
      : (typeof inverterConfigNameForSerial === "function" &&
          inverterConfigNameForSerial(proposal)) ||
        nextInverterName(),
    display_name: proposal.display_name || proposal.hardware_model || "",
    // Device proposals carry config_fragment; grid-meter proposals carry the
    // read-only grid_meter_fragment. Neither holds any broker secret.
    config_fragment: isGrid ? undefined : proposal.config_fragment,
    grid_meter_fragment: isGrid ? proposal.grid_meter_fragment : undefined,
    // Trusted proposal metadata required for server-side validation/mapping.
    topic_family: proposal.topic_family,
    broker_ref: proposal.broker_ref,
    physical_identity_token: proposal.physical_identity_token,
    // The full trusted alias set (opaque tokens only) so a route-only selection
    // still intersects the enriched proposal after a serial appears. The backend
    // remains authoritative for identity; these are browser grouping hints only.
    physical_identity_alias_tokens: normalizeInverterAliasTokens(
      proposal.physical_identity_alias_tokens
    ),
    serial_number: proposal.serial_number,
    device_id: proposal.device_id,
    seen_topics: Array.isArray(proposal.seen_topics)
      ? proposal.seen_topics
      : undefined,
    broker_host: proposal.broker_host,
    broker_port: proposal.broker_port,
    broker_tls: proposal.broker_tls,
    connection_source: proposal.connection_source,
    config_values: keptValues,
    enabled: resolvedEnabled === false ? false : undefined,
    replace_grid_meter: !!replaceGridMeter,
  };
}

// The preview payload is exactly the stored selection entries; both are built by
// the one canonical serializer so no required field is ever dropped.
function mqttPreviewPayload() {
  return Array.from(zendureMqttPreviewProposals.values()).map((entry) =>
    serializeMqttProposalSelection(entry, {
      target: entry.target,
      replaceGridMeter: entry.replace_grid_meter,
    })
  );
}

// The id of the currently selected MQTT grid-meter proposal, or "".
function selectedMqttGridMeterId() {
  for (const entry of zendureMqttPreviewProposals.values()) {
    if (entry && String(entry.target || "device").toLowerCase() === "grid_meter") {
      return String(entry.id);
    }
  }
  return "";
}

// True when a non-MQTT grid meter (HTTP/Shelly/Tasmota) is already the chosen
// EMS grid meter in the discovered/manual draft.
function hasSelectedHttpGridMeter() {
  const meter = typeof gridMeterItem === "function" ? gridMeterItem() : null;
  return !!(meter && meter.enabled);
}

function toggleMqttPreviewProposal(proposalId) {
  const id = String(proposalId);
  if (zendureMqttPreviewProposals.has(id)) {
    zendureMqttPreviewProposals.delete(id);
    saveMqttPreviewProposals();
    renderMqttProposals(latestMqttProposals);
    renderConfigDraft();
    renderConfigAvailable();
    return;
  }
  const proposal = latestMqttProposals.find((p) => String(p.id || "") === id);
  if (!proposal) return;
  const isGrid = isMqttGridMeterProposal(proposal);
  if (!isGrid && !proposal.config_fragment) return;
  if (isGrid && !mqttGridMeterConfigFromProposal(proposal)) return;

  let replaceGridMeter = false;
  if (isGrid) {
    // Exactly one central grid meter: drop any other selected MQTT grid meter.
    const previous = selectedMqttGridMeterId();
    if (previous && previous !== id) {
      zendureMqttPreviewProposals.delete(previous);
    }
    // Never silently replace an HTTP/Shelly grid meter already selected.
    if (hasSelectedHttpGridMeter()) {
      if (!confirmGridMeterReplacement()) return;
      replaceGridMeter = true;
    }
  }

  const entry = serializeMqttProposalSelection(proposal, {
    target: isGrid ? "grid_meter" : "device",
    replaceGridMeter,
  });
  // Manual so the reconciler never overrides it; re-adding clears any dismissal.
  entry.selection_origin = "manual";
  entry.display_name = proposal.display_name || proposal.hardware_model || "";
  if (!isGrid) undismissSerial(proposal);
  zendureMqttPreviewProposals.set(id, entry);
  saveMqttPreviewProposals();
  renderMqttProposals(latestMqttProposals);
  // A device selection reconciles so the same-serial Local-API draft item is
  // dropped (never two transports for one serial); grid meters keep their own path.
  if (isGrid) {
    renderConfigPreview();
  } else {
    syncConfigFromDiscovery();
  }
}

if (els.mqttProposalsList) {
  els.mqttProposalsList.addEventListener("click", (event) => {
    const button = event.target.closest(".mqtt-proposal-add");
    if (!button) return;
    toggleMqttPreviewProposal(button.getAttribute("data-proposal-id"));
  });
}

// --- Manual Zendure MQTT broker + devices ---------------------------------
// The broker password is deliberately never persisted: only non-secret broker
// fields and the manual device list live in localStorage. Users pick a
// friendly hardware generation; the backend maps it to the internal topic
// family and enables output control only where that family has a verified
// write protocol.
const CONFIG_MQTT_BROKER_STORAGE_KEY = "ems-admin-config-mqtt-broker";
const CONFIG_MQTT_MANUAL_DEVICES_STORAGE_KEY = "ems-admin-config-mqtt-manual-devices";

const mqttManualEls = {
  brokerHelp: document.getElementById("config-mqtt-broker-help"),
  brokerName: document.getElementById("config-mqtt-broker-name"),
  brokerHost: document.getElementById("config-mqtt-broker-host"),
  brokerPort: document.getElementById("config-mqtt-broker-port"),
  brokerSecurity: document.getElementById("config-mqtt-broker-security"),
  brokerUsername: document.getElementById("config-mqtt-broker-username"),
  brokerPassword: document.getElementById("config-mqtt-broker-password"),
  deviceForm: document.getElementById("config-mqtt-device-form"),
  deviceName: document.getElementById("config-mqtt-device-name"),
  deviceSerial: document.getElementById("config-mqtt-device-serial"),
  deviceMqttId: document.getElementById("config-mqtt-device-mqttid"),
  deviceGeneration: document.getElementById("config-mqtt-device-generation"),
  deviceModel: document.getElementById("config-mqtt-device-model"),
  deviceProductKeyField: document.getElementById("config-mqtt-device-productkey-field"),
  deviceProductKey: document.getElementById("config-mqtt-device-productkey"),
  deviceControlField: document.getElementById("config-mqtt-device-control-field"),
  deviceControl: document.getElementById("config-mqtt-device-control"),
  deviceGenerationHelp: document.getElementById("config-mqtt-device-generation-help"),
  deviceModelHelp: document.getElementById("config-mqtt-device-model-help"),
  deviceError: document.getElementById("config-mqtt-device-error"),
  deviceList: document.getElementById("config-mqtt-device-list"),
};

let manualMqttDevices = loadManualMqttDevices();

function loadManualMqttDevices() {
  try {
    const raw = window.localStorage.getItem(CONFIG_MQTT_MANUAL_DEVICES_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((device) => device && typeof device === "object")
      : [];
  } catch (err) {
    return [];
  }
}

function saveManualMqttDevices() {
  try {
    window.localStorage.setItem(
      CONFIG_MQTT_MANUAL_DEVICES_STORAGE_KEY,
      JSON.stringify(manualMqttDevices)
    );
  } catch (err) {
    /* localStorage may be unavailable; the list still lives in memory. */
  }
}

// Reset the manual Zendure MQTT broker form inputs to empty defaults.
function resetMqttBrokerForm() {
  for (const field of [
    mqttManualEls.brokerName,
    mqttManualEls.brokerHost,
    mqttManualEls.brokerPort,
    mqttManualEls.brokerUsername,
    mqttManualEls.brokerPassword,
  ]) {
    if (field) field.value = "";
  }
  if (mqttManualEls.brokerSecurity) mqttManualEls.brokerSecurity.value = "plain";
}

// Clear every Zendure MQTT selection store together, so the MQTT half of the
// setup draft is reset in lockstep with the HTTP device draft and the two can
// never drift apart.
function clearMqttSelection() {
  zendureMqttPreviewProposals.clear();
  manualMqttDevices = [];
  transportInverterNames.clear();
  resetMqttBrokerForm();
  for (const key of [
    CONFIG_MQTT_PREVIEW_STORAGE_KEY,
    CONFIG_MQTT_MANUAL_DEVICES_STORAGE_KEY,
    CONFIG_MQTT_BROKER_STORAGE_KEY,
  ]) {
    try {
      window.localStorage.removeItem(key);
    } catch (err) {
      /* localStorage may be unavailable; the selection is already cleared. */
    }
  }
  renderMqttProposals(latestMqttProposals);
  renderManualMqttDevices();
  resetManualMqttDeviceForm();
}

function loadStoredBroker() {
  try {
    const raw = window.localStorage.getItem(CONFIG_MQTT_BROKER_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (err) {
    return {};
  }
}

// The password is intentionally excluded so it never reaches localStorage.
function saveStoredBroker() {
  if (!mqttManualEls.brokerHost) return;
  const stored = {
    name: (mqttManualEls.brokerName.value || "").trim(),
    host: (mqttManualEls.brokerHost.value || "").trim(),
    port: (mqttManualEls.brokerPort.value || "").trim(),
    security: mqttManualEls.brokerSecurity ? mqttManualEls.brokerSecurity.value : "plain",
    username: (mqttManualEls.brokerUsername.value || "").trim(),
  };
  try {
    window.localStorage.setItem(CONFIG_MQTT_BROKER_STORAGE_KEY, JSON.stringify(stored));
  } catch (err) {
    /* localStorage may be unavailable. */
  }
}

function zendureMqttGenerations() {
  const list = setupCatalog && setupCatalog.zendure_mqtt_generations;
  return Array.isArray(list) ? list : [];
}

function selectedMqttGeneration() {
  const generations = zendureMqttGenerations();
  const value = mqttManualEls.deviceGeneration ? mqttManualEls.deviceGeneration.value : "";
  return (
    generations.find((generation) => generation.id === value) ||
    generations.find((generation) => generation.default) ||
    generations[0] ||
    null
  );
}

function zendureMqttHardwareModels() {
  const list = setupCatalog && setupCatalog.zendure_mqtt_hardware_models;
  return Array.isArray(list) ? list : [];
}

function mqttModelsForGeneration(generationId) {
  return zendureMqttHardwareModels().filter((model) => {
    if (!model.id) return true;
    const compatible = Array.isArray(model.compatible_generations)
      ? model.compatible_generations
      : [model.generation];
    return compatible.includes(generationId);
  });
}

function selectedMqttModel() {
  const value = mqttManualEls.deviceModel ? mqttManualEls.deviceModel.value : "";
  return zendureMqttHardwareModels().find((model) => model.id === value) || null;
}

function populateMqttModels({ preserve = true } = {}) {
  const select = mqttManualEls.deviceModel;
  if (!select) return;
  const generation = selectedMqttGeneration();
  const models = mqttModelsForGeneration(generation ? generation.id : "");
  const previous = preserve ? select.value : "";
  select.replaceChildren();
  models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id || "";
    option.textContent = model.label || model.id || "Unknown / telemetry only";
    select.appendChild(option);
  });
  select.value = models.some((model) => model.id === previous) ? previous : "";
}

// The product key field is only meaningful for legacy generations; output
// control is offered only for a generation whose topic family can be written.
function syncMqttGenerationDetails() {
  const generation = selectedMqttGeneration();
  const model = selectedMqttModel();
  if (mqttManualEls.deviceProductKeyField) {
    mqttManualEls.deviceProductKeyField.hidden = !(generation && generation.product_key);
  }
  const controllable = !!(
    generation &&
    generation.supports_output_control &&
    model &&
    model.id &&
    model.control_supported
  );
  if (mqttManualEls.deviceControlField) {
    mqttManualEls.deviceControlField.hidden = !controllable;
  }
  if (mqttManualEls.deviceControl && !controllable) {
    mqttManualEls.deviceControl.checked = false;
  }
  if (mqttManualEls.deviceGenerationHelp) {
    mqttManualEls.deviceGenerationHelp.textContent =
      generation && generation.description ? generation.description : "";
  }
  if (mqttManualEls.deviceModelHelp) {
    if (!model || !model.id) {
      mqttManualEls.deviceModelHelp.textContent =
        "Unknown hardware remains telemetry only. Select the exact model before enabling control.";
    } else {
      const operations = Array.isArray(model.supported_operations)
        ? model.supported_operations.join(", ") || "telemetry only"
        : "telemetry only";
      mqttManualEls.deviceModelHelp.textContent =
        "Write protocol: " + (model.power_write_profile || "none") +
        " · validation: " + (model.validation_maturity || "unknown") +
        " · operations: " + operations;
    }
  }
}

function populateMqttGenerations() {
  const select = mqttManualEls.deviceGeneration;
  if (!select) return;
  const generations = zendureMqttGenerations();
  const previous = select.value;
  select.innerHTML = generations
    .map(
      (generation) =>
        '<option value="' + escapeHtml(generation.id) + '">' +
        escapeHtml(generation.label || generation.id) + "</option>"
    )
    .join("");
  if (generations.some((generation) => generation.id === previous)) {
    select.value = previous;
  } else {
    const preferred = generations.find((generation) => generation.default);
    if (preferred) select.value = preferred.id;
  }
  populateMqttModels();
  syncMqttGenerationDetails();
}

function initMqttBrokerSection() {
  if (mqttManualEls.brokerHelp && setupCatalog && setupCatalog.zendure_mqtt_broker) {
    mqttManualEls.brokerHelp.textContent = setupCatalog.zendure_mqtt_broker.help || "";
  }
  const stored = loadStoredBroker();
  if (mqttManualEls.brokerName && !mqttManualEls.brokerName.value) {
    mqttManualEls.brokerName.value = stored.name || "";
  }
  if (mqttManualEls.brokerHost && !mqttManualEls.brokerHost.value) {
    mqttManualEls.brokerHost.value = stored.host || "";
  }
  if (mqttManualEls.brokerPort && !mqttManualEls.brokerPort.value) {
    mqttManualEls.brokerPort.value = stored.port || "";
  }
  if (mqttManualEls.brokerSecurity && stored.security) {
    mqttManualEls.brokerSecurity.value = stored.security;
  }
  if (mqttManualEls.brokerUsername && !mqttManualEls.brokerUsername.value) {
    mqttManualEls.brokerUsername.value = stored.username || "";
  }
  populateMqttGenerations();
  resetManualMqttDeviceForm();
  renderManualMqttDevices();
}

// The password is read live from the input (memory), never from storage.
function mqttBrokerPayload() {
  if (!mqttManualEls.brokerHost) return null;
  const host = (mqttManualEls.brokerHost.value || "").trim();
  const name = (mqttManualEls.brokerName.value || "").trim();
  const port = (mqttManualEls.brokerPort.value || "").trim();
  const username = (mqttManualEls.brokerUsername.value || "").trim();
  const password = mqttManualEls.brokerPassword ? mqttManualEls.brokerPassword.value : "";
  if (!host && !name && !username && !password) return null;
  const broker = { host };
  if (name) broker.name = name;
  if (port) broker.port = port;
  broker.security = mqttManualEls.brokerSecurity ? mqttManualEls.brokerSecurity.value : "plain";
  if (username) broker.username = username;
  if (password) broker.password = password;
  return broker;
}

function manualMqttDevicesPayload() {
  return manualMqttDevices.map((device) => ({
    name: device.name || "",
    serial_number: device.serial_number || "",
    // Explicit MQTT route/payload device id, independent of the physical serial.
    mqtt_device_id: device.mqtt_device_id || "",
    hardware_generation: device.hardware_generation || device.generation || "",
    hardware_model: device.hardware_model || device.power_hardware_profile || "",
    product_key: device.product_key || "",
    output_control: device.output_control === true,
  }));
}

function showMqttDeviceError(text) {
  if (!mqttManualEls.deviceError) return;
  mqttManualEls.deviceError.hidden = !text;
  mqttManualEls.deviceError.textContent = text || "";
}

function generationLabel(id) {
  const generation = zendureMqttGenerations().find((entry) => entry.id === id);
  return generation ? generation.label : id;
}

function mqttModelLabel(id) {
  const model = zendureMqttHardwareModels().find((entry) => entry.id === id);
  return model ? model.label : "Unknown / telemetry only";
}

function renderManualMqttDevices() {
  const list = mqttManualEls.deviceList;
  if (!list) return;
  if (!manualMqttDevices.length) {
    list.hidden = true;
    list.innerHTML = "";
    return;
  }
  list.hidden = false;
  list.innerHTML = manualMqttDevices
    .map((device, index) => {
      const title = escapeHtml(
        device.name || device.serial_number || "Zendure MQTT device"
      );
      const generation = device.hardware_generation || device.generation || "";
      const model = device.hardware_model || device.power_hardware_profile || "";
      const meta = escapeHtml(generationLabel(generation) + " · " + mqttModelLabel(model));
      return (
        '<div class="config-mqtt-device-row">' +
        '<span class="config-mqtt-device-title">' + title + "</span>" +
        '<span class="config-mqtt-device-meta">' + meta + "</span>" +
        '<button type="button" class="secondary-button compact config-mqtt-device-remove"' +
        ' data-mqtt-device-index="' + index + '">Remove</button>' +
        "</div>"
      );
    })
    .join("");
}

function addManualMqttDevice() {
  const serial = (mqttManualEls.deviceSerial.value || "").trim();
  const mqttId = mqttManualEls.deviceMqttId
    ? (mqttManualEls.deviceMqttId.value || "").trim()
    : "";
  if (!serial) {
    showMqttDeviceError("Physical serial number is required.");
    return;
  }
  const generation = selectedMqttGeneration();
  const model = selectedMqttModel();
  if (!generation) {
    showMqttDeviceError("Choose a Zendure hardware generation.");
    return;
  }
  if (manualMqttDevices.some((device) => device.serial_number === serial)) {
    showMqttDeviceError("A device with this serial number is already added.");
    return;
  }
  const wantsControl = Boolean(
    generation.supports_output_control &&
      model && model.control_supported &&
      mqttManualEls.deviceControl &&
      mqttManualEls.deviceControl.checked
  );
  // A control write is addressed by the explicit MQTT route id, never the
  // physical serial, so output control needs the MQTT device ID.
  if (wantsControl && !mqttId) {
    showMqttDeviceError(
      "MQTT device ID is required to enable output control."
    );
    return;
  }
  manualMqttDevices.push({
    name: (mqttManualEls.deviceName.value || "").trim(),
    serial_number: serial,
    mqtt_device_id: mqttId,
    hardware_generation: generation.id,
    hardware_model: model && model.id ? model.id : "",
    product_key: generation.product_key
      ? (mqttManualEls.deviceProductKey.value || "").trim()
      : "",
    output_control: wantsControl,
  });
  saveManualMqttDevices();
  showMqttDeviceError("");
  mqttManualEls.deviceForm.reset();
  populateMqttGenerations();
  resetManualMqttDeviceForm();
  renderManualMqttDevices();
  renderConfigPreview();
}

function resetManualMqttDeviceForm() {
  if (mqttManualEls.deviceName) {
    mqttManualEls.deviceName.value = nextInverterName();
  }
}

function removeManualMqttDevice(index) {
  if (index < 0 || index >= manualMqttDevices.length) return;
  manualMqttDevices.splice(index, 1);
  saveManualMqttDevices();
  renderManualMqttDevices();
  renderConfigPreview();
}

if (mqttManualEls.deviceForm) {
  mqttManualEls.deviceForm.addEventListener("submit", (event) => {
    event.preventDefault();
    addManualMqttDevice();
  });
}

if (mqttManualEls.deviceGeneration) {
  mqttManualEls.deviceGeneration.addEventListener("change", () => {
    populateMqttModels({ preserve: false });
    syncMqttGenerationDetails();
  });
}

if (mqttManualEls.deviceModel) {
  mqttManualEls.deviceModel.addEventListener("change", syncMqttGenerationDetails);
}

if (mqttManualEls.deviceList) {
  mqttManualEls.deviceList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mqtt-device-index]");
    if (button) {
      removeManualMqttDevice(Number(button.getAttribute("data-mqtt-device-index")));
    }
  });
}

for (const input of [
  mqttManualEls.brokerName,
  mqttManualEls.brokerHost,
  mqttManualEls.brokerPort,
  mqttManualEls.brokerUsername,
]) {
  if (input) {
    input.addEventListener("input", () => {
      saveStoredBroker();
      renderConfigPreview();
    });
  }
}

if (mqttManualEls.brokerSecurity) {
  mqttManualEls.brokerSecurity.addEventListener("change", () => {
    saveStoredBroker();
    renderConfigPreview();
  });
}

if (mqttManualEls.brokerPassword) {
  // The password is never persisted; it only feeds the live preview.
  mqttManualEls.brokerPassword.addEventListener("input", renderConfigPreview);
}

async function probeMqttNetworks(cidrs) {
  els.mqttMessage.textContent =
    "The network scan is also checking TCP ports 1883 and 8883 for MQTT brokers…";
  const results = await Promise.all(
    cidrs.map(async (cidr) => {
      try {
        const res = await setupDiscoveryFetch("/api/discovery/mqtt-brokers/probe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cidr }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "broker probe failed");
        return { found: Number(data.found) || 0, error: null };
      } catch (err) {
        return { found: 0, error: err.message || String(err) };
      }
    })
  );
  await loadMqttBrokers();
  const found = results.reduce((total, result) => total + result.found, 0);
  const failed = results.filter((result) => result.error);
  els.mqttMessage.textContent = failed.length
    ? "MQTT broker checks failed for " + String(failed.length) + " network(s)."
    : "Network scan checked for MQTT brokers: " +
      String(found) +
      " open endpoint(s) found. Use Refresh to list hardware topics on reachable brokers.";
}

async function refreshMqttBrokers() {
  els.mqttRefresh.disabled = true;
  els.mqttMessage.textContent = "Refreshing broker discovery…";
  const context = discoveryContextFor(els.mqttRefresh);
  try {
    const res = await discoveryFetch("/api/discovery/mqtt-brokers/refresh", {
      method: "POST",
    }, context);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "broker refresh failed");
    els.mqttMessage.textContent =
      "Broker discovery refreshed. " +
      String(data.reachable || 0) +
      " reachable, " +
      String(data.devices_found || 0) +
      " hardware candidate(s) found.";
    await loadMqttBrokers();
  } catch (err) {
    els.mqttMessage.textContent =
      "MQTT broker refresh failed: " + (err.message || String(err));
  } finally {
    els.mqttRefresh.disabled = false;
  }
}

els.mdnsToggle.addEventListener("click", toggleMdns);
els.mdnsRefresh.addEventListener("click", refreshMdns);
els.mqttRefresh.addEventListener("click", refreshMqttBrokers);

// --- Local MQTT discovery credential pool --------------------------------
// A reusable pool of MQTT username/password pairs, endpoint-independent.
// Discovery tries anonymous plus every saved credential against reachable
// brokers. Credentials are write-only: POSTed to the server (stored encrypted)
// and never rendered back; the list shows only redacted status. No broker
// host/port/TLS connection config lives here — that belongs to the Config step.

function renderMqttCredentialCard(credential) {
  const label = credential.label || credential.id || "credential";
  const credFacts =
    fact("Username", credential.username_configured ? "configured" : "—") +
    fact("Password", credential.password_configured ? "configured" : "—") +
    fact(
      "Stored",
      credential.credentials_encrypted
        ? "encrypted"
        : "not encrypted — re-save required",
    );
  return (
    '<article class="device-card mqtt-card mqtt-credential-card">' +
    '<div class="device-card-head">' +
    '<span class="device-name">' +
    escapeHtml(label) +
    "</span></div>" +
    '<div class="device-facts">' +
    credFacts +
    "</div>" +
    '<div class="prep-config-actions">' +
    '<button type="button" class="secondary-button compact" data-forget-credential="' +
    escapeHtml(String(credential.id || "")) +
    '">Remove credential</button>' +
    "</div></article>"
  );
}

function renderMqttCredentials(credentials) {
  const list = Array.isArray(credentials) ? credentials : [];
  if (els.mqttCredentialEmpty) els.mqttCredentialEmpty.hidden = list.length > 0;
  els.mqttCredentialList.innerHTML = list.map(renderMqttCredentialCard).join("");
}

async function loadMqttCredentials() {
  if (!isAuthenticated()) return;
  try {
    const res = await fetch("/api/discovery/connections/mqtt-credentials");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "credentials load failed");
    renderMqttCredentials(data.credentials);
  } catch (err) {
    els.mqttCredentialMessage.textContent =
      "Could not load discovery credentials: " + (err.message || String(err));
  }
}

async function saveMqttCredential(event) {
  event.preventDefault();
  const label = els.mqttCredentialLabel.value.trim();
  if (!label) {
    els.mqttCredentialMessage.textContent = "A label is required.";
    return;
  }
  const body = {
    label,
    username: els.mqttCredentialUsername.value,
    password: els.mqttCredentialPassword.value,
  };
  els.mqttCredentialSave.disabled = true;
  els.mqttCredentialMessage.textContent = "Saving credential…";
  const context = discoveryContextFor(els.mqttCredentialForm);
  try {
    const res = await discoveryFetch(
      "/api/discovery/connections/mqtt-credentials",
      {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      },
      context
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || "save failed");
    els.mqttCredentialForm.reset();
    els.mqttCredentialMessage.textContent = "Credential saved.";
    renderMqttCredentials((data.local_mqtt || {}).credentials);
  } catch (err) {
    els.mqttCredentialMessage.textContent =
      "Could not save the credential: " + (err.message || String(err));
  } finally {
    els.mqttCredentialSave.disabled = false;
  }
}

async function deleteMqttCredential(id) {
  els.mqttCredentialMessage.textContent = "Removing credential…";
  const context = discoveryContextFor(els.mqttCredentialList);
  try {
    const res = await discoveryFetch(
      "/api/discovery/connections/mqtt-credentials/" + encodeURIComponent(id),
      { method: "DELETE" },
      context
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || "delete failed");
    els.mqttCredentialMessage.textContent = "Credential removed.";
    await loadMqttCredentials();
  } catch (err) {
    els.mqttCredentialMessage.textContent =
      "Could not remove the credential: " + (err.message || String(err));
  }
}

if (els.mqttCredentialForm) {
  els.mqttCredentialForm.addEventListener("submit", saveMqttCredential);
  els.mqttCredentialList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-forget-credential]");
    if (button) deleteMqttCredential(button.getAttribute("data-forget-credential"));
  });
}

// --- Zendure cloud MQTT discovery ----------------------------------------
// The token is saved server-side (encrypted) and never echoed back; every
// dynamic value below passes through escapeHtml.

const ZENDURE_CLOUD_BASE = "/api/discovery/zendure-cloud-mqtt";
let zendureCloudTokenSaved = false;
const zendureCloudDevices = [];

function renderZendureCloudDeviceCard(device) {
  const idLabel = device.serial_number || device.device_id || "unknown";
  const metrics = Array.isArray(device.metrics_seen) ? device.metrics_seen : [];
  const topics = Array.isArray(device.topics_seen) ? device.topics_seen : [];
  const confidence = Math.round((Number(device.confidence) || 0) * 100);
  const status = String(device.discovery_status || "device_list_only").replace(
    /_/g,
    " "
  );
  const modelHtml = device.model_hint
    ? fact("Model", escapeHtml(device.model_hint))
    : "";
  const nameHtml = device.device_name
    ? fact("Device name", escapeHtml(device.device_name))
    : "";
  const metricsHtml = metrics.length
    ? fact("Metrics seen", escapeHtml(metrics.slice(0, 8).join(", ")))
    : "";
  const topicsHtml = topics.length
    ? fact("Topics seen", escapeHtml(topics.slice(0, 4).join(", ")))
    : "";
  return (
    '<article class="mqtt-device-card zendure-cloud-device-card">' +
    '<div class="mqtt-device-head">' +
    '<span class="mqtt-device-title">' +
    escapeHtml(device.display_name || "Zendure cloud device") +
    "</span>" +
    '<span class="pill muted">' +
    confidence +
    "% match</span></div>" +
    '<div class="device-facts">' +
    nameHtml +
    fact("Serial / device id", escapeHtml(idLabel)) +
    modelHtml +
    fact("Discovery status", escapeHtml(status)) +
    fact("Topic family", escapeHtml(device.topic_family || "unknown")) +
    metricsHtml +
    topicsHtml +
    fact("TLS mode", escapeHtml(device.tls_mode || "—")) +
    "</div></article>"
  );
}

function renderZendureCloudDevices() {
  const hasDevices = zendureCloudDevices.length > 0;
  els.zendureCloudEmpty.hidden = hasDevices;
  els.zendureCloudList.hidden = !hasDevices;
  els.zendureCloudList.innerHTML = zendureCloudDevices
    .map(renderZendureCloudDeviceCard)
    .join("");
}

function applyZendureCloudSettings(settings) {
  const data = settings || {};
  zendureCloudTokenSaved = Boolean(data.token_saved);
  els.zendureCloudTokenState.textContent = zendureCloudTokenSaved
    ? "saved"
    : "not saved";
  els.zendureCloudForget.hidden = !zendureCloudTokenSaved;
  els.zendureCloudSave.textContent = zendureCloudTokenSaved
    ? "Replace credential"
    : "Save credential";
  els.zendureCloudTls.textContent = data.tls_mode || "system_ca";
  els.zendureCloudBroker.textContent = data.last_broker || "—";
  els.zendureCloudLastStatus.textContent = data.last_status || "—";
  els.zendureCloudLastError.textContent = data.last_error || "—";
  const count = Number(data.last_device_count);
  els.zendureCloudCount.textContent = zendureCloudTokenSaved
    ? "credential saved" + (count > 0 ? " / " + plural(count, "cloud device") : "")
    : "not configured";
  if (!zendureCloudTokenSaved) {
    els.zendureCloudMessage.textContent =
      "Save a Zendure API key or HA/deviceList token to discover devices from the Zendure MQTT broker.";
  }
}

async function loadZendureCloudSettings() {
  if (!isAuthenticated()) return;
  try {
    const res = await fetch(ZENDURE_CLOUD_BASE + "/settings");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "settings failed");
    applyZendureCloudSettings(data);
  } catch (err) {
    els.zendureCloudMessage.textContent =
      "Zendure cloud settings unavailable: " + (err.message || String(err));
  }
}

async function saveZendureCloudToken(event) {
  if (event) event.preventDefault();
  const apiKey = els.zendureCloudTokenInput.value.trim();
  if (!apiKey) {
    els.zendureCloudMessage.textContent = "Enter a Zendure API key or HA token to save.";
    return;
  }
  els.zendureCloudSave.disabled = true;
  els.zendureCloudMessage.textContent = "Saving Zendure credential…";
  const context = discoveryContextFor(els.zendureCloudForm);
  try {
    const res = await discoveryFetch(ZENDURE_CLOUD_BASE + "/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    }, context);
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || "save failed");
    }
    els.zendureCloudTokenInput.value = "";
    els.zendureCloudMessage.textContent = data.message || "Zendure credential saved.";
    await loadZendureCloudSettings();
  } catch (err) {
    els.zendureCloudMessage.textContent =
      "Could not save Zendure credential: " + escapeHtml(err.message || String(err));
  } finally {
    els.zendureCloudSave.disabled = false;
  }
}

async function testZendureCloudToken() {
  const apiKey = els.zendureCloudTokenInput.value.trim();
  els.zendureCloudTest.disabled = true;
  els.zendureCloudMessage.textContent = "Testing Zendure credential…";
  const context = discoveryContextFor(els.zendureCloudForm);
  try {
    const body = apiKey ? { api_key: apiKey } : {};
    const res = await discoveryFetch(ZENDURE_CLOUD_BASE + "/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, context);
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || "test failed");
    }
    els.zendureCloudMessage.textContent =
      "Zendure credential OK: " +
      String(Number(data.devices_found) || 0) +
      " device(s) via " +
      escapeHtml(data.broker || "broker") +
      " (" +
      escapeHtml(data.tls_mode || "system_ca") +
      ").";
    await loadZendureCloudSettings();
  } catch (err) {
    els.zendureCloudMessage.textContent =
      "Zendure credential test failed: " + escapeHtml(err.message || String(err));
  } finally {
    els.zendureCloudTest.disabled = false;
  }
}

async function refreshZendureCloudDiscovery() {
  els.zendureCloudRefresh.disabled = true;
  els.zendureCloudMessage.textContent = "Discovering Zendure cloud devices…";
  const context = discoveryContextFor(els.zendureCloudForm);
  try {
    const res = await discoveryFetch(ZENDURE_CLOUD_BASE + "/refresh", {
      method: "POST",
    }, context);
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || "refresh failed");
    }
    zendureCloudDevices.length = 0;
    for (const device of Array.isArray(data.candidates) ? data.candidates : []) {
      zendureCloudDevices.push(device);
    }
    renderZendureCloudDevices();
    els.zendureCloudCount.textContent =
      plural(Number(data.device_list_count) || 0, "cloud device") +
      " / " +
      String(Number(data.mqtt_observed_count) || 0) +
      " observed";
    els.zendureCloudMessage.textContent =
      data.mqtt_message ||
      "Zendure cloud discovery complete via " +
        escapeHtml(data.broker || "broker") +
        " (" +
        escapeHtml(data.tls_mode || "system_ca") +
        ").";
    await loadZendureCloudSettings();
    // Refresh proposals (and reconcile the draft) so a rescan actually swaps
    // transport instead of leaving latestMqttProposals stale. Both feed the
    // Setup draft only; a Maintenance-mounted refresh must not touch the Setup
    // wizard (its Start discovery run reads the proposals itself).
    if (context === "setup") {
      await loadMqttProposals();
      await refreshUnifiedDevices();
    }
  } catch (err) {
    els.zendureCloudMessage.textContent =
      "Zendure cloud discovery failed: " + escapeHtml(err.message || String(err));
  } finally {
    els.zendureCloudRefresh.disabled = false;
  }
}

async function forgetZendureCloudToken() {
  els.zendureCloudForget.disabled = true;
  els.zendureCloudMessage.textContent = "Removing Zendure credential…";
  const context = discoveryContextFor(els.zendureCloudForm);
  try {
    const res = await discoveryFetch(ZENDURE_CLOUD_BASE + "/token", {
      method: "DELETE",
    }, context);
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || "delete failed");
    }
    zendureCloudDevices.length = 0;
    renderZendureCloudDevices();
    els.zendureCloudMessage.textContent = data.message || "Zendure credential removed.";
    await loadZendureCloudSettings();
    if (context === "setup") await refreshUnifiedDevices();
  } catch (err) {
    els.zendureCloudMessage.textContent =
      "Could not remove Zendure credential: " + escapeHtml(err.message || String(err));
  } finally {
    els.zendureCloudForget.disabled = false;
  }
}

els.zendureCloudForm.addEventListener("submit", saveZendureCloudToken);
els.zendureCloudTest.addEventListener("click", testZendureCloudToken);
els.zendureCloudRefresh.addEventListener("click", refreshZendureCloudDiscovery);
els.zendureCloudForget.addEventListener("click", forgetZendureCloudToken);

// --- config draft workflow (Config tab) ----------------------------------
// The Config tab reuses the same aggregated discovery data as the Discovery
// tab. The editable draft lives in frontend state and export actions send it
// to the validated Admin endpoints. Available-device cards re-render on every
// discovery update, but draft cards only redraw on structural changes.

const CONFIG_DRAFT_STORAGE_KEY = "ems-admin-config-draft";
// The live-config revision the draft was reviewed against. Stored beside the
// draft so a reload restores the pair; without it the server refuses to mutate.
const CONFIG_BASELINE_STORAGE_KEY = "ems-admin-config-baseline";
const CONFIG_WORKFLOW_STORAGE_KEY = "ems-admin-setup-workflow";
const CONFIG_DISMISSED_STORAGE_KEY = "ems-admin-config-dismissed";
// Serials removed outright: the reconciler skips them over either transport.
const CONFIG_DISMISSED_SERIALS_STORAGE_KEY = "ems-admin-config-dismissed-serials";
const CONFIG_FEATURES_STORAGE_KEY = "ems-admin-config-features";
const DEFAULT_INVERTER_DISPLAY = "SolarFlow 800 Pro 2";
const DEFAULT_GRID_METER_DISPLAY = "Shelly Pro 3EM";

const configEls = {
  availableCount: document.getElementById("config-available-count"),
  availableEmpty: document.getElementById("config-available-empty"),
  availableList: document.getElementById("config-available-list"),
  clearDraft: document.getElementById("config-clear-draft"),
  manualForm: document.getElementById("config-manual-form"),
  manualName: document.getElementById("config-manual-name"),
  manualRole: document.getElementById("config-manual-role"),
  manualType: document.getElementById("config-manual-type"),
  manualHost: document.getElementById("config-manual-host"),
  manualPort: document.getElementById("config-manual-port"),
  manualSerial: document.getElementById("config-manual-serial"),
  manualError: document.getElementById("config-manual-error"),
  gridMeterSelection: document.getElementById("config-grid-meter-selection"),
  validation: document.getElementById("config-validation"),
  draftEmpty: document.getElementById("config-draft-empty"),
  draftList: document.getElementById("config-draft-list"),
  preview: document.getElementById("config-preview"),
  featureSettings: document.getElementById("config-feature-settings"),
  featureLists: {
    features: document.getElementById("config-feature-list-features"),
    advanced: document.getElementById("config-feature-list-advanced"),
  },
  featureEmpty: document.getElementById("config-feature-empty"),
  templateStatus: document.getElementById("config-template-status"),
  templatePreview: document.getElementById("config-template-preview"),
  validationCard: document.getElementById("config-validation-card"),
  previewReady: document.getElementById("config-preview-ready"),
  previewDevices: document.getElementById("config-preview-devices"),
  previewRelease: document.getElementById("config-preview-release"),
  previewBase: document.getElementById("config-preview-base"),
  download: document.getElementById("config-download"),
  exportStatus: document.getElementById("config-export-status"),
  apply: document.getElementById("config-apply"),
  applyStatus: document.getElementById("config-apply-status"),
  applyRollback: document.getElementById("config-apply-rollback"),
  applyTarget: document.getElementById("config-apply-target"),
  conflict: document.getElementById("setup-config-conflict"),
  conflictMessage: document.getElementById("setup-config-conflict-message"),
  conflictReview: document.getElementById("setup-config-conflict-review"),
  conflictDiscard: document.getElementById("setup-config-conflict-discard"),
  conflictDetails: document.getElementById("setup-config-conflict-details"),
  conflictDetail: document.getElementById("setup-config-conflict-detail"),
  workflowConflict: document.getElementById("setup-workflow-conflict"),
  workflowConflictMessage: document.getElementById(
    "setup-workflow-conflict-message"
  ),
  workflowConflictOpen: document.getElementById("setup-workflow-conflict-open"),
  workflowConflictDiscard: document.getElementById(
    "setup-workflow-conflict-discard"
  ),
};

const SETUP_CONFLICT_MESSAGES = {
  stale_setup_config:
    "The live EMS configuration changed after this setup was opened. " +
    "Your setup draft was not applied.",
  setup_preview_required:
    "Review the current configuration again before saving or applying it.",
  setup_preview_mismatch:
    "This setup changed after the displayed preview was created. Review the " +
    "current configuration again before saving or applying it.",
};

// Workflow-identity conflicts are terminal for this tab's authority (unlike
// preview conflicts, which a re-review repairs), so they get their own panel.
// A refused setup intent or an unprovable transition owner means the same thing:
// this tab is not the workflow the server is willing to change.
const SETUP_WORKFLOW_CONFLICT_ERRORS = new Set([
  "setup_workflow_required",
  "setup_workflow_not_active",
  "setup_intent_workflow_mismatch",
  "setup_transition_owner_unproven",
  "setup_transition_context_mismatch",
]);

const SETUP_WORKFLOW_STALE_MESSAGE =
  "This browser tab belongs to an older setup session and can no longer " +
  "change the current workflow.";

// A terminal operation was refused because a mutation still owns the workflow.
// It is not an abandonment: the workflow, its draft and its identity stay.
const SETUP_OPERATION_LABELS = {
  config_write: "saving the generated configuration",
  config_apply: "applying the configuration",
  deployment_prepare: "preparing the deployment",
  deployment_start: "starting EMS",
  permission_repair: "repairing the workspace permissions",
  container_conflict_resolution: "resolving a container conflict",
};

function isSetupOperationInProgress(data) {
  return Boolean(data && data.error === "setup_operation_in_progress");
}

function setupOperationInProgressMessage(data) {
  const label = SETUP_OPERATION_LABELS[(data && data.operation) || ""];
  return (
    "Setup is still " +
    (label || "finishing another operation") +
    ". Wait for it to finish, then try again. Nothing was discarded."
  );
}

function isSetupConfigConflict(data) {
  return Boolean(data && SETUP_CONFLICT_MESSAGES[data.error]);
}

function isSetupWorkflowConflict(data) {
  return Boolean(data && SETUP_WORKFLOW_CONFLICT_ERRORS.has(data.error));
}

// Never clears the draft: the user keeps their work and chooses how to continue.
function showSetupConfigConflict(data) {
  if (!configEls.conflict) return;
  const conflict = isSetupConfigConflict(data);
  configEls.conflict.hidden = !conflict;
  if (!conflict) return;
  if (configEls.conflictMessage) {
    configEls.conflictMessage.textContent = SETUP_CONFLICT_MESSAGES[data.error];
  }
  if (configEls.conflictDetail && configEls.conflictDetails) {
    const detail = data.message || "";
    configEls.conflictDetail.textContent = detail;
    configEls.conflictDetails.hidden = !detail;
  }
}

// The exact preview is spent or stale: a new one is only ever earned by a real
// regeneration against the current live config, never by replaying the old ID.
async function reviewCurrentSetupConfiguration() {
  showSetupConfigConflict(null);
  setConfigBaseline(null);
  setSetupPreviewId(null);
  await requestConfigPreview();
}

// This tab's workflow identity was refused: stop polling and mutating, keep
// the user's draft visible, and let them explicitly rejoin or discard. The local
// one-shot intent goes with it — a confirmation issued for a workflow this tab no
// longer owns can never authorize anything, so keeping it would only produce a
// second, more confusing rejection.
function handleSetupWorkflowConflict(data) {
  setupWorkflowStale = true;
  setupIntentId = null;
  // Supersede every in-flight preview generation. A preview response that was
  // already on the wire describes a workflow the server has since refused; it
  // must not repaint the preview, its verdict or its readiness afterwards.
  configPreviewRequest += 1;
  latestConfigPreview = null;
  setSetupPreviewId(null);
  setConfigExportReady(false);
  if (configPreviewTimer) {
    window.clearTimeout(configPreviewTimer);
    configPreviewTimer = null;
  }
  if (configEls.previewReady) {
    configEls.previewReady.textContent = "Session superseded";
  }
  if (configEls.workflowConflict) {
    configEls.workflowConflict.hidden = false;
    if (configEls.workflowConflictMessage) {
      configEls.workflowConflictMessage.textContent =
        (data && data.message) || SETUP_WORKFLOW_STALE_MESSAGE;
    }
  }
}

// The server's redacted workflow view: identity, lifecycle and cleanup state.
async function fetchSetupWorkflowSnapshot() {
  try {
    const res = await fetch("/api/setup/workflow", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) return null;
    const workflow = data && data.workflow;
    return workflow && typeof workflow.workflow_id === "string" ? workflow : null;
  } catch (err) {
    return null;
  }
}

function setupCleanupBlocks(workflow) {
  const cleanup = workflow && workflow.cleanup;
  return Boolean(cleanup && cleanup.blocking === true);
}

// The workflow this tab may still act on. A terminal workflow whose cleanup has
// not converged keeps its identity: it is the only id its retry can name.
async function fetchCurrentSetupWorkflowId() {
  const workflow = await fetchSetupWorkflowSnapshot();
  if (!workflow) return null;
  if (workflow.status === "active" || setupCleanupBlocks(workflow)) {
    return workflow.workflow_id;
  }
  return null;
}

// Any destructive Setup action must name the workflow on record — including a
// terminal one. Only a genuinely absent record yields null.
async function fetchOwningSetupWorkflowId() {
  const workflow = await fetchSetupWorkflowSnapshot();
  return (workflow && workflow.workflow_id) || null;
}

// Rejoin the server's current workflow (or drop identity if none is active);
// the local draft is kept and re-previewed under the adopted identity.
async function openCurrentSetupWorkflow() {
  const workflow = await fetchSetupWorkflowSnapshot();
  if (workflow && setupCleanupBlocks(workflow)) {
    // There is nothing to rejoin: the workflow is terminal and still owns files.
    setupWorkflowId = workflow.workflow_id;
    saveSetupWorkflowState();
    showSetupCleanupIncomplete(workflow.cleanup);
    return;
  }
  setSetupWorkflowId(workflow && workflow.status === "active" ? workflow.workflow_id : null);
  if (configEls.workflowConflict) configEls.workflowConflict.hidden = true;
  showSetupConfigConflict(null);
  renderConfigPreview();
}

// Discard the active Setup through its backend owner. Returns the abandon
// response; local identity is cleared only after the backend confirmed.
async function discardActiveSetup() {
  const current = await fetchOwningSetupWorkflowId();
  const res = await fetch("/api/setup/abandon", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(current ? { setup_workflow_id: current } : {}),
  });
  const data = await res.json().catch(() => ({}));
  if (isSetupOperationInProgress(data)) {
    // Another Setup operation still owns the workflow: nothing was discarded,
    // so the local identity and draft must stay exactly as they are.
    return { ok: false, status: res.status, data };
  }
  if (res.ok && data.ok === true) {
    setSetupWorkflowId(null);
  }
  return { ok: res.ok && data.ok === true, status: res.status, data };
}

let activeConfigTemplate = null;
let activeConfigTemplateTag = null;
let latestConfigPreview = null;
let configPreviewRequest = 0;
let configPreviewTimer = null;

// Flat, ordered list of draft items keyed by their discovery source id. Order
// is display order; inverter numbering and preview grouping derive from it.
let configDraftItems = loadConfigDraft();
let setupConfigBaseline = loadConfigBaseline();
// Server-owned mutation authority: the durable workflow identity plus the
// exact preview the server issued for the current draft. The browser renders
// them but never invents them; both clear only on confirmed backend lifecycle
// events (abandon, supersede) or when the server refuses them.
let setupWorkflowState = loadSetupWorkflowState();
let setupWorkflowId = setupWorkflowState.workflow_id;
let setupConfigPreviewId = setupWorkflowState.preview_id;
let setupWorkflowStale = false;
upgradeStoredInverterNames();
// Source ids the user removed/cleared: auto-config must not re-add these, so a
// manual "Remove" or "Clear draft" is not undone by the next discovery poll.
const configDismissed = loadConfigDismissed();
const dismissedSerials = loadDismissedSerials();
// Source-id -> latest discovered device, refreshed on every available render so
// the add-button handler always has the current device record.
const configAvailableIndex = new Map();

// Catalog-driven setup feature settings. The catalog (fetched once) is the
// reference for possible options; featureValues holds only user-changed values
// keyed by their stable config path, so unopened features keep template
// defaults. openFeatures tracks which accordion rows are expanded.
let setupCatalog = null;
const featureValues = loadFeatureValues();
const openFeatures = new Set();
const openHardwareCards = new Set();

// Sections the catalog marks as non-collapsible (collapsible:false, e.g.
// "System basics") start expanded. This only seeds the default open state — the
// row stays collapsible, so the user can still fold it afterwards.
function seedDefaultOpenFeatureSections(sections, target) {
  for (const section of sections || []) {
    if (section && section.collapsible === false && section.id != null) {
      target.add(String(section.id));
    }
  }
}

function loadFeatureValues() {
  try {
    const raw = window.localStorage.getItem(CONFIG_FEATURES_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch (err) {
    return {};
  }
}

function saveFeatureValues() {
  try {
    window.localStorage.setItem(
      CONFIG_FEATURES_STORAGE_KEY,
      JSON.stringify(featureValues)
    );
  } catch (err) {
    /* localStorage may be unavailable; feature values still live in memory. */
  }
}

function isConfigBaseline(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      typeof value.expect_absent === "boolean" &&
      (value.expected_revision === null ||
        typeof value.expected_revision === "string")
  );
}

function loadConfigBaseline() {
  try {
    const raw = window.localStorage.getItem(CONFIG_BASELINE_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return isConfigBaseline(parsed) ? parsed : null;
  } catch (err) {
    return null;
  }
}

function loadSetupWorkflowState() {
  try {
    const raw = window.localStorage.getItem(CONFIG_WORKFLOW_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (
      parsed &&
      typeof parsed === "object" &&
      typeof parsed.workflow_id === "string" &&
      parsed.workflow_id
    ) {
      return {
        workflow_id: parsed.workflow_id,
        preview_id:
          typeof parsed.preview_id === "string" && parsed.preview_id
            ? parsed.preview_id
            : null,
      };
    }
  } catch (err) {
    /* localStorage may be unavailable; identity then lives in memory only. */
  }
  return { workflow_id: null, preview_id: null };
}

function saveSetupWorkflowState() {
  try {
    if (setupWorkflowId) {
      window.localStorage.setItem(
        CONFIG_WORKFLOW_STORAGE_KEY,
        JSON.stringify({
          workflow_id: setupWorkflowId,
          preview_id: setupConfigPreviewId,
        })
      );
    } else {
      window.localStorage.removeItem(CONFIG_WORKFLOW_STORAGE_KEY);
    }
  } catch (err) {
    /* localStorage may be unavailable; identity then lives in memory only. */
  }
}

function setSetupWorkflowId(value) {
  setupWorkflowId = typeof value === "string" && value ? value : null;
  if (!setupWorkflowId) setupConfigPreviewId = null;
  setupWorkflowStale = false;
  saveSetupWorkflowState();
}

function setSetupPreviewId(value) {
  setupConfigPreviewId = typeof value === "string" && value ? value : null;
  saveSetupWorkflowState();
}

function setConfigBaseline(value) {
  setupConfigBaseline = isConfigBaseline(value) ? value : null;
  try {
    if (setupConfigBaseline) {
      window.localStorage.setItem(
        CONFIG_BASELINE_STORAGE_KEY,
        JSON.stringify(setupConfigBaseline)
      );
    } else {
      window.localStorage.removeItem(CONFIG_BASELINE_STORAGE_KEY);
    }
  } catch (err) {
    /* localStorage may be unavailable; the baseline still lives in memory. */
  }
}

function loadConfigDraft() {
  try {
    const raw = window.localStorage.getItem(CONFIG_DRAFT_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    return [];
  }
}

function saveConfigDraft() {
  try {
    window.localStorage.setItem(
      CONFIG_DRAFT_STORAGE_KEY,
      JSON.stringify(configDraftItems)
    );
  } catch (err) {
    /* localStorage may be unavailable; draft still lives in memory. */
  }
}

function upgradeStoredInverterNames() {
  const names = [
    ...configDraftItems
      .filter((item) => item && item.role === "inverter" && item.config_name)
      .map((item) => item.config_name),
    ...Array.from(zendureMqttPreviewProposals.values())
      .filter(
        (entry) =>
          entry &&
          String(entry.target || "device").toLowerCase() !== "grid_meter" &&
          Object.prototype.hasOwnProperty.call(entry, "config_name")
      )
      .map((entry) => entry.config_name),
    ...manualMqttDevices
      .filter(
        (device) =>
          device && Object.prototype.hasOwnProperty.call(device, "name")
      )
      .map((device) => device.name),
  ];
  let count = names.length;
  let proposalsChanged = false;
  for (const entry of zendureMqttPreviewProposals.values()) {
    if (
      !entry ||
      String(entry.target || "device").toLowerCase() === "grid_meter" ||
      Object.prototype.hasOwnProperty.call(entry, "config_name")
    ) continue;
    entry.config_name = nextCompactInverterName(names, count);
    names.push(entry.config_name);
    count += 1;
    proposalsChanged = true;
  }
  let manualChanged = false;
  for (const device of manualMqttDevices) {
    if (!device || Object.prototype.hasOwnProperty.call(device, "name")) continue;
    device.name = nextCompactInverterName(names, count);
    names.push(device.name);
    count += 1;
    manualChanged = true;
  }
  if (proposalsChanged) saveMqttPreviewProposals();
  if (manualChanged) saveManualMqttDevices();
}

function loadConfigDismissed() {
  try {
    const raw = window.localStorage.getItem(CONFIG_DISMISSED_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch (err) {
    return new Set();
  }
}

function saveConfigDismissed() {
  try {
    window.localStorage.setItem(
      CONFIG_DISMISSED_STORAGE_KEY,
      JSON.stringify([...configDismissed])
    );
  } catch (err) {
    /* localStorage may be unavailable; dismissed set still lives in memory. */
  }
}

// A stored dismissal key is an identity-set member: a "serial:<serial>" sentinel
// or an opaque token. Legacy stores held bare serials, so those are upgraded to
// the sentinel form on load.
function dismissalStorageKey(value) {
  const raw = String(value == null ? "" : value).trim();
  if (!raw) return "";
  if (raw.startsWith("serial:") || /^opaque:v1:[A-Za-z0-9_-]+$/.test(raw)) return raw;
  const serial = usableSerialValue(raw);
  return serial ? "serial:" + serial : "";
}

function loadDismissedSerials() {
  try {
    const raw = window.localStorage.getItem(CONFIG_DISMISSED_SERIALS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(
      (Array.isArray(parsed) ? parsed : []).map(dismissalStorageKey).filter(Boolean)
    );
  } catch (err) {
    return new Set();
  }
}

function saveDismissedSerials() {
  try {
    window.localStorage.setItem(
      CONFIG_DISMISSED_SERIALS_STORAGE_KEY,
      JSON.stringify([...dismissedSerials])
    );
  } catch (err) {
    /* localStorage may be unavailable; the set still lives in memory. */
  }
}

// Dismissal is stored under the *strongest* identity: a visible serial when
// present, else the opaque tokens. Keying a serial-bearing device by its serial
// alone (not its MQTT tokens) is what lets re-adding a dual-transport inverter
// over Local API — whose scan device carries only the serial — clear a dismissal
// created from its token-rich MQTT entry. Undismiss and the dismissed-check below
// still span the whole alias set, so a route-only dismissal survives enrichment.
function dismissalKeysForInverter(deviceOrSerial) {
  if (deviceOrSerial && typeof deviceOrSerial === "object") {
    const serial = inverterVisibleSerial(deviceOrSerial);
    if (serial) return new Set(["serial:" + serial]);
    return inverterIdentityTokens(deviceOrSerial);
  }
  const serial = usableSerialValue(deviceOrSerial);
  return serial ? new Set(["serial:" + serial]) : new Set();
}

function dismissSerial(deviceOrSerial) {
  let changed = false;
  for (const key of dismissalKeysForInverter(deviceOrSerial)) {
    if (!dismissedSerials.has(key)) {
      dismissedSerials.add(key);
      changed = true;
    }
  }
  if (changed) saveDismissedSerials();
}

function undismissSerial(deviceOrSerial) {
  let changed = false;
  for (const key of inverterIdentitySetOf(deviceOrSerial)) {
    if (dismissedSerials.delete(key)) changed = true;
  }
  if (changed) saveDismissedSerials();
}

function inverterDismissed(deviceOrSerial) {
  for (const key of inverterIdentitySetOf(deviceOrSerial)) {
    if (dismissedSerials.has(key)) return true;
  }
  return false;
}

// Only supported inverters and grid meters are config candidates; unknown and
// ignored devices (a separate map) never reach the aggregate list.
function isConfigCandidate(device) {
  const role = String(device.role_suggestion || "unknown");
  return role === "inverter" || role === "grid_meter";
}

function availableConfigDevices() {
  return aggregateDevices().filter(isConfigCandidate);
}

// A device is auto-config ready only when it verified over HTTP and carries the
// fields the EMS config needs (serial for inverters, reachable meter). Unknown
// verification (older records without the flag) counts as verified.
function isAutoConfigReady(device) {
  const ready =
    device.usable_for_config !== undefined
      ? device.usable_for_config
      : device.config_ready;
  return device.verified !== false && Boolean(ready);
}

function supportedGridMeters() {
  return availableConfigDevices().filter(
    (device) =>
      String(device.role_suggestion) === "grid_meter" &&
      isAutoConfigReady(device)
  );
}

function draftHasSource(sourceId) {
  return configDraftItems.some((item) => item.source_id === sourceId);
}

function inverterItems() {
  return configDraftItems.filter((item) => item.role === "inverter");
}

function gridMeterItem() {
  return configDraftItems.find((item) => item.role === "grid_meter") || null;
}

function nextCompactInverterName(existingNames, inverterCount) {
  const names = Array.isArray(existingNames) ? existingNames : [];
  const used = new Set(
    names.map((name) => String(name == null ? "" : name).trim().toLowerCase())
  );
  let highest = 0;
  for (const name of names) {
    const match = /^INV_([1-9][0-9]*)$/i.exec(
      String(name == null ? "" : name).trim()
    );
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  const count = Number.isFinite(Number(inverterCount))
    ? Math.max(0, Math.trunc(Number(inverterCount)))
    : names.length;
  let number = Math.max(1, highest + 1, count + 1);
  while (used.has(("INV_" + number).toLowerCase())) number += 1;
  return "INV_" + number;
}

function freshInverterConfigNames(excludeEntry) {
  return [
    ...inverterItems()
      .filter((item) => item !== excludeEntry)
      .map((item) => item.config_name),
    ...selectedMqttDeviceEntries()
      .filter((entry) => entry !== excludeEntry)
      .map((entry) => entry.config_name),
    ...manualMqttDevices
      .filter((device) => device !== excludeEntry)
      .map((device) => device.name),
  ];
}

function nextInverterName(excludeEntry) {
  const names = freshInverterConfigNames(excludeEntry);
  return nextCompactInverterName(names, names.length);
}

function uniqueDisplayName(base, role) {
  const used = new Set(
    configDraftItems
      .filter((item) => item.role === role)
      .map((item) => item.display_name)
  );
  if (!used.has(base)) return base;
  let suffix = 2;
  while (used.has(base + " #" + suffix)) suffix += 1;
  return base + " #" + suffix;
}

function draftItemFromDevice(device, role) {
  const sources = sourcesOf(device);
  if (role === "grid_meter") {
    return {
      source_id: deviceKey(device),
      config_name: "grid_meter",
      display_name: uniqueDisplayName(
        device.display_name || device.model || DEFAULT_GRID_METER_DISPLAY,
        "grid_meter"
      ),
      role: "grid_meter",
      enabled: true,
      grid_meter_type: "",
      ip: device.ip || "",
      port: device.port || "",
      serial_number: device.serial_number || "",
      device_type: device.device_type || "",
      api_family: device.api_family || "",
      discovery_source: sources[0] || "",
    };
  }
  return {
    source_id: deviceKey(device),
    config_name:
      rememberedInverterName(device) || nextInverterName(),
    display_name: uniqueDisplayName(
      device.display_name || device.model || DEFAULT_INVERTER_DISPLAY,
      "inverter"
    ),
    role: "inverter",
    enabled: true,
    ip: device.ip || "",
    port: device.port || "",
    serial_number: device.serial_number || "",
    device_type: device.device_type || "",
    api_family: device.api_family || "",
    discovery_source: sources[0] || "",
  };
}

function addDeviceToDraft(sourceId, role) {
  const device = configAvailableIndex.get(sourceId);
  if (!device) return;
  configDismissed.delete(sourceId);
  saveConfigDismissed();
  // Clear the identity dismissal too, or the reconciler would drop this manual
  // re-add on the next pass (removal dismisses the identity across transports).
  undismissSerial(device);
  if (role === "grid_meter") {
    selectGridMeter(sourceId);
    return;
  }
  if (draftHasSource(sourceId)) return;
  const item = draftItemFromDevice(device, "inverter");
  item.auto_added = false;
  configDraftItems.push(item);
  commitDraftChange();
}

// Single primary grid meter: a manual pick always replaces any existing one so
// the draft can never hold a duplicate_grid_meter.
function selectGridMeter(sourceId) {
  const device = configAvailableIndex.get(sourceId);
  if (!device) return;
  configDismissed.delete(sourceId);
  saveConfigDismissed();
  configDraftItems = configDraftItems.filter(
    (item) => item.role !== "grid_meter"
  );
  const item = draftItemFromDevice(device, "grid_meter");
  item.auto_selected = false;
  syncGridMeterFeatureValues(item);
  configDraftItems.push(item);
  commitDraftChange();
}

function removeDraftItem(sourceId) {
  // Remember the removal so auto-config does not re-add it on the next poll.
  configDismissed.add(sourceId);
  openHardwareCards.delete(sourceId);
  saveConfigDismissed();
  // Dismiss the serial too, so the reconciler does not re-select it over MQTT.
  const removed = configDraftItems.find((item) => item.source_id === sourceId);
  if (removed) {
    dismissSerial(removed);
    forgetInverterName(removed);
  }
  configDraftItems = configDraftItems.filter(
    (item) => item.source_id !== sourceId
  );
  commitDraftChange();
}

function removeMqttInverter(proposalId) {
  const entry = zendureMqttPreviewProposals.get(String(proposalId));
  if (!entry) return;
  dismissSerial(entry);
  forgetInverterName(entry);
  zendureMqttPreviewProposals.delete(String(proposalId));
  openHardwareCards.delete(String(proposalId));
  saveMqttPreviewProposals();
  renderMqttProposals(latestMqttProposals);
  renderConfigDraft();
  renderConfigAvailable();
}

// Candidate actions address a connection through an opaque per-render token. A
// serial-less Cloud proposal id falls back to the raw route device id or product
// key, which must never reach the DOM; a token from an earlier render no longer
// resolves, so a click on a stale card fails closed instead of switching.
let connectionCandidateTokens = new Map();

function resetConnectionCandidateTokens() {
  connectionCandidateTokens = new Map();
}

function connectionCandidateToken(source, ref) {
  const value = String(ref || "");
  if (!value) return "";
  const token = "conn" + (connectionCandidateTokens.size + 1);
  connectionCandidateTokens.set(token, { source: String(source || ""), ref: value });
  return token;
}

function resolveConnectionCandidateToken(token) {
  return connectionCandidateTokens.get(String(token || "")) || null;
}

// Everything that survives a connection change: every catalog-driven common
// device value the user entered. Identity and connection fields are owned by the
// target connection and are never carried over.
function preservedInverterValues(item) {
  const values = {};
  const source = item && item.config_values;
  if (!source || typeof source !== "object") return values;
  for (const key of Object.keys(source)) {
    if (Object.prototype.hasOwnProperty.call(DEVICE_MAPPED_FIELD_KEYS, key)) {
      continue;
    }
    values[key] = source[key];
  }
  return values;
}

// Switch a device to another connection as a manual choice; drop the previous
// one. Accepts an identity reference (a visible serial or an opaque token), so a
// route-only device switches without a physical serial and without exposing a
// raw route id. The logical inverter survives: name, enabled state and common
// EMS values are carried over, only connection fields are replaced.
function switchInverterTransport(serial, targetSource, options) {
  const ref = String(serial == null ? "" : serial).trim();
  if (!ref) return;
  const probe = /^opaque:v1:[A-Za-z0-9_-]+$/.test(ref)
    ? { physical_identity_token: ref }
    : { serial_number: ref };
  if (!inverterHasIdentity(probe)) return;
  const request = options || {};
  let candidateRef = String(request.candidateRef || "").trim();
  if (request.token) {
    const resolved = resolveConnectionCandidateToken(request.token);
    if (!resolved) {
      // The pool was redrawn since this card: never switch on a stale reference.
      renderConfigAvailable();
      return;
    }
    candidateRef = resolved.ref;
  }
  const matches = (candidate) => inverterIdentitiesMatch(probe, candidate);
  const current = configuredInverterConnection(probe);
  const preservedName = inverterConfigNameForSerial(probe) || nextInverterName();
  const preservedValues = preservedInverterValues(current && current.item);
  const preservedEnabled = current ? current.item.enabled !== false : true;

  // Resolve the exact target before mutating anything: a stale or ambiguous
  // reference must leave the draft untouched rather than half-switch it.
  let device = null;
  let proposal = null;
  if (targetSource === "local_api") {
    const offered = availableConfigDevices().filter(
      (candidate) =>
        String(candidate.role_suggestion) === "inverter" && matches(candidate)
    );
    device = candidateRef
      ? offered.find((candidate) => deviceKey(candidate) === candidateRef) || null
      : offered[0] || null;
    if (!device) return;
  } else {
    const offered = availableMqttDeviceProposals().filter(
      (candidate) =>
        matches(candidate) &&
        mqttSourceOfConnection(candidate.connection_source) === targetSource
    );
    // Without an exact reference several brokers for one inverter are
    // ambiguous; picking the first would bind the wrong broker and route.
    proposal = candidateRef
      ? offered.find((candidate) => String(candidate.id || "") === candidateRef) || null
      : offered.length === 1
        ? offered[0]
        : null;
    if (!proposal) return;
  }

  rememberInverterName(probe, preservedName);
  undismissSerial(probe);
  if (device) {
    for (const [id, entry] of [...zendureMqttPreviewProposals.entries()]) {
      if (matches(entry)) zendureMqttPreviewProposals.delete(id);
    }
    saveMqttPreviewProposals();
    const sourceId = deviceKey(device);
    configDismissed.delete(sourceId);
    saveConfigDismissed();
    if (!draftHasSource(sourceId)) {
      const item = draftItemFromDevice(device, "inverter");
      item.config_name = preservedName;
      item.config_values = preservedValues;
      item.enabled = preservedEnabled;
      item.auto_added = false;
      configDraftItems.push(item);
      saveConfigDraft();
    }
  } else {
    for (const [id, entry] of [...zendureMqttPreviewProposals.entries()]) {
      if (matches(entry)) zendureMqttPreviewProposals.delete(id);
    }
    for (const item of configDraftItems) {
      if (item.role === "inverter" && matches(item)) {
        configDismissed.add(item.source_id);
      }
    }
    saveConfigDismissed();
    configDraftItems = configDraftItems.filter(
      (item) => item.role !== "inverter" || !matches(item)
    );
    saveConfigDraft();
    const entry = serializeMqttProposalSelection(proposal, {
      target: "device",
      configValues: preservedValues,
      enabled: preservedEnabled,
    });
    entry.config_name = preservedName;
    entry.selection_origin = "manual";
    entry.display_name = proposal.display_name || proposal.hardware_model || "";
    zendureMqttPreviewProposals.set(String(proposal.id || ""), entry);
    saveMqttPreviewProposals();
    renderMqttProposals(latestMqttProposals);
  }
  renderConfigDraft();
  renderConfigAvailable();
}

function moveDraftItem(sourceId, delta) {
  const index = configDraftItems.findIndex(
    (item) => item.source_id === sourceId
  );
  const target = index + delta;
  if (index < 0 || target < 0 || target >= configDraftItems.length) return;
  const [item] = configDraftItems.splice(index, 1);
  configDraftItems.splice(target, 0, item);
  commitDraftChange();
}

function resetDraftItemName(sourceId) {
  const item = configDraftItems.find((entry) => entry.source_id === sourceId);
  if (!item) return;
  if (item.role === "grid_meter") {
    item.config_name = "grid_meter";
  } else {
    item.config_name = nextInverterName(item);
    rememberInverterName(item.serial_number, item.config_name);
  }
  commitDraftChange();
}

function resetMqttInverterName(proposalId) {
  const entry = zendureMqttPreviewProposals.get(String(proposalId));
  if (!entry) return;
  entry.config_name = nextInverterName(entry);
  rememberInverterName(entry, entry.config_name);
  saveMqttPreviewProposals();
  renderInverterList();
  renderConfigPreview();
  renderConfigValidation();
}

// Structural change: persist, then redraw draft + available (added-state) + preview.
function commitDraftChange() {
  saveConfigDraft();
  renderConfigDraft();
  renderConfigAvailable();
}

// True when zendure_mqtt is enabled and ranked above local_api, so a device
// offered over MQTT must not be auto-grabbed as a local-HTTP device.
function zendureMqttPreferredOverLocalApi() {
  if (!discoverySourceEnabled("zendure_mqtt")) return false;
  const priority = discoveryPreparation.discovery_priority || [];
  const mqttIndex = priority.indexOf("zendure_mqtt");
  if (mqttIndex === -1) return false;
  const apiIndex = priority.indexOf("local_api");
  return apiIndex === -1 || mqttIndex < apiIndex;
}

// True when the same physical device (by serial) is available as a Zendure MQTT
// device proposal, so the user can select it over MQTT instead.
function serialOfferedOverZendureMqtt(serial) {
  const wanted = String(serial || "").trim().toLowerCase();
  if (!wanted) return false;
  return latestMqttProposals.some(
    (proposal) =>
      !isMqttGridMeterProposal(proposal) &&
      String(proposal.serial_number || "").trim().toLowerCase() === wanted
  );
}

// --- Unified physical-device transport selection ---------------------------
// One per-serial view derived from the two stores (Local-API draft + selected
// MQTT proposals); one reconciler keeps them to a single transport per serial.

function normalizeSerial(value) {
  return String(value == null ? "" : value).trim().toLowerCase();
}

function usableSerialValue(value) {
  if (typeof value === "string" && (value.includes("•") || value.includes("…"))) {
    return "";
  }
  const key = normalizeSerial(value);
  if (
    !key ||
    key.startsWith("your_") ||
    key.startsWith("your-") ||
    ["<redacted>", "[redacted]", "redacted"].includes(key)
  ) {
    return "";
  }
  return key;
}

// One browser-safe inverter equality key across transports: an explicit
// physical serial wins; otherwise only the server-issued opaque token may be
// used. MQTT routing ids are account-scoped write targets and never identity
// material in the browser. Placeholder and display-masked values are ignored.
function physicalInverterIdentity(device) {
  if (!device) return "";
  const serial = usableSerialValue(device.sn) || usableSerialValue(device.serial_number);
  if (serial) return serial;
  const token = String(device.physical_identity_token || "").trim();
  return /^opaque:v1:[A-Za-z0-9_-]+$/.test(token) ? token : "";
}

function inverterVisibleSerial(device) {
  if (!device) return "";
  return usableSerialValue(device.sn) || usableSerialValue(device.serial_number);
}

// The server-issued opaque equality tokens (primary + trusted aliases) a device
// or proposal carries. Tokens are keyed, non-reversible and equality-only; raw
// MQTT route ids are never identity material in the browser. An inverter keeps
// its scoped-route alias token even once a physical serial is added, so a
// route-only device and a later serial-bearing observation still intersect.
function inverterIdentityTokens(device) {
  const tokens = new Set();
  if (!device) return tokens;
  const add = (value) => {
    const token = String(value == null ? "" : value).trim();
    if (/^opaque:v1:[A-Za-z0-9_-]+$/.test(token)) tokens.add(token);
  };
  add(device.physical_identity_token);
  const aliases = device.physical_identity_alias_tokens;
  if (Array.isArray(aliases)) aliases.forEach(add);
  return tokens;
}

// The full identity set: a visible physical serial (stable across transports)
// plus every opaque alias token.
function inverterIdentitySet(device) {
  const set = inverterIdentityTokens(device);
  const serial = inverterVisibleSerial(device);
  if (serial) set.add("serial:" + serial);
  return set;
}

function inverterHasIdentity(device) {
  return inverterIdentitySet(device).size > 0;
}

// A shared scoped-route alias combined with different visible serials is a
// contradiction: the route claims one inverter, the serials claim two.
function inverterIdentityConflict(a, b) {
  const sa = inverterVisibleSerial(a);
  const sb = inverterVisibleSerial(b);
  if (!sa || !sb || sa === sb) return false;
  const ta = inverterIdentityTokens(a);
  const tb = inverterIdentityTokens(b);
  for (const token of ta) {
    if (tb.has(token)) return true;
  }
  return false;
}

// Two observations are the same logical inverter when any trusted identity
// alias intersects and no serial/route conflict contradicts it.
function inverterIdentitiesMatch(a, b) {
  if (inverterIdentityConflict(a, b)) return false;
  const sa = inverterIdentitySet(a);
  const sb = inverterIdentitySet(b);
  for (const key of sa) {
    if (sb.has(key)) return true;
  }
  return false;
}

// The identity set for a device object or a bare serial string, so the same
// alias-aware Setup state (names, dismissal) works for a serial-bearing device
// and a route-only device carrying only opaque tokens.
function inverterIdentitySetOf(deviceOrSerial) {
  if (deviceOrSerial && typeof deviceOrSerial === "object") {
    return inverterIdentitySet(deviceOrSerial);
  }
  const serial = usableSerialValue(deviceOrSerial);
  return serial ? new Set(["serial:" + serial]) : new Set();
}

// Validated, de-duplicated opaque alias tokens. Only server-issued opaque
// tokens are kept; raw route ids are never carried as identity material.
function normalizeInverterAliasTokens(value) {
  if (!Array.isArray(value)) return undefined;
  const seen = new Set();
  const tokens = [];
  for (const raw of value) {
    const token = String(raw == null ? "" : raw).trim();
    if (/^opaque:v1:[A-Za-z0-9_-]+$/.test(token) && !seen.has(token)) {
      seen.add(token);
      tokens.push(token);
    }
  }
  return tokens.length ? tokens : undefined;
}

// Match a candidate against a canonical identity reference (a visible serial or
// an opaque token), so transport switching accepts an identity reference rather
// than requiring a physical serial.
function inverterIdentityRefMatches(ref, candidate) {
  const raw = String(ref == null ? "" : ref).trim();
  if (!raw) return false;
  const probe = /^opaque:v1:[A-Za-z0-9_-]+$/.test(raw)
    ? { physical_identity_token: raw }
    : { serial_number: raw };
  return inverterIdentitiesMatch(probe, candidate);
}

function mqttSourceOfConnection(connectionSource) {
  return String(connectionSource || "") === "zendure_cloud_mqtt"
    ? "zendure_mqtt"
    : "local_mqtt";
}

// The one user-facing connection vocabulary for Setup and Maintenance. An
// unrecognized source never claims a concrete transport.
function connectionLabelFor(source) {
  if (source === "local_api") return "API";
  if (source === "local_mqtt") return "MQTT";
  if (source === "zendure_mqtt") return "Zendure MQTT";
  return "Unknown";
}

// The broker/account scope of an MQTT connection. Setup proposals and selections
// carry it top level, Maintenance devices under mqtt/broker.
function connectionBrokerScope(entry) {
  if (!entry) return "";
  const mqtt = entry.mqtt || {};
  const broker = entry.broker || {};
  return String(entry.broker_ref || mqtt.broker_ref || broker.ref || "").trim();
}

// Two MQTT observations are one concrete connection only within the same source
// and broker scope. Missing scope evidence never counts as equal on its own: the
// trusted proposal reference then decides, so two distinguishable brokers are
// never collapsed into one.
function sameMqttConnectionScope(a, b) {
  if (
    mqttSourceOfConnection(a && a.connection_source) !==
    mqttSourceOfConnection(b && b.connection_source)
  ) {
    return false;
  }
  const configured = connectionBrokerScope(a);
  const offered = connectionBrokerScope(b);
  if (configured && offered) return configured === offered;
  return String((a && a.id) || "") === String((b && b.id) || "");
}

function concreteMqttConnectionKey(entry) {
  const identity =
    physicalInverterIdentity(entry) || String((entry && entry.id) || "");
  return (
    identity +
    "|" +
    mqttSourceOfConnection(entry && entry.connection_source) +
    "|" +
    connectionBrokerScope(entry)
  );
}

function selectedMqttDeviceEntries() {
  return Array.from(zendureMqttPreviewProposals.values()).filter(
    (entry) => String(entry.target || "device").toLowerCase() !== "grid_meter"
  );
}

// Name memory is keyed by every identity alias (serial sentinel + opaque
// tokens), so a route-only inverter's name survives serial enrichment: the name
// stored under the route token is still found once the serial (and its token)
// is added.
function rememberedInverterName(deviceOrSerial) {
  for (const key of inverterIdentitySetOf(deviceOrSerial)) {
    const name = transportInverterNames.get(key);
    if (name) return name;
  }
  return "";
}

function rememberInverterName(deviceOrSerial, name) {
  const value = String(name || "").trim();
  if (!value) return;
  for (const key of inverterIdentitySetOf(deviceOrSerial)) {
    transportInverterNames.set(key, value);
  }
}

function forgetInverterName(deviceOrSerial) {
  for (const key of inverterIdentitySetOf(deviceOrSerial)) {
    transportInverterNames.delete(key);
  }
}

function inverterConfigNameForSerial(deviceOrSerial) {
  const target =
    deviceOrSerial && typeof deviceOrSerial === "object"
      ? deviceOrSerial
      : { serial_number: deviceOrSerial };
  if (!inverterHasIdentity(target)) return "";
  const http = inverterItems().find((item) => inverterIdentitiesMatch(item, target));
  if (http && String(http.config_name || "").trim()) return http.config_name.trim();
  const mqtt = selectedMqttDeviceEntries().find((entry) =>
    inverterIdentitiesMatch(entry, target)
  );
  if (mqtt && String(mqtt.config_name || "").trim()) return mqtt.config_name.trim();
  return rememberedInverterName(target);
}

function availableMqttDeviceProposals() {
  return latestMqttProposals.filter(
    (proposal) => !isMqttGridMeterProposal(proposal) && proposal.config_fragment
  );
}

// Manual choice is never overridden (surfaces as unavailable if its source is
// gone); otherwise the highest-priority available source wins.
function resolveSelectedDeviceSource({ available, sourcePriority, previous }) {
  const present = (Array.isArray(available) ? available : []).filter(Boolean);
  const priority = Array.isArray(sourcePriority) ? sourcePriority : [];
  if (previous && previous.selectionOrigin === "manual" && previous.selectedSource) {
    return {
      selectedSource: previous.selectedSource,
      selectionOrigin: "manual",
      available: present.includes(previous.selectedSource),
    };
  }
  const ranked = priority.filter((source) => present.includes(source));
  const selectedSource = ranked[0] || present[0] || null;
  const selectionOrigin =
    selectedSource == null ? "none" : present.length > 1 ? "priority" : "automatic";
  return { selectedSource, selectionOrigin, available: selectedSource != null };
}

// Pure planner: group physical inverters into connected identity components
// (serial + opaque tokens, the same semantics as Maintenance), pick one
// transport each, and return the drops/adds. A device intersecting several
// groups unions all of them, so a bridging observation merges every group it
// connects (transitive). Two different serials never merge — not directly and
// not through a bridge. Idempotent.
function reconcileTransportSelection(state) {
  const priority = Array.isArray(state.priority) ? state.priority : [];
  const enabled = state.enabledSources || {};
  const dismissed = new Set(
    (state.dismissedSerials || []).map(dismissalStorageKey).filter(Boolean)
  );
  const groups = [];
  const mergeIdentity = (group, device) => {
    const serial = inverterVisibleSerial(device);
    if (serial && !group.identity.serial_number) group.identity.serial_number = serial;
    for (const token of inverterIdentityTokens(device)) {
      if (!group.identity.physical_identity_token) {
        group.identity.physical_identity_token = token;
      }
      group.aliasTokens.add(token);
    }
    group.identity.physical_identity_alias_tokens = [...group.aliasTokens];
    group.keys = inverterIdentitySet(group.identity);
  };
  const newGroup = () => ({
    identity: {
      serial_number: "",
      physical_identity_token: "",
      physical_identity_alias_tokens: [],
    },
    aliasTokens: new Set(),
    keys: new Set(),
    http: [],
    mqtt: [],
    proposalBySource: {},
    sources: new Set(),
  });
  // Two groups with different visible serials are two physical inverters and
  // must never be unioned, even by a shared route token: that is a contradictory
  // bridge (fail closed).
  const groupsConflict = (a, b) => {
    const sa = inverterVisibleSerial(a.identity);
    const sb = inverterVisibleSerial(b.identity);
    return Boolean(sa && sb && sa !== sb);
  };
  const mergeGroup = (target, other) => {
    mergeIdentity(target, other.identity);
    target.http.push(...other.http);
    target.mqtt.push(...other.mqtt);
    for (const source of other.sources) target.sources.add(source);
    for (const [source, id] of Object.entries(other.proposalBySource)) {
      if (!target.proposalBySource[source]) target.proposalBySource[source] = id;
    }
  };
  const groupFor = (device) => {
    const matches = groups.filter(
      (group) =>
        !inverterIdentityConflict(group.identity, device) &&
        inverterIdentitiesMatch(group.identity, device)
    );
    if (!matches.length) {
      const group = newGroup();
      mergeIdentity(group, device);
      groups.push(group);
      return group;
    }
    // If any two matched groups conflict with each other, the device is a
    // contradictory bridge: keep it in its own group and never union them.
    for (let i = 0; i < matches.length; i++) {
      for (let j = i + 1; j < matches.length; j++) {
        if (groupsConflict(matches[i], matches[j])) {
          const group = newGroup();
          mergeIdentity(group, device);
          groups.push(group);
          return group;
        }
      }
    }
    const primary = matches[0];
    mergeIdentity(primary, device);
    for (let k = 1; k < matches.length; k++) mergeGroup(primary, matches[k]);
    const absorbed = new Set(matches.slice(1));
    for (let idx = groups.length - 1; idx >= 0; idx--) {
      if (absorbed.has(groups[idx])) groups.splice(idx, 1);
    }
    return primary;
  };

  for (const item of state.httpInverters || []) {
    if (!inverterHasIdentity(item)) continue;
    const group = groupFor(item);
    group.http.push(item);
    group.sources.add("local_api");
  }
  for (const serial of state.httpCandidateSerials || []) {
    const candidate = { serial_number: serial };
    if (!inverterHasIdentity(candidate)) continue;
    groupFor(candidate).sources.add("local_api");
  }
  for (const sel of state.mqttSelections || []) {
    if (!inverterHasIdentity(sel)) continue;
    const source = mqttSourceOfConnection(sel.connection_source);
    const group = groupFor(sel);
    group.mqtt.push({ id: String(sel.id || ""), source, origin: sel.selection_origin });
    group.sources.add(source);
  }
  for (const proposal of state.mqttProposals || []) {
    if (!inverterHasIdentity(proposal)) continue;
    const source = mqttSourceOfConnection(proposal.connection_source);
    const group = groupFor(proposal);
    if (!group.proposalBySource[source]) {
      group.proposalBySource[source] = String(proposal.id || "");
    }
    group.sources.add(source);
  }

  const dropHttpSourceIds = [];
  const dropMqttSelectionIds = [];
  const selectMqttProposalIds = [];
  const physicalDevices = [];

  for (const group of groups) {
    const groupRef = physicalInverterIdentity(group.identity);
    const isDismissed = [...group.keys].some((key) => dismissed.has(key));
    if (isDismissed) {
      for (const item of group.http) dropHttpSourceIds.push(item.source_id);
      for (const sel of group.mqtt) dropMqttSelectionIds.push(sel.id);
      physicalDevices.push({
        serial: groupRef,
        sources: [...group.sources],
        selectedSource: null,
        selectionOrigin: "none",
        available: false,
      });
      continue;
    }

    const manualHttp = group.http.find((item) => item.auto_added === false);
    const manualMqtt = group.mqtt.find((sel) => sel.origin === "manual");
    let previous = null;
    if (manualHttp) {
      previous = { selectedSource: "local_api", selectionOrigin: "manual" };
    } else if (manualMqtt) {
      previous = { selectedSource: manualMqtt.source, selectionOrigin: "manual" };
    } else if (group.mqtt.length) {
      previous = { selectedSource: group.mqtt[0].source, selectionOrigin: "automatic" };
    } else if (group.http.length) {
      previous = { selectedSource: "local_api", selectionOrigin: "automatic" };
    }

    const available = [...group.sources].filter((source) => enabled[source] !== false);
    const resolved = resolveSelectedDeviceSource({
      available,
      sourcePriority: priority,
      previous,
    });
    const selected = resolved.selectedSource;
    const selectedIsMqtt = selected === "zendure_mqtt" || selected === "local_mqtt";

    if (selected === "local_api") {
      for (const sel of group.mqtt) dropMqttSelectionIds.push(sel.id);
    } else if (selectedIsMqtt) {
      for (const item of group.http) dropHttpSourceIds.push(item.source_id);
      for (const sel of group.mqtt) {
        if (sel.source !== selected) dropMqttSelectionIds.push(sel.id);
      }
      const proposalId = group.proposalBySource[selected];
      const sameSource = group.mqtt.filter((sel) => sel.source === selected);
      const stale = proposalId
        ? sameSource.filter((sel) => sel.id !== proposalId)
        : [];
      const hasCanonical =
        proposalId && sameSource.some((sel) => sel.id === proposalId);
      if (stale.length) {
        // A stored selection predates the current proposal id (a route-only
        // selection enriched with a product key/serial). Replace it with the
        // current proposal so exactly one selected entry remains, preserving a
        // manual transport choice.
        for (const sel of stale) dropMqttSelectionIds.push(sel.id);
        if (!hasCanonical) {
          const manual = stale.some((sel) => sel.origin === "manual");
          selectMqttProposalIds.push({
            id: proposalId,
            serial_number: groupRef,
            selection_origin: manual ? "manual" : resolved.selectionOrigin,
          });
        }
      } else if (!sameSource.length && proposalId && group.sources.has("local_api")) {
        // Auto-select only when local_api also has it; MQTT-only devices are added manually.
        selectMqttProposalIds.push({
          id: proposalId,
          serial_number: groupRef,
          selection_origin: resolved.selectionOrigin,
        });
      }
    } else {
      // No source resolved: drop auto-added HTTP only, keep manual entries.
      for (const item of group.http) {
        if (item.auto_added !== false) dropHttpSourceIds.push(item.source_id);
      }
    }

    physicalDevices.push({
      serial: groupRef,
      sources: [...group.sources],
      selectedSource: selected,
      selectionOrigin: resolved.selectionOrigin,
      available: resolved.available !== false,
    });
  }

  return {
    physicalDevices,
    dropHttpSourceIds,
    dropMqttSelectionIds,
    selectMqttProposalIds,
  };
}

// So HTTP auto-add never adds a serial already selected over MQTT (one transport).
function serialSelectedOverMqtt(serial) {
  const wanted = normalizeSerial(serial);
  if (!wanted) return false;
  return selectedMqttDeviceEntries().some(
    (entry) => normalizeSerial(entry.serial_number) === wanted
  );
}

// Build reconciler state from the two live stores, run the planner, apply it.
function reconcileInverterTransports() {
  for (const item of inverterItems()) {
    rememberInverterName(item, item.config_name);
  }
  for (const entry of selectedMqttDeviceEntries()) {
    rememberInverterName(entry, entry.config_name);
  }
  const httpCandidateSerials = availableConfigDevices()
    .filter(
      (device) =>
        String(device.role_suggestion) === "inverter" && isAutoConfigReady(device)
    )
    .map((device) => device.serial_number);
  const plan = reconcileTransportSelection({
    httpInverters: inverterItems().map((item) => ({
      source_id: item.source_id,
      serial_number: item.serial_number,
      auto_added: item.auto_added === true,
    })),
    mqttSelections: selectedMqttDeviceEntries().map((entry) => ({
      id: entry.id,
      serial_number: entry.serial_number,
      physical_identity_token: entry.physical_identity_token,
      physical_identity_alias_tokens: entry.physical_identity_alias_tokens,
      connection_source: entry.connection_source,
      selection_origin: entry.selection_origin,
    })),
    httpCandidateSerials,
    mqttProposals: availableMqttDeviceProposals().map((proposal) => ({
      id: proposal.id,
      serial_number: proposal.serial_number,
      physical_identity_token: proposal.physical_identity_token,
      physical_identity_alias_tokens: proposal.physical_identity_alias_tokens,
      connection_source: proposal.connection_source,
    })),
    priority: discoveryPreparation.discovery_priority || [],
    enabledSources: {
      local_api: discoverySourceEnabled("local_api"),
      local_mqtt: discoverySourceEnabled("local_mqtt"),
      zendure_mqtt: discoverySourceEnabled("zendure_mqtt"),
    },
    dismissedSerials: [...dismissedSerials],
  });

  let changed = false;
  if (plan.dropHttpSourceIds.length) {
    const drop = new Set(plan.dropHttpSourceIds);
    const before = configDraftItems.length;
    configDraftItems = configDraftItems.filter(
      (item) => item.role !== "inverter" || !drop.has(item.source_id)
    );
    if (configDraftItems.length !== before) changed = true;
  }
  let mqttChanged = false;
  for (const id of plan.dropMqttSelectionIds) {
    if (zendureMqttPreviewProposals.delete(String(id))) mqttChanged = true;
  }
  for (const selection of plan.selectMqttProposalIds) {
    const proposal = latestMqttProposals.find(
      (candidate) => String(candidate.id || "") === String(selection.id)
    );
    if (!proposal) continue;
    const entry = serializeMqttProposalSelection(proposal, { target: "device" });
    entry.config_name = rememberedInverterName(proposal) || entry.config_name;
    entry.selection_origin = selection.selection_origin || "automatic";
    entry.display_name = proposal.display_name || proposal.hardware_model || "";
    zendureMqttPreviewProposals.set(String(selection.id), entry);
    mqttChanged = true;
  }
  if (mqttChanged) {
    saveMqttPreviewProposals();
    renderMqttProposals(latestMqttProposals);
    changed = true;
  }
  return changed;
}

// Auto-add every verified inverter that is not already in the draft and was not
// removed by the user. Stale-but-present inverters are kept, never re-added. A
// device also offered over the higher-priority Zendure MQTT source is left for
// the user to select over MQTT rather than auto-grabbed as local HTTP.
function autoAddInverters() {
  let changed = false;
  const deferToMqtt = zendureMqttPreferredOverLocalApi();
  for (const device of availableConfigDevices()) {
    if (String(device.role_suggestion) !== "inverter") continue;
    if (!isAutoConfigReady(device)) continue;
    const sourceId = deviceKey(device);
    if (draftHasSource(sourceId) || configDismissed.has(sourceId)) continue;
    if (inverterDismissed(device)) continue;
    if (serialSelectedOverMqtt(device.serial_number)) continue;
    if (deferToMqtt && serialOfferedOverZendureMqtt(device.serial_number)) continue;
    const item = draftItemFromDevice(device, "inverter");
    item.auto_added = true;
    configDraftItems.push(item);
    changed = true;
  }
  return changed;
}

// Auto-select a grid meter only when exactly one is available and none is
// chosen yet. Zero or two-plus meters are left for the user to resolve, and an
// existing selection (auto or manual) is never replaced automatically.
function autoSelectGridMeter() {
  if (gridMeterItem()) return false;
  const meters = supportedGridMeters();
  if (meters.length !== 1) return false;
  const sourceId = deviceKey(meters[0]);
  if (configDismissed.has(sourceId)) return false;
  const item = draftItemFromDevice(meters[0], "grid_meter");
  item.auto_selected = true;
  syncGridMeterFeatureValues(item);
  configDraftItems.push(item);
  return true;
}

function applyAutoConfig() {
  let changed = autoAddInverters();
  if (autoSelectGridMeter()) changed = true;
  if (reconcileInverterTransports()) changed = true;
  // Reconcile may have freed a serial from MQTT (priority moved back to local
  // API); add its HTTP item now so the device is never dropped from both stores.
  if (autoAddInverters()) changed = true;
  return changed;
}

// Discovery-driven refresh: index the current devices, run auto-config, then
// render. Kept separate from renderConfigAvailable so manual edits (which call
// commitDraftChange -> renderConfigAvailable) never trigger auto re-adds.
function syncConfigFromDiscovery() {
  renderConfigAvailable();
  if (applyAutoConfig()) saveConfigDraft();
  renderConfigAvailable();
  renderConfigDraft();
}

function selectedGridMeterStale() {
  const meter = gridMeterItem();
  // Manually entered meters have no discovery record, so liveness never applies.
  if (!meter || meter.manual) return false;
  const device = configAvailableIndex.get(meter.source_id);
  return !device || Boolean(device.stale);
}

function showManualError(text) {
  if (!configEls.manualError) return;
  configEls.manualError.hidden = !text;
  configEls.manualError.textContent = text || "";
}

function manualRole() {
  return configEls.manualRole && configEls.manualRole.value === "grid_meter"
    ? "grid_meter"
    : "inverter";
}

function manualHardwareVariants(role) {
  const variants =
    setupCatalog &&
    setupCatalog.hardware_variants &&
    setupCatalog.hardware_variants[role];
  return Array.isArray(variants) ? variants : [];
}

function selectedManualHardwareVariant() {
  const select = configEls.manualType;
  const variants = manualHardwareVariants(manualRole());
  return (
    variants.find((variant) => variant.id === select.value) ||
    variants.find((variant) => variant.default) ||
    variants[0] ||
    null
  );
}

function syncManualTypeDetails(resetPort) {
  const variant = selectedManualHardwareVariant();
  if (resetPort && configEls.manualPort) {
    configEls.manualPort.value =
      variant && variant.default_port != null ? String(variant.default_port) : "";
  }
}

// Manual type choices are role-specific and come from the setup catalog.
function populateManualTypes(resetSelection) {
  const select = configEls.manualType;
  if (!select) return;
  const variants = manualHardwareVariants(manualRole());
  const previous = select.value;
  select.innerHTML = variants
    .map(
      (variant) =>
        '<option value="' + escapeHtml(variant.id) + '">' +
        escapeHtml(variant.label || variant.id) + "</option>"
    )
    .join("");
  if (!resetSelection && variants.some((variant) => variant.id === previous)) {
    select.value = previous;
  } else {
    const defaultVariant = variants.find((variant) => variant.default);
    if (defaultVariant) select.value = defaultVariant.id;
  }
  syncManualTypeDetails(Boolean(resetSelection));
}

function resetManualTypeForRole() {
  populateManualTypes(true);
}

// Build a draft item from hand-entered details for devices discovery can't reach.
// Keyed by host:port so re-adding the same endpoint is a no-op, and flagged
// `manual` so auto-config and staleness leave it alone.
function addManualDevice() {
  const host = (configEls.manualHost.value || "").trim();
  if (!host) {
    showManualError("Host / IP is required.");
    return;
  }
  const role = manualRole();
  const port = (configEls.manualPort.value || "").trim();
  const sourceId = "manual:" + host + ":" + (port || "");
  if (draftHasSource(sourceId)) {
    showManualError("A device with this host is already in the draft.");
    return;
  }
  const variant = selectedManualHardwareVariant();
  if (!variant) {
    showManualError("Choose a supported connection type.");
    return;
  }
  const selectedType = String(variant.id || "").trim();
  const displayBase =
    (configEls.manualName.value || "").trim() ||
    (role === "grid_meter"
      ? variant.label || DEFAULT_GRID_METER_DISPLAY
      : DEFAULT_INVERTER_DISPLAY);
  if (role === "grid_meter") {
    configDraftItems = configDraftItems.filter((item) => item.role !== "grid_meter");
  }
  configDismissed.delete(sourceId);
  saveConfigDismissed();
  const item = {
    source_id: sourceId,
    config_name: role === "grid_meter" ? "grid_meter" : nextInverterName(),
    display_name: uniqueDisplayName(displayBase, role),
    role,
    enabled: true,
    ip: host,
    port,
    serial_number: (configEls.manualSerial.value || "").trim(),
    device_type: "",
    api_family: "",
    discovery_source: "manual",
    manual: true,
    auto_selected: false,
  };
  if (role === "grid_meter") {
    item.grid_meter_type = selectedType;
  } else {
    item.connection_type = selectedType;
  }
  configDraftItems.push(item);
  if (role === "grid_meter") syncGridMeterFeatureValues(item);
  showManualError("");
  configEls.manualForm.reset();
  resetManualTypeForRole();
  commitDraftChange();
}

function roleLabel(role) {
  return role === "grid_meter" ? "grid meter" : "inverter";
}

// Add more devices is a connection pool, not a physical-device list: one entry
// per concrete connection (identity + source + broker scope), so an alternative
// connection for an already configured inverter stays offered. Only the exact
// selected connection drops out — the configured card shows it instead. The pool
// is built from the current trusted proposals alone, so an obsolete alternative
// disappears with the discovery generation that produced it.
function unselectedMqttDeviceProposals() {
  const selected = selectedMqttDeviceEntries();
  // Seeded with the concrete connections already selected, so a second
  // observation of the active connection collapses instead of reappearing.
  const seen = new Set(selected.map(concreteMqttConnectionKey));
  const candidates = [];
  for (const proposal of availableMqttDeviceProposals()) {
    const id = String(proposal.id || "");
    if (!id || zendureMqttPreviewProposals.has(id)) continue;
    const key = concreteMqttConnectionKey(proposal);
    if (seen.has(key)) continue;
    // A trusted alias still ties an observation to the active connection when
    // only one of the two carries a visible serial.
    if (
      selected.some(
        (entry) =>
          inverterIdentitiesMatch(entry, proposal) &&
          sameMqttConnectionScope(entry, proposal)
      )
    ) {
      continue;
    }
    seen.add(key);
    candidates.push(proposal);
  }
  return candidates;
}

function renderConfigAvailable() {
  if (!configEls.availableList) return;
  resetConnectionCandidateTokens();
  const devices = availableConfigDevices();
  configAvailableIndex.clear();
  for (const device of devices) {
    configAvailableIndex.set(deviceKey(device), device);
  }
  const mqttCandidates = unselectedMqttDeviceProposals();
  const total = devices.length + mqttCandidates.length;
  configEls.availableCount.textContent = total + " ready";
  if (!total) {
    configEls.availableList.hidden = true;
    configEls.availableList.innerHTML = "";
    configEls.availableEmpty.hidden = false;
    return;
  }
  configEls.availableEmpty.hidden = true;
  configEls.availableList.hidden = false;
  configEls.availableList.innerHTML =
    devices.map(renderConfigAvailableCard).join("") +
    mqttCandidates.map(renderMqttCandidateCard).join("");
}

function renderMqttCandidateCard(proposal) {
  const id = escapeHtml(String(proposal.id || ""));
  const safe = String(proposal.id || "").replace(/[^a-z0-9]/gi, "-");
  const source = mqttSourceOfConnection(proposal.connection_source);
  const serial = proposal.serial_number || "";
  const controllable = Boolean(proposal.output_control_supported);
  const model = proposal.display_name || proposal.hardware_model || DEFAULT_INVERTER_DISPLAY;
  const meta = [connectionLabelFor(source), serial ? "SN " + serial : "SN missing"]
    .map((part) => escapeHtml(String(part)))
    .join(" · ");
  const open = openHardwareCards.has(String(proposal.id || ""));
  const candidate = inverterCandidateConnectionState(
    proposal,
    source,
    String(proposal.id || "")
  );
  const action = renderConnectionCandidateAction(
    candidate,
    '<button type="button" class="primary-button compact config-mqtt-add"' +
      ' data-action="add-inverter"' +
      ' data-proposal-id="' + id + '">Add inverter</button>'
  );
  const body =
    '<div class="device-facts">' +
    fact("Connection", escapeHtml(connectionLabelFor(source))) +
    fact(
      "Serial",
      serial
        ? '<span class="v">' + escapeHtml(serial) + "</span>"
        : '<span class="v missing">missing</span>',
      true
    ) +
    fact("Output control", controllable ? "Supported" : "Telemetry only") +
    "</div>";
  return (
    '<article class="' + hardwareCardClass("inverter") + '"' +
    ' data-source-id="' + id + '"' +
    ' data-candidate-state="' + escapeHtml(candidate.state) + '"' +
    (open ? ' data-open="true"' : "") + ">" +
    '<div class="hardware-card-head">' +
    '<button type="button" class="hardware-card-summary" data-available-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") + '"' +
    ' aria-controls="config-available-body-' + safe + '">' +
    '<span class="hardware-card-title">Inverter candidate</span>' +
    '<span class="hardware-card-model">' + escapeHtml(String(model)) + "</span>" +
    '<span class="hardware-card-meta">' + meta + "</span>" +
    "</button>" +
    '<div class="hardware-card-actions">' +
    '<span class="hardware-card-status">Ready</span>' +
    renderConnectionPill(source) +
    action +
    '<button type="button" class="hardware-card-toggle" data-available-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") +
    '" aria-controls="config-available-body-' + safe +
    '" aria-label="' + (open ? "Collapse " : "Expand ") + 'inverter candidate">' +
    '<span aria-hidden="true">' + (open ? "▾" : "▸") + "</span>" +
    "</button>" +
    "</div>" +
    "</div>" +
    connectionCandidateNote(candidate) +
    '<div class="hardware-card-body" id="config-available-body-' + safe + '"' +
    (open ? "" : " hidden") + ">" +
    (open ? body : "") +
    "</div>" +
    "</article>"
  );
}

function addMqttInverterFromCandidate(proposalId) {
  const id = String(proposalId);
  if (zendureMqttPreviewProposals.has(id)) return;
  const proposal = availableMqttDeviceProposals().find(
    (candidate) => String(candidate.id || "") === id
  );
  if (!proposal) return;
  const entry = serializeMqttProposalSelection(proposal, { target: "device" });
  entry.selection_origin = "manual";
  entry.display_name = proposal.display_name || proposal.hardware_model || "";
  undismissSerial(proposal);
  zendureMqttPreviewProposals.set(id, entry);
  saveMqttPreviewProposals();
  renderMqttProposals(latestMqttProposals);
  renderConfigDraft();
  renderConfigAvailable();
}

// Setup discovered candidates reuse the Maintenance hardware-card layout so the
// two lists read the same; only the action stays "Add as ..." instead of Remove.
function renderConfigAvailableCard(device) {
  const sourceId = deviceKey(device);
  const role = String(device.role_suggestion || "unknown");
  const isGridMeter = role === "grid_meter";
  const hardwareRole = isGridMeter ? "grid_meter" : "inverter";
  const id = escapeHtml(sourceId);
  const safe = String(sourceId).replace(/[^a-z0-9]/gi, "-");
  const ready =
    device.usable_for_config !== undefined
      ? device.usable_for_config
      : device.config_ready;
  const endpoint =
    String(device.ip || "") + (device.port ? ":" + String(device.port) : "");
  const meta = [
    endpoint,
    device.serial_number ? "SN " + device.serial_number : "SN missing",
    device.api_family,
    device.device_type,
  ]
    .filter(Boolean)
    .map((part) => escapeHtml(String(part)))
    .join(" · ");
  const title = isGridMeter ? "Grid meter candidate" : "Inverter candidate";
  const model = device.display_name || device.device_type || "Device";
  const added = draftHasSource(sourceId);
  const addLabel = isGridMeter ? "Add as grid meter" : "Add inverter";
  const addButton = added
    ? '<button type="button" class="secondary-button compact is-added" disabled>Added to draft</button>'
    : '<button type="button" class="primary-button compact config-add"' +
      ' data-action="add-inverter"' +
      ' data-source-id="' + id + '"' +
      ' data-add-role="' + escapeHtml(role) + '">' +
      escapeHtml(addLabel) + "</button>";
  // Inverters resolve through the shared candidate-state contract; the grid
  // meter is a single-slot concept with its own add/added presentation.
  const candidate = isGridMeter
    ? { state: added ? "active" : "new", configuredName: "", currentSource: null }
    : inverterCandidateConnectionState(device, "local_api", sourceId);
  const button = isGridMeter
    ? addButton
    : renderConnectionCandidateAction(candidate, addButton);
  const open = openHardwareCards.has(sourceId);
  const status = ready ? "Ready" : "Needs info";
  const body =
    '<div class="device-facts">' +
    fact("Endpoint", escapeHtml(endpoint)) +
    fact(
      "Serial",
      device.serial_number
        ? '<span class="v">' + escapeHtml(device.serial_number) + "</span>"
        : '<span class="v missing">missing</span>',
      true
    ) +
    fact("API family", escapeHtml(String(device.api_family || ""))) +
    fact("Type", escapeHtml(String(device.device_type || ""))) +
    '<div class="device-sources">' + sourceBadges(device) + "</div>" +
    "</div>";

  return (
    '<article class="' + hardwareCardClass(hardwareRole) +
    '" data-source-id="' + id + '"' +
    ' data-candidate-state="' + escapeHtml(candidate.state) + '"' +
    (open ? ' data-open="true"' : "") + ">" +
    '<div class="hardware-card-head">' +
    '<button type="button" class="hardware-card-summary" data-available-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") + '"' +
    ' aria-controls="config-available-body-' + safe + '">' +
    '<span class="hardware-card-title">' + escapeHtml(title) + "</span>" +
    '<span class="hardware-card-model">' + escapeHtml(String(model)) + "</span>" +
    '<span class="hardware-card-meta">' + meta + "</span>" +
    "</button>" +
    '<div class="hardware-card-actions">' +
    '<span class="hardware-card-status">' + escapeHtml(status) + "</span>" +
    (isGridMeter ? "" : renderConnectionPill("local_api")) +
    button +
    '<button type="button" class="hardware-card-toggle" data-available-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") +
    '" aria-controls="config-available-body-' + safe +
    '" aria-label="' + (open ? "Collapse " : "Expand ") + escapeHtml(title) + '">' +
    '<span aria-hidden="true">' + (open ? "▾" : "▸") + "</span>" +
    "</button>" +
    "</div>" +
    "</div>" +
    (isGridMeter ? "" : connectionCandidateNote(candidate)) +
    '<div class="hardware-card-body" id="config-available-body-' + safe + '"' +
    (open ? "" : " hidden") + ">" +
    (open ? body : "") +
    "</div>" +
    "</article>"
  );
}

function renderConfigDraft() {
  if (!configEls.draftList) return;
  renderGridMeterSelection();
  renderConfigPreview();
  renderConfigValidation();
  notifySetupStatus();
  renderInverterList();
}

// The visible draft list only holds inverters; the grid meter is a separate
// Hardware concept shown in its own selection area, never as an inverter row.
function mqttSelectionControllable(entry) {
  const fragment = entry && entry.config_fragment;
  const capabilities = fragment && fragment.capabilities;
  return Boolean(capabilities && capabilities.write_output_limit);
}

function mqttInverterModel(entry) {
  if (entry && entry.display_name) return String(entry.display_name);
  const proposal = latestMqttProposals.find(
    (candidate) => String(candidate.id || "") === String(entry.id || "")
  );
  const fragment = entry && entry.config_fragment;
  return String(
    (proposal && (proposal.display_name || proposal.hardware_model)) ||
      (fragment && fragment.name) ||
      DEFAULT_INVERTER_DISPLAY
  );
}

// The configured inverter an identity probe belongs to, with its concrete
// connection. Local API draft items and selected MQTT entries are one pool: a
// physical inverter is configured over exactly one of them.
function configuredInverterConnection(probe) {
  for (const item of inverterItems()) {
    if (inverterIdentitiesMatch(item, probe)) {
      return { item, source: "local_api", ref: String(item.source_id || "") };
    }
  }
  for (const entry of selectedMqttDeviceEntries()) {
    if (inverterIdentitiesMatch(entry, probe)) {
      return {
        item: entry,
        source: mqttSourceOfConnection(entry.connection_source),
        ref: String(entry.id || ""),
      };
    }
  }
  return null;
}

// Whether a candidate is the very connection already configured. For MQTT that
// means one source and one broker scope; for Local API the exact discovered
// endpoint the draft item was built from.
function sameConcreteConnection(match, candidate, candidateSource, candidateRef) {
  if (match.source !== candidateSource) return false;
  if (candidateSource === "local_api") {
    return !match.ref || !candidateRef || match.ref === candidateRef;
  }
  return sameMqttConnectionScope(match.item, candidate);
}

// One classification for every discovered inverter connection, shared by the
// Setup candidate cards. Contradictory identity evidence stays fail-closed and
// never resolves to an actionable state.
function inverterCandidateConnectionState(candidate, candidateSource, candidateRef) {
  const state = {
    state: "new",
    configuredItem: null,
    configuredName: "",
    currentSource: null,
    candidateSource: candidateSource || null,
    candidateRef: String(candidateRef || ""),
    identityRef: "",
  };
  if (!candidate || !inverterHasIdentity(candidate)) return state;
  state.identityRef = physicalInverterIdentity(candidate);
  const configured = [
    ...inverterItems(),
    ...selectedMqttDeviceEntries(),
  ];
  if (configured.some((item) => inverterIdentityConflict(item, candidate))) {
    state.state = "identity_conflict";
    return state;
  }
  const match = configuredInverterConnection(candidate);
  if (!match) return state;
  state.configuredItem = match.item;
  state.configuredName = String(match.item.config_name || "").trim();
  state.currentSource = match.source;
  state.state = sameConcreteConnection(
    match,
    candidate,
    candidateSource,
    state.candidateRef
  )
    ? "active"
    : "alternative";
  return state;
}

// The single contextual action a discovered connection offers. "Use connection"
// carries only a trusted identity reference (visible serial or opaque token) —
// never a route id, product key or credential.
function renderConnectionCandidateAction(state, addButton) {
  if (state.state === "identity_conflict") {
    return (
      '<button type="button" class="secondary-button compact is-conflict"' +
      " disabled>Identity conflict</button>"
    );
  }
  if (state.state === "active") {
    return (
      '<button type="button" class="secondary-button compact is-added"' +
      " disabled>Active</button>"
    );
  }
  if (state.state === "alternative") {
    const token = connectionCandidateToken(state.candidateSource, state.candidateRef);
    return (
      '<button type="button" class="primary-button compact config-use-connection"' +
      ' data-action="use-connection"' +
      ' data-identity-ref="' + escapeHtml(state.identityRef) + '"' +
      ' data-connection-source="' + escapeHtml(String(state.candidateSource || "")) + '"' +
      ' data-candidate-token="' + escapeHtml(token) + '">' +
      "Use connection</button>"
    );
  }
  return addButton;
}

function connectionCandidateNote(state) {
  if (state.state === "alternative") {
    return (
      '<p class="candidate-connection-note">Already configured as ' +
      escapeHtml(state.configuredName || "another inverter") +
      " via " +
      escapeHtml(connectionLabelFor(state.currentSource)) +
      "</p>"
    );
  }
  if (state.state === "identity_conflict") {
    return (
      '<p class="candidate-connection-note is-conflict">Identity conflict with ' +
      "a configured inverter. Resolve it before using this connection.</p>"
    );
  }
  return "";
}

function renderConnectionPill(source) {
  return (
    '<span class="connection-pill" data-connection="' +
    escapeHtml(String(source || "")) + '">' +
    escapeHtml(connectionLabelFor(source)) +
    "</span>"
  );
}

// Local-API draft items and selected MQTT devices as one list, deduped by
// trusted identity aliases so one enriched physical inverter renders one card.
function selectedInverterCards() {
  const cards = [];
  const seen = [];
  for (const item of inverterItems()) {
    cards.push({ kind: "http", item });
    seen.push(item);
  }
  for (const entry of selectedMqttDeviceEntries()) {
    if (seen.some((device) => inverterIdentitiesMatch(device, entry))) continue;
    cards.push({ kind: "mqtt", entry });
    seen.push(entry);
  }
  return cards;
}

function renderInverterList() {
  if (!configEls.draftList) return;
  const cards = selectedInverterCards();
  if (!cards.length) {
    configEls.draftList.hidden = true;
    configEls.draftList.innerHTML = "";
    configEls.draftEmpty.hidden = false;
    return;
  }
  configEls.draftEmpty.hidden = true;
  configEls.draftList.hidden = false;
  configEls.draftList.innerHTML = cards
    .map((card, index) =>
      card.kind === "http"
        ? renderInverterDraftRow(card.item, index)
        : renderMqttInverterCard(card.entry, index)
    )
    .join("");
}

function renderMqttInverterCard(entry, index) {
  const proposalId = String(entry.id || "");
  const source = mqttSourceOfConnection(entry.connection_source);
  const serial = entry.serial_number || "";
  const controllable = mqttSelectionControllable(entry);
  const meta = mqttInverterSummaryText(entry);
  const body =
    renderHardwareEnabledRow(
      "data-mqtt-enable",
      proposalId,
      entry.enabled !== false,
      "Include this inverter in the generated EMS config."
    ) +
    '<label class="feature-field-row">' +
    '<span class="feature-field-label">Device name</span>' +
    '<span class="feature-field-control"><input type="text" class="feature-input"' +
    ' data-mqtt-config-name value="' + escapeHtml(entry.config_name || "") + '"></span>' +
    '<span class="feature-field-desc">Short unique EMS name used in config, logs, dashboard and Flowchart. ' +
    'Model, address and serial remain in the device details.</span>' +
    "</label>" +
    '<div class="device-facts">' +
    fact("Connection", escapeHtml(connectionLabelFor(source))) +
    fact(
      "Serial",
      serial
        ? '<span class="v">' + escapeHtml(serial) + "</span>"
        : '<span class="v missing">missing</span>',
      true
    ) +
    fact("Output control", controllable ? "Enabled" : "Telemetry only") +
    "</div>" +
    '<div class="proposal-safety">' +
    renderMqttProposalPill(
      controllable
        ? "Output control enabled"
        : "Telemetry only — output write disabled"
    ) +
    "</div>" +
    '<div class="inverter-row-actions">' +
    '<button type="button" class="secondary-button compact config-mqtt-reset">Reset name</button>' +
    "</div>";
  return renderHardwareCard({
    role: "inverter",
    sourceId: proposalId,
    title: "Inverter " + (index + 1),
    model: mqttInverterModel(entry),
    meta,
    enabled: entry.enabled !== false,
    open: openHardwareCards.has(proposalId),
    toggleAttr: "data-inverter-toggle",
    removeClass: "config-mqtt-remove",
    badges: [renderConnectionPill(source)],
    body,
  });
}

function mqttInverterSummaryText(entry) {
  const source = mqttSourceOfConnection(entry.connection_source);
  const serial = entry.serial_number ? "SN " + entry.serial_number : "Serial missing";
  return [entry.config_name, serial, connectionLabelFor(source)]
    .filter(Boolean)
    .join(" · ");
}

// --- inverter draft rows (catalog-driven, compact hardware style) ---------
// Inverters mirror the grid meter presentation: a collapsed summary row that
// expands into compact label | control | description field rows. Field labels,
// descriptions, types, units and ordering all come from the devices section of
// the setup catalog; only name/ip/sn map to dedicated draft item properties,
// the rest are stored as per-device overrides in item.config_values.

const DEVICE_MAPPED_FIELD_KEYS = {
  name: "config_name",
  ip: "ip",
  sn: "serial_number",
};

function deviceCatalogSection() {
  if (!setupCatalog || !Array.isArray(setupCatalog.sections)) return null;
  return setupCatalog.sections.find((section) => section.id === "devices") || null;
}

function deviceCatalogFields() {
  const section = deviceCatalogSection();
  return section && Array.isArray(section.fields) ? section.fields : [];
}

function deviceCatalogField(path) {
  return deviceCatalogFields().find((field) => field.path === path) || null;
}

function deviceFieldKey(fieldPath) {
  return String(fieldPath).replace(/^devices\[\]\./, "");
}

// Unset device values fall back to the release template prototype so the row
// shows the same defaults the backend preview would generate.
function deviceTemplatePrototype() {
  if (!activeConfigTemplate || !Array.isArray(activeConfigTemplate.devices)) {
    return {};
  }
  return activeConfigTemplate.devices[0] || {};
}

function deviceFieldValue(item, field) {
  const key = deviceFieldKey(field.path);
  const mapped = DEVICE_MAPPED_FIELD_KEYS[key];
  if (mapped) return item[mapped];
  if (
    item.config_values &&
    Object.prototype.hasOwnProperty.call(item.config_values, key)
  ) {
    return item.config_values[key];
  }
  const proto = deviceTemplatePrototype();
  if (Object.prototype.hasOwnProperty.call(proto, key)) return proto[key];
  return field.default != null ? field.default : "";
}

function updateDraftDeviceField(item, field, rawValue) {
  const key = deviceFieldKey(field.path);
  const mapped = DEVICE_MAPPED_FIELD_KEYS[key];
  if (mapped) {
    item[mapped] = rawValue;
    return;
  }
  if (!item.config_values || typeof item.config_values !== "object") {
    item.config_values = {};
  }
  item.config_values[key] = rawValue;
}

function inverterSummaryText(item) {
  const endpoint =
    String(item.ip || "") + (item.port ? ":" + String(item.port) : "");
  const serial = item.serial_number ? "SN " + item.serial_number : "Serial missing";
  const parts = [item.config_name, endpoint, serial];
  const outputField = deviceCatalogField("devices[].max_power");
  if (outputField) {
    const output = deviceFieldValue(item, outputField);
    if (output !== "" && output != null) parts.push(String(output) + " W");
  }
  return parts.filter(Boolean).join(" · ");
}

function inverterModelText(item) {
  return String(item.display_name || item.model || DEFAULT_INVERTER_DISPLAY);
}

function renderHardwareCard(card) {
  const id = escapeHtml(card.sourceId);
  const safe = String(card.sourceId).replace(/[^a-z0-9]/gi, "-");
  const status = card.enabled ? "Enabled" : "Disabled";
  const badges = (card.badges || []).join("");
  return (
    '<article class="' + hardwareCardClass(card.role) +
    '" data-source-id="' + id + '"' +
    (card.open ? ' data-open="true"' : "") + ">" +
    '<div class="hardware-card-head">' +
    '<button type="button" class="hardware-card-summary" ' +
    card.toggleAttr + '="' + id + '"' +
    ' aria-expanded="' + (card.open ? "true" : "false") + '"' +
    ' aria-controls="hardware-body-' + safe + '">' +
    '<span class="hardware-card-title">' + escapeHtml(card.title) + "</span>" +
    '<span class="hardware-card-model">' + escapeHtml(card.model) + "</span>" +
    '<span class="hardware-card-meta">' + escapeHtml(card.meta) + "</span>" +
    "</button>" +
    '<div class="hardware-card-actions">' +
    '<span class="hardware-card-status">' + escapeHtml(status) + "</span>" +
    badges +
    '<button type="button" class="hardware-card-remove secondary-button compact ' +
    card.removeClass + '">Remove</button>' +
    '<button type="button" class="hardware-card-toggle" ' +
    card.toggleAttr + '="' + id + '"' +
    ' aria-expanded="' + (card.open ? "true" : "false") +
    '" aria-controls="hardware-body-' + safe +
    '" aria-label="' + (card.open ? "Collapse " : "Expand ") +
    escapeHtml(card.title) + '">' +
    '<span aria-hidden="true">' + (card.open ? "▾" : "▸") + "</span>" +
    "</button>" +
    "</div>" +
    "</div>" +
    '<div class="hardware-card-body" id="hardware-body-' + safe + '"' +
    (card.open ? "" : " hidden") + ">" +
    (card.open ? card.body : "") +
    "</div>" +
    "</article>"
  );
}

function renderInverterDraftRow(item, index) {
  const safe = String(item.source_id).replace(/[^a-z0-9]/gi, "-");
  const open = openHardwareCards.has(item.source_id);
  const title = "Inverter " + (index + 1);
  return renderHardwareCard({
    role: "inverter",
    sourceId: item.source_id,
    title,
    model: inverterModelText(item),
    meta: inverterSummaryText(item),
    enabled: item.enabled,
    open,
    toggleAttr: "data-inverter-toggle",
    removeClass: "config-draft-remove",
    badges: [renderConnectionPill("local_api")],
    body: renderInverterBody(item, safe),
  });
}

function renderHardwareEnabledRow(dataAttr, id, enabled, description) {
  const inputId = "enabled-" + String(id).replace(/[^a-z0-9]/gi, "-");
  return (
    '<div class="feature-fields">' +
    '<label class="feature-field-row" for="' + inputId + '">' +
    '<span class="feature-field-label">Enabled</span>' +
    '<span class="feature-field-control">' +
    '<input type="checkbox" id="' + inputId + '" class="feature-input"' +
    " " + dataAttr + '="' + escapeHtml(String(id)) + '"' +
    (enabled ? " checked" : "") + ">" +
    "</span>" +
    '<span class="feature-field-desc">' + escapeHtml(description) + "</span>" +
    "</label>" +
    "</div>"
  );
}

function renderInverterBody(item, safe) {
  const transportRow =
    '<div class="device-facts">' +
    fact("Connection", escapeHtml(connectionLabelFor("local_api"))) +
    "</div>";
  return (
    renderHardwareEnabledRow(
      "data-inverter-enable",
      item.source_id,
      item.enabled,
      "Include this inverter in the generated EMS config."
    ) +
    renderInverterFields(item, safe) +
    transportRow +
    renderInverterActions()
  );
}

function renderInverterFields(item, safe) {
  const byLevel = { normal: [], advanced: [], expert: [] };
  for (const field of deviceCatalogFields()) {
    if (FEATURE_LEVELS_HIDDEN.has(field.level)) continue;
    const level =
      field.level === "advanced" || field.level === "expert" ? field.level : "normal";
    byLevel[level].push(field);
  }
  if (!byLevel.normal.length && !byLevel.advanced.length && !byLevel.expert.length) {
    return '<p class="future-note">Device settings load with the release template.</p>';
  }
  let html =
    '<div class="feature-fields">' +
    byLevel.normal.map((field) => renderDeviceField(item, field, safe)).join("") +
    "</div>";
  if (byLevel.advanced.length) {
    html +=
      '<details class="feature-advanced"><summary>Advanced settings</summary>' +
      '<div class="feature-fields">' +
      byLevel.advanced.map((field) => renderDeviceField(item, field, safe)).join("") +
      "</div></details>";
  }
  if (byLevel.expert.length) {
    html +=
      '<details class="feature-expert">' +
      "<summary>Developer / expert settings</summary>" +
      '<p class="feature-expert-warning">Changing expert tuning values can affect ' +
      "EMS control stability. Only change these values if you know why they are " +
      "needed.</p>" +
      '<div class="feature-fields">' +
      byLevel.expert.map((field) => renderDeviceField(item, field, safe)).join("") +
      "</div></details>";
  }
  return html;
}

function renderDeviceField(item, field, safe) {
  const key = deviceFieldKey(field.path);
  const inputId = "device-" + safe + "-" + key.replace(/[^a-z0-9]/gi, "-");
  const unit = field.unit
    ? '<span class="feature-unit">' + escapeHtml(field.unit) + "</span>"
    : "";
  const desc = field.description
    ? '<span class="feature-field-desc">' + escapeHtml(field.description) + "</span>"
    : '<span class="feature-field-desc"></span>';
  return (
    '<label class="feature-field-row" for="' + inputId + '">' +
    '<span class="feature-field-label">' + escapeHtml(field.label) + "</span>" +
    '<span class="feature-field-control">' +
    renderDeviceControl(item, field, inputId) +
    unit +
    "</span>" +
    desc +
    "</label>"
  );
}

function renderDeviceControl(item, field, inputId) {
  const value = deviceFieldValue(item, field);
  const common =
    ' id="' + inputId + '" data-device-field="' + escapeHtml(field.path) + '"';
  if (field.type === "boolean") {
    return (
      '<input type="checkbox"' + common + ' class="feature-input"' +
      (value ? " checked" : "") + ">"
    );
  }
  if (Array.isArray(field.options)) {
    const current = String(value == null ? "" : value);
    const options = field.options
      .map((option) => {
        const opt = String(option);
        return (
          '<option value="' + escapeHtml(opt) + '"' +
          (opt === current ? " selected" : "") + ">" +
          escapeHtml(opt) + "</option>"
        );
      })
      .join("");
    return "<select" + common + ' class="feature-input">' + options + "</select>";
  }
  const inputType =
    field.type === "number" || field.type === "integer" ? "number" : "text";
  return (
    '<input type="' + inputType + '"' + common + ' class="feature-input"' +
    ' value="' + escapeHtml(formatFeatureValue(value)) + '">'
  );
}

function renderInverterActions() {
  return (
    '<div class="inverter-row-actions">' +
    '<button type="button" class="secondary-button compact config-draft-move" data-move="up" aria-label="Move up" title="Move up">↑</button>' +
    '<button type="button" class="secondary-button compact config-draft-move" data-move="down" aria-label="Move down" title="Move down">↓</button>' +
    '<button type="button" class="secondary-button compact config-draft-reset">Reset name</button>' +
    "</div>"
  );
}

// Update the collapsed summary in place so editing a field does not force a
// full list re-render (which would drop input focus mid-typing).
function updateInverterSummary(row, item) {
  if (!row) return;
  const desc = row.querySelector(".hardware-card-meta");
  if (desc) desc.textContent = inverterSummaryText(item);
  const status = row.querySelector(".hardware-card-status");
  if (status) status.textContent = item.enabled ? "Enabled" : "Disabled";
}

function configValidationHints() {
  const hints = [];
  const names = [
    ...freshInverterConfigNames(),
    ...configDraftItems
      .filter((item) => item.role === "grid_meter")
      .map((item) => item.config_name),
  ];
  const seen = new Set();
  const dupes = new Set();
  for (const name of names) {
    if (!name) continue;
    if (seen.has(name)) dupes.add(name);
    seen.add(name);
  }
  if (names.some((name) => !String(name || "").trim())) {
    hints.push({ tone: "error", text: "Every config name must be non-empty." });
  }
  for (const name of dupes) {
    hints.push({
      tone: "error",
      text: 'Duplicate config name: "' + name + '".',
    });
  }
  if (configDraftItems.length && !inverterItems().length) {
    hints.push({ tone: "warn", text: "At least one inverter is recommended." });
  }
  const meters = configDraftItems.filter((item) => item.role === "grid_meter");
  if (!meters.length && supportedGridMeters().length >= 2) {
    hints.push({
      code: "grid_meter_selection_needed",
      tone: "warn",
      text: "Grid meter selection needed — choose which grid meter EMS should use.",
    });
  } else if (configDraftItems.length && !meters.length) {
    hints.push({
      code: "missing_grid_meter",
      tone: "warn",
      text: "No grid meter selected yet.",
    });
  }
  if (meters.length > 1) {
    hints.push({
      code: "duplicate_grid_meter",
      tone: "error",
      text: "Only one grid meter is supported in the draft.",
    });
  }
  if (selectedGridMeterStale()) {
    hints.push({
      code: "selected_grid_meter_stale",
      tone: "warn",
      text: "Selected grid meter has not been seen recently.",
    });
  }
  return hints;
}

// Compact grid-meter picker: shown only when the user still has to choose (2+
// supported meters, none selected) or as a one-line "none found yet" hint. When
// exactly one meter exists it is auto-selected, so nothing is shown here.
function renderGridMeterSelection() {
  const el = configEls.gridMeterSelection;
  if (!el) return;
  const meters = supportedGridMeters();
  const selected = gridMeterItem();
  if (selected) {
    // The grid meter no longer renders as a draft card, so its compact summary
    // here keeps a change/remove path without regressing the Grid meter area.
    el.hidden = false;
    el.innerHTML = renderSelectedGridMeter(selected);
    return;
  }
  if (meters.length === 1) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  if (!meters.length) {
    el.innerHTML =
      '<p class="config-grid-hint">No supported grid meter found yet</p>';
    return;
  }
  const options = meters
    .map((device) => {
      const endpoint =
        escapeHtml(device.ip) +
        (device.port ? ":" + escapeHtml(device.port) : "");
      const name = escapeHtml(
        device.display_name || device.model || DEFAULT_GRID_METER_DISPLAY
      );
      return (
        '<div class="config-grid-option">' +
        '<button type="button" class="primary-button compact config-grid-use"' +
        ' data-source-id="' +
        escapeHtml(deviceKey(device)) +
        '">Use this</button>' +
        '<span class="config-grid-option-name">' +
        name +
        "</span>" +
        '<span class="config-grid-option-endpoint">' +
        endpoint +
        "</span>" +
        "</div>"
      );
    })
    .join("");
  el.innerHTML =
    '<div class="config-grid-selection-card">' +
    '<div class="config-grid-selection-head">' +
    '<span class="config-grid-selection-title">Grid meter selection needed</span>' +
    '<span class="pill muted">' +
    meters.length +
    " found</span>" +
    "</div>" +
    '<p class="config-grid-selection-copy">' +
    meters.length +
    " supported grid meters were found. Choose the one EMS should use for control." +
    "</p>" +
    '<div class="config-grid-selection-list">' +
    options +
    "</div>" +
    "</div>";
}

function gridMeterModelText(meter) {
  const type = gridMeterType(meter, "shelly");
  const variant = gridMeterVariants()[type];
  return (variant && variant.label) || meter.display_name || DEFAULT_GRID_METER_DISPLAY;
}

function renderSelectedGridMeter(meter) {
  const enabled = meter.enabled !== false;
  const endpoint =
    String(meter.ip || "") + (meter.port ? ":" + String(meter.port) : "");
  const badges = [];
  if (meter.auto_selected) {
    badges.push('<span class="config-auto-badge">Auto-selected</span>');
  }
  if (selectedGridMeterStale()) {
    badges.push('<span class="stale-badge">stale</span>');
  }
  return renderHardwareCard({
    role: "grid_meter",
    sourceId: meter.source_id,
    title: "Grid meter",
    model: gridMeterModelText(meter),
    meta: endpoint,
    enabled,
    open: openHardwareCards.has(meter.source_id),
    toggleAttr: "data-grid-toggle",
    removeClass: "config-grid-remove",
    badges,
    body: renderGridMeterBody(meter),
  });
}

function gridMeterCatalogSection() {
  if (!setupCatalog || !Array.isArray(setupCatalog.sections)) return null;
  return setupCatalog.sections.find((section) => section.id === "grid_meter") || null;
}

function renderGridMeterBody(meter) {
  return (
    renderHardwareEnabledRow(
      "data-grid-enable",
      meter.source_id,
      meter.enabled !== false,
      "Include this grid meter in the generated EMS config."
    ) +
    renderGridMeterFields(meter)
  );
}

function renderGridMeterFields(meter) {
  const type = gridMeterType(meter, "shelly");
  const isMqtt = type === "mqtt" || type === "zendure_smartmeter_d0";
  const isD0 = type === "zendure_smartmeter_d0";
  const section = gridMeterCatalogSection();
  const fields = section ? visibleFeatureFields(section, type) : [];
  const standard = fields.filter(
    (field) =>
      field.path !== "grid_meter.type" && field.path !== "grid_meter.ip"
  );
  const byLevel = { normal: [], advanced: [], expert: [] };
  for (const field of standard) {
    // For D0 the topic is generated from the serial; keep it in Advanced as a
    // read-back/override so the basic flow never asks for a raw MQTT topic.
    const forcedAdvanced = isD0 && field.path === "grid_meter.mqtt.topic";
    const level = forcedAdvanced
      ? "advanced"
      : field.level === "advanced" || field.level === "expert"
        ? field.level
        : "normal";
    byLevel[level].push(field);
  }
  let html =
    '<div class="feature-fields">' +
    renderGridMeterTypeField(meter) +
    (isD0
      ? renderGridMeterEndpointField(
          "serial_number",
          "D0 serial number",
          meter.serial_number,
          "Used to generate Zendure/sensor/<serial>/totalPower automatically.",
          { required: true }
        ) +
        (effectiveD0TopicMode(meter, featureValues["grid_meter.mqtt.topic"]) === "manual"
          ? '<button type="button" class="secondary-button compact" ' +
            'data-d0-use-generated>Use generated topic</button>'
          : "")
      : "") +
    (isMqtt
      ? ""
      : renderGridMeterEndpointField(
          "ip",
          "Host / IP",
          meter.ip,
          "Address of the meter."
        ) +
        renderGridMeterEndpointField("port", "Port", meter.port, "HTTP port.")) +
    byLevel.normal.map(renderFeatureField).join("") +
    "</div>";
  if (byLevel.advanced.length) {
    html +=
      '<details class="feature-advanced"><summary>Advanced settings</summary>' +
      '<div class="feature-fields">' +
      byLevel.advanced.map(renderFeatureField).join("") +
      "</div></details>";
  }
  if (byLevel.expert.length) {
    html +=
      '<details class="feature-expert"><summary>Developer / expert settings</summary>' +
      '<div class="feature-fields">' +
      byLevel.expert.map(renderFeatureField).join("") +
      "</div></details>";
  }
  return html;
}

function renderGridMeterTypeField(meter) {
  const section = gridMeterCatalogSection();
  const field = section && section.fields.find(
    (item) => item.path === "grid_meter.type"
  );
  if (!field) return "";
  return (
    '<label class="feature-field-row" for="grid-meter-type">' +
    '<span class="feature-field-label">Meter type</span>' +
    '<span class="feature-field-control">' +
    renderGridTypeSelect(field, "grid-meter-type", gridMeterType(meter, "shelly")) +
    "</span>" +
    '<span class="feature-field-desc">Hardware/API family used for config generation.</span>' +
    "</label>"
  );
}

function renderGridMeterEndpointField(key, label, value, description, opts) {
  const inputId = "grid-meter-" + key;
  const required = opts && opts.required ? " required" : "";
  return (
    '<label class="feature-field-row" for="' + inputId + '">' +
    '<span class="feature-field-label">' + escapeHtml(label) + "</span>" +
    '<span class="feature-field-control">' +
    '<input type="' + (key === "port" ? "number" : "text") + '"' +
    ' id="' + inputId + '" class="feature-input" data-grid-field="' +
    escapeHtml(key) + '"' + required + ' value="' + escapeHtml(String(value || "")) + '">' +
    "</span>" +
    '<span class="feature-field-desc">' + escapeHtml(description) + "</span>" +
    "</label>"
  );
}

// --- catalog-driven feature settings -------------------------------------
// The setup UI renders a compact accordion from catalog metadata instead of a
// wall of forms. Each section is one collapsible row; normal fields show first,
// advanced and expert fields sit in nested collapsed areas that stay closed by
// default. The catalog is the reference for possible options; the committed
// template is only the default example. Every dynamic value passes through
// escapeHtml before it reaches the DOM.

const FEATURE_LEVELS_HIDDEN = new Set(["deprecated", "internal"]);

async function loadSetupCatalog() {
  try {
    const res = await fetch("/api/setup/config/catalog");
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data && data.error ? data.error : "catalog unavailable");
    }
    setupCatalog = data;
    seedDefaultOpenFeatureSections(setupCatalog.sections, openFeatures);
  } catch (err) {
    setupCatalog = null;
  }
  renderFeatureSettings();
  populateManualTypes(true);
  initMqttBrokerSection();
  renderGridMeterSelection();
  renderInverterList();
}

function featureSections() {
  if (!setupCatalog || !Array.isArray(setupCatalog.sections)) return [];
  return setupCatalog.sections.filter(
    (section) => section.id !== "devices" && section.id !== "grid_meter"
  );
}

function fieldCurrentValue(field) {
  if (Object.prototype.hasOwnProperty.call(featureValues, field.path)) {
    return featureValues[field.path];
  }
  if (field.secret) return "";
  return field.default;
}

function featureEnabledPath(section) {
  const path = section.enabled_path;
  if (typeof path === "string" && path) return path;
  if (Array.isArray(path) && path.length) return path.join(".");
  return null;
}

function isFeatureEnabled(section) {
  const path = featureEnabledPath(section);
  if (!path) return null;
  const field = (section.fields || []).find((item) => item.path === path);
  const value = Object.prototype.hasOwnProperty.call(featureValues, path)
    ? featureValues[path]
    : field
    ? field.default
    : false;
  return Boolean(value);
}

function gridMeterVariants() {
  return (setupCatalog && setupCatalog.grid_meter_variants) || {};
}

function selectedGridMeterType() {
  const value = fieldCurrentValue({ path: "grid_meter.type", default: "shelly" });
  return String(value == null ? "shelly" : value);
}

// Only the fields for the selected grid meter variant are shown, so switching
// the meter type updates which connection fields appear.
function gridVariantFields(section, selectedType) {
  const variant = gridMeterVariants()[selectedType || selectedGridMeterType()];
  const allowed = new Set(variant ? variant.fields : []);
  return section.fields.filter(
    (field) => field.path === "grid_meter.type" || allowed.has(field.path)
  );
}

function visibleFeatureFields(section, selectedType) {
  const fields =
    section.id === "grid_meter"
      ? gridVariantFields(section, selectedType)
      : section.fields;
  const enabledPath = featureEnabledPath(section);
  return fields.filter((field) => {
    if (field.path === enabledPath) return false; // shown as the row toggle
    if (FEATURE_LEVELS_HIDDEN.has(field.level)) return false;
    return true;
  });
}

function featureStatusText(section) {
  const enabled = isFeatureEnabled(section);
  if (enabled !== null) return enabled ? "Enabled" : "Disabled";
  if (section.id === "grid_meter") {
    const variant = gridMeterVariants()[selectedGridMeterType()];
    return variant ? variant.label : selectedGridMeterType();
  }
  return "Configured";
}

// Top-level setup groups render in order: Hardware, Features, Advanced/System.
// Grid meter and devices live under Hardware; devices keep their dedicated draft
// UI, so only the grid meter section renders as a Hardware feature row here.
const SETUP_GROUP_ORDER = ["hardware", "features", "advanced"];

function setupGroupOrder() {
  if (setupCatalog && Array.isArray(setupCatalog.groups) && setupCatalog.groups.length) {
    return setupCatalog.groups.map((group) => group.id);
  }
  return SETUP_GROUP_ORDER;
}

function sectionsForGroup(groupId) {
  return featureSections().filter(
    (section) => (section.setup_group || "features") === groupId
  );
}

function renderFeatureSettings() {
  const lists = configEls.featureLists || {};
  if (!Object.values(lists).some(Boolean)) return;
  const hasCatalog = featureSections().length > 0;
  for (const groupId of setupGroupOrder()) {
    const list = lists[groupId];
    if (!list) continue;
    const groupSections = sectionsForGroup(groupId);
    list.hidden = groupSections.length === 0;
    list.innerHTML = groupSections.map(renderFeatureRow).join("");
  }
  if (configEls.featureEmpty) configEls.featureEmpty.hidden = hasCatalog;
}

function renderFeatureRow(section) {
  const id = escapeHtml(section.id);
  const open = openFeatures.has(section.id);
  const enabled = isFeatureEnabled(section);
  const status = escapeHtml(featureStatusText(section));
  const toggle =
    enabled === null
      ? ""
      : '<input type="checkbox" class="feature-enable"' +
        ' data-feature-enable="' + id + '"' +
        (enabled ? " checked" : "") +
        ' aria-label="Enable ' + escapeHtml(section.title) + '">';
  return (
    '<div class="feature-row" role="listitem" data-feature-id="' + id + '"' +
    (open ? ' data-open="true"' : "") + ">" +
    '<div class="feature-row-head">' +
    toggle +
    '<button type="button" class="feature-row-summary"' +
    ' data-feature-toggle="' + id + '"' +
    ' aria-expanded="' + (open ? "true" : "false") + '"' +
    ' aria-controls="feature-body-' + id + '">' +
    '<span class="feature-title">' + escapeHtml(section.title) + "</span>" +
    '<span class="feature-desc">' +
    escapeHtml(section.description || section.summary || "") + "</span>" +
    '<span class="feature-status">' + status + "</span>" +
    '<span class="feature-caret" aria-hidden="true">' + (open ? "▾" : "▸") + "</span>" +
    "</button>" +
    "</div>" +
    '<div class="feature-body" id="feature-body-' + id + '"' +
    (open ? "" : " hidden") + ">" +
    (open ? renderFeatureBody(section) : "") +
    "</div>" +
    "</div>"
  );
}

function renderFeatureBody(section) {
  const byLevel = { normal: [], advanced: [], expert: [] };
  for (const field of visibleFeatureFields(section)) {
    const level =
      field.level === "advanced" || field.level === "expert" ? field.level : "normal";
    byLevel[level].push(field);
  }
  let html =
    '<div class="feature-fields">' +
    byLevel.normal.map(renderFeatureField).join("") +
    "</div>";
  if (byLevel.advanced.length) {
    html +=
      '<details class="feature-advanced"><summary>Advanced settings</summary>' +
      '<div class="feature-fields">' +
      byLevel.advanced.map(renderFeatureField).join("") +
      "</div></details>";
  }
  if (byLevel.expert.length) {
    html +=
      '<details class="feature-expert">' +
      "<summary>Developer / expert settings</summary>" +
      '<p class="feature-expert-warning">Changing expert tuning values can affect ' +
      "EMS control stability. Only change these values if you know why they are " +
      "needed.</p>" +
      '<div class="feature-fields">' +
      byLevel.expert.map(renderFeatureField).join("") +
      "</div></details>";
  }
  return html;
}

// One compact settings row per field: label | control (+ unit) | description.
// The grid columns are set in CSS so long labels, values and descriptions line
// up and wrap cleanly instead of stacking into card-like tiles.
function renderFeatureField(field) {
  const inputId = "feature-field-" + field.path.replace(/[^a-z0-9]/gi, "-");
  const unit = field.unit
    ? '<span class="feature-unit">' + escapeHtml(field.unit) + "</span>"
    : "";
  const desc = field.description
    ? '<span class="feature-field-desc">' + escapeHtml(field.description) + "</span>"
    : '<span class="feature-field-desc"></span>';
  return (
    '<label class="feature-field-row" for="' + inputId + '">' +
    '<span class="feature-field-label">' + escapeHtml(field.label) + "</span>" +
    '<span class="feature-field-control">' +
    renderFeatureControl(field, inputId) +
    unit +
    "</span>" +
    desc +
    "</label>"
  );
}

function renderFeatureControl(field, inputId) {
  const path = escapeHtml(field.path);
  const value = fieldCurrentValue(field);
  const common = ' id="' + inputId + '" data-feature-path="' + path + '"';
  if (field.path === "grid_meter.type") {
    return renderGridTypeSelect(field, inputId);
  }
  if (field.type === "boolean") {
    return (
      '<input type="checkbox"' + common + ' class="feature-input"' +
      (value ? " checked" : "") + ">"
    );
  }
  if (field.type === "select" && Array.isArray(field.options)) {
    const current = String(value == null ? "" : value);
    const options = field.options
      .map((option) => {
        const opt = String(option);
        return (
          '<option value="' + escapeHtml(opt) + '"' +
          (opt === current ? " selected" : "") + ">" +
          escapeHtml(opt) + "</option>"
        );
      })
      .join("");
    return "<select" + common + ' class="feature-input">' + options + "</select>";
  }
  const inputType = field.secret
    ? "password"
    : field.type === "number" || field.type === "integer"
    ? "number"
    : "text";
  return (
    '<input type="' + inputType + '"' + common + ' class="feature-input"' +
    ' value="' + escapeHtml(formatFeatureValue(value)) + '">'
  );
}

function renderGridTypeSelect(field, inputId, selectedValue) {
  const variants = gridMeterVariants();
  const current = selectedValue || selectedGridMeterType();
  const resolved = GRID_METER_TYPE_CHOICES.has(current);
  // An unresolved (unidentified) meter shows a disabled placeholder so the
  // select never silently presents Shelly as the chosen type.
  const placeholder = resolved
    ? ""
    : '<option value="" selected disabled>Select grid-meter type…</option>';
  const options = Object.keys(variants)
    .map((key) => {
      return (
        '<option value="' + escapeHtml(key) + '"' +
        (key === current ? " selected" : "") + ">" +
        escapeHtml(variants[key].label) + "</option>"
      );
    })
    .join("");
  return (
    '<select id="' + inputId + '" data-feature-path="grid_meter.type"' +
    ' data-feature-variant-select class="feature-input">' + placeholder + options + "</select>"
  );
}

function formatFeatureValue(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function updateFeatureValue(path, value) {
  featureValues[path] = value;
  saveFeatureValues();
  renderConfigPreview();
}

function handleFeatureListClick(event) {
  const toggle = event.target.closest("[data-feature-toggle]");
  if (!toggle) return;
  const id = toggle.getAttribute("data-feature-toggle");
  if (openFeatures.has(id)) openFeatures.delete(id);
  else openFeatures.add(id);
  renderFeatureSettings();
}

function handleFeatureListChange(event) {
  const target = event.target;
  if (target.matches("[data-feature-enable]")) {
    const id = target.getAttribute("data-feature-enable");
    const section = featureSections().find((item) => item.id === id);
    const path = section ? featureEnabledPath(section) : null;
    if (!path) return;
    featureValues[path] = target.checked;
    saveFeatureValues();
    renderFeatureSettings();
    renderConfigPreview();
    return;
  }
  if (target.matches("[data-feature-variant-select]")) {
    featureValues["grid_meter.type"] = target.value;
    saveFeatureValues();
    renderFeatureSettings();
    renderConfigPreview();
    return;
  }
  if (target.matches("[data-feature-path]")) {
    const path = target.getAttribute("data-feature-path");
    updateFeatureValue(path, target.type === "checkbox" ? target.checked : target.value);
  }
}

function handleFeatureListInput(event) {
  const target = event.target;
  if (
    !target.matches("[data-feature-path]") ||
    target.matches("[data-feature-variant-select]") ||
    target.type === "checkbox"
  ) {
    return;
  }
  featureValues[target.getAttribute("data-feature-path")] = target.value;
  saveFeatureValues();
  renderConfigPreview();
}

function initFeatureSettings() {
  const lists = Object.values(configEls.featureLists || {}).filter(Boolean);
  if (!lists.length) return;
  for (const list of lists) {
    list.addEventListener("click", handleFeatureListClick);
    list.addEventListener("change", handleFeatureListChange);
    list.addEventListener("input", handleFeatureListInput);
  }
  renderFeatureSettings();
}

function renderConfigValidation() {
  if (!configEls.validation) return;
  notifySetupStatus();
  let hints = latestConfigPreview
    ? ["errors", "warnings", "info"].flatMap((level) =>
        (latestConfigPreview.validation[level] || []).map((issue) => ({
          tone: level === "errors" ? "error" : level === "warnings" ? "warn" : "info",
          text: issue.message,
        }))
      )
    : configValidationHints();
  if (latestConfigPreview && latestConfigPreview.summary) {
    const summary = latestConfigPreview.summary;
    const inverterCount = Number(summary.inverters || 0);
    const meterCount = Number(summary.grid_meters || 0);
    if (inverterCount && meterCount === 1) {
      hints = hints.concat({
        tone: "info",
        text:
          inverterCount +
          (inverterCount === 1 ? " inverter and " : " inverters and ") +
          "1 grid meter selected.",
      });
    }
  }
  if (!hints.length) {
    hints = [{ tone: "info", text: "Waiting for config preview validation." }];
  }
  const hasError = hints.some((hint) => hint.tone === "error");
  const hasWarning = hints.some((hint) => hint.tone === "warn");
  const tone = hasError ? "error" : hasWarning ? "warn" : latestConfigPreview ? "ready" : "pending";
  if (configEls.validationCard) configEls.validationCard.dataset.tone = tone;
  if (configEls.previewReady) {
    configEls.previewReady.textContent =
      tone === "ready" ? "Ready" : tone === "pending" ? "Checking…" : "Needs attention";
  }
  configEls.validation.innerHTML = hints
    .map(
      (hint) => {
        const icon = hint.tone === "error" ? "×" : hint.tone === "warn" ? "!" : "✓";
        return (
        '<div class="config-validation-item config-validation-item-' +
        hint.tone +
        '"><span class="config-validation-icon" aria-hidden="true">' +
        icon +
        "</span><span>" +
        escapeHtml(hint.text) +
        "</span></div>"
        );
      }
    )
    .join("");
}

function cloneConfigValue(value) {
  return JSON.parse(JSON.stringify(value));
}

// Static mirror of the backend catalog's grid-meter types (ems.config_catalog
// GRID_METER_VARIANTS / admin.config_preview._GRID_TYPE_CHOICES). The concrete
// Zendure local-API meters (3CT and D0) are distinct selectable types so a
// manually added D0 is never labelled or stored as a 3CT.
const GRID_METER_TYPE_CHOICES = new Set([
  "shelly",
  "shelly_3em_gen1",
  "ecotracker",
  "zendure_grid_meter_http",
  "zendure_smartmeter_3ct_http",
  "zendure_smartmeter_d0_http",
  "tasmota_http",
  "zendure_smartmeter_d0",
  "mqtt",
  "ha",
]);

// Discovery api_family → concrete grid-meter type. A known family resolves
// exactly; it never has to be guessed from a substring of the model text. A
// Zendure D0 and a Smart Meter 3CT both discover as the generic local-HTTP
// family, which is config-ready on numeric total_power alone.
const GRID_METER_FAMILY_TYPES = {
  shelly_gen2: "shelly",
  shelly_3em_gen1: "shelly_3em_gen1",
  ecotracker: "ecotracker",
  zendure_grid_meter_http: "zendure_grid_meter_http",
  zendure_smartmeter_3ct_http: "zendure_grid_meter_http",
  tasmota_http: "tasmota_http",
};

// An explicit meter type (chosen manually) wins over discovery inference so a
// manual grid meter never has to be guessed from IP/port.
function gridMeterType(item, fallback) {
  const explicit = String(item.grid_meter_type || "").trim().toLowerCase();
  if (GRID_METER_TYPE_CHOICES.has(explicit)) return explicit;
  const family = String(item.api_family || "").trim().toLowerCase();
  if (GRID_METER_FAMILY_TYPES[family]) return GRID_METER_FAMILY_TYPES[family];
  const description = (item.device_type + " " + item.api_family).toLowerCase();
  if (description.includes("ecotracker")) return "ecotracker";
  if (description.includes("3em") && description.includes("gen1")) {
    return "shelly_3em_gen1";
  }
  return fallback || "shelly";
}

// Canonical Zendure SmartMeter D0 topic rule. EMS/Core owns the authoritative
// version (ems.config); this mirror keeps the live preview in sync so the user
// never has to type the topic. The backend regenerates it on every preview.
const ZENDURE_D0_TOPIC_PREFIX = "Zendure/sensor/";
const ZENDURE_D0_TOPIC_SUFFIX = "/totalPower";

function zendureD0Topic(serial) {
  const value = String(serial || "").trim();
  return value ? ZENDURE_D0_TOPIC_PREFIX + value + ZENDURE_D0_TOPIC_SUFFIX : "";
}

function zendureD0SerialFromTopic(topic) {
  const text = String(topic || "").trim();
  if (text.startsWith(ZENDURE_D0_TOPIC_PREFIX) && text.endsWith(ZENDURE_D0_TOPIC_SUFFIX)) {
    return text.slice(
      ZENDURE_D0_TOPIC_PREFIX.length,
      text.length - ZENDURE_D0_TOPIC_SUFFIX.length
    );
  }
  return "";
}

function resolveZendureD0Serial(item) {
  const explicit = String(item.serial_number || "").trim();
  if (explicit) return explicit;
  return zendureD0SerialFromTopic(featureValues["grid_meter.mqtt.topic"]);
}

// Topic ownership is tracked explicitly, never inferred from the topic string:
// "auto" regenerates from the serial, "manual" is preserved as-is. An unset mode
// is resolved conservatively — a non-empty topic of unknown provenance is
// treated as manual so it is never clobbered.
function effectiveD0TopicMode(item, currentTopic) {
  const mode = item && item.d0_topic_mode;
  if (mode === "auto" || mode === "manual") return mode;
  return currentTopic ? "manual" : "auto";
}

function setD0TopicMode(item, mode) {
  if (item) item.d0_topic_mode = mode === "manual" ? "manual" : "auto";
}

function syncZendureD0FeatureValues(item) {
  // D0 is MQTT-only; the HTTP IP must never reach a D0 preview.
  delete featureValues["grid_meter.ip"];
  featureValues["grid_meter.mqtt.payload_format"] = "number";
  const serial = resolveZendureD0Serial(item);
  const currentTopic = String(featureValues["grid_meter.mqtt.topic"] || "").trim();
  const mode = effectiveD0TopicMode(item, currentTopic);
  setD0TopicMode(item, mode);
  if (mode === "auto" && serial) {
    featureValues["grid_meter.mqtt.topic"] = zendureD0Topic(serial);
  }
}

function syncGridMeterFeatureValues(item) {
  const type = gridMeterType(item, "shelly");
  item.grid_meter_type = type;
  if (type) {
    featureValues["grid_meter.type"] = type;
  } else {
    // Unresolved neutral candidate: send no type so the backend keeps the
    // preview not-ready instead of falling back to Shelly.
    delete featureValues["grid_meter.type"];
  }
  if (type === "zendure_smartmeter_d0") {
    syncZendureD0FeatureValues(item);
  } else if (type) {
    featureValues["grid_meter.ip"] = item.ip || "";
  }
  saveFeatureValues();
}

function configDraftPreview() {
  if (
    !activeConfigTemplate ||
    activeConfigTemplateTag !== setupState.release.version
  ) {
    return {};
  }
  const preview = cloneConfigValue(activeConfigTemplate);
  const templateDevices = Array.isArray(activeConfigTemplate.devices)
    ? activeConfigTemplate.devices
    : [];
  const prototype = templateDevices[0] || {};
  preview.devices = configDraftItems
    .filter((item) => item.role === "inverter" && item.enabled)
    .map((item, index) => {
      const device = cloneConfigValue(templateDevices[index] || prototype);
      device.name = item.config_name;
      device.ip = item.ip;
      device.sn = item.serial_number || "";
      return device;
    });
  const meter = gridMeterItem();
  if (meter && meter.enabled) {
    const gridTemplate =
      activeConfigTemplate.grid_meter &&
      typeof activeConfigTemplate.grid_meter === "object"
        ? cloneConfigValue(activeConfigTemplate.grid_meter)
        : {};
    gridTemplate.type = gridMeterType(meter, gridTemplate.type);
    if (gridTemplate.type === "mqtt" && gridTemplate.mqtt) {
      gridTemplate.mqtt.host = meter.ip;
    } else {
      gridTemplate.ip = meter.ip;
    }
    preview.grid_meter = gridTemplate;
  } else {
    delete preview.grid_meter;
  }
  return preview;
}

// The preview is written via textContent, so JSON values never touch innerHTML.
function renderConfigPreview() {
  if (!configEls.preview) return;
  latestConfigPreview = null;
  // Supersede any in-flight preview: its response describes a draft (and a
  // workflow identity) that is no longer the current one, so it must not paint
  // a verdict or a conflict over this render.
  configPreviewRequest += 1;
  // A config-affecting change immediately revokes the exact preview authority;
  // Apply/Write stay disabled until the new preview succeeds.
  setSetupPreviewId(null);
  configEls.preview.textContent = "{}";
  setConfigExportReady(false);
  if (configEls.exportStatus) configEls.exportStatus.hidden = true;
  if (configEls.validationCard) configEls.validationCard.dataset.tone = "pending";
  if (configEls.previewReady) configEls.previewReady.textContent = "Checking…";
  if (configPreviewTimer) window.clearTimeout(configPreviewTimer);
  configPreviewTimer = window.setTimeout(requestConfigPreview, 100);
}

async function requestConfigPreview() {
  if (setupWorkflowStale) return;
  const requestId = ++configPreviewRequest;
  const generation = guidedSetupGeneration;
  try {
    const res = await fetch("/api/setup/config-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        setup_workflow_id: setupWorkflowId,
        devices: configDraftItems,
        supported_grid_meter_count: supportedGridMeters().length,
        features: featureValues,
        zendure_mqtt_proposals: mqttPreviewPayload(),
        zendure_mqtt_broker: mqttBrokerPayload(),
        zendure_mqtt_manual_devices: manualMqttDevicesPayload(),
      }),
    });
    // Re-check after every await: a workflow conflict raised by another request
    // while this one was in flight has already revoked this tab's authority.
    if (setupWorkflowStale || requestId !== configPreviewRequest) return;
    const data = await res.json();
    if (setupWorkflowStale) return;
    if (requestId !== configPreviewRequest || generation !== guidedSetupGeneration) return;
    if (isSetupWorkflowConflict(data)) {
      handleSetupWorkflowConflict(data);
      return;
    }
    if (!res.ok) throw new Error(data.error || "Config preview unavailable.");
    latestConfigPreview = data;
    setSetupPreviewId(data.config_preview_id || null);
    setConfigBaseline(data.config_revision);
    showSetupConfigConflict(null);
    configEls.preview.textContent = JSON.stringify(data.config || {}, null, 2);
    setConfigExportReady(Boolean(data.ready));
    if (configEls.previewReady) {
      configEls.previewReady.textContent = data.ready ? "Ready" : "Needs attention";
    }
    if (configEls.previewRelease) {
      configEls.previewRelease.textContent = data.release || "Not prepared";
    }
    if (configEls.previewBase) {
      const base = data.base || {};
      configEls.previewBase.textContent =
        base.source === "existing_config" ? "Existing EMS config" : "Release template";
    }
    if (configEls.previewDevices) {
      const summary = data.summary || {};
      const inverterCount = Number(summary.inverters || 0);
      const meterCount = Number(summary.grid_meters || 0);
      configEls.previewDevices.textContent =
        inverterCount + (inverterCount === 1 ? " inverter" : " inverters") +
        " · " + (meterCount ? meterCount + " grid meter" : "no grid meter");
    }
    renderConfigValidation();
    notifySetupStatus();
  } catch (err) {
    // A delayed failure must not repaint a superseded tab either.
    if (setupWorkflowStale || requestId !== configPreviewRequest) return;
    latestConfigPreview = {
      ready: false,
      validation: {
        errors: [{ message: err.message || String(err) }],
        warnings: [],
        info: [],
      },
    };
    renderConfigValidation();
    if (configEls.previewReady) configEls.previewReady.textContent = "Unavailable";
    configEls.preview.textContent = "{}";
    setConfigExportReady(false);
  }
}

function configExportBody(overwrite) {
  // Mutation authority is the workflow ID plus the exact server-issued
  // preview ID; the raw live revision is display-only and never sent back.
  return {
    setup_workflow_id: setupWorkflowId,
    config_preview_id: setupConfigPreviewId,
    devices: configDraftItems,
    supported_grid_meter_count: supportedGridMeters().length,
    features: featureValues,
    overwrite: Boolean(overwrite),
    zendure_mqtt_proposals: mqttPreviewPayload(),
    zendure_mqtt_broker: mqttBrokerPayload(),
    zendure_mqtt_manual_devices: manualMqttDevicesPayload(),
  };
}

// Selected Zendure MQTT proposals are allowed in the generated config, so
// export/apply readiness follows the backend preview alone — and mutations
// additionally need the exact preview ID the server issued for it.
function configExportAllowed() {
  return Boolean(
    latestConfigPreview && latestConfigPreview.ready && setupConfigPreviewId
  );
}

function setConfigExportReady(ready) {
  if (configEls.download) configEls.download.disabled = !ready;
  if (configEls.apply) configEls.apply.disabled = !ready;
  if (ready && hasMqttPreviewProposals()) {
    showConfigExportStatus(
      "Selected Zendure MQTT devices are included in the generated config; " +
        "output control is enabled only for supported inverters.",
      "info"
    );
  }
}

function showConfigApplyStatus(message, tone) {
  if (!configEls.applyStatus) return;
  configEls.applyStatus.hidden = false;
  configEls.applyStatus.dataset.tone = tone;
  configEls.applyStatus.textContent = message;
}

function showConfigExportStatus(message, tone) {
  if (!configEls.exportStatus) return;
  configEls.exportStatus.hidden = false;
  configEls.exportStatus.dataset.tone = tone;
  configEls.exportStatus.textContent = message;
}

function configExportError(data, fallback) {
  const errors =
    data && data.validation && Array.isArray(data.validation.errors)
      ? data.validation.errors.map((issue) => issue.message).filter(Boolean)
      : [];
  return errors.length ? errors.join(" ") : (data && data.message) || fallback;
}

async function downloadGeneratedConfig() {
  if (!configEls.download || configEls.download.disabled) return;
  configEls.download.disabled = true;
  showConfigExportStatus("Preparing validated config.json…", "info");
  try {
    const res = await fetch("/api/setup/config/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configExportBody(false)),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(configExportError(data, "Config download failed."));
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "config.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showConfigExportStatus("✓ config.json download ready.", "success");
  } catch (err) {
    showConfigExportStatus(err.message || String(err), "error");
  } finally {
    configEls.download.disabled = !configExportAllowed();
  }
}

async function applyGeneratedConfig() {
  if (!configEls.apply || configEls.apply.disabled) return;
  configEls.apply.disabled = true;
  showConfigApplyStatus("Applying config to the EMS installation…", "info");
  showCredentialRollbackWarning(configEls.applyRollback, null);
  try {
    const res = await fetch("/api/setup/config/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configExportBody(false)),
    });
    const data = await res.json();
    showCredentialRollbackWarning(configEls.applyRollback, data);
    if (isSetupWorkflowConflict(data)) {
      handleSetupWorkflowConflict(data);
      showConfigApplyStatus(SETUP_WORKFLOW_STALE_MESSAGE, "error");
      return;
    }
    if (isSetupConfigConflict(data)) {
      setSetupPreviewId(null);
      showSetupConfigConflict(data);
      showConfigApplyStatus(SETUP_CONFLICT_MESSAGES[data.error], "error");
      return;
    }
    if (!res.ok || !data.ok) {
      throw new Error(configExportError(data, "Could not apply the config."));
    }
    if (configEls.applyTarget && data.path) {
      configEls.applyTarget.textContent = data.path;
    }
    const lines = [
      data.created
        ? "✓ New config created at " + data.path
        : "✓ Config applied to " + data.path,
    ];
    if (data.backup_path) {
      lines.push("Previous config backed up to " + data.backup_path);
    } else if (!data.created) {
      lines.push("Existing config was preserved.");
    }
    showConfigApplyStatus(lines.join(" "), "success");
  } catch (err) {
    showConfigApplyStatus(err.message || String(err), "error");
  } finally {
    configEls.apply.disabled = !configExportAllowed();
  }
}

function findDraftItem(sourceId) {
  return configDraftItems.find((item) => item.source_id === sourceId) || null;
}

if (configEls.availableList) {
  configEls.availableList.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-available-toggle]");
    if (toggle) {
      const sourceId = toggle.getAttribute("data-available-toggle");
      if (openHardwareCards.has(sourceId)) openHardwareCards.delete(sourceId);
      else openHardwareCards.add(sourceId);
      renderConfigAvailable();
      return;
    }
    // An alternative connection switches the configured inverter in place; it
    // never reaches the add path, so no duplicate device can be created.
    const useConnection = event.target.closest('[data-action="use-connection"]');
    if (useConnection) {
      switchInverterTransport(
        useConnection.getAttribute("data-identity-ref"),
        useConnection.getAttribute("data-connection-source"),
        { token: useConnection.getAttribute("data-candidate-token") }
      );
      return;
    }
    const mqttAdd = event.target.closest(".config-mqtt-add");
    if (mqttAdd) {
      addMqttInverterFromCandidate(mqttAdd.getAttribute("data-proposal-id"));
      return;
    }
    const button = event.target.closest(".config-add");
    if (!button) return;
    addDeviceToDraft(
      button.getAttribute("data-source-id"),
      button.getAttribute("data-add-role")
    );
  });
}

if (configEls.gridMeterSelection) {
  configEls.gridMeterSelection.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-grid-toggle]");
    if (toggle) {
      const sourceId = toggle.getAttribute("data-grid-toggle");
      if (openHardwareCards.has(sourceId)) openHardwareCards.delete(sourceId);
      else openHardwareCards.add(sourceId);
      renderGridMeterSelection();
      return;
    }
    const use = event.target.closest(".config-grid-use");
    if (use) {
      selectGridMeter(use.getAttribute("data-source-id"));
      return;
    }
    const remove = event.target.closest(".config-grid-remove");
    if (remove) {
      const card = remove.closest("[data-source-id]");
      if (card) removeDraftItem(card.getAttribute("data-source-id"));
    }
  });
  configEls.gridMeterSelection.addEventListener("change", (event) => {
    const target = event.target;
    const card = target.closest("[data-source-id]");
    const meter = card && findDraftItem(card.getAttribute("data-source-id"));
    if (!meter) return;
    if (target.matches("[data-grid-enable]")) {
      meter.enabled = target.checked;
      saveConfigDraft();
      renderConfigDraft();
      renderConfigAvailable();
      return;
    }
    if (target.matches("[data-feature-variant-select]")) {
      meter.grid_meter_type = target.value;
      syncGridMeterFeatureValues(meter);
      saveConfigDraft();
      renderGridMeterSelection();
      renderConfigPreview();
      return;
    }
    if (target.matches("[data-feature-path]")) {
      markManualD0TopicEdit(target, meter);
      handleFeatureListChange(event);
    }
  });
  configEls.gridMeterSelection.addEventListener("input", (event) => {
    const target = event.target;
    const card = target.closest("[data-source-id]");
    const meter = card && findDraftItem(card.getAttribute("data-source-id"));
    if (!meter) return;
    const key = target.getAttribute("data-grid-field");
    if (key) {
      meter[key] = target.value;
      if (key === "ip") {
        featureValues["grid_meter.ip"] = target.value;
        saveFeatureValues();
      }
      if (key === "serial_number") {
        // Re-derive the generated D0 topic from the freshly typed serial.
        syncGridMeterFeatureValues(meter);
      }
      saveConfigDraft();
      const meta = card.querySelector(".hardware-card-meta");
      if (meta) {
        meta.textContent =
          String(meter.ip || "") + (meter.port ? ":" + String(meter.port) : "");
      }
      renderConfigPreview();
      return;
    }
    markManualD0TopicEdit(target, meter);
    handleFeatureListInput(event);
  });
  configEls.gridMeterSelection.addEventListener("click", (event) => {
    const reset = event.target.closest("[data-d0-use-generated]");
    if (!reset) return;
    const card = reset.closest("[data-source-id]");
    const meter = card && findDraftItem(card.getAttribute("data-source-id"));
    if (!meter) return;
    // Reset-to-default: switch back to auto mode and regenerate from the serial.
    setD0TopicMode(meter, "auto");
    delete featureValues["grid_meter.mqtt.topic"];
    syncGridMeterFeatureValues(meter);
    saveConfigDraft();
    renderGridMeterSelection();
    renderConfigPreview();
  });
}

// A direct edit of the D0 topic input takes manual ownership; the serial then
// no longer regenerates it. Topic provenance is tracked here, never inferred
// from the topic string.
function markManualD0TopicEdit(target, meter) {
  if (target.getAttribute("data-feature-path") === "grid_meter.mqtt.topic") {
    setD0TopicMode(meter, "manual");
    saveConfigDraft();
  }
}

if (configEls.manualForm) {
  configEls.manualForm.addEventListener("submit", (event) => {
    event.preventDefault();
    addManualDevice();
  });
}

if (configEls.manualRole) {
  configEls.manualRole.addEventListener("change", resetManualTypeForRole);
  resetManualTypeForRole();
}

if (configEls.manualType) {
  configEls.manualType.addEventListener("change", () => syncManualTypeDetails(true));
}

if (configEls.download) {
  configEls.download.addEventListener("click", downloadGeneratedConfig);
}

if (configEls.apply) {
  configEls.apply.addEventListener("click", applyGeneratedConfig);
}
if (configEls.conflictReview) {
  configEls.conflictReview.addEventListener("click", reviewCurrentSetupConfiguration);
}
if (configEls.conflictDiscard) {
  configEls.conflictDiscard.addEventListener("click", startGuidedSetupOver);
}

if (configEls.workflowConflictOpen) {
  configEls.workflowConflictOpen.addEventListener(
    "click",
    openCurrentSetupWorkflow
  );
}

if (configEls.workflowConflictDiscard) {
  // Drop only this tab's local draft, then rejoin whatever workflow is
  // current; the server-side setup of the newer session stays untouched.
  configEls.workflowConflictDiscard.addEventListener("click", async () => {
    configDraftItems = [];
    try {
      window.localStorage.removeItem(CONFIG_DRAFT_STORAGE_KEY);
    } catch (err) {
      /* localStorage may be unavailable; draft still lives in memory. */
    }
    setConfigBaseline(null);
    clearMqttSelection();
    clearFeatureValues();
    renderConfigDraft();
    renderConfigAvailable();
    await openCurrentSetupWorkflow();
  });
}

if (configEls.draftList) {
  configEls.draftList.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-inverter-toggle]");
    if (toggle) {
      const sourceId = toggle.getAttribute("data-inverter-toggle");
      if (openHardwareCards.has(sourceId)) openHardwareCards.delete(sourceId);
      else openHardwareCards.add(sourceId);
      renderInverterList();
      return;
    }
    const row = event.target.closest("[data-source-id]");
    if (!row) return;
    const sourceId = row.getAttribute("data-source-id");
    if (event.target.closest(".config-mqtt-remove")) {
      removeMqttInverter(sourceId);
    } else if (event.target.closest(".config-mqtt-reset")) {
      resetMqttInverterName(sourceId);
    } else if (event.target.closest(".config-draft-remove")) {
      removeDraftItem(sourceId);
    } else if (event.target.closest(".config-draft-reset")) {
      resetDraftItemName(sourceId);
    } else if (event.target.closest(".config-draft-move")) {
      const dir = event.target
        .closest(".config-draft-move")
        .getAttribute("data-move");
      moveDraftItem(sourceId, dir === "up" ? -1 : 1);
    }
  });

  // Text/number inputs update state without a full redraw so focus is kept
  // while typing; only the preview, validation, and collapsed summary refresh.
  configEls.draftList.addEventListener("input", (event) => {
    const target = event.target;
    if (target.hasAttribute("data-mqtt-config-name")) {
      const row = target.closest("[data-source-id]");
      const entry =
        row && zendureMqttPreviewProposals.get(row.getAttribute("data-source-id"));
      if (!entry) return;
      entry.config_name = target.value;
      rememberInverterName(entry, entry.config_name);
      saveMqttPreviewProposals();
      const meta = row.querySelector(".hardware-card-meta");
      if (meta) meta.textContent = mqttInverterSummaryText(entry);
      renderConfigPreview();
      renderConfigValidation();
      return;
    }
    const fieldPath = target.getAttribute("data-device-field");
    if (!fieldPath || target.type === "checkbox") return;
    const row = target.closest("[data-source-id]");
    const item = row && findDraftItem(row.getAttribute("data-source-id"));
    const field = item && deviceCatalogField(fieldPath);
    if (!field) return;
    updateDraftDeviceField(item, field, target.value);
    saveConfigDraft();
    renderConfigPreview();
    renderConfigValidation();
    updateInverterSummary(row, item);
  });

  configEls.draftList.addEventListener("change", (event) => {
    const target = event.target;
    const row = target.closest("[data-source-id]");
    if (target.matches("[data-mqtt-enable]")) {
      const entry =
        row && zendureMqttPreviewProposals.get(row.getAttribute("data-source-id"));
      if (!entry) return;
      entry.enabled = target.checked;
      saveMqttPreviewProposals();
      renderInverterList();
      renderConfigPreview();
      renderConfigValidation();
      return;
    }
    const item = row && findDraftItem(row.getAttribute("data-source-id"));
    if (!item) return;
    if (target.matches("[data-inverter-enable]")) {
      item.enabled = target.checked;
      saveConfigDraft();
      renderInverterList();
      renderConfigPreview();
      return;
    }
    const fieldPath = target.getAttribute("data-device-field");
    if (!fieldPath) return;
    // Text/number inputs already committed on input; only checkboxes/selects
    // need their final value applied here.
    if (target.tagName !== "SELECT" && target.type !== "checkbox") return;
    const field = deviceCatalogField(fieldPath);
    if (!field) return;
    const value = target.type === "checkbox" ? target.checked : target.value;
    updateDraftDeviceField(item, field, value);
    saveConfigDraft();
    renderConfigPreview();
    renderConfigValidation();
    updateInverterSummary(row, item);
  });
}

if (configEls.clearDraft) {
  configEls.clearDraft.addEventListener("click", () => {
    // Dismiss every discovered source and identity so the cleared draft stays clear.
    for (const device of availableConfigDevices()) {
      configDismissed.add(deviceKey(device));
      dismissSerial(device);
    }
    for (const proposal of availableMqttDeviceProposals()) {
      dismissSerial(proposal);
    }
    saveConfigDismissed();
    saveDismissedSerials();
    configDraftItems = [];
    try {
      window.localStorage.removeItem(CONFIG_DRAFT_STORAGE_KEY);
    } catch (err) {
      /* ignore */
    }
    setConfigBaseline(null);
    showSetupConfigConflict(null);
    // Clear the MQTT selection in lockstep so the two draft halves stay in sync.
    clearMqttSelection();
    renderConfigDraft();
    renderConfigAvailable();
  });
}

const ADMIN_VIEWS = ["setup", "maintenance"];

// Maintenance is a small hub with three nested paths. Only "manual" opens the
// detailed editor and touches the backend; the placeholders never do.
const MAINTENANCE_PATHS = ["hub", "manual", "upgrade", "backup"];

function setAdminView(view) {
  const next = ADMIN_VIEWS.includes(view) ? view : "setup";
  document.querySelectorAll("[data-admin-view]").forEach((button) => {
    const active = button.dataset.adminView === next;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-admin-view-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.adminViewPanel !== next;
  });
  if (next !== "maintenance") {
    // Return any maintenance-mounted source config to parking so the setup
    // Discovery step finds its nodes where it expects them.
    parkMaintenanceSourceConfigs();
  }
  if (next === "setup") {
    syncConfigFromDiscovery();
  }
  // Re-scope the pipeline after the view switch: a Setup-owned transition must
  // not remain visible in Maintenance and vice versa, and a synthetic preview
  // from the previous task must not follow into this one.
  rescopeSystemBuildForNavigation();
}

// Each maintenance path maps to exactly one full-page panel. "manual" loads the
// read-only overview; "upgrade" loads its own read-only planning data.
const MAINTENANCE_PANEL_IDS = {
  hub: "maintenance-hub",
  manual: "maintenance-manual-panel",
  upgrade: "maintenance-upgrade-panel",
  backup: "maintenance-backup-panel",
};

// Show a maintenance sub-panel and kick off its data load. Async paths return
// the load promise so a caller (e.g. a resume that must select the transition
// tag before continuing) can await full completion; ``pinnedTag`` is forwarded
// to the upgrade planning load.
function setMaintenancePath(path, pinnedTag) {
  const next = MAINTENANCE_PATHS.includes(path) ? path : "hub";
  Object.entries(MAINTENANCE_PANEL_IDS).forEach(([key, id]) => {
    const panel = document.getElementById(id);
    if (panel) panel.hidden = key !== next;
  });
  // Only the Guided Upgrade sub-panel owns the pipeline; leaving it for any
  // other maintenance sub-panel parks and hides the workflow immediately, and a
  // synthetic preview from another task never follows in.
  rescopeSystemBuildForNavigation();
  if (next === "manual") {
    return loadMaintenanceOverview();
  }
  if (next === "upgrade") {
    // Same-session navigation may keep an existing verification for the same
    // selected build (no forced re-verify just for returning to the panel).
    return loadUpgradePlanning(pinnedTag, { preserveVerification: true });
  }
  if (next === "backup") {
    return loadBackups();
  }
  return undefined;
}

function currentHashView() {
  return (window.location.hash || "").replace(/^#/, "");
}

function adminViewForHash(hash) {
  if (hash === "maintenance" || hash.startsWith("maintenance-")) return "maintenance";
  return hash;
}

function maintenancePathForHash(hash) {
  if (hash.startsWith("maintenance-")) return hash.slice("maintenance-".length);
  return "hub";
}

// Deep links (#maintenance, #maintenance-manual) still resolve to the
// right panel, but only once the start gate has revealed the workspace — while
// the landing gate is showing, hash changes must not un-hide a workspace panel.
function applyHashRoute() {
  if (!workspaceRevealed) return;
  const hash = currentHashView();
  const view = adminViewForHash(hash);
  setAdminView(view);
  if (view === "maintenance") setMaintenancePath(maintenancePathForHash(hash));
}
window.addEventListener("hashchange", applyHashRoute);

// --- setup wizard --------------------------------------------------------
// Compact stepper: only the active step's panel is shown. Devices and Config
// stay locked until the Release step reports "ready". Lightweight in-memory
// state model backed by the release-resource API.

const SETUP_STEPS = ["release", "devices", "config", "deployment", "start"];
const SETUP_STEP_STORAGE_KEY = "ems-admin-setup-step";

// Small factories for each Guided Setup state section. The initial page load and
// "Start over" share them so a reset can never drift from the initial shape when
// new fields are added later.
function createInitialDevicesState() {
  return { status: "idle", supported_count: 0, ignored_count: 0, mqtt_broker_count: 0 };
}

function createInitialConfigState() {
  return {
    status: "empty",
    auto_added_count: 0,
    warnings: [],
    template_loaded: false,
    template_tag: null,
  };
}

function createInitialDeploymentState() {
  return {
    generated_ready: false,
    generated_path: "/data/generated/config.json",
    plan: null,
    prepared: false,
    workspace: null,
    bootstrap_source: null,
    images: [],
    steps: [],
    phase: null,
    job_id: null,
    status: "idle",
    error: null,
    error_detail: null,
    conflict: false,
    existing_conflict: null,
    docker: null,
    auto_prepare_attempted: false,
  };
}

function createInitialStartState() {
  return {
    status: "idle",
    phase: null,
    steps: [],
    services: [],
    job_id: null,
    error: null,
    error_code: null,
    error_detail: null,
    running: false,
    dashboard_url: null,
    dashboard_reachable: false,
    errors: [],
    conflict: null,
    resolving_conflict: false,
  };
}

const setupState = {
  activeStep: "release",
  release: {
    selected: null,
    status: "loading",
    version: null,
    current: null,
    error: null,
    releases: [],
    resources: null,
    docker_image: null,
  },
  devices: createInitialDevicesState(),
  config: createInitialConfigState(),
  deployment: createInitialDeploymentState(),
  start: createInitialStartState(),
};

let deploymentJobTimer = null;
let startJobTimer = null;
// Bumped by "Start over" so an async response begun before the reset can detect
// it is stale and refuse to repopulate the freshly reset wizard.
let guidedSetupGeneration = 0;
// Start over awaits the backend abandon; a second click must not race it.
let startOverRunning = false;

let setupInitialized = false;
let devicesDiscoveryStarted = false;

const setupEls = {
  back: document.getElementById("setup-back"),
  next: document.getElementById("setup-next"),
  startOver: document.getElementById("setup-start-over"),
  navError: document.getElementById("setup-nav-error"),
  releaseForm: document.getElementById("release-form"),
  releaseSelect: document.getElementById("release-select"),
  releaseReload: document.getElementById("release-reload"),
  releaseStatus: document.getElementById("release-status"),
  releaseError: document.getElementById("release-error"),
  releaseSelectedVal: document.getElementById("release-selected-val"),
  releaseStatusVal: document.getElementById("release-status-val"),
  releaseBadges: document.getElementById("release-badges"),
  releaseResourceConfig: document.getElementById("release-resource-config"),
  releaseResourceInstall: document.getElementById("release-resource-install"),
  releaseResourceCompose: document.getElementById("release-resource-compose"),
  releaseResourceManifest: document.getElementById("release-resource-manifest"),
  releaseReadySummary: document.getElementById("release-ready-summary"),
  releaseTemplateLoaded: document.getElementById("release-template-loaded"),
  releaseDockerResources: document.getElementById("release-docker-resources"),
  releaseDockerImage: document.getElementById("release-docker-image"),
  deploymentState: document.getElementById("deployment-config-state"),
  deploymentStatus: document.getElementById("deployment-config-status"),
  deploymentPath: document.getElementById("deployment-config-path"),
  deploymentWorkspace: document.getElementById("deployment-workspace"),
  deploymentBootstrapSource: document.getElementById("deployment-bootstrap-source"),
  deploymentImages: document.getElementById("deployment-images"),
  deploymentImagesEmpty: document.getElementById("deployment-images-empty"),
  deploymentProgress: document.getElementById("deployment-progress"),
  deploymentSteps: document.getElementById("deployment-steps"),
  deploymentPrepare: document.getElementById("deployment-prepare"),
  deploymentReadySummary: document.getElementById("deployment-ready-summary"),
  deploymentInfluxNote: document.getElementById("deployment-influx-note"),
  deploymentStatusLine: document.getElementById("deployment-status"),
  deploymentErrorLine: document.getElementById("deployment-error"),
  deploymentErrorDetails: document.getElementById("deployment-error-details"),
  deploymentErrorDetail: document.getElementById("deployment-error-detail"),
  deploymentOverwrite: document.getElementById("deployment-overwrite"),
  deploymentOverwriteConfirm: document.getElementById("deployment-overwrite-confirm"),
  deploymentExistingInstall: document.getElementById("deployment-existing-install"),
  deploymentExistingReplace: document.getElementById("deployment-existing-replace"),
  deploymentLogDetails: document.getElementById("deployment-log-details"),
  deploymentDocker: document.getElementById("deployment-docker"),
  deploymentDockerState: document.getElementById("deployment-docker-state"),
  deploymentDockerNote: document.getElementById("deployment-docker-note"),
  deploymentDockerMode: document.getElementById("deployment-docker-mode"),
  deploymentDockerVersion: document.getElementById("deployment-docker-version"),
  deploymentDockerRecheck: document.getElementById("deployment-docker-recheck"),
  startBlocked: document.getElementById("start-blocked"),
  startWorkspace: document.getElementById("start-workspace"),
  startPrepared: document.getElementById("start-prepared"),
  startRelease: document.getElementById("start-release"),
  startDocker: document.getElementById("start-docker"),
  startServices: document.getElementById("start-services"),
  startServicesEmpty: document.getElementById("start-services-empty"),
  startProgress: document.getElementById("start-progress"),
  startSteps: document.getElementById("start-steps"),
  startButton: document.getElementById("start-button"),
  startRecheck: document.getElementById("start-recheck"),
  startRunningBadge: document.getElementById("start-running-badge"),
  startStatusLine: document.getElementById("start-status"),
  startErrorLine: document.getElementById("start-error"),
  startErrorDetails: document.getElementById("start-error-details"),
  startErrorDetail: document.getElementById("start-error-detail"),
  startPermissionError: document.getElementById("start-permission-error"),
  startPermissionIdentity: document.getElementById("start-permission-identity"),
  startPermissionRepair: document.getElementById("start-permission-repair"),
  startContainerConflict: document.getElementById("start-container-conflict"),
  startConflictTitle: document.getElementById("start-conflict-title"),
  startConflictMessage: document.getElementById("start-conflict-message"),
  startConflictName: document.getElementById("start-conflict-name"),
  startConflictImage: document.getElementById("start-conflict-image"),
  startConflictSelectedImage: document.getElementById("start-conflict-selected-image"),
  startConflictResolve: document.getElementById("start-conflict-resolve"),
  startSuccess: document.getElementById("start-success"),
  startDashboardLink: document.getElementById("start-dashboard-link"),
  stepStatus: {
    release: document.getElementById("step-status-release"),
    devices: document.getElementById("step-status-devices"),
    config: document.getElementById("step-status-config"),
    deployment: document.getElementById("step-status-deployment"),
    start: document.getElementById("step-status-start"),
  },
};

const RELEASE_STATUS_TEXT = {
  loading: "Loading…",
  not_started: "Not started",
  downloading: "Downloading…",
  ready: "Ready",
  failed: "Failed",
};

const CONFIG_STATUS_TEXT = {
  empty: "Empty",
  draft: "Draft",
  needs_attention: "Needs attention",
  valid: "Draft ready",
};

function releaseReady() {
  return (
    setupState.release.status === "ready" &&
    setupState.config.template_loaded &&
    setupState.config.template_tag === setupState.release.version
  );
}

// A prepared release cache is not a confirmed Fresh Setup operation: the later
// steps open only when release resources are ready AND a tag-bound operation
// context confirms the selected build.
function confirmedSetupBuildReady() {
  return (
    releaseReady() &&
    Boolean(setupOperationContext && setupOperationContext.operationId) &&
    setupOperationContext.systemTag === setupState.release.version
  );
}

// Devices and Config cannot be opened until the setup operation is confirmed;
// Deployment additionally needs a saved generated config.
function stepLocked(step) {
  if (step === "devices" || step === "config") return !confirmedSetupBuildReady();
  if (step === "deployment") {
    return !confirmedSetupBuildReady() || !setupState.deployment.generated_ready;
  }
  if (step === "start") {
    return stepLocked("deployment") || !deploymentReady();
  }
  return false;
}

function deviceStepStatusText() {
  if (stepLocked("devices")) return "Locked";
  if (setupState.devices.status === "discovering") return "Discovering…";
  const count = setupState.devices.supported_count;
  return count ? plural(count, "device") : "No devices yet";
}

function computeSetupStatus() {
  setupState.devices.supported_count = availableConfigDevices().length;
  setupState.devices.ignored_count = ignoredMdnsDevices.size;
  setupState.devices.mqtt_broker_count = mqttBrokers.size;
  setupState.devices.status = scanning
    ? "discovering"
    : setupState.devices.supported_count
    ? "ready"
    : "idle";

  const hints = latestConfigPreview
    ? ["errors", "warnings", "info"].flatMap((level) =>
        (latestConfigPreview.validation[level] || []).map((issue) => ({
          tone: level === "errors" ? "error" : level === "warnings" ? "warn" : "info",
          text: issue.message,
        }))
      )
    : configValidationHints();
  setupState.config.auto_added_count = configDraftItems.length;
  setupState.config.warnings = hints
    .filter((hint) => hint.tone !== "info")
    .map((hint) => hint.text);
  if (!configDraftItems.length) {
    setupState.config.status = "empty";
  } else if (hints.some((hint) => hint.tone === "error")) {
    setupState.config.status = "needs_attention";
  } else if (hints.some((hint) => hint.tone === "warn")) {
    setupState.config.status = "draft";
  } else {
    setupState.config.status = "valid";
  }
}

function renderStepper() {
  computeSetupStatus();
  setSummary(
    setupEls.stepStatus.release,
    RELEASE_STATUS_TEXT[setupState.release.status] || "Not started"
  );
  setSummary(setupEls.stepStatus.devices, deviceStepStatusText());
  setSummary(
    setupEls.stepStatus.config,
    stepLocked("config")
      ? "Locked"
      : CONFIG_STATUS_TEXT[setupState.config.status] || "Empty"
  );
  setSummary(
    setupEls.stepStatus.deployment,
    stepLocked("deployment")
      ? "Locked"
      : setupState.deployment.generated_ready
      ? "Config ready"
      : "Pending"
  );
  setSummary(
    setupEls.stepStatus.start,
    stepLocked("start") ? "Locked" : startStepStatusText()
  );
  document.querySelectorAll("[data-setup-step]").forEach((button) => {
    const step = button.dataset.setupStep;
    const active = step === setupState.activeStep;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.disabled = stepLocked(step);
  });
  renderSetupNav();
}

function renderSetupNav() {
  const index = SETUP_STEPS.indexOf(setupState.activeStep);
  if (setupEls.back) setupEls.back.hidden = index <= 0;
  if (!setupEls.next) return;
  const isLast = index >= SETUP_STEPS.length - 1;
  // Step 1 (release) owns its own Align / Continue footer, so the shared nav Next
  // is hidden there: exactly one proceed control, never a duplicate.
  const onRelease = setupState.activeStep === "release";
  setupEls.next.hidden = isLast || onRelease;
  if (isLast || onRelease) return;
  // Config commits the generated config on Continue; other steps just unlock.
  const onConfig = setupState.activeStep === "config";
  setupEls.next.textContent = onConfig ? "Continue to deployment" : "Next";
  // Continue follows the backend preview result alone; selected MQTT devices no
  // longer block it (genuine blockers all surface as preview errors).
  const canAdvance =
    setupState.activeStep === "deployment"
      ? deploymentReady()
      : onConfig
      ? Boolean(latestConfigPreview && latestConfigPreview.ready)
      : !stepLocked(SETUP_STEPS[index + 1]);
  setupEls.next.disabled = !canAdvance;
}

// The single re-render used after any status-affecting change. No-op until the
// wizard has initialized so early discovery renders never touch the stepper.
function notifySetupStatus() {
  if (setupInitialized) renderStepper();
}

function setActiveStep(step) {
  let next = SETUP_STEPS.includes(step) ? step : "release";
  if (stepLocked(next)) next = "release";
  setupState.activeStep = next;
  try {
    window.localStorage.setItem(SETUP_STEP_STORAGE_KEY, next);
  } catch (err) {
    /* localStorage may be unavailable; step still lives in memory. */
  }
  showSetupNavError("");
  document.querySelectorAll("[data-setup-step-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.setupStepPanel !== next;
  });
  renderStepper();
  if (next === "devices") enterDevicesStep();
  if (next === "config") syncConfigFromDiscovery();
  if (next === "deployment") {
    refreshDeploymentStatus();
    loadDeploymentPlan();
  }
  if (next === "start") {
    loadDeploymentPlan();
    refreshStartStatus();
  }
}

function showSetupNavError(message) {
  if (!setupEls.navError) return;
  setupEls.navError.hidden = !message;
  setupEls.navError.textContent = message || "";
}

// Rebuild, validate, and persist the generated config, then advance. Validation
// or write failures keep the user on Config with a visible error.
async function continueFromConfig() {
  if (!latestConfigPreview || !latestConfigPreview.ready) {
    showSetupNavError("Fix the config validation issues before continuing.");
    return;
  }
  showSetupNavError("");
  if (setupEls.next) setupEls.next.disabled = true;
  try {
    const res = await fetch("/api/setup/config/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configExportBody(true)),
    });
    const data = await res.json();
    if (isSetupWorkflowConflict(data)) {
      handleSetupWorkflowConflict(data);
      showSetupNavError(SETUP_WORKFLOW_STALE_MESSAGE);
      renderSetupNav();
      return;
    }
    if (isSetupConfigConflict(data)) {
      setSetupPreviewId(null);
      showSetupConfigConflict(data);
      showSetupNavError(SETUP_CONFLICT_MESSAGES[data.error]);
      renderSetupNav();
      return;
    }
    if (!res.ok || data.ok === false) {
      throw new Error(configExportError(data, "Could not save the generated config."));
    }
    setupState.deployment.generated_ready = true;
    setupState.deployment.generated_path = data.path || setupState.deployment.generated_path;
    setupState.deployment.prepared = false;
    setupState.deployment.auto_prepare_attempted = false;
    renderDeployment();
    setActiveStep("deployment");
  } catch (err) {
    showSetupNavError(err.message || String(err));
    renderSetupNav();
  }
}

const DEPLOYMENT_STEP_ICON = {
  done: "✓",
  running: "…",
  failed: "✗",
  pending: "•",
};

function deploymentReady() {
  const dep = setupState.deployment;
  return dep.prepared && dep.status === "succeeded";
}

function renderDeployment() {
  const dep = setupState.deployment;
  const ready = dep.generated_ready;
  if (setupEls.deploymentState) {
    setupEls.deploymentState.textContent = ready
      ? "Generated config ready"
      : "Generated config pending";
  }
  setSummary(setupEls.deploymentStatus, ready ? "Saved" : "Not saved");
  setSummary(
    setupEls.deploymentPath,
    dep.generated_path || "/data/generated/config.json"
  );
  setSummary(setupEls.deploymentWorkspace, dep.workspace || "—");
  setSummary(setupEls.deploymentBootstrapSource, deploymentBootstrapText());
  renderDeploymentDocker();
  renderDeploymentImages();
  renderDeploymentSteps();
  renderDeploymentControls();
}

const DOCKER_STATE_BADGE = {
  ready: { label: "Available", cls: "source-mdns" },
  socket_missing: { label: "Discovery only", cls: "source-scan" },
  client_missing: { label: "No Docker client", cls: "source-scan" },
  permission_denied: { label: "Permission problem", cls: "source-scan" },
  daemon_unreachable: { label: "Daemon unreachable", cls: "source-scan" },
  unavailable: { label: "Unavailable", cls: "source-scan" },
};

const DOCKER_MODE_TEXT = {
  deployment_controller: "Deployment controller (host Docker socket mounted)",
  discovery_only: "Discovery only (no Docker socket mounted)",
};

function dockerReady() {
  const docker = setupState.deployment.docker;
  // Unknown status (null) does not block; the server still gates prepare.
  return !docker || docker.state === "ready";
}

function renderDeploymentDocker() {
  const docker = setupState.deployment.docker;
  if (!setupEls.deploymentDocker) return;
  const badge = setupEls.deploymentDockerState;
  if (badge) {
    const info = docker ? DOCKER_STATE_BADGE[docker.state] : null;
    badge.hidden = !info;
    badge.textContent = info ? info.label : "";
    badge.className =
      "source-badge" + (info ? " " + info.cls : "");
  }
  setSummary(
    setupEls.deploymentDockerMode,
    docker ? DOCKER_MODE_TEXT[docker.mode] || docker.mode : "Checking…"
  );
  setSummary(
    setupEls.deploymentDockerVersion,
    docker && docker.server_version ? docker.server_version : "—"
  );
  if (setupEls.deploymentDockerNote) {
    setupEls.deploymentDockerNote.textContent = docker ? docker.message : "";
  }
}

function deploymentBootstrapText() {
  const dep = setupState.deployment;
  const tag = setupState.release.version || setupState.release.selected;
  if (!dep.bootstrap_source) return "—";
  return "Release resources from " + (tag || "selected release");
}

// Planned images come from the server plan; the frontend never chooses images.
function renderDeploymentImages() {
  const list = setupEls.deploymentImages;
  const empty = setupEls.deploymentImagesEmpty;
  if (!list || !empty) return;
  const images = setupState.deployment.images || [];
  list.replaceChildren();
  if (!images.length) {
    empty.hidden = false;
    list.hidden = true;
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  for (const image of images) {
    const row = document.createElement("div");
    row.className = "deployment-image-row";
    const label = document.createElement("span");
    label.className = "deployment-image-label";
    label.textContent =
      (image.service === "influxdb" ? "InfluxDB" : "EMS") + ": ";
    const ref = document.createElement("span");
    ref.className = "deployment-image-ref";
    ref.textContent = image.image;
    row.append(label, ref);
    if (image.status && image.status !== "pending") {
      const state = document.createElement("span");
      state.className = "deployment-image-state";
      state.textContent =
        image.status === "done"
          ? "done"
          : image.status === "downloading"
          ? (image.percent != null ? image.percent + "%" : "downloading…")
          : image.status;
      row.appendChild(state);
    }
    list.appendChild(row);
  }
  if (setupEls.deploymentInfluxNote) {
    const influx = setupState.deployment.plan && setupState.deployment.plan.influxdb;
    setupEls.deploymentInfluxNote.hidden = !influx || Boolean(influx.bundled);
  }
}

function renderDeploymentSteps() {
  const container = setupEls.deploymentSteps;
  const wrap = setupEls.deploymentProgress;
  if (!container || !wrap) return;
  const steps = setupState.deployment.steps || [];
  container.replaceChildren();
  if (!steps.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  for (const step of steps) {
    const row = document.createElement("div");
    row.className = "deployment-step deployment-step-" + step.status;
    const icon = document.createElement("span");
    icon.className = "deployment-step-icon";
    icon.textContent = DEPLOYMENT_STEP_ICON[step.status] || "•";
    const label = document.createElement("span");
    label.className = "deployment-step-label";
    label.textContent = step.label;
    row.append(icon, label);
    container.appendChild(row);
  }
}

function renderDeploymentControls() {
  const dep = setupState.deployment;
  const btn = setupEls.deploymentPrepare;
  if (btn) {
    btn.hidden = dep.status !== "failed";
    btn.disabled = !dep.plan || !dep.plan.can_prepare || !dockerReady();
    btn.textContent = "Retry preparation";
  }
  if (setupEls.deploymentReadySummary) {
    setupEls.deploymentReadySummary.hidden = !deploymentReady();
  }
  if (setupEls.deploymentLogDetails) {
    setupEls.deploymentLogDetails.hidden = !dep.prepared;
  }
  if (setupEls.deploymentStatusLine) {
    setupEls.deploymentStatusLine.textContent = deploymentStatusText();
  }
  if (setupEls.deploymentErrorLine) {
    setupEls.deploymentErrorLine.hidden = !dep.error;
    setupEls.deploymentErrorLine.textContent = dep.error || "";
  }
  if (setupEls.deploymentErrorDetails) {
    setupEls.deploymentErrorDetails.hidden = !dep.error_detail;
    if (setupEls.deploymentErrorDetail) {
      setupEls.deploymentErrorDetail.textContent = dep.error_detail || "";
    }
  }
  if (setupEls.deploymentOverwrite) {
    setupEls.deploymentOverwrite.hidden = !dep.conflict;
  }
  renderExistingInstallConflict();
}

function renderExistingInstallConflict() {
  const wrap = setupEls.deploymentExistingInstall;
  if (!wrap) return;
  wrap.hidden = !setupState.deployment.existing_conflict;
}

function deploymentStatusText() {
  const dep = setupState.deployment;
  if (dep.status === "running") return dep.phase || "Preparing deployment…";
  if (deploymentReady()) return "Deployment ready. Continue to Start EMS.";
  if (dep.status === "failed") return "Preparation failed.";
  if (!setupState.release.version) return "Prepare a release first.";
  if (!dep.generated_ready) return "Save a generated config first.";
  if (!dockerReady()) return dep.docker ? dep.docker.message : "Docker is not available.";
  return "Preparing deployment will start automatically.";
}

async function refreshDeploymentStatus() {
  try {
    const res = await fetch("/api/setup/config/status");
    const data = await res.json();
    if (!res.ok) return;
    setupState.deployment.generated_ready = Boolean(data.exists);
    if (data.path) setupState.deployment.generated_path = data.path;
    renderDeployment();
    notifySetupStatus();
  } catch (err) {
    /* status is best-effort; the wizard still works without it. */
  }
}

// The plan is read-only: it reports what will be prepared/downloaded before the
// user acts. The prepared marker survives page refresh.
async function loadDeploymentPlan() {
  try {
    const res = await fetch("/api/setup/deployment/plan");
    const data = await res.json();
    if (!res.ok) return;
    const dep = setupState.deployment;
    dep.plan = data;
    if (data.generated_config) {
      dep.generated_ready = Boolean(data.generated_config.ready);
      dep.generated_path = data.generated_config.path || dep.generated_path;
    }
    dep.workspace = data.workspace || dep.workspace;
    dep.bootstrap_source = data.bootstrap_source || null;
    dep.docker = data.docker || null;
    dep.images = Array.isArray(data.images) ? data.images : [];
    dep.prepared = Boolean(data.prepared);
    if (dep.prepared) {
      if (dep.status !== "running") dep.status = "succeeded";
    } else if (dep.status === "succeeded") {
      dep.status = "idle";
    }
    renderDeployment();
    renderStart();
    notifySetupStatus();
    autoPrepareDeploymentIfNeeded();
  } catch (err) {
    /* plan is best-effort; controls fall back to disabled. */
  }
}

function autoPrepareDeploymentIfNeeded() {
  const dep = setupState.deployment;
  if (
    setupState.activeStep !== "deployment" ||
    dep.auto_prepare_attempted ||
    dep.status === "running" ||
    dep.status === "failed" ||
    dep.conflict ||
    dep.existing_conflict ||
    deploymentReady() ||
    !dep.generated_ready ||
    !dep.plan ||
    !dep.plan.can_prepare ||
    !dockerReady()
  ) {
    return;
  }
  dep.auto_prepare_attempted = true;
  prepareDeployment(false);
}

async function prepareDeployment(overwrite) {
  const dep = setupState.deployment;
  dep.status = "running";
  dep.error = null;
  dep.error_detail = null;
  dep.conflict = false;
  dep.existing_conflict = null;
  dep.auto_prepare_attempted = true;
  dep.steps = [];
  renderDeploymentControls();
  try {
    const res = await fetch("/api/setup/deployment/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        setup_workflow_id: setupWorkflowId,
        overwrite: Boolean(overwrite),
      }),
    });
    const data = await res.json();
    if (res.status === 409 && data.reason === "existing_install_conflict") {
      // Never auto-replace an existing install: require explicit confirmation.
      dep.status = "idle";
      dep.existing_conflict = data;
      dep.error = null;
      renderDeployment();
      return;
    }
    if (res.status === 409 && data.reason === "workspace_conflict") {
      dep.status = "idle";
      dep.conflict = true;
      dep.error = data.message || "Workspace already prepared for another config.";
      renderDeployment();
      return;
    }
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || "Could not start preparation.");
    }
    applyDeploymentJob(data);
    if (data.job_id) pollDeploymentJob(data.job_id);
  } catch (err) {
    dep.status = "failed";
    dep.error = err.message || String(err);
    renderDeployment();
  }
}

function applyDeploymentJob(job) {
  const dep = setupState.deployment;
  dep.job_id = job.job_id || dep.job_id;
  dep.status = job.status === "running" ? "running" : job.status;
  dep.phase = job.phase || dep.phase;
  dep.steps = Array.isArray(job.steps) ? job.steps : [];
  if (Array.isArray(job.images) && job.images.length) dep.images = job.images;
  if (job.workspace) dep.workspace = job.workspace;
  if (job.status === "succeeded") {
    dep.prepared = true;
    dep.error = null;
    dep.error_detail = null;
  } else if (job.status === "failed") {
    dep.prepared = false;
    dep.error = (job.error && job.error.message) || "Preparation failed.";
    dep.error_detail = (job.error && job.error.detail) || null;
  }
  renderDeployment();
  notifySetupStatus();
}

function pollDeploymentJob(jobId) {
  if (deploymentJobTimer) window.clearTimeout(deploymentJobTimer);
  const generation = guidedSetupGeneration;
  const tick = async () => {
    try {
      const res = await fetch("/api/setup/deployment/jobs/" + encodeURIComponent(jobId));
      const job = await res.json();
      // "Start over" invalidates an in-flight poll so it cannot revive state.
      if (generation !== guidedSetupGeneration) return;
      if (!res.ok) throw new Error(job.error || "Job status unavailable.");
      applyDeploymentJob(job);
      if (job.status === "running") {
        deploymentJobTimer = window.setTimeout(tick, 800);
      } else {
        notifySetupStatus();
        loadDeploymentPlan();
      }
    } catch (err) {
      if (generation !== guidedSetupGeneration) return;
      setupState.deployment.status = "failed";
      setupState.deployment.error = err.message || String(err);
      renderDeployment();
    }
  };
  deploymentJobTimer = window.setTimeout(tick, 400);
}

// --- step 05: start EMS --------------------------------------------------

function startStepStatusText() {
  const start = setupState.start;
  if (start.status === "running") return "Starting…";
  if (start.status === "failed") return "Failed";
  if (start.running) return "Running";
  return "Ready";
}

function renderStart() {
  const dep = setupState.deployment;
  const blocked = !dep.prepared;
  if (setupEls.startBlocked) setupEls.startBlocked.hidden = !blocked;
  setSummary(setupEls.startWorkspace, dep.workspace || "—");
  setSummary(setupEls.startPrepared, dep.prepared ? "Prepared" : "Not prepared");
  setSummary(
    setupEls.startRelease,
    setupState.release.version || setupState.release.selected || "—"
  );
  renderStartDocker();
  renderStartServices();
  renderStartSteps();
  renderWorkspacePermissionError();
  renderContainerConflict();
  renderStartControls();
}

function renderWorkspacePermissionError() {
  const start = setupState.start;
  const visible = start.error_code === "workspace_permission_denied";
  if (setupEls.startPermissionError) {
    setupEls.startPermissionError.hidden = !visible;
  }
  const identity =
    setupState.deployment.plan && setupState.deployment.plan.runtime_identity;
  setSummary(
    setupEls.startPermissionIdentity,
    identity ? identity.puid + " / " + identity.pgid : "—"
  );
  if (setupEls.startPermissionRepair) {
    setupEls.startPermissionRepair.disabled = start.repairing_permissions;
    setupEls.startPermissionRepair.textContent = start.repairing_permissions
      ? "Repairing permissions…"
      : "Repair permissions and continue";
  }
}

function renderContainerConflict() {
  const start = setupState.start;
  const conflict = start.conflict;
  if (!setupEls.startContainerConflict) return;
  setupEls.startContainerConflict.hidden = !conflict;
  if (!conflict) return;
  const safe = conflict.safe_fix_available === true;
  const replace = conflict.replace_available === true;
  const running = conflict.status === "running";
  const serviceName = containerConflictServiceName(conflict);
  setupEls.startConflictTitle.textContent = replace
    ? serviceName + " is running with a different image"
    : running
    ? serviceName + " is already running"
    : serviceName === "InfluxDB"
      ? "Existing InfluxDB container found"
      : "Existing EMS container found";
  setupEls.startConflictMessage.textContent = replace
    ? "A running " + serviceName + " container already uses this name, but it is not using the selected image. Replacing it will stop and remove the current container, then start the prepared deployment with the selected image. Bind-mounted config/data folders are preserved, but switching images can change config/runtime compatibility."
    : safe
      ? "A stopped " + serviceName + " container already uses this name and blocks starting the selected release."
      : running
        ? "A running " + serviceName + " container already uses this name. Re-check its image and status before taking action."
        : "An existing container uses this name. Re-check its status before taking action.";
  setSummary(setupEls.startConflictName, conflict.container_name || "—");
  setSummary(setupEls.startConflictImage, conflict.image || "—");
  setSummary(setupEls.startConflictSelectedImage, conflict.selected_image || "—");
  if (setupEls.startConflictResolve) {
    setupEls.startConflictResolve.hidden = !safe && !replace;
    setupEls.startConflictResolve.disabled = start.resolving_conflict;
    setupEls.startConflictResolve.textContent = start.resolving_conflict
      ? replace
        ? "Replacing running " + serviceName + "…"
        : "Removing old container…"
      : replace
        ? "Replace running " + serviceName + " and continue"
        : "Remove old container and continue";
  }
}

function containerConflictServiceName(conflict) {
  const name = String((conflict && conflict.container_name) || "").toLowerCase();
  return name.includes("influx") ? "InfluxDB" : "EMS";
}

function renderStartDocker() {
  const docker = setupState.deployment.docker;
  if (!setupEls.startDocker) return;
  if (!docker) {
    setSummary(setupEls.startDocker, "Checking…");
    return;
  }
  const info = DOCKER_STATE_BADGE[docker.state];
  setSummary(setupEls.startDocker, info ? info.label : docker.state || "Unknown");
}

// Services come from the deployment status endpoint (docker compose ps) and,
// before a start, from the planned images. Values are untrusted → textContent.
function renderStartServices() {
  const list = setupEls.startServices;
  const empty = setupEls.startServicesEmpty;
  if (!list || !empty) return;
  const services = startServiceRows();
  list.replaceChildren();
  if (!services.length) {
    empty.hidden = false;
    list.hidden = true;
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  for (const service of services) {
    const row = document.createElement("div");
    row.className = "deployment-image-row";
    const label = document.createElement("span");
    label.className = "deployment-image-label";
    label.textContent = (service.label || service.service || "service") + ": ";
    const ref = document.createElement("span");
    ref.className = "deployment-image-ref";
    ref.textContent = service.image || "—";
    row.append(label, ref);
    if (service.state || service.status) {
      const state = document.createElement("span");
      state.className = "deployment-image-state";
      state.textContent = service.state || service.status;
      row.appendChild(state);
    }
    list.appendChild(row);
  }
}

// Real container status (from status/start job) wins; otherwise show the
// planned images as pending services so the user sees what will start.
function startServiceRows() {
  const start = setupState.start;
  if (Array.isArray(start.services) && start.services.length) {
    return start.services.map((service) => ({
      label: serviceLabel(service.service),
      service: service.service,
      image: service.image,
      state: service.state,
      status: service.status,
    }));
  }
  return (setupState.deployment.images || []).map((image) => ({
    label: serviceLabel(image.service),
    service: image.service,
    image: image.image,
    state: null,
    status: "pending",
  }));
}

function serviceLabel(service) {
  if (service === "influxdb") return "InfluxDB";
  if (service === "ems") return "EMS";
  if (service === "ems-solarflow-admin") return "Admin";
  return service || "Service";
}

function renderStartSteps() {
  const container = setupEls.startSteps;
  const wrap = setupEls.startProgress;
  if (!container || !wrap) return;
  const steps = setupState.start.steps || [];
  container.replaceChildren();
  if (!steps.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  for (const step of steps) {
    const row = document.createElement("div");
    row.className = "deployment-step deployment-step-" + step.status;
    const icon = document.createElement("span");
    icon.className = "deployment-step-icon";
    icon.textContent = DEPLOYMENT_STEP_ICON[step.status] || "•";
    const label = document.createElement("span");
    label.className = "deployment-step-label";
    label.textContent = step.label;
    row.append(icon, label);
    container.appendChild(row);
  }
}

function renderStartControls() {
  const start = setupState.start;
  const dep = setupState.deployment;
  const btn = setupEls.startButton;
  if (btn) {
    const running = start.status === "running";
    btn.disabled =
      running ||
      !dep.prepared ||
      !dockerReady() ||
      Boolean(start.conflict) ||
      start.error_code === "workspace_permission_denied";
    btn.textContent = running
      ? "Starting…"
      : start.running
      ? "Restart EMS"
      : "Start EMS";
  }
  if (setupEls.startRecheck) {
    setupEls.startRecheck.disabled = start.status === "running";
  }
  if (setupEls.startRunningBadge) {
    setupEls.startRunningBadge.hidden = !start.running;
  }
  if (setupEls.startStatusLine) {
    setupEls.startStatusLine.textContent = startStatusText();
  }
  if (setupEls.startErrorLine) {
    setupEls.startErrorLine.hidden = !start.error;
    setupEls.startErrorLine.textContent = start.error || "";
  }
  if (setupEls.startErrorDetails) {
    setupEls.startErrorDetails.hidden = !start.error_detail;
  }
  if (setupEls.startErrorDetail) {
    setupEls.startErrorDetail.textContent = start.error_detail || "";
  }
  renderStartSuccess();
}

function renderStartSuccess() {
  const start = setupState.start;
  const success = setupEls.startSuccess;
  const link = setupEls.startDashboardLink;
  const show = start.running && !start.conflict;
  if (success) success.hidden = !show;
  if (link) link.href = startDashboardHref();
}

function startStatusText() {
  const start = setupState.start;
  if (start.status === "running") return start.phase || "Starting EMS…";
  if (start.conflict) return "Resolve the container conflict to continue.";
  if (start.status === "failed") return "Start failed.";
  if (start.running) return "EMS is running.";
  if (!setupState.deployment.prepared) return "Prepare deployment first before starting EMS.";
  if (!dockerReady()) {
    return setupState.deployment.docker
      ? setupState.deployment.docker.message
      : "Docker is not available.";
  }
  return "Ready to start the prepared deployment.";
}

// Prefer the prepared config host/port. When the Admin UI is opened from another
// machine, localhost would point at the user's own device, so swap in the
// current browser hostname while keeping the dashboard scheme and port.
function startDashboardHref() {
  const url = setupState.start.dashboard_url || "http://localhost:8080";
  try {
    const parsed = new URL(url);
    const host = window.location.hostname;
    if (host && host !== "localhost" && host !== "127.0.0.1") {
      parsed.hostname = host;
    }
    return parsed.href;
  } catch (err) {
    return url;
  }
}

async function refreshStartStatus() {
  try {
    const res = await fetch("/api/setup/deployment/status");
    const data = await res.json();
    if (!res.ok) return;
    const start = setupState.start;
    setupState.deployment.prepared = Boolean(data.prepared);
    if (data.docker) setupState.deployment.docker = data.docker;
    start.running = Boolean(data.running);
    start.services = Array.isArray(data.services) ? data.services : [];
    start.dashboard_url = data.dashboard_url || start.dashboard_url;
    start.dashboard_reachable = Boolean(data.dashboard_reachable);
    start.errors = Array.isArray(data.errors) ? data.errors : [];
    if (data.conflict) start.conflict = data.conflict;
    else if (start.status !== "failed") start.conflict = null;
    if (start.running) {
      start.status = "succeeded";
      start.error = null;
      start.error_code = null;
      start.error_detail = null;
    } else if (start.status !== "running" && start.status !== "failed") {
      start.status = start.running ? "succeeded" : "idle";
      start.error =
        !start.running && start.errors.length
          ? start.errors[0].message || "Could not read deployment status."
          : null;
      start.error_detail =
        !start.running && start.errors.length ? start.errors[0].detail || null : null;
      start.error_code =
        !start.running && start.errors.length ? start.errors[0].code || null : null;
    }
    renderStart();
    notifySetupStatus();
  } catch (err) {
    /* status is best-effort; the button still works without it. */
  }
}

async function startDeployment() {
  const start = setupState.start;
  start.status = "running";
  start.error = null;
  start.error_code = null;
  start.error_detail = null;
  start.conflict = null;
  start.steps = [];
  renderStartControls();
  try {
    const res = await fetch("/api/setup/deployment/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setup_workflow_id: setupWorkflowId }),
    });
    const data = await res.json();
    if (data && data.transition) {
      renderSystemAlignmentStatus(data);
    } else {
      await loadSystemAlignmentStatus();
    }
    if (!res.ok || data.ok === false) {
      start.status = "failed";
      start.error_code = data.reason || null;
      start.error = data.message || data.error || "Could not start EMS.";
      start.error_detail = data.detail || null;
      renderStart();
      return;
    }
    applyStartJob(data);
    if (data.job_id) pollStartJob(data.job_id);
  } catch (err) {
    start.status = "failed";
    start.error = err.message || String(err);
    renderStart();
  }
}

function applyStartJob(job) {
  const start = setupState.start;
  start.job_id = job.job_id || start.job_id;
  start.status = job.status === "running" ? "running" : job.status;
  start.phase = job.phase || start.phase;
  start.steps = Array.isArray(job.steps) ? job.steps : [];
  if (Array.isArray(job.services)) start.services = job.services;
  if (job.dashboard_url) start.dashboard_url = job.dashboard_url;
  start.dashboard_reachable = Boolean(job.dashboard_reachable);
  if (job.status === "succeeded") {
    start.running = true;
    start.error = null;
    start.error_code = null;
  } else if (job.status === "failed") {
    start.running = false;
    start.error_code = (job.error && job.error.code) || null;
    start.error = (job.error && job.error.message) || "Start failed.";
    start.error_detail = (job.error && job.error.detail) || null;
    start.conflict = job.conflict || null;
  }
  if (job && job.transition) renderSystemAlignmentStatus(job);
  renderStart();
}

async function repairWorkspacePermissions() {
  const start = setupState.start;
  if (start.error_code !== "workspace_permission_denied") return;
  start.repairing_permissions = true;
  renderStart();
  try {
    const res = await fetch("/api/setup/deployment/repair-permissions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setup_workflow_id: setupWorkflowId }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      start.status = "failed";
      start.error_code = data.reason || "workspace_permission_repair_failed";
      start.error = data.message || data.error || "Could not repair permissions.";
      start.error_detail = data.detail || null;
      return;
    }
    start.error = null;
    start.error_code = null;
    start.error_detail = null;
    start.repairing_permissions = false;
    await startDeployment();
  } catch (err) {
    start.status = "failed";
    start.error = err.message || String(err);
  } finally {
    start.repairing_permissions = false;
    renderStart();
  }
}

async function resolveContainerConflict() {
  const start = setupState.start;
  const conflict = start.conflict;
  const replace = conflict && conflict.replace_available === true;
  const safe = conflict && conflict.safe_fix_available === true;
  if (!conflict || (!safe && !replace)) return;
  start.resolving_conflict = true;
  start.error = null;
  renderStart();
  try {
    const res = await fetch("/api/setup/deployment/resolve-container-conflict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        setup_workflow_id: setupWorkflowId,
        container_name: conflict.container_name,
        action: replace
          ? "replace_running_and_continue"
          : "remove_stopped_and_continue",
      }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || "Could not resolve the container conflict.");
    }
    if (data && data.transition) renderSystemAlignmentStatus(data);
    start.conflict = data.conflict || null;
    start.resolving_conflict = false;
    start.error = null;
    start.error_code = null;
    start.error_detail = null;
    if (data.continue !== true) {
      start.status = "idle";
      renderStart();
      return;
    }
    await startDeployment();
  } catch (err) {
    start.resolving_conflict = false;
    start.status = "failed";
    start.error = err.message || String(err);
    renderStart();
  }
}

function pollStartJob(jobId) {
  if (startJobTimer) window.clearTimeout(startJobTimer);
  const generation = guidedSetupGeneration;
  const tick = async () => {
    try {
      const res = await fetch(
        "/api/setup/deployment/start/jobs/" + encodeURIComponent(jobId)
      );
      const job = await res.json();
      if (generation !== guidedSetupGeneration) return;
      if (!res.ok) throw new Error(job.error || "Job status unavailable.");
      applyStartJob(job);
      if (job.status === "running") {
        startJobTimer = window.setTimeout(tick, 900);
      } else {
        notifySetupStatus();
        refreshStartStatus();
      }
    } catch (err) {
      if (generation !== guidedSetupGeneration) return;
      setupState.start.status = "failed";
      setupState.start.error = err.message || String(err);
      renderStart();
    }
  };
  startJobTimer = window.setTimeout(tick, 500);
}

function goToStep(delta) {
  const target = SETUP_STEPS[SETUP_STEPS.indexOf(setupState.activeStep) + delta];
  if (target) setActiveStep(target);
}

// Discovery is deferred until the Devices step is first opened, and runs once
// per session (mDNS keeps polling on its own from startup).
function enterDevicesStep() {
  const prep = loadDiscoveryPreparation();
  if (devicesDiscoveryStarted) {
    prep.then(refreshUnifiedDevices);
    return;
  }
  devicesDiscoveryStarted = true;
  // First open of the Devices step auto-runs the same discovery as the
  // "Run discovery" button, so a scan starts without an extra click. The
  // preparation (enabled sources + priority order) must be loaded first.
  prep.then(() => runUnifiedDiscovery());
}

async function onReleaseSelectChange() {
  const value = setupEls.releaseSelect.value;
  const previousTag = selectedSystemBuildTag;
  selectedSystemBuildTag = value || null;
  // Supersede any validation still returning for the previous value, and bind
  // this selection's validation to its epoch so a newer selection arriving
  // during the awaits below wins.
  systemBuildState.validationGeneration += 1;
  // Clear the previous build's immutable facts before any asynchronous work for
  // the new selection starts.  A failed validation must never leave the old
  // revision or image refs visible beside the newly selected tag.
  resetSystemAlignmentPresentation(value || null, "selection_started");
  setupState.release.selected = value;
  const release = setupState.release.releases.find((item) => item.tag === value);
  setSummary(setupEls.releaseSelectedVal, release ? release.name : value);
  renderReleaseBadges(release);
  setupState.release.resources = null;
  setupState.release.docker_image = null;
  setupState.release.version = null;
  clearActiveConfigTemplate();
  // A changed selection revokes any confirmed operation: a prepared cache alone
  // never re-authorizes the later steps.
  clearSetupOperationContext();
  // A changed selection invalidates the previously confirmed resource context.
  if (release && release.prepared && setupState.release.current === release.tag) {
    setupState.release.version = release.tag;
    setupState.release.resources = preparedResourceStatus();
    try {
      await loadActiveConfigTemplate(release.tag);
      setReleaseStatus("ready");
    } catch (err) {
      setupState.release.resources = null;
      setReleaseStatus("failed", err.message || String(err));
    }
  } else if (release && !release.selectable) {
    setReleaseStatus("failed", release.reason || "This System Build cannot be used.");
  } else {
    setReleaseStatus("not_started");
  }
  if (release && release.reason && setupEls.releaseStatus) {
    setupEls.releaseStatus.textContent = release.reason;
  }
  renderReleaseResources();
  try {
    await supersedeSetupBuild(value, previousTag);
  } catch (err) {
    systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
    systemBuildState.failedAction = "validate";
    systemBuildState.error =
      "Could not switch System Builds: " + (err.message || String(err));
    setReleaseStatus("failed", systemBuildState.error);
    applySystemBuildAlignment();
    return;
  }
  // A changed selection is side-effect free: show the local catalogue preview
  // and require an explicit Verify. No image is pulled and no full validation
  // runs on selection — that only happens when the user clicks Verify.
  presentSelectedSystemBuild(value || null);
}

function setReleaseStatus(status, error) {
  setupState.release.status = status;
  setupState.release.error = error || null;
  setSummary(setupEls.releaseStatusVal, RELEASE_STATUS_TEXT[status] || status);
  const messages = {
    loading: "Loading System Builds…",
    not_started:
      "Select a System Build, then Verify System Build to download and verify it.",
    downloading: "Downloading and verifying the Admin and EMS images…",
    ready: "System Build resources are verified.",
    failed: "System Build preparation failed.",
  };
  if (setupEls.releaseStatus) {
    setupEls.releaseStatus.textContent = messages[status] || "";
  }
  if (setupEls.releaseError) {
    setupEls.releaseError.hidden = !error;
    setupEls.releaseError.textContent = error || "";
  }
  renderStepper();
}

// Map a server-provided release channel to its plain-language selector group.
// The rolling "latest" tag is its own Latest group (a main-branch build, not a
// versioned release); versioned finals are Stable; release candidates are
// Unstable; development builds are Experimental. An unrecognised channel returns
// null so it is never silently offered under Stable.
function systemBuildGroupLabel(channel) {
  if (channel === "latest") return "Latest";
  if (channel === "stable") return "Stable";
  if (channel === "rc" || channel === "release_candidate") return "Unstable";
  if (channel === "development") return "Experimental";
  return null;
}

function groupSetupReleaseOptions(releases) {
  // Ordered selector groups; Experimental (development) is always last. A release
  // whose channel maps to no group is dropped, never folded into Stable.
  const order = ["Latest", "Stable", "Unstable", "Experimental"];
  const list = Array.isArray(releases) ? releases : [];
  return order
    .map((label) => ({
      label,
      releases: list.filter(
        (release) => systemBuildGroupLabel(release.channel) === label
      ),
    }))
    .filter((group) => group.releases.length > 0);
}

// Render catalogue timestamps in one compact, locale-independent shape while
// using the operator's local browser timezone. Remote versioned releases expose
// ``published_at``; development builds also expose ``created_at``.
function systemBuildTimestampLabel(release) {
  const value = release && (release.published_at || release.created_at);
  if (typeof value !== "string" || !value.trim()) return "";
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return "";
  const pad = (part) => String(part).padStart(2, "0");
  return (
    timestamp.getFullYear() +
    "-" +
    pad(timestamp.getMonth() + 1) +
    "-" +
    pad(timestamp.getDate()) +
    " · " +
    pad(timestamp.getHours()) +
    ":" +
    pad(timestamp.getMinutes())
  );
}

function releaseOptionLabel(release) {
  const timestamp = systemBuildTimestampLabel(release);
  const timestampSuffix = timestamp ? " · " + timestamp : "";
  if (release.selection_label) return release.selection_label + timestampSuffix;
  if (release.channel === "development") {
    const name = release.display_name || release.name || release.tag;
    const revision = release.revision_short ? " · " + release.revision_short : "";
    return "Development — " + name + revision + timestampSuffix;
  }
  if (release.channel === "latest") {
    // The rolling main-branch build; the label states what it is rather than
    // pinning a version, and never calls it stable.
    const base = "Latest · current main build";
    return (
      base +
      timestampSuffix +
      (release.selectable === false && release.reason ? " — " + release.reason : "")
    );
  }
  const labels = [release.name || release.tag];
  if (release.channel === "stable") labels.push("stable");
  if (release.prerelease) labels.push("rc", "not stable");
  labels.push(release.docker_supported ? "docker" : "unsupported");
  if (release.prepared) labels.push("prepared");
  if (release.active) labels.push("active");
  return (
    labels[0] +
    (labels.length > 1 ? " — " + labels.slice(1).join(" · ") : "") +
    timestampSuffix +
    (release.selectable === false && release.reason ? " — " + release.reason : "")
  );
}

function renderReleaseBadges(release) {
  if (!setupEls.releaseBadges) return;
  setupEls.releaseBadges.replaceChildren();
  if (!release) return;
  const badges = [];
  if (release.channel === "stable") badges.push(["stable", "source-mdns"]);
  if (release.channel === "latest") badges.push(["latest", "source-scan"]);
  if (release.prerelease) badges.push(["rc", "source-scan"]);
  badges.push(
    release.docker_supported
      ? ["docker", "source-mdns"]
      : ["unsupported", "source-scan"]
  );
  if (release.prepared) badges.push(["prepared", "source-mdns"]);
  if (release.active) badges.push(["active", "source-mdns"]);
  for (const badge of badges) {
    const span = document.createElement("span");
    span.className = "source-badge " + badge[1];
    span.textContent = badge[0];
    setupEls.releaseBadges.appendChild(span);
  }
}

function preparedResourceStatus() {
  return {
    config_template_available: true,
    config_template_loaded: true,
    docker_install_available: true,
    compose_example_available: true,
    deploy_docker_available: true,
  };
}

function renderReleaseResources() {
  const resources = setupState.release.resources;
  setSummary(
    setupEls.releaseResourceConfig,
    resources && resources.config_template_available ? "Ready" : "Not prepared"
  );
  setSummary(
    setupEls.releaseResourceInstall,
    resources && resources.docker_install_available ? "Ready" : "Not prepared"
  );
  setSummary(
    setupEls.releaseResourceCompose,
    resources &&
      resources.compose_example_available &&
      resources.deploy_docker_available
      ? "Ready"
      : "Not prepared"
  );
  setSummary(
    setupEls.releaseResourceManifest,
    releaseReady() && setupState.release.version
      ? setupState.release.version + "/manifest.json"
      : "Not prepared"
  );
  const templateLoaded =
    resources &&
    (resources.config_template_loaded || resources.config_template_available) &&
    setupState.config.template_loaded;
  const dockerResources =
    resources &&
    resources.docker_install_available &&
    resources.compose_example_available &&
    resources.deploy_docker_available;
  if (setupEls.releaseReadySummary) {
    setupEls.releaseReadySummary.hidden = !releaseReady();
  }
  setSummary(setupEls.releaseTemplateLoaded, templateLoaded ? "Ready" : "Missing");
  setSummary(setupEls.releaseDockerResources, dockerResources ? "Ready" : "Missing");
  setSummary(
    setupEls.releaseDockerImage,
    setupState.release.docker_image || "Not prepared"
  );
}

function clearActiveConfigTemplate() {
  activeConfigTemplate = null;
  activeConfigTemplateTag = null;
  setupState.config.template_loaded = false;
  setupState.config.template_tag = null;
  if (configEls.templateStatus) {
    configEls.templateStatus.textContent =
      "Confirm a System Build to load config.template.json.";
  }
  if (configEls.templatePreview) configEls.templatePreview.textContent = "{}";
  renderConfigPreview();
}

async function loadActiveConfigTemplate(expectedTag) {
  const res = await fetch("/api/setup/config-template");
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data && data.error ? data.error : "config template unavailable");
  }
  if (
    !data ||
    data.tag !== expectedTag ||
    !data.template ||
    typeof data.template !== "object" ||
    Array.isArray(data.template)
  ) {
    throw new Error("The confirmed System Build returned an invalid config template.");
  }
  activeConfigTemplate = data.template;
  activeConfigTemplateTag = data.tag;
  setupState.config.template_loaded = true;
  setupState.config.template_tag = data.tag;
  setupState.release.docker_image = data.docker_image || setupState.release.docker_image;
  if (configEls.templateStatus) {
    configEls.templateStatus.textContent =
      "Using config template from " + data.tag;
  }
  if (configEls.templatePreview) {
    configEls.templatePreview.textContent = JSON.stringify(data.template, null, 2);
  }
  renderConfigPreview();
  // Device rows fall back to template prototype defaults for unset values, so
  // refresh them once the prepared template is available.
  renderInverterList();
  return data;
}

async function loadReleases() {
  setReleaseStatus("loading");
  try {
    const res = await fetch("/api/setup/releases");
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data && data.error ? data.error : "release list unavailable");
    }
    const releases = Array.isArray(data.releases) ? data.releases : [];
    setupState.release.releases = releases;
    // One supported list drives both what is rendered and what can be selected:
    // a build in an unknown or hidden channel is dropped here and can therefore
    // never become the internal selection.
    const grouped = groupSetupReleaseOptions(releases);
    const supportedReleases = grouped.flatMap((group) => group.releases);
    setupEls.releaseSelect.innerHTML = "";
    for (const group of grouped) {
      const optgroup = document.createElement("optgroup");
      optgroup.label = group.label;
      for (const release of group.releases) {
        const option = document.createElement("option");
        option.value = release.tag;
        option.textContent = releaseOptionLabel(release);
        option.dataset.channel = release.channel;
        if (release.revision) option.dataset.revision = release.revision;
        if (release.build_id) option.dataset.buildId = release.build_id;
        option.disabled = release.selectable === false;
        optgroup.appendChild(option);
      }
      setupEls.releaseSelect.appendChild(optgroup);
    }
    const selected =
      // A build restored from the server transition (reconnect/reload/login)
      // wins over the catalogue default so a reload lands on the same build.
      // Every candidate is drawn from the rendered supported list: an unsupported
      // server default falls through to the next supported option.
      (selectedSystemBuildTag &&
        supportedReleases.find((item) => item.tag === selectedSystemBuildTag)) ||
      supportedReleases.find((item) => item.tag === data.default_release) ||
      supportedReleases.find(
        (item) => item.tag === data.prepared_release && item.selectable !== false
      ) ||
      supportedReleases.find((item) => item.selectable !== false);
    if (!selected) {
      throw new Error(
        Array.isArray(data.warnings) && data.warnings.length
          ? data.warnings[0]
          : "No System Builds are available."
      );
    }
    setupEls.releaseSelect.value = selected.tag;
    setupEls.releaseSelect.disabled = false;
    setupState.release.selected = selected.tag;
    setupState.release.current = data.prepared_release || null;
    const selectedIsCurrent =
      selected.prepared && selected.tag === setupState.release.current;
    setupState.release.version = selectedIsCurrent ? selected.tag : null;
    setupState.release.resources = selectedIsCurrent ? preparedResourceStatus() : null;
    setupState.release.docker_image = null;
    setSummary(setupEls.releaseSelectedVal, selected.name || selected.tag);
    renderReleaseBadges(selected);
    if (selectedIsCurrent) {
      await loadActiveConfigTemplate(selected.tag);
      setReleaseStatus("ready");
    } else {
      clearActiveConfigTemplate();
      setReleaseStatus("not_started");
    }
    if (selected.reason && setupEls.releaseStatus) {
      setupEls.releaseStatus.textContent = selected.reason;
    }
    if (Array.isArray(data.warnings) && data.warnings.length) {
      setupEls.releaseStatus.textContent =
        (selected.prepared ? "Cached resources are ready. " : "") + data.warnings[0];
    }
    renderReleaseResources();
    if (systemBuildResumeValidationTag) {
      const resumeTag = systemBuildResumeValidationTag;
      systemBuildResumeValidationTag = null;
      if (
        setupEls.releaseSelect &&
        setupState.release.releases.some((item) => item.tag === resumeTag)
      ) {
        setupEls.releaseSelect.value = resumeTag;
      }
      // A reconnect/reload resume re-verifies the in-progress build so its
      // aligned/verified state is restored — this is not a fresh selection.
      validateSelectedSystemBuild({ tag: resumeTag });
    } else {
      // Preview the pre-selected build only (a programmatic selection fires no
      // change event). Selection is side-effect free: the pair is verified only
      // when the user clicks Verify System Build, never on load.
      presentSelectedSystemBuild(selected.tag);
    }
  } catch (err) {
    setupState.release.releases = [];
    setupEls.releaseSelect.disabled = true;
    setReleaseStatus("failed", err.message || String(err));
  }
}

document.querySelectorAll("[data-setup-step]").forEach((button) => {
  button.addEventListener("click", () => setActiveStep(button.dataset.setupStep));
});
const START_OVER_CONFIRM =
  "Restart Guided Setup?\n\n" +
  "This removes the current setup draft, generated configuration, deployment " +
  "plan and setup progress, then returns to the first setup step.\n\n" +
  "It does not change the installed EMS system, live configuration, runtime " +
  "data, containers, volumes or backups.";

function clearFeatureValues() {
  for (const key of Object.keys(featureValues)) delete featureValues[key];
  try {
    window.localStorage.removeItem(CONFIG_FEATURES_STORAGE_KEY);
  } catch (err) {
    /* localStorage may be unavailable; feature values still live in memory. */
  }
}

// Restart the lifecycle a still-active Setup state needs after a refused reset.
// Both pollers clear their own timer and capture the current generation, so this
// can neither duplicate a timer nor revive a request the reset superseded.
function resumeGuidedSetupLifecycle() {
  loadSystemAlignmentStatus();
  const deployment = setupState.deployment;
  if (deployment.status === "running" && deployment.job_id) {
    pollDeploymentJob(deployment.job_id);
  }
  const start = setupState.start;
  if (start.status === "running" && start.job_id) {
    pollStartJob(start.job_id);
  }
  if (setupState.activeStep === "config") renderConfigPreview();
}

// Stop every Guided Setup-owned timer/poll. Unrelated global Admin timers (mDNS
// heartbeat, upgrade/backup polling) are intentionally left running.
function clearGuidedSetupTimers() {
  if (configPreviewTimer) {
    window.clearTimeout(configPreviewTimer);
    configPreviewTimer = null;
  }
  if (deploymentJobTimer) {
    window.clearTimeout(deploymentJobTimer);
    deploymentJobTimer = null;
  }
  if (startJobTimer) {
    window.clearTimeout(startJobTimer);
    startJobTimer = null;
  }
}

// Resets the Guided Setup session, browser and backend. The only request it
// makes is the backend-owned abandon, which drops Setup's own transition,
// generated config and deployment marker; it never calls a deployment,
// container, volume, backup or live-config deletion endpoint, so an already
// installed EMS system, its config/data, containers, volumes and backups are
// left untouched. The prepared-release cache is harmless and is kept.
async function startGuidedSetupOver() {
  if (!window.confirm(START_OVER_CONFIRM)) return;
  if (startOverRunning) return;
  startOverRunning = true;

  // Invalidate any in-flight wizard response and stop all wizard timers first,
  // so nothing repopulates state while the abandon request is in flight.
  guidedSetupGeneration += 1;
  clearGuidedSetupTimers();

  // The backend owns the durable Setup state (pending transition, generated
  // config, deployment marker), so it is abandoned first. Only once that
  // succeeds is the browser state cleared — otherwise the console would look
  // reset while the server still blocks Maintenance on the old workflow.
  try {
    // The workflow must be named exactly: an empty request is never authority
    // over whatever workflow happens to be stored.
    const workflowId = setupWorkflowId || (await fetchOwningSetupWorkflowId());
    const res = await fetch("/api/setup/abandon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        workflowId ? { setup_workflow_id: workflowId } : {}
      ),
    });
    const data = await res.json().catch(() => ({}));
    if (isSetupOperationInProgress(data)) {
      throw new Error(setupOperationInProgressMessage(data));
    }
    if (setupCleanupStateFor(data) !== null) {
      showSetupCleanupIncomplete(data);
      throw new Error(
        data.message || "Some temporary setup files could not be removed."
      );
    }
    if (!res.ok || data.ok !== true) {
      throw new Error(
        data.message || data.error || "The setup state could not be cleared."
      );
    }
  } catch (err) {
    startOverRunning = false;
    showError(
      "Could not reset Guided Setup: " +
        (err.message || String(err)) +
        " Your installed EMS system was not changed."
    );
    setStatus("Guided Setup was not reset.", "is-error");
    // The workflow is still live, so its polling must come back with it.
    resumeGuidedSetupLifecycle();
    return;
  }
  startOverRunning = false;
  // The old workflow is terminal on the backend; only now drop its identity.
  setSetupWorkflowId(null);

  // Discovery session and device caches.
  resetDiscoverySession(discoverySessions.setup);
  keptDevices.clear();
  mdnsDevices.clear();
  ignoredMdnsDevices.clear();
  mqttBrokers.clear();
  autoScannedCidrs.clear();
  lastDiscoverySignature = null;
  scanning = false;
  devicesDiscoveryStarted = false;

  // Config draft, dismissed set and feature/config form values.
  configDraftItems = [];
  try {
    window.localStorage.removeItem(CONFIG_DRAFT_STORAGE_KEY);
  } catch (err) {
    /* localStorage may be unavailable; draft still lives in memory. */
  }
  setConfigBaseline(null);
  showSetupConfigConflict(null);
  configDismissed.clear();
  dismissedSerials.clear();
  try {
    window.localStorage.removeItem(CONFIG_DISMISSED_STORAGE_KEY);
    window.localStorage.removeItem(CONFIG_DISMISSED_SERIALS_STORAGE_KEY);
  } catch (err) {
    /* localStorage may be unavailable; dismissed set still lives in memory. */
  }
  clearFeatureValues();
  // Zendure MQTT selection stores are reset with the rest so Start over leaves
  // no stale MQTT devices/broker behind.
  clearMqttSelection();
  latestConfigPreview = null;

  // Deployment/start draft state (job IDs, prepared/generated flags, progress,
  // success/error/conflict messages) back to their initial shape.
  setupState.devices = createInitialDevicesState();
  setupState.config = createInitialConfigState();
  setupState.deployment = createInitialDeploymentState();
  setupState.start = createInitialStartState();
  clearSetupOperationContext();

  // Continue under a fresh workflow identity (and one-shot intent) so the
  // restarted wizard can mutate again without returning to the start gate.
  try {
    const { result } = await postStartPath("setup_new", false);
    if (result.ok && result.setup_workflow_id) {
      setupIntentId = result.setup_intent_id || setupIntentId;
      setSetupWorkflowId(result.setup_workflow_id);
      freshSetupConfirmationRequired = false;
    }
  } catch (err) {
    /* The start gate re-issues identity on the next explicit entry. */
  }

  showError("");
  showSetupNavError("");
  setActiveStep("release");
  updateBusy();
  renderSetupDiscoveryProgress();
  renderAggregate();
  renderConfigDraft();
  renderConfigAvailable();
  renderConfigPreview();
  renderDeployment();
  renderStart();
  loadSystemAlignmentStatus();
  setStatus("Guided Setup reset. Your installed EMS system was not changed.", "is-done");
}

if (setupEls.startOver) {
  setupEls.startOver.addEventListener("click", startGuidedSetupOver);
}
if (setupEls.back) setupEls.back.addEventListener("click", () => goToStep(-1));
if (setupEls.next) {
  // Step 1 (release) has its own Continue button, so the shared nav Next never
  // fires there; it only commits Config or advances the later steps.
  setupEls.next.addEventListener("click", () => {
    if (setupState.activeStep === "config") {
      continueFromConfig();
    } else {
      goToStep(1);
    }
  });
}
if (setupEls.releaseSelect) {
  setupEls.releaseSelect.addEventListener("change", onReleaseSelectChange);
}
if (setupEls.releaseReload) {
  setupEls.releaseReload.addEventListener("click", () => loadReleases());
}
if (setupEls.deploymentPrepare) {
  setupEls.deploymentPrepare.addEventListener("click", () => prepareDeployment(false));
}
if (setupEls.deploymentOverwriteConfirm) {
  setupEls.deploymentOverwriteConfirm.addEventListener("click", () =>
    prepareDeployment(true)
  );
}
if (setupEls.deploymentExistingReplace) {
  setupEls.deploymentExistingReplace.addEventListener("click", () =>
    prepareDeployment(true)
  );
}
if (setupEls.deploymentDockerRecheck) {
  setupEls.deploymentDockerRecheck.addEventListener("click", () => loadDeploymentPlan());
}
if (setupEls.startButton) {
  setupEls.startButton.addEventListener("click", startDeployment);
}
if (setupEls.startRecheck) {
  setupEls.startRecheck.addEventListener("click", refreshStartStatus);
}
if (setupEls.startConflictResolve) {
  setupEls.startConflictResolve.addEventListener("click", resolveContainerConflict);
}
if (setupEls.startPermissionRepair) {
  setupEls.startPermissionRepair.addEventListener("click", repairWorkspacePermissions);
}

function initSetupWizard() {
  setupInitialized = true;
  let saved = null;
  try {
    saved = window.localStorage.getItem(SETUP_STEP_STORAGE_KEY);
  } catch (err) {
    saved = null;
  }
  setActiveStep(saved || "release");
  initFeatureSettings();
  loadReleases();
  loadSetupCatalog();
  refreshDeploymentStatus();
  restoreSetupWorkflowFromServer();
}

// After a reload or an Admin restart the backend record is the one workflow
// interpretation: adopt the active identity (or drop a stale local one) and
// re-establish the exact preview for the restored draft.
async function restoreSetupWorkflowFromServer() {
  const workflow = await fetchSetupWorkflowSnapshot();
  if (workflow && setupCleanupBlocks(workflow)) {
    // A reload must not look like a clean slate: the workflow is terminal but
    // still owns files, so keep its id (the retry needs it) and say so.
    setupWorkflowId = workflow.workflow_id;
    setupConfigPreviewId = null;
    saveSetupWorkflowState();
    showSetupCleanupIncomplete(workflow.cleanup);
    return;
  }
  showSetupCleanupIncomplete(null);
  const current =
    workflow && workflow.status === "active" ? workflow.workflow_id : null;
  const changed = current !== setupWorkflowId;
  if (changed) setSetupWorkflowId(current);
  if (changed && setupState.activeStep === "config") {
    renderConfigPreview();
  }
}

// --- start gate ----------------------------------------------------------
// --- maintenance overview (read-only) ------------------------------------
// Manage-existing routes here: a read-only snapshot of the installed layout,
// container status and versions. Compact collapsible cards show a one-line
// summary in the header and keep detailed rows behind an expand. Every dynamic
// value is written via textContent (or escapeHtml for the warnings list); no
// mutating action is exposed yet.

const maintenanceEls = {
  warnings: document.getElementById("maintenance-warnings"),
  systemStatus: document.getElementById("maintenance-system-status"),
  layoutSummary: document.getElementById("maintenance-layout-summary"),
  containersSummary: document.getElementById("maintenance-containers-summary"),
  versionsSummary: document.getElementById("maintenance-versions-summary"),
  config: document.getElementById("maintenance-config"),
  configPath: document.getElementById("maintenance-config-path"),
  data: document.getElementById("maintenance-data"),
  dataPath: document.getElementById("maintenance-data-path"),
  compose: document.getElementById("maintenance-compose"),
  composePath: document.getElementById("maintenance-compose-path"),
  state: document.getElementById("maintenance-state"),
  stateMessage: document.getElementById("maintenance-state-message"),
  ems: document.getElementById("maintenance-ems"),
  emsDetail: document.getElementById("maintenance-ems-detail"),
  emsName: document.getElementById("maintenance-ems-name"),
  influx: document.getElementById("maintenance-influx"),
  influxDetail: document.getElementById("maintenance-influx-detail"),
  influxName: document.getElementById("maintenance-influx-name"),
  docker: document.getElementById("maintenance-docker"),
  dockerServer: document.getElementById("maintenance-docker-server"),
  dockerNote: document.getElementById("maintenance-docker-note"),
  adminImage: document.getElementById("maintenance-admin-image"),
  emsImage: document.getElementById("maintenance-ems-image"),
  influxImage: document.getElementById("maintenance-influx-image"),
  dashboard: document.getElementById("maintenance-dashboard"),
  dashboardLink: document.getElementById("maintenance-dashboard-link"),
  refresh: document.getElementById("maintenance-refresh"),
  runtimeEmsDesired: document.getElementById("maintenance-runtime-ems-desired"),
  runtimeInfluxDesired: document.getElementById("maintenance-runtime-influx-desired"),
  runtimeActionSummary: document.getElementById("maintenance-runtime-action-summary"),
  runtimeContainersSync: document.getElementById("maintenance-runtime-containers-sync"),
  runtimeContainersRecheck: document.getElementById("maintenance-runtime-containers-recheck"),
  runtimeDiagnostics: document.getElementById("maintenance-runtime-diagnostics"),
  runtimeContainersStatus: document.getElementById("maintenance-runtime-containers-status"),
  zendureMqttSummary: document.getElementById("maintenance-zendure-mqtt-summary"),
  zendureMqttState: document.getElementById("maintenance-zendure-mqtt-state"),
  zendureMqttEndpoint: document.getElementById("maintenance-zendure-mqtt-endpoint"),
  zendureMqttDevices: document.getElementById("maintenance-zendure-mqtt-devices"),
  zendureMqttInvalid: document.getElementById("maintenance-zendure-mqtt-invalid"),
  zendureMqttStale: document.getElementById("maintenance-zendure-mqtt-stale"),
  zendureMqttSource: document.getElementById("maintenance-zendure-mqtt-source"),
  zendureMqttMessage: document.getElementById("maintenance-zendure-mqtt-message"),
  zendureMqttFallback: document.getElementById("maintenance-zendure-mqtt-fallback"),
  zendureMqttEmpty: document.getElementById("maintenance-zendure-mqtt-empty"),
  zendureMqttList: document.getElementById("maintenance-zendure-mqtt-list"),
  zendureMqttBrokers: document.getElementById("maintenance-zendure-mqtt-brokers"),
};

const MAINTENANCE_HEALTHY_STATES = ["standard_install", "admin_prepared_install"];

function setMaintenanceFact(el, text, tone) {
  if (!el) return;
  el.textContent = text;
  if (tone) el.dataset.tone = tone;
  else delete el.dataset.tone;
}

// Card tone drives the collapsed row's left accent and status badge.
function setMaintenanceCardTone(cardId, tone) {
  const card = document.getElementById(cardId);
  if (!card) return;
  if (tone) card.dataset.tone = tone;
  else delete card.dataset.tone;
}

function maintenancePathFact(entry) {
  const exists = Boolean(entry && entry.exists);
  return { text: exists ? "found" : "missing", tone: exists ? "ok" : "warn" };
}

function renderMaintenanceWarnings(warnings) {
  const el = maintenanceEls.warnings;
  if (!el) return;
  const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
  if (!list.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = list.map((note) => "<span>" + escapeHtml(note) + "</span>").join("<br>");
}

function renderMaintenanceImage(el, image) {
  setMaintenanceFact(el, image || "unavailable", image ? null : "muted");
}

function renderMaintenance(data) {
  const paths = data.paths || {};
  const config = maintenancePathFact(paths.config);
  setMaintenanceFact(maintenanceEls.config, config.text, config.tone);
  setMaintenanceFact(maintenanceEls.configPath, (paths.config && paths.config.path) || "—", "muted");
  const dataDir = maintenancePathFact(paths.data);
  setMaintenanceFact(maintenanceEls.data, dataDir.text, dataDir.tone);
  setMaintenanceFact(maintenanceEls.dataPath, (paths.data && paths.data.path) || "—", "muted");
  const compose = maintenancePathFact(paths.compose);
  setMaintenanceFact(maintenanceEls.compose, compose.text, compose.tone);
  setMaintenanceFact(maintenanceEls.composePath, (paths.compose && paths.compose.path) || "—", "muted");

  const state = data.install_state || {};
  const healthy = MAINTENANCE_HEALTHY_STATES.includes(state.state);
  setMaintenanceFact(
    maintenanceEls.state,
    state.label || state.state || "unknown",
    healthy ? "ok" : "warn"
  );
  if (maintenanceEls.stateMessage) {
    maintenanceEls.stateMessage.textContent = state.message || "";
  }

  const docker = data.docker || {};
  const containers = data.containers || {};
  // The main EMS/InfluxDB status labels (and the collapsed summary) are owned by
  // the container plan, which knows the config feature-state; the overview only
  // fills the raw docker container name here.
  setMaintenanceFact(
    maintenanceEls.emsName,
    (containers.ems && containers.ems.name) || "—",
    "muted"
  );
  setMaintenanceFact(
    maintenanceEls.influxName,
    (containers.influxdb && containers.influxdb.name) || "—",
    "muted"
  );
  setMaintenanceFact(
    maintenanceEls.docker,
    docker.available ? "ok" : "unavailable",
    docker.available ? "ok" : "warn"
  );
  setMaintenanceFact(
    maintenanceEls.dockerServer,
    docker.server_version || "unknown",
    "muted"
  );
  if (maintenanceEls.dockerNote) {
    maintenanceEls.dockerNote.textContent = docker.available ? "" : docker.error || "";
  }

  const components = data.components || {};
  renderMaintenanceImage(
    maintenanceEls.adminImage,
    components.admin && components.admin.image
  );
  renderMaintenanceImage(
    maintenanceEls.emsImage,
    (components.ems && components.ems.image) || (containers.ems && containers.ems.image)
  );
  renderMaintenanceImage(
    maintenanceEls.influxImage,
    containers.influxdb && containers.influxdb.image
  );

  const dashboard = data.links && data.links.dashboard_url;
  renderMaintenanceDashboard(dashboard);
  renderMaintenanceSummaries(data);
  renderMaintenanceWarnings(data.warnings);
}

// The dashboard link href is set through the DOM property (never innerHTML) so
// the config-derived URL cannot inject markup.
function renderMaintenanceDashboard(url) {
  const link = maintenanceEls.dashboardLink;
  const label = maintenanceEls.dashboard;
  if (link) {
    if (url) {
      link.href = url;
      link.hidden = false;
    } else {
      link.removeAttribute("href");
      link.hidden = true;
    }
  }
  if (label) {
    label.hidden = Boolean(url);
    setMaintenanceFact(label, url ? "" : "unavailable", "muted");
  }
}

// Collapsed headers must stay informative: each summary condenses the card's key
// facts into one line so the state is readable without expanding.
function renderMaintenanceSummaries(data) {
  const state = data.install_state || {};
  const healthy = MAINTENANCE_HEALTHY_STATES.includes(state.state);
  const paths = data.paths || {};
  const docker = data.docker || {};
  const containers = data.containers || {};

  const present = ["config", "data", "compose"].filter(
    (key) => paths[key] && paths[key].exists
  );
  const layoutOk = present.length === 3;
  const layoutText = layoutOk
    ? "OK · config/data/compose found"
    : (state.label || "Partial") + " · " + present.length + "/3 paths found";
  setMaintenanceFact(maintenanceEls.layoutSummary, layoutText, layoutOk ? "ok" : "warn");
  setMaintenanceCardTone("maintenance-layout", layoutOk ? "ok" : "warn");

  const emsRunning = !!(containers.ems && containers.ems.running);
  // The Runtime containers summary + tone are owned by renderRuntimeServiceStatus
  // (driven by the plan, which knows the config feature-state).

  const emsTag = containers.ems && containers.ems.tag;
  const components = data.components || {};
  const adminTag = components.admin && components.admin.tag;
  const dashboard = data.links && data.links.dashboard_url;
  const versionsText =
    (adminTag ? "Admin " + adminTag : "Admin version unknown") +
    " · " +
    (emsTag ? "EMS " + emsTag : "EMS version unknown") +
    " · " +
    (dashboard ? "Dashboard " + dashboard : "Dashboard unavailable");
  setMaintenanceFact(maintenanceEls.versionsSummary, versionsText, dashboard ? "info" : "warn");
  setMaintenanceCardTone("maintenance-versions", dashboard ? "info" : "warn");

  setMaintenanceCardTone("maintenance-diagnostics", "info");

  const systemText =
    (state.label || state.state || "Unknown") +
    " · " +
    (docker.available
      ? emsRunning
        ? "EMS running"
        : "EMS not running"
      : "Docker unavailable") +
    (emsTag ? " · " + emsTag : "");
  setMaintenanceFact(maintenanceEls.systemStatus, systemText, healthy ? "ok" : "warn");
}

function renderMaintenanceError() {
  [
    maintenanceEls.config,
    maintenanceEls.configPath,
    maintenanceEls.data,
    maintenanceEls.dataPath,
    maintenanceEls.compose,
    maintenanceEls.composePath,
    maintenanceEls.state,
    maintenanceEls.ems,
    maintenanceEls.emsDetail,
    maintenanceEls.emsName,
    maintenanceEls.influx,
    maintenanceEls.influxDetail,
    maintenanceEls.influxName,
    maintenanceEls.docker,
    maintenanceEls.dockerServer,
    maintenanceEls.emsImage,
    maintenanceEls.influxImage,
    maintenanceEls.dashboard,
    maintenanceEls.layoutSummary,
    maintenanceEls.containersSummary,
    maintenanceEls.versionsSummary,
    maintenanceEls.systemStatus,
  ].forEach((el) => setMaintenanceFact(el, "unavailable", "muted"));
  renderMaintenanceDashboard(null);
  if (maintenanceEls.stateMessage) maintenanceEls.stateMessage.textContent = "";
  renderMaintenanceWarnings([
    "Could not load the Maintenance overview. The Admin server may be unavailable.",
  ]);
}

function toggleMaintenanceCard(id) {
  const section = document.getElementById(id);
  if (!section) return;
  const open = section.getAttribute("data-open") !== "true";
  section.setAttribute("data-open", open ? "true" : "false");
  const body = document.getElementById(id + "-body");
  if (body) body.hidden = !open;
  const button = section.querySelector("[data-maintenance-toggle]");
  if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
  const caret = section.querySelector(".maintenance-caret");
  if (caret) caret.textContent = open ? "▾" : "▸";
}

document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-maintenance-toggle]");
  if (!toggle) return;
  toggleMaintenanceCard(toggle.getAttribute("data-maintenance-toggle"));
});

let maintenanceLoading = false;

async function loadMaintenanceOverview(options = {}) {
  const refreshConfig = options.refreshConfig !== false;
  const refreshContainerPlan = options.refreshContainerPlan !== false;
  const showPostApply = options.showPostApply === true;

  if (maintenanceLoading) return;
  maintenanceLoading = true;
  try {
    const resp = await fetch("/api/admin/maintenance/overview");
    if (!resp.ok) throw new Error("maintenance overview request failed");
    renderMaintenance(await resp.json());
  } catch (err) {
    renderMaintenanceError();
  } finally {
    maintenanceLoading = false;
  }

  // Awaited follow-ups: an unawaited config reload would re-run
  // renderMaintenanceConfig later and hide a just-revealed post-apply panel.
  if (refreshConfig) {
    await loadMaintenanceConfig();
  }
  if (refreshContainerPlan) {
    await loadMaintenanceContainerPlan({ showPostApply });
  }
  await loadZendureMqttRuntimeStatus();
  await loadMqttMigrationReview();
}

if (maintenanceEls.refresh) {
  maintenanceEls.refresh.addEventListener("click", loadMaintenanceOverview);
}

// --- Zendure MQTT telemetry runtime status (read-only) -------------------
// EMS/Core owns the status; Admin only fetches and renders it. There is no
// publish/write/control action here. Every dynamic value is escaped or written
// via textContent, and secret-looking fields never reach this view.

const ZENDURE_MQTT_STATUS_LABELS = {
  inactive: "Inactive",
  configured: "Configured",
  unavailable: "Unavailable",
};

const ZENDURE_MQTT_STATUS_TONES = {
  configured: "ok",
  inactive: "muted",
  unavailable: "warn",
};

const ZENDURE_MQTT_SOURCE_LABELS = {
  live_runtime: "Live EMS runtime",
  offline_config: "Offline config check",
};

function renderZendureMqttDeviceCard(device) {
  const status = String(device.status || "unseen");
  const name = escapeHtml(String(device.name || "Device"));
  const metricCount = Number(device.metric_count || 0);
  const metrics = Array.isArray(device.metrics) ? device.metrics : [];
  const capabilities = Array.isArray(device.capabilities) ? device.capabilities : [];
  const issues = Array.isArray(device.issues) ? device.issues : [];

  const facts = [];
  if (device.broker_ref) {
    facts.push(zmqttFact("Broker", device.broker_ref));
  }
  if (device.source) {
    facts.push(zmqttFact("Source", device.source));
  }
  facts.push(zmqttFact("Topic family", device.topic_family || "—"));
  if (device.age_seconds != null) {
    facts.push(zmqttFact("Age", Math.round(Number(device.age_seconds)) + "s"));
  } else if (device.last_seen) {
    facts.push(zmqttFact("Last seen", device.last_seen));
  }
  facts.push(zmqttFact("Metrics", String(metricCount)));

  let extra = "";
  if (metrics.length) {
    extra +=
      '<div class="future-note">Metrics: ' +
      escapeHtml(metrics.join(", ")) +
      "</div>";
  }
  if (capabilities.length) {
    extra +=
      '<div class="future-note">Capabilities: ' +
      escapeHtml(capabilities.join(", ")) +
      "</div>";
  }
  if (issues.length) {
    extra +=
      '<div class="future-note">Issues: ' +
      escapeHtml(issues.join(", ")) +
      "</div>";
  }

  return (
    '<article class="device-card">' +
    '<div class="device-card-head">' +
    '<span class="device-name">' + name + "</span>" +
    '<span class="pill" data-status="' + escapeHtml(status) + '">' +
    escapeHtml(status) +
    "</span>" +
    "</div>" +
    '<div class="device-facts">' + facts.join("") + "</div>" +
    extra +
    '<div class="device-card-foot">' +
    '<span class="future-note">Telemetry view only</span>' +
    "</div>" +
    "</article>"
  );
}

function zmqttFact(key, value) {
  return (
    '<div class="device-fact">' +
    '<span class="k">' + escapeHtml(String(key)) + "</span>" +
    '<span class="v">' + escapeHtml(String(value)) + "</span>" +
    "</div>"
  );
}

// Endpoint host:port only — never any broker credential. Renders gracefully
// whether or not the runtime supplied a per-broker view.
function renderZendureMqttBrokerCard(broker) {
  const ref = escapeHtml(String(broker.broker_ref || "broker"));
  const running = broker.running === true;
  const connected = broker.connected === true;
  let status = "configured";
  if (!broker.enabled) status = "disabled";
  else if (connected) status = "connected";
  else if (running) status = "running";

  const facts = [];
  if (broker.source) facts.push(zmqttFact("Source", broker.source));
  facts.push(zmqttFact("Endpoint", broker.endpoint || "—"));
  facts.push(zmqttFact("Devices", String(Number(broker.device_count || 0))));
  if (broker.last_error) {
    facts.push(zmqttFact("Last error", broker.last_error));
  }

  const issue = zmqttBrokerIssueCopy(broker.issue);
  const issueNote = issue
    ? '<div class="future-note" data-tone="warn">' +
      escapeHtml(issue.title) + ": " + escapeHtml(issue.detail) +
      "</div>"
    : "";

  return (
    '<article class="device-card">' +
    '<div class="device-card-head">' +
    '<span class="device-name">' + ref + "</span>" +
    '<span class="pill" data-status="' + escapeHtml(status) + '">' +
    escapeHtml(status) +
    "</span>" +
    "</div>" +
    '<div class="device-facts">' + facts.join("") + "</div>" +
    issueNote +
    "</article>"
  );
}

// Short, actionable copy for a sanitized broker-profile issue code. Reuses the
// existing status-note style; never surfaces hosts or credentials.
function zmqttBrokerIssueCopy(code) {
  switch (code) {
    case "broker_profile_disabled":
      return {
        title: "Broker profile disabled",
        detail: "Enable the broker before applying this MQTT device.",
      };
    case "broker_profile_incomplete":
      return {
        title: "Broker profile incomplete",
        detail: "Configure the broker before applying this MQTT device.",
      };
    case "broker_auth_missing":
      return {
        title: "Broker runtime credential missing",
        detail:
          "This broker has no usable runtime credential. Zendure Cloud discovery does not provision it automatically.",
      };
    default:
      return null;
  }
}

function renderZendureMqttRuntimeStatus(data) {
  const view = data && typeof data === "object" ? data : {};
  const state = String(view.runtime_state || "unavailable");
  // An unknown state (version skew: an already-open page rendering a newer
  // backend state) degrades neutrally — echo the state name in muted tone
  // instead of lighting the card up as a warning.
  const tone = ZENDURE_MQTT_STATUS_TONES[state] || "muted";
  const label =
    ZENDURE_MQTT_STATUS_LABELS[state] ||
    state.charAt(0).toUpperCase() + state.slice(1);

  setMaintenanceFact(maintenanceEls.zendureMqttState, label, tone);
  setMaintenanceFact(
    maintenanceEls.zendureMqttEndpoint,
    view.endpoint || "—",
    "muted"
  );
  setMaintenanceFact(
    maintenanceEls.zendureMqttDevices,
    String(view.configured_device_count || 0),
    "info"
  );
  const invalidCount = Number(view.invalid_device_count || 0);
  setMaintenanceFact(
    maintenanceEls.zendureMqttInvalid,
    String(invalidCount),
    invalidCount > 0 ? "warn" : "muted"
  );
  setMaintenanceFact(
    maintenanceEls.zendureMqttStale,
    view.stale_after_seconds != null ? view.stale_after_seconds + "s" : "—",
    "muted"
  );
  const live = view.live_available === true;
  const sourceLabel =
    ZENDURE_MQTT_SOURCE_LABELS[String(view.source)] ||
    ZENDURE_MQTT_SOURCE_LABELS.offline_config;
  if (maintenanceEls.zendureMqttSource) {
    maintenanceEls.zendureMqttSource.textContent =
      state === "unavailable" ? "" : "Source: " + sourceLabel;
  }
  if (maintenanceEls.zendureMqttMessage) {
    maintenanceEls.zendureMqttMessage.textContent = view.message || "";
  }
  if (maintenanceEls.zendureMqttFallback) {
    if (live || state === "unavailable") {
      maintenanceEls.zendureMqttFallback.textContent = "";
      maintenanceEls.zendureMqttFallback.hidden = true;
    } else {
      maintenanceEls.zendureMqttFallback.textContent =
        "Live status unavailable; showing config-derived telemetry setup.";
      maintenanceEls.zendureMqttFallback.hidden = false;
    }
  }
  setMaintenanceFact(maintenanceEls.zendureMqttSummary, label, tone);
  setMaintenanceCardTone("maintenance-zendure-mqtt", tone);

  const brokers = Array.isArray(view.brokers) ? view.brokers : [];
  const brokerList = maintenanceEls.zendureMqttBrokers;
  if (brokerList) {
    if (brokers.length) {
      brokerList.innerHTML = brokers.map(renderZendureMqttBrokerCard).join("");
      brokerList.hidden = false;
    } else {
      brokerList.innerHTML = "";
      brokerList.hidden = true;
    }
  }

  const devices = Array.isArray(view.devices) ? view.devices : [];
  const list = maintenanceEls.zendureMqttList;
  const empty = maintenanceEls.zendureMqttEmpty;
  if (list) {
    if (devices.length) {
      list.innerHTML = devices.map(renderZendureMqttDeviceCard).join("");
      list.hidden = false;
    } else {
      list.innerHTML = "";
      list.hidden = true;
    }
  }
  if (empty) {
    empty.hidden = devices.length > 0;
  }
}

async function loadZendureMqttRuntimeStatus() {
  try {
    const resp = await fetch("/api/admin/maintenance/zendure-mqtt/runtime-status");
    if (!resp.ok) throw new Error("zendure mqtt runtime status request failed");
    renderZendureMqttRuntimeStatus(await resp.json());
  } catch (err) {
    renderZendureMqttRuntimeStatus({
      runtime_state: "unavailable",
      message: "Could not load Zendure MQTT telemetry status. The Admin server may be unavailable.",
      devices: [],
    });
  }
}

// --- Zendure MQTT migration ----------------------------------------------
// EMS/Core owns the plan, validation and apply semantics. This compact
// Maintenance workflow only renders the secret-free review and submits the
// confirmed revision through the authenticated request wrapper.

const mqttMigrationEls = {
  summary: document.getElementById("maintenance-mqtt-migration-summary"),
  required: document.getElementById("maintenance-mqtt-migration-required"),
  affected: document.getElementById("maintenance-mqtt-migration-affected"),
  disabled: document.getElementById("maintenance-mqtt-migration-disabled"),
  validation: document.getElementById("maintenance-mqtt-migration-validation"),
  devices: document.getElementById("maintenance-mqtt-migration-devices"),
  warnings: document.getElementById("maintenance-mqtt-migration-warnings"),
  backup: document.getElementById("maintenance-mqtt-migration-backup"),
  apply: document.getElementById("maintenance-mqtt-migration-apply"),
  refresh: document.getElementById("maintenance-mqtt-migration-refresh"),
  status: document.getElementById("maintenance-mqtt-migration-status"),
};

const mqttMigrationState = {
  revision: null,
  review: null,
  applying: false,
};

function setMqttMigrationStage(stage, state) {
  const node = document.querySelector(
    '[data-mqtt-migration-stage="' + stage + '"]'
  );
  if (!node) return;
  if (state) node.dataset.state = state;
  else delete node.dataset.state;
}

function resetMqttMigrationStages() {
  ["review", "backup", "apply", "validate"].forEach((stage) =>
    setMqttMigrationStage(stage, null)
  );
}

function mqttMigrationDeviceRow(change) {
  const row = document.createElement("article");
  row.className = "mqtt-migration-device";
  const title = document.createElement("strong");
  title.textContent = change.device || change.device_id || "Zendure MQTT device";
  const model = document.createElement("span");
  model.textContent = change.hardware_profile
    ? "Exact model: " + change.hardware_profile
    : "Exact model unresolved";
  const control = document.createElement("span");
  control.textContent = change.disables_control
    ? "Control disabled; telemetry kept"
    : "Control kept with Core-derived write protocol";
  const decision = document.createElement("span");
  decision.textContent = change.message || "Migration decision available.";
  row.append(title, model, control, decision);
  return row;
}

function renderMqttMigrationReview(data) {
  resetMqttMigrationStages();
  mqttMigrationState.revision = null;
  mqttMigrationState.review = null;
  if (!data || data.status !== "ok") {
    setMaintenanceFact(mqttMigrationEls.summary, "Review unavailable", "warn");
    setMaintenanceFact(mqttMigrationEls.required, "unknown", "warn");
    setMaintenanceFact(mqttMigrationEls.affected, "—", "muted");
    setMaintenanceFact(mqttMigrationEls.disabled, "—", "muted");
    setMaintenanceFact(mqttMigrationEls.validation, "not run", "muted");
    if (mqttMigrationEls.devices) {
      mqttMigrationEls.devices.replaceChildren();
      mqttMigrationEls.devices.hidden = true;
    }
    if (mqttMigrationEls.warnings) {
      mqttMigrationEls.warnings.textContent = data && data.message
        ? data.message
        : "Could not load the migration review.";
    }
    if (mqttMigrationEls.apply) mqttMigrationEls.apply.disabled = true;
    setMqttMigrationStage("review", "failed");
    setMaintenanceCardTone("maintenance-mqtt-migration", "warn");
    return;
  }

  const review = data.review || {};
  const changes = Array.isArray(review.changes) ? review.changes : [];
  const disabling = changes.filter((change) => change.disables_control);
  const required = review.needs_migration === true;
  mqttMigrationState.revision = data.revision || null;
  mqttMigrationState.review = review;
  setMqttMigrationStage("review", "done");
  setMaintenanceFact(
    mqttMigrationEls.summary,
    required ? changes.length + " device(s) need migration" : "No migration required",
    required ? "action" : "ok"
  );
  setMaintenanceFact(mqttMigrationEls.required, required ? "required" : "not required", required ? "warn" : "ok");
  setMaintenanceFact(mqttMigrationEls.affected, String(changes.length), changes.length ? "info" : "muted");
  setMaintenanceFact(mqttMigrationEls.disabled, String(disabling.length), disabling.length ? "warn" : "muted");
  setMaintenanceFact(
    mqttMigrationEls.validation,
    review.final_valid ? "valid after migration" : "validation failed",
    review.final_valid ? "ok" : "warn"
  );
  if (mqttMigrationEls.devices) {
    mqttMigrationEls.devices.replaceChildren();
    changes.forEach((change) =>
      mqttMigrationEls.devices.appendChild(mqttMigrationDeviceRow(change))
    );
    mqttMigrationEls.devices.hidden = changes.length === 0;
  }
  if (mqttMigrationEls.warnings) {
    const warnings = disabling.map((change) => change.message).filter(Boolean);
    mqttMigrationEls.warnings.textContent = warnings.join(" · ");
  }
  if (mqttMigrationEls.apply) {
    mqttMigrationEls.apply.disabled = !required || !review.final_valid || !data.revision;
  }
  if (mqttMigrationEls.status) {
    mqttMigrationEls.status.textContent = required
      ? "Review each model decision, keep backup enabled, then apply."
      : "The current config already satisfies the MQTT model contract.";
  }
  if (!required) {
    setMqttMigrationStage("backup", "done");
    setMqttMigrationStage("apply", "done");
    setMqttMigrationStage("validate", review.final_valid ? "done" : "failed");
  }
  setMaintenanceCardTone("maintenance-mqtt-migration", required ? "action" : "ok");
}

async function loadMqttMigrationReview() {
  setMqttMigrationStage("review", "running");
  try {
    const resp = await fetch(
      "/api/admin/maintenance/zendure-mqtt/migration-review"
    );
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.message || "Migration review failed.");
    renderMqttMigrationReview(data);
    return data;
  } catch (err) {
    renderMqttMigrationReview({ status: "error", message: err.message || String(err) });
    return null;
  }
}

async function applyMqttMigration() {
  if (mqttMigrationState.applying || !mqttMigrationState.revision) return;
  const review = mqttMigrationState.review || {};
  const changes = Array.isArray(review.changes) ? review.changes : [];
  const disabled = changes.filter((change) => change.disables_control).length;
  const backup = !mqttMigrationEls.backup || mqttMigrationEls.backup.checked;
  const confirmation =
    "Apply the reviewed Zendure MQTT migration? " +
    changes.length + " device(s) will change; " + disabled +
    " will lose control; backup " + (backup ? "enabled." : "disabled.");
  if (!window.confirm(confirmation)) return;

  mqttMigrationState.applying = true;
  if (mqttMigrationEls.apply) mqttMigrationEls.apply.disabled = true;
  if (mqttMigrationEls.status) {
    mqttMigrationEls.status.textContent = backup
      ? "Creating backup before the atomic migration write…"
      : "Applying the reviewed migration without a backup…";
  }
  setMqttMigrationStage("backup", backup ? "running" : "done");
  setMqttMigrationStage("apply", backup ? null : "running");
  setMqttMigrationStage("validate", null);
  try {
    const resp = await fetch("/api/admin/maintenance/zendure-mqtt/migration-apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        revision: mqttMigrationState.revision,
        confirm: true,
        backup,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      const error = new Error(data.message || data.error || "Migration apply failed.");
      error.status = data.status || "error";
      throw error;
    }
    setMqttMigrationStage("backup", "done");
    setMqttMigrationStage("apply", "done");
    setMqttMigrationStage("validate", "running");
    if (mqttMigrationEls.status) {
      mqttMigrationEls.status.textContent = data.changed === false
        ? "Already migrated; no config write was needed. Refreshing validation…"
        : "Migration applied. Refreshing config, runtime and control readiness…";
    }
    await loadMaintenanceConfig();
    await loadZendureMqttRuntimeStatus();
    await loadMqttMigrationReview();
    setMqttMigrationStage("validate", "done");
  } catch (err) {
    const message = err.message || String(err);
    if (message.toLowerCase().includes("backup")) {
      setMqttMigrationStage("backup", "failed");
    } else {
      setMqttMigrationStage("backup", "done");
      setMqttMigrationStage("apply", "failed");
    }
    if (mqttMigrationEls.status) {
      mqttMigrationEls.status.textContent = err.status === "conflict"
        ? "The review is stale. Refresh and confirm the new plan."
        : message;
    }
    if (err.status === "conflict") {
      await loadMqttMigrationReview();
      if (mqttMigrationEls.status) {
        mqttMigrationEls.status.textContent =
          "The review is stale. Review the refreshed plan and confirm it again.";
      }
    }
  } finally {
    mqttMigrationState.applying = false;
    if (mqttMigrationEls.apply && mqttMigrationState.review) {
      mqttMigrationEls.apply.disabled = !mqttMigrationState.review.needs_migration;
    }
  }
}

if (mqttMigrationEls.apply) {
  mqttMigrationEls.apply.addEventListener("click", applyMqttMigration);
}
if (mqttMigrationEls.refresh) {
  mqttMigrationEls.refresh.addEventListener("click", loadMqttMigrationReview);
}

// --- Guided upgrade planning + execute -----------------------------------
// Reads the maintenance overview for the current version, lists releases, and
// (using the existing setup prepare flow) can download a target release.
// "Plan upgrade" only renders the summary; the guarded "Upgrade EMS" button is
// the sole mutating action and POSTs the confirmed executor, which is enabled
// only once a target is selected, prepared, and planned. Every dynamic value is
// escaped before it reaches the DOM.

const upgradeEls = {
  currentVersion: document.getElementById("upgrade-current-version"),
  currentDetail: document.getElementById("upgrade-current-detail"),
  form: document.getElementById("upgrade-release-form"),
  select: document.getElementById("upgrade-release-select"),
  badges: document.getElementById("upgrade-release-badges"),
  prepareBtn: document.getElementById("upgrade-prepare-btn"),
  reload: document.getElementById("upgrade-release-reload"),
  releaseStatus: document.getElementById("upgrade-release-status"),
  releaseError: document.getElementById("upgrade-release-error"),
  options: Array.from(document.querySelectorAll("[data-upgrade-option]")),
  validationCard: document.getElementById("upgrade-validation-card"),
  validationState: document.getElementById("upgrade-validation-state"),
  validationList: document.getElementById("upgrade-validation"),
  factCurrent: document.getElementById("upgrade-fact-current"),
  factTarget: document.getElementById("upgrade-fact-target"),
  planBtn: document.getElementById("upgrade-plan-btn"),
  executeBtn: document.getElementById("upgrade-execute-btn"),
};

// Plan-summary wording for each option, keyed by its data-upgrade-option value.
const UPGRADE_OPTIONS = [
  { key: "backup", plan: "Create a pre-upgrade backup" },
  { key: "config_check", plan: "Check config against the target template" },
  { key: "config_add_keys", plan: "Add missing config keys" },
  { key: "config_comments", plan: "Refresh config comments / metadata" },
  { key: "pull_image", plan: "Pull the target Docker image" },
  { key: "recreate", plan: "Recreate the EMS container" },
  { key: "diagnostics", plan: "Run diagnostics after the upgrade" },
];

const UPGRADE_RELEASE_STATUS_TEXT = {
  idle: "Select a System Build, then verify it.",
  loading: "Loading System Builds…",
  preparing: "Downloading and verifying the Admin and EMS images…",
  ready: "System Build verified.",
  failed: "System Build verification unavailable.",
};

const upgradeState = {
  current: { tag: null, image: null, state: null },
  runningAdmin: { tag: null, image: null },
  releases: [],
  selected: null,
  prepared: false,
  preparedTag: null,
  // The verified pair's selection fingerprint (tag:channel:revision:build_id:
  // admin_digest:ems_digest). The plan is bound to it, so a changed digest /
  // build id / revision / channel invalidates a previous verification.
  preparedFingerprint: null,
  // The Admin instance id the verification was obtained from. Same-session
  // navigation may restore the verification only while this still matches the
  // live Admin; a replaced Admin clears it. This is UX state only — the server
  // re-enforces the fingerprint at execute.
  preparedAdminInstanceId: null,
  preparedReleaseIdentity: null,
  validation: null,
  status: "idle",
  error: null,
  planned: false,
  // The fingerprint the current plan is bound to. A plan is executable only
  // while it still matches the verified preparedFingerprint.
  plannedFingerprint: null,
  planning: false,
  planGeneration: 0,
  completed: false,
  loading: false,
  loadingPromise: null,
  alignmentTransition: null,
  migrationReview: null,
  migrationRevision: null,
  running: false,
  // Monotonic epoch: only the newest verification's response is applied, so a
  // slow earlier response can never verify or paint a newer target selection.
  validationGeneration: 0,
};

// A selected release is a development build when the catalogue marks its channel
// or its tag is an immutable dev tag (matches the Guided Setup classification).
function upgradeSelectedIsDevelopment() {
  const release = upgradeSelectedRelease();
  if (release && release.channel === "development") return true;
  return isImmutableDevelopmentBuildTag(upgradeState.selected);
}

// Selecting a development build is itself the acknowledgement (mirroring Guided
// Setup), so there is no separate checkbox gate: dev risk is always satisfied
// and the acknowledge_risk flag is sent automatically for development builds.
function upgradeDevAckSatisfied() {
  return true;
}

// --- Admin alignment (step 03 of the guided upgrade) ---------------------
// Admin alignment is an automatic pipeline stage, not a separate decision. The
// confirmed upgrade resolves one Target System Build and aligns the Admin to it
// through the shared system-alignment transition: a matching Admin is kept, a
// mismatched Admin is updated out of band and the browser reconnects, then EMS
// is deployed. This stage is read-only — there is no standalone Admin update.
const upgradeAdminEls = {
  current: document.getElementById("upgrade-admin-current"),
  target: document.getElementById("upgrade-admin-target"),
  status: document.getElementById("upgrade-admin-alignment-status"),
};

// The reconnect overlay is shared with Guided Setup; it is retained here as the
// single reconnect surface used after any Admin container is replaced.
const adminUpdateOverlayEls = {
  overlay: document.getElementById("admin-update-overlay"),
  title: document.getElementById("admin-update-overlay-title"),
  message: document.getElementById("admin-update-overlay-message"),
  hint: document.getElementById("admin-update-overlay-hint"),
};

// Human copy for each alignment state (decision or live transition stage).
const UPGRADE_ALIGNMENT_STATUS_TEXT = {
  aligned: "Admin already matches the target System Build.",
  retag_required: "The persistent Admin tag will be updated.",
  admin_recreate_required: "The Admin container will be recreated.",
  admin_update_required: "The target Admin image will be installed.",
  admin_reconnect_pending: "Waiting for the replacement Admin…",
  resources_verified: "Admin aligned and target resources verified.",
  failed_recoverable: "Admin alignment failed; recovery is required.",
  completed: "Admin aligned to the target System Build.",
};

// Choose which alignment state to show: a live guided_upgrade transition stage
// wins (it reflects real progress), otherwise the server's validation decision.
function upgradeAlignmentState(validation, transition) {
  const stage =
    transition && transition.mode === "guided_upgrade" ? transition.stage : null;
  if (stage && UPGRADE_ALIGNMENT_STATUS_TEXT[stage]) return stage;
  return (validation && validation.alignment) || null;
}

// Render the read-only Admin alignment stage from SERVER-provided identity —
// the running Admin's own build and the target Admin build — never the EMS
// container tag. Admin alignment happens automatically as part of the upgrade.
function renderUpgradeAdminAlignment() {
  const validation = upgradeState.validation;
  const transition = upgradeState.alignmentTransition;
  const currentAdmin =
    (validation && validation.current_admin && validation.current_admin.system_tag) ||
    (upgradeState.runningAdmin && upgradeState.runningAdmin.tag) ||
    (upgradeState.runningAdmin && upgradeState.runningAdmin.image) ||
    "Unknown";
  const targetAdmin =
    (validation && validation.system_build && validation.system_build.canonical_tag) ||
    upgradeState.selected ||
    "Not selected";
  if (upgradeAdminEls.current) upgradeAdminEls.current.textContent = currentAdmin;
  if (upgradeAdminEls.target) upgradeAdminEls.target.textContent = targetAdmin;
  const state = upgradeAlignmentState(validation, transition);
  if (upgradeAdminEls.status && state && UPGRADE_ALIGNMENT_STATUS_TEXT[state]) {
    upgradeAdminEls.status.textContent = UPGRADE_ALIGNMENT_STATUS_TEXT[state];
  }
}

function applyUpgradeAlignmentTransition() {
  upgradeState.alignmentTransition =
    (systemAlignmentState && systemAlignmentState.transition) || null;
  renderUpgradeAdminAlignment();
  updateUpgradeActionButtons();
}

function renderUpgradeBadges(release) {
  if (!upgradeEls.badges) return;
  upgradeEls.badges.replaceChildren();
  if (!release) return;
  const badges = [];
  if (release.channel === "stable") badges.push(["stable", "source-mdns"]);
  if (release.channel === "latest") badges.push(["latest", "source-scan"]);
  if (release.prerelease) badges.push(["rc", "source-scan"]);
  badges.push(
    release.docker_supported ? ["docker", "source-mdns"] : ["unsupported", "source-scan"]
  );
  if (release.prepared) badges.push(["prepared", "source-mdns"]);
  if (release.active) badges.push(["active", "source-mdns"]);
  for (const badge of badges) {
    const span = document.createElement("span");
    span.className = "source-badge " + badge[1];
    span.textContent = badge[0];
    upgradeEls.badges.appendChild(span);
  }
}

function upgradeSelectedRelease() {
  return upgradeState.releases.find((item) => item.tag === upgradeState.selected) || null;
}

function upgradeTargetPrepared() {
  return upgradeState.prepared && upgradeState.preparedTag === upgradeState.selected;
}

function upgradeReleaseIdentity(release) {
  if (!release) return null;
  return JSON.stringify({
    tag: release.tag || null,
    channel: release.channel || null,
    revision: release.revision || null,
    build_id: release.build_id || null,
    admin_image: release.admin_image || null,
    admin_digest: release.admin_digest || null,
    ems_image: release.ems_image || null,
    ems_digest: release.ems_digest || null,
    docker_supported: release.docker_supported !== false,
    selectable: release.selectable !== false,
  });
}

function upgradeAdminVerificationCurrent() {
  if (!upgradeState.preparedAdminInstanceId) return true;
  if (!authState.adminInstanceId) return false;
  return upgradeState.preparedAdminInstanceId === authState.adminInstanceId;
}

function invalidateUpgradePlan({ resetCompleted = false } = {}) {
  upgradeState.planGeneration += 1;
  upgradeState.planned = false;
  upgradeState.plannedFingerprint = null;
  if (resetCompleted) upgradeState.completed = false;
}

function upgradeCanPlan() {
  return Boolean(
    upgradeState.selected &&
      upgradeTargetPrepared() &&
      upgradeState.preparedFingerprint &&
      upgradeState.status === "ready" &&
      upgradeAdminVerificationCurrent() &&
      !upgradeState.loading &&
      !upgradeState.planning &&
      !upgradeState.planned &&
      !upgradeState.running &&
      !upgradeState.completed
  );
}

function upgradePlanStillCurrent(generation, selectedTag, fingerprint) {
  return Boolean(
    generation === upgradeState.planGeneration &&
      upgradeState.planning &&
      upgradeState.selected === selectedTag &&
      upgradeTargetPrepared() &&
      upgradeState.preparedTag === selectedTag &&
      upgradeState.preparedFingerprint === fingerprint &&
      upgradeState.status === "ready" &&
      upgradeAdminVerificationCurrent() &&
      !upgradeState.loading &&
      !upgradeState.running &&
      !upgradeState.completed
  );
}

// Executable only when the selected target is the verified one AND the plan is
// still bound to that verification: the verified fingerprint must be present and
// the planned fingerprint must match it. A moved tag, a re-verification, or a
// selection change breaks one of these and disables Upgrade System.
function upgradeTargetVerified() {
  return Boolean(
    upgradeState.selected &&
      upgradeTargetPrepared() &&
      upgradeState.preparedFingerprint &&
      upgradeState.planned &&
      upgradeState.plannedFingerprint === upgradeState.preparedFingerprint
  );
}

// Drop the verified state and its plan binding while keeping the selection, so
// the operator must run Verify System Build again before Upgrade System.
function clearUpgradeVerification() {
  upgradeState.validationGeneration += 1;
  upgradeState.prepared = false;
  upgradeState.preparedTag = null;
  upgradeState.preparedFingerprint = null;
  upgradeState.preparedAdminInstanceId = null;
  upgradeState.preparedReleaseIdentity = null;
  upgradeState.validation = null;
  invalidateUpgradePlan({ resetCompleted: true });
  upgradeState.status = "idle";
}

function setUpgradeReleaseStatus() {
  if (upgradeEls.releaseStatus) {
    upgradeEls.releaseStatus.textContent =
      UPGRADE_RELEASE_STATUS_TEXT[upgradeState.status] || "";
  }
  if (upgradeEls.releaseError) {
    upgradeEls.releaseError.hidden = !upgradeState.error;
    upgradeEls.releaseError.textContent = upgradeState.error || "";
  }
  if (upgradeEls.prepareBtn) {
    const release = upgradeSelectedRelease();
    const verifying = upgradeState.status === "preparing";
    upgradeEls.prepareBtn.disabled =
      upgradeState.status === "loading" ||
      verifying ||
      upgradeTargetPrepared() ||
      !release ||
      release.selectable === false;
    upgradeEls.prepareBtn.classList.toggle("is-scanning", verifying);
    upgradeEls.prepareBtn.textContent = verifying
      ? "Verifying…"
      : upgradeTargetPrepared()
      ? "System Build verified"
      : upgradeState.status === "failed"
      ? "Try again"
      : "Verify System Build";
  }
  updateUpgradeActionButtons();
}

function readUpgradeOptions() {
  const state = {};
  for (const el of upgradeEls.options) {
    state[el.dataset.upgradeOption] = el.checked;
  }
  // Deploying the System Build is mandatory: the target image is always pulled
  // and the EMS container is always recreated, so a Compose ref update can never
  // be left running the old container. These are not operator-toggleable.
  state.pull_image = true;
  state.recreate = true;
  return state;
}

function renderUpgradeValidation(items, prepared) {
  if (!upgradeEls.validationCard) return;
  const hasError = items.some((item) => item.tone === "error");
  const hasWarn = items.some((item) => item.tone === "warn");
  const tone = hasError ? "error" : hasWarn ? "warn" : prepared ? "ready" : "pending";
  upgradeEls.validationCard.dataset.tone = tone;
  if (upgradeEls.validationState) {
    upgradeEls.validationState.textContent =
      tone === "ready"
        ? "Ready"
        : tone === "warn"
        ? "Review"
        : tone === "error"
        ? "Blocked"
        : "Planning";
  }
  upgradeEls.validationList.innerHTML = items
    .map((item) => {
      const icon = item.tone === "error" ? "×" : item.tone === "warn" ? "!" : "✓";
      return (
        '<div class="config-validation-item config-validation-item-' +
        item.tone +
        '"><span class="config-validation-icon" aria-hidden="true">' +
        icon +
        "</span><span>" +
        escapeHtml(item.text) +
        "</span></div>"
      );
    })
    .join("");
}

function renderUpgradeCurrent() {
  const cur = upgradeState.current;
  if (upgradeEls.currentVersion) {
    upgradeEls.currentVersion.textContent =
      cur.image || cur.tag || "Current version unknown";
  }
  if (upgradeEls.currentDetail) {
    upgradeEls.currentDetail.textContent = cur.state || "—";
  }
}

function summarizeMqttMigration(review) {
  const migration = review || {};
  const changes = Array.isArray(migration.changes) ? migration.changes : [];
  const affected = changes.length;
  const losingControl = changes.filter(
    (change) => change && change.disables_control
  ).length;
  const relevant = migration.needs_migration === true || affected > 0;
  let text = "";
  if (relevant) {
    let summary = "MQTT configuration migration required";
    if (affected > 0) {
      summary += " for " + affected + (affected === 1 ? " device" : " devices");
    }
    if (affected > 0 && losingControl > 0) {
      summary +=
        "; " +
        losingControl +
        (losingControl === 1 ? " device" : " devices") +
        " will lose output control";
    }
    text = summary + ".";
  }
  return { relevant, affected, losingControl, text };
}

function renderUpgradePlan() {
  const release = upgradeSelectedRelease();
  const cur = upgradeState.current;
  if (upgradeEls.factCurrent) {
    upgradeEls.factCurrent.textContent = cur.image || cur.tag || "Current version unknown";
  }
  if (upgradeEls.factTarget) {
    upgradeEls.factTarget.textContent = release
      ? release.name || release.tag
      : "Not selected";
  }

  if (upgradeState.completed) {
    updateUpgradeActionButtons();
    return;
  }

  if (upgradeState.planning) {
    renderUpgradeValidation(
      [
        {
          tone: "info",
          text: "Refreshing migration review and building the upgrade plan…",
        },
      ],
      false
    );
    updateUpgradeActionButtons();
    return;
  }

  if (!upgradeState.planned) {
    renderUpgradeValidation(
      [{ tone: "info", text: "Review the target release and options, then plan the upgrade." }],
      false
    );
    updateUpgradeActionButtons();
    return;
  }

  const prepared = upgradeTargetPrepared();
  const items = [];
  if (!upgradeState.selected) {
    items.push({ tone: "warn", text: "Select a target version manually" });
  } else if (prepared) {
    items.push({ tone: "info", text: "System Build validated" });
  } else if (upgradeState.status === "failed") {
    items.push({ tone: "warn", text: "Update check unavailable" });
  } else {
    items.push({ tone: "warn", text: "Prepare the target release before upgrading" });
  }
  if (!cur.tag && !cur.image) {
    items.push({ tone: "warn", text: "Current version unknown" });
  }
  items.push({ tone: "info", text: "Verify the target image identity" });
  const mqttMigration = summarizeMqttMigration(upgradeState.migrationReview);
  if (mqttMigration.relevant) {
    items.push({ tone: "warn", text: mqttMigration.text });
  }
  // Admin alignment is always part of the plan, never a separate decision: the
  // Admin is aligned to the same System Build before EMS is deployed.
  items.push({
    tone: "info",
    text: "Align Admin to the target System Build (automatic; reconnects if the Admin is replaced)",
  });
  if (release && release.upgrade_warning) {
    items.push({ tone: "warn", text: release.upgrade_warning });
  }

  const options = readUpgradeOptions();
  for (const opt of UPGRADE_OPTIONS) {
    if (options[opt.key]) items.push({ tone: "info", text: opt.plan });
  }
  if (!options.backup) {
    items.push({
      tone: "warn",
      text: "Backup is disabled. Planning can continue, but a real upgrade should normally create a backup first.",
    });
  }

  items.push({ tone: "info", text: "Review the plan, then run Upgrade system" });
  renderUpgradeValidation(items, prepared);
  updateUpgradeActionButtons();
}

function upgradeAlignmentRequiresRecovery() {
  const transition = upgradeState.alignmentTransition;
  return Boolean(transition && transition.stage === "failed_recoverable");
}

// Execute is only allowed once the plan is bound to the verified System Build
// (matching verified + planned fingerprints) and no upgrade is already running.
function updateExecuteButton() {
  if (!upgradeEls.executeBtn) return;
  const allowed =
    upgradeTargetVerified() &&
    upgradeDevAckSatisfied() &&
    upgradeState.status === "ready" &&
    !upgradeAlignmentRequiresRecovery() &&
    !upgradeState.loading &&
    !upgradeState.planning &&
    !upgradeState.running &&
    !upgradeState.completed;
  upgradeEls.executeBtn.disabled = !allowed;
  upgradeEls.executeBtn.textContent = upgradeState.running
    ? "Upgrading…"
    : "Upgrade system";
}

function updateUpgradeActionButtons() {
  if (upgradeEls.planBtn) {
    if (upgradeState.completed) {
      upgradeEls.planBtn.textContent = "Upgrade completed";
    } else if (upgradeState.planning) {
      upgradeEls.planBtn.textContent = "Planning…";
    } else if (upgradeTargetVerified()) {
      upgradeEls.planBtn.textContent = "Plan ready";
    } else {
      upgradeEls.planBtn.textContent = "Plan upgrade";
    }
    upgradeEls.planBtn.disabled = !upgradeCanPlan();
    if (upgradeState.planning) {
      upgradeEls.planBtn.setAttribute("aria-busy", "true");
    } else {
      upgradeEls.planBtn.removeAttribute("aria-busy");
    }
  }
  for (const el of upgradeEls.options) {
    el.disabled = upgradeState.running || upgradeState.planning || upgradeState.completed;
  }
  updateExecuteButton();
}

function setUpgradeRunning(running) {
  upgradeState.running = running;
  if (upgradeEls.select) upgradeEls.select.disabled = running || !upgradeState.releases.length;
  for (const el of upgradeEls.options) {
    el.disabled = running || upgradeState.planning || upgradeState.completed;
  }
  if (running && upgradeEls.prepareBtn) upgradeEls.prepareBtn.disabled = true;
  if (!running) setUpgradeReleaseStatus();
  updateUpgradeActionButtons();
}

// Live step glyphs while a job is running, keyed by the job step state.
const UPGRADE_STEP_ICON = {
  done: "✓",
  running: "▶",
  pending: "○",
  failed: "×",
  skipped: "✓",
};
const UPGRADE_POLL_INTERVAL_MS = 1200;
let upgradePollTimer = null;

function stopUpgradePolling() {
  if (upgradePollTimer !== null) {
    clearTimeout(upgradePollTimer);
    upgradePollTimer = null;
  }
}

function renderUpgradeSteps(steps) {
  if (!upgradeEls.validationCard || !upgradeEls.validationList) return;
  const list = Array.isArray(steps) ? steps : [];
  const hasFailed = list.some((step) => step.state === "failed");
  upgradeEls.validationCard.dataset.tone = hasFailed ? "error" : "pending";
  if (upgradeEls.validationState) {
    upgradeEls.validationState.textContent = hasFailed ? "Blocked" : "Running";
  }
  upgradeEls.validationList.innerHTML = list
    .map((step) => {
      const state = step.state || "pending";
      const icon = UPGRADE_STEP_ICON[state] || "○";
      const tone = state === "failed" ? "error" : "info";
      const label = step.label || step.key || "";
      const text = step.message ? label + " — " + step.message : label;
      return (
        '<div class="config-validation-item config-validation-item-' +
        tone +
        '"><span class="config-validation-icon" aria-hidden="true">' +
        icon +
        "</span><span>" +
        escapeHtml(text) +
        "</span></div>"
      );
    })
    .join("");
}

async function pollUpgradeJob(jobId) {
  try {
    const res = await fetch(
      "/api/admin/maintenance/upgrade/jobs/" + encodeURIComponent(jobId)
    );
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Upgrade status unavailable.");
    }
    if (data.transition) renderSystemAlignmentStatus(data);
    renderUpgradeSteps(data.steps);
    if (data.status === "succeeded" || data.status === "failed") {
      stopUpgradePolling();
      const result = data.result || { ok: data.status === "succeeded", steps: data.steps };
      renderUpgradeResult(result);
      setUpgradeRunning(false);
      return;
    }
    upgradePollTimer = setTimeout(() => pollUpgradeJob(jobId), UPGRADE_POLL_INTERVAL_MS);
  } catch (err) {
    stopUpgradePolling();
    renderUpgradeValidation([{ tone: "error", text: err.message || String(err) }], false);
    setUpgradeRunning(false);
  }
}

const UPGRADE_REGISTRY_RATE_LIMIT_REASONS = new Set([
  "system_build_registry_rate_limited",
  "image_pull_rate_limited",
]);
const UPGRADE_REGISTRY_RATE_LIMIT_MESSAGE =
  "GitHub Container Registry rate limit reached. No installation changes were " +
  "made. Wait before retrying, or authenticate Docker with a GitHub account to " +
  "increase the available request quota.";

function upgradeFailureReason(data) {
  if (data && typeof data.reason === "string" && data.reason) return data.reason;
  const steps = Array.isArray(data && data.steps) ? data.steps : [];
  const pull = steps.find(
    (step) => step && step.id === "pull_image" && step.status === "error"
  );
  return pull && typeof pull.code === "string" ? pull.code : null;
}

function renderUpgradeResult(data) {
  const items = [];
  const steps = Array.isArray(data.steps) ? data.steps : [];
  for (const step of steps) {
    // Skipped/disabled steps stay out of the summary unless they warn.
    if (step.status === "skipped") continue;
    const tone =
      step.status === "error" ? "error" : step.status === "warning" ? "warn" : "info";
    items.push({ tone, text: step.detail ? step.label + " — " + step.detail : step.label });
  }
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  for (const warning of warnings) items.push({ tone: "warn", text: warning });
  if (data.ok) {
    upgradeState.completed = true;
    // Show the readable release tag, not the digest-pinned runtime ref.
    items.push({
      tone: "info",
      text: "Upgrade completed: " + (data.target_release || data.target_image || ""),
    });
  } else if (UPGRADE_REGISTRY_RATE_LIMIT_REASONS.has(upgradeFailureReason(data))) {
    items.push({
      tone: "warn",
      text: data.message || UPGRADE_REGISTRY_RATE_LIMIT_MESSAGE,
    });
  } else {
    items.push({ tone: "error", text: data.message || "Upgrade did not complete." });
  }
  renderUpgradeValidation(items, upgradeTargetPrepared());
}

// Build the confirmed execute request body. A development System Build carries
// its explicit, tag-bound risk acknowledgement so the server can start a new
// transition; stable/RC builds never send it.
function upgradeExecuteBody(
  target,
  options,
  isDevelopment,
  acknowledged,
  migrationRevision,
  selectionFingerprint
) {
  const body = {
    confirm: true,
    target_release: target,
    options,
    migration_revision: migrationRevision,
    // The exact fingerprint Verify returned; the server re-resolves the target
    // and refuses to upgrade if the pair changed since verification.
    selection_fingerprint: selectionFingerprint,
  };
  if (isDevelopment && acknowledged) body.acknowledge_risk = true;
  return body;
}

async function executeUpgrade() {
  if (
    upgradeState.running ||
    upgradeState.planning ||
    upgradeState.completed ||
    !upgradeTargetVerified() ||
    upgradeEls.executeBtn.disabled
  ) {
    return;
  }
  // The verified fingerprint is never synthesized client-side; without it the
  // request is not sent and the operator is asked to verify again.
  if (!upgradeState.preparedFingerprint) {
    renderUpgradeValidation(
      [{ tone: "warn", text: "Verify the selected System Build again." }],
      false
    );
    return;
  }
  const previousAdminInstanceId = authState.adminInstanceId;
  const target = upgradeState.preparedTag || upgradeState.selected;
  const options = readUpgradeOptions();
  stopUpgradePolling();
  setUpgradeRunning(true);
  renderUpgradeValidation([{ tone: "info", text: "Upgrade running — applying steps…" }], false);
  if (upgradeEls.validationState) upgradeEls.validationState.textContent = "Running";
  try {
    const res = await fetch("/api/admin/maintenance/upgrade/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        upgradeExecuteBody(
          target,
          options,
          upgradeSelectedIsDevelopment(),
          upgradeDevAckSatisfied(),
          upgradeState.migrationRevision,
          upgradeState.preparedFingerprint
        )
      ),
    });
    const data = await res.json();
    if (
      data &&
      (data.transition ||
        SYSTEM_ALIGNMENT_TRANSITION_STAGES.has(resolveSystemAlignmentStage(data)))
    ) {
      renderSystemAlignmentStatus(data);
    }
    if (res.ok && data && (data.reconnect || data.status === "admin_alignment_started")) {
      setUpgradeRunning(false);
      showReconnectOverlay(data.message);
      // Bind the reconnect poller to this operation so a failed/cancelled
      // Admin update on the still-answering old instance is surfaced at once,
      // and a different operation id fails closed instead of resuming.
      const operationId =
        data.operation_id || (data.transition && data.transition.operation_id);
      waitForAdminReconnect(previousAdminInstanceId, operationId);
      return;
    }
    if (!res.ok || !data.ok || !data.job_id) {
      // Synchronous rejection (guard checks) — render it and stop.
      const reason = data && data.reason;
      if (
        reason === "system_build_verification_required" ||
        reason === "system_build_verification_stale"
      ) {
        // The verified build is no longer current: drop the verification and
        // plan binding, keep the selection, and require an explicit re-verify.
        // Never retry automatically. Disable the button, then render the notice
        // last so the plan render cannot overwrite it.
        setUpgradeRunning(false);
        clearUpgradeVerification();
        // Refresh the Verify button (now enabled, "Verify System Build") and the
        // execute button before rendering the notice.
        setUpgradeReleaseStatus();
        updateExecuteButton();
        renderUpgradeValidation(
          [
            {
              tone: "warn",
              text:
                "System Build verification is no longer current. The target " +
                "image or build metadata changed after verification. Verify the " +
                "selected System Build again before upgrading.",
            },
          ],
          false
        );
        return;
      }
      renderUpgradeResult(data);
      if (reason === "mqtt_migration_review_stale") {
        invalidateUpgradePlan();
        await loadUpgradeMigrationReview();
      }
      setUpgradeRunning(false);
      return;
    }
    renderUpgradeSteps(data.steps);
    pollUpgradeJob(data.job_id);
  } catch (err) {
    renderUpgradeValidation([{ tone: "error", text: err.message || String(err) }], false);
    setUpgradeRunning(false);
  }
}

async function loadUpgradeCurrentVersion() {
  try {
    const resp = await fetch("/api/admin/maintenance/overview");
    if (!resp.ok) throw new Error("overview request failed");
    const data = await resp.json();
    const ems = (data.containers && data.containers.ems) || {};
    const admin = (data.components && data.components.admin) || {};
    const state = data.install_state || {};
    upgradeState.current = {
      tag: ems.tag || null,
      image: ems.image || null,
      state: state.label || state.state || null,
    };
    upgradeState.runningAdmin = {
      tag: admin.tag || null,
      image: admin.image || null,
    };
  } catch (err) {
    upgradeState.current = { tag: null, image: null, state: null };
    upgradeState.runningAdmin = { tag: null, image: null };
  }
  renderUpgradeCurrent();
}

function applyUpgradeMigrationReview(data) {
  if (data && data.status === "ok") {
    upgradeState.migrationReview = data.review || null;
    upgradeState.migrationRevision = data.revision || null;
    return true;
  } else {
    upgradeState.migrationReview = null;
    upgradeState.migrationRevision = null;
    return false;
  }
}

async function loadUpgradeMigrationReview() {
  const data = await loadMqttMigrationReview();
  applyUpgradeMigrationReview(data);
  return data;
}

async function planUpgrade() {
  if (!upgradeCanPlan()) {
    updateUpgradeActionButtons();
    return false;
  }
  const selectedTag = upgradeState.selected;
  const fingerprint = upgradeState.preparedFingerprint;
  const generation = upgradeState.planGeneration + 1;
  upgradeState.planGeneration = generation;
  upgradeState.planning = true;
  upgradeState.planned = false;
  upgradeState.plannedFingerprint = null;
  renderUpgradePlan();

  const data = await loadMqttMigrationReview();
  if (!upgradePlanStillCurrent(generation, selectedTag, fingerprint)) {
    upgradeState.planning = false;
    upgradeState.planned = false;
    upgradeState.plannedFingerprint = null;
    updateUpgradeActionButtons();
    renderUpgradeValidation(
      [
        {
          tone: "warn",
          text: "The verified System Build changed while planning. Verify it again.",
        },
      ],
      false
    );
    return false;
  }
  if (!applyUpgradeMigrationReview(data)) {
    upgradeState.planning = false;
    updateUpgradeActionButtons();
    renderUpgradeValidation(
      [
        {
          tone: "warn",
          text: "Could not refresh the migration review. Try planning again.",
        },
      ],
      false
    );
    return false;
  }

  upgradeState.planned = true;
  upgradeState.plannedFingerprint = fingerprint;
  upgradeState.planning = false;
  renderUpgradePlan();
  return true;
}

// Pick which System Build the upgrade selector shows. A resume PINS the exact
// transition tag so a server default/prepared release can never overwrite the
// resumed target; a pinned tag that is not a selectable build fails closed
// (returns null) instead of silently falling back to a default.
function selectUpgradeReleaseTag(releases, data, pinnedTag) {
  const selectable = (item) => item && item.selectable !== false;
  if (pinnedTag) {
    const pinned = releases.find(
      (item) => item.tag === pinnedTag && selectable(item)
    );
    return pinned ? pinned.tag : null;
  }
  const chosen =
    releases.find((item) => item.tag === data.default_release && selectable(item)) ||
    releases.find((item) => item.tag === data.prepared_release && selectable(item)) ||
    releases.find(selectable);
  return chosen ? chosen.tag : null;
}

// Decide whether an existing in-memory verification survives re-loading the
// catalogue. It survives ONLY on a same-session navigation (preserve=true) when
// the same tag is still selected and present in the fresh catalogue and the same
// Admin instance is still answering. Any other case (explicit refresh, changed
// tag, missing build, replaced Admin) drops it so Verify must run again. This is
// UX-only state; the server still enforces the fingerprint at execute.
function upgradeVerificationSurvivesReload(preserve, releases, selectedTag) {
  if (!preserve) return false;
  if (!upgradeState.prepared || !upgradeState.preparedFingerprint) return false;
  if (upgradeState.preparedTag !== selectedTag) return false;
  if (
    upgradeState.preparedAdminInstanceId &&
    authState.adminInstanceId &&
    upgradeState.preparedAdminInstanceId !== authState.adminInstanceId
  ) {
    return false;
  }
  const release = releases.find(
    (item) => item.tag === selectedTag && item.selectable !== false
  );
  return Boolean(
    release &&
      upgradeState.preparedReleaseIdentity &&
      upgradeState.preparedReleaseIdentity === upgradeReleaseIdentity(release)
  );
}

async function loadUpgradeReleases(pinnedTag, { preserveVerification = false } = {}) {
  upgradeState.status = "loading";
  upgradeState.error = null;
  setUpgradeReleaseStatus();
  try {
    // Maintenance/upgrade flow: apply the upgrade-only build-identity gate.
    const res = await fetch("/api/setup/releases?flow=upgrade");
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data && data.error ? data.error : "release list unavailable");
    }
    const releases = Array.isArray(data.releases) ? data.releases : [];
    upgradeState.releases = releases;
    upgradeEls.select.innerHTML = "";
    // One selector grouped from the single shared System Build catalogue:
    // Latest, Stable, Unstable, then Experimental (always last).
    for (const group of groupSetupReleaseOptions(releases)) {
      const optgroup = document.createElement("optgroup");
      optgroup.label = group.label;
      for (const release of group.releases) {
        const option = document.createElement("option");
        option.value = release.tag;
        option.textContent = releaseOptionLabel(release);
        option.disabled = release.selectable === false;
        optgroup.appendChild(option);
      }
      upgradeEls.select.appendChild(optgroup);
    }
    let selectedTag = selectUpgradeReleaseTag(releases, data, pinnedTag);
    // On a same-session navigation (no explicit pin) keep the current selection
    // when it is still offered, so returning to the panel does not silently jump
    // to the default build and drop a verification for the selected one.
    if (!pinnedTag && preserveVerification && upgradeState.selected) {
      const keep = releases.find(
        (item) => item.tag === upgradeState.selected && item.selectable !== false
      );
      if (keep) selectedTag = keep.tag;
    }
    if (!selectedTag) {
      throw new Error(
        pinnedTag
          ? "The System Build being upgraded is no longer available."
          : Array.isArray(data.warnings) && data.warnings.length
          ? data.warnings[0]
          : "No System Builds are available."
      );
    }
    const selected = releases.find((item) => item.tag === selectedTag);
    upgradeEls.select.value = selected.tag;
    upgradeEls.select.disabled = false;
    upgradeState.selected = selected.tag;
    // A catalogue-prepared release only means its resources are cached locally
    // (shown as a badge); it is NOT a verified System Build. Verification is only
    // ever established by an explicit Verify System Build (or an authoritative
    // resume). A same-session navigation back to this panel may keep an existing
    // in-memory verification for the same tag (no second registry pull), but a
    // full page load, an explicit refresh or a changed build leaves it unverified.
    if (!upgradeVerificationSurvivesReload(preserveVerification, releases, selected.tag)) {
      clearUpgradeVerification();
    } else {
      upgradeState.status = "ready";
    }
    renderUpgradeBadges(selected);
  } catch (err) {
    upgradeState.releases = [];
    upgradeState.selected = null;
    clearUpgradeVerification();
    upgradeState.status = "failed";
    upgradeState.error = err.message || String(err);
    if (upgradeEls.select) upgradeEls.select.disabled = true;
    renderUpgradeBadges(null);
  }
  setUpgradeReleaseStatus();
}

// Load the upgrade planning page once. Concurrent callers (e.g. a hash-route
// navigation racing an explicit resume) share ONE in-flight run and await the
// same promise, so there is never a second parallel loadUpgradePlanning pass and
// a caller can reliably await full completion. ``pinnedTag`` forces the selector
// to the resumed transition tag.
function loadUpgradePlanning(pinnedTag, { preserveVerification = false } = {}) {
  if (upgradeState.loadingPromise) return upgradeState.loadingPromise;
  upgradeState.loading = true;
  upgradeState.loadingPromise = (async () => {
    try {
      await loadUpgradeCurrentVersion();
      await loadUpgradeReleases(pinnedTag, { preserveVerification });
      await loadUpgradeMigrationReview();
      // Admin alignment is automatic and driven by the shared system-alignment
      // transition; the planning page only reads the current state (recovery of
      // an in-flight transition happens via /api/admin/system-alignment/status).
      await loadSystemAlignmentStatus();
      renderUpgradeAdminAlignment();
      renderUpgradePlan();
    } finally {
      upgradeState.loading = false;
      upgradeState.loadingPromise = null;
      updateUpgradeActionButtons();
    }
  })();
  return upgradeState.loadingPromise;
}

function onUpgradeReleaseChange() {
  const nextTag = upgradeEls.select.value || null;
  // A changed target is a side-effect-free local preview: it never verifies or
  // pulls. It supersedes any in-flight verification (epoch bump) and drops the
  // previous build's verification and plan so Upgrade System cannot run against
  // a stale plan. Re-selecting a build requires an explicit Verify again.
  if (nextTag !== upgradeState.selected) {
    upgradeState.validationGeneration += 1;
    upgradeState.prepared = false;
    upgradeState.preparedTag = null;
    upgradeState.preparedFingerprint = null;
    upgradeState.preparedAdminInstanceId = null;
    upgradeState.preparedReleaseIdentity = null;
    upgradeState.validation = null;
    invalidateUpgradePlan({ resetCompleted: true });
  }
  upgradeState.selected = nextTag;
  resetSystemAlignmentPresentation(
    upgradeState.selected,
    upgradeState.selected ? "selection_started" : null
  );
  upgradeState.prepared = upgradeState.preparedTag === upgradeState.selected;
  upgradeState.status = upgradeTargetPrepared() ? "ready" : "idle";
  upgradeState.error = null;
  renderUpgradeBadges(upgradeSelectedRelease());
  renderUpgradeAdminAlignment();
  setUpgradeReleaseStatus();
  renderUpgradePlan();
}

// The non-empty selection fingerprint the server returned for the verified pair,
// or null. It is never synthesized in the browser.
function upgradeResponseFingerprint(data) {
  const fingerprint = data && data.selection_fingerprint;
  return typeof fingerprint === "string" && fingerprint.length > 0
    ? fingerprint
    : null;
}

// A build counts as validated only when the server confirmed a valid pair, a
// permitted (non-downgrade) direction AND returned a selection fingerprint. A
// fingerprint is mandatory: without it Upgrade System can never run, so the UI
// must fail closed rather than claim the build is verified. The browser never
// decides validity locally.
function upgradeValidationAccepted(ok, data) {
  return Boolean(
    ok &&
      data &&
      data.valid &&
      data.upgrade_allowed !== false &&
      upgradeResponseFingerprint(data)
  );
}

// An unresolved Guided Setup blocks upgrade validation server-side. Resolving
// it is the Setup owner's job: an explicit Discard setup confirmation, the
// backend abandon operation (which removes the Setup transition together with
// its artifacts), and only then may validation start.
async function resolveSetupConflictForUpgrade() {
  if (!window.confirm(DISCARD_SETUP_CONFIRM)) {
    return {
      ok: false,
      message: "Discard the unfinished setup before validating an upgrade.",
    };
  }
  const discarded = await discardActiveSetup();
  if (discarded.ok) {
    showSetupCleanupIncomplete(null);
    loadSystemAlignmentStatus();
    return { ok: true };
  }
  if (isSetupOperationInProgress(discarded.data)) {
    return { ok: false, message: setupOperationInProgressMessage(discarded.data) };
  }
  const cleanupState = setupCleanupStateFor(discarded.data);
  if (cleanupState !== null) {
    showSetupCleanupIncomplete(discarded.data);
    return {
      ok: false,
      message:
        cleanupState === "review_required"
          ? SETUP_CLEANUP_REVIEW_MESSAGE
          : "Setup has stopped, but some temporary setup files could not be " +
            "removed. Retry cleanup, then verify the build again.",
    };
  }
  return {
    ok: false,
    message:
      (discarded.data && (discarded.data.message || discarded.data.error)) ||
      "The unfinished setup could not be discarded.",
  };
}

// The explicit verification: download or reuse the Admin/EMS images and verify
// the pair. This is the only heavy trigger — selecting a build never reaches it.
async function prepareUpgradeTarget() {
  if (upgradeState.running || upgradeState.planning || upgradeState.completed) return;
  const tag = upgradeEls.select.value;
  if (!tag) {
    upgradeState.status = "failed";
    upgradeState.error = "Select a target System Build first.";
    setUpgradeReleaseStatus();
    return;
  }
  // Bind this verification to its target and epoch, captured before any await, so
  // a newer selection arriving mid-flight supersedes this response instead of
  // painting a stale build's verification over the new selection.
  upgradeState.validationGeneration += 1;
  const generation = upgradeState.validationGeneration;
  // A build is only ever marked verified by the server. This resolves the
  // Admin/EMS pair (reusing the cached, digest-pinned resolution) and checks the
  // upgrade direction; it never starts a transition, imports resources, or
  // changes anything.
  upgradeState.selected = tag;
  upgradeState.prepared = false;
  upgradeState.preparedTag = null;
  upgradeState.preparedFingerprint = null;
  upgradeState.preparedAdminInstanceId = null;
  upgradeState.preparedReleaseIdentity = null;
  upgradeState.validation = null;
  // A new verification supersedes any prior plan binding.
  invalidateUpgradePlan();
  upgradeState.status = "preparing";
  upgradeState.error = null;
  resetSystemAlignmentPresentation(tag, "validation_running");
  setUpgradeReleaseStatus();
  renderUpgradePlan();
  const stale = () =>
    generation !== upgradeState.validationGeneration || tag !== upgradeState.selected;
  const body = { tag };
  if (upgradeSelectedIsDevelopment() && upgradeDevAckSatisfied()) {
    body.acknowledge_risk = true;
  }
  try {
    let res = await fetch("/api/admin/maintenance/upgrade/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = await res.json().catch(() => ({}));
    if (stale()) return;
    if (res.status === 409 && data && data.error === "setup_cleanup_required") {
      // A terminal Setup whose cleanup has not converged owns the files it left
      // behind. Only its own retry can unblock the upgrade — never a new abandon.
      showSetupCleanupIncomplete(data);
      upgradeState.prepared = false;
      upgradeState.preparedTag = null;
      upgradeState.status = "failed";
      upgradeState.error = data.message || SETUP_CLEANUP_PENDING_MESSAGE;
      resetSystemAlignmentPresentation(tag, "validation_failed", upgradeState.error);
      renderUpgradeBadges(upgradeSelectedRelease());
      setUpgradeReleaseStatus();
      renderUpgradePlan();
      return;
    }
    if (res.status === 409 && data && data.error === "setup_abandon_required") {
      // The server blocks validation while Guided Setup owns unresolved state.
      // Resolution goes through the Setup owner (explicit Discard setup), and
      // its artifacts must be gone before validation starts.
      const resolved = await resolveSetupConflictForUpgrade();
      if (stale()) return;
      if (!resolved.ok) {
        upgradeState.prepared = false;
        upgradeState.preparedTag = null;
        upgradeState.status = "failed";
        upgradeState.error = resolved.message;
        resetSystemAlignmentPresentation(tag, "validation_failed", upgradeState.error);
        renderUpgradeBadges(upgradeSelectedRelease());
        setUpgradeReleaseStatus();
        renderUpgradePlan();
        return;
      }
      res = await fetch("/api/admin/maintenance/upgrade/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      data = await res.json().catch(() => ({}));
    }
    // A superseded verification never applies its verdict: a newer selection, or
    // a changed target, wins.
    if (stale()) return;
    if (!upgradeValidationAccepted(res.ok, data)) {
      upgradeState.prepared = false;
      upgradeState.preparedTag = null;
      upgradeState.preparedFingerprint = null;
      upgradeState.status = "failed";
      // A response that otherwise validated but returned no fingerprint cannot be
      // executed; surface an actionable retry instead of a "verified" state.
      const missingFingerprint =
        res.ok &&
        data &&
        data.valid &&
        data.upgrade_allowed !== false &&
        !upgradeResponseFingerprint(data);
      upgradeState.error =
        (data && (data.message || data.error)) ||
        (missingFingerprint
          ? "Verification did not return a System Build fingerprint. Verify again."
          : "This System Build cannot be installed.");
      resetSystemAlignmentPresentation(tag, "validation_failed", upgradeState.error);
      renderUpgradeBadges(upgradeSelectedRelease());
      setUpgradeReleaseStatus();
      renderUpgradePlan();
      return;
    }
    upgradeState.prepared = true;
    upgradeState.preparedTag = tag;
    // Bind the verified plan to the exact resolved pair; a changed digest,
    // build id, revision or channel produces a different fingerprint.
    upgradeState.preparedFingerprint = upgradeResponseFingerprint(data);
    // Stamp the Admin instance this verification belongs to, so navigation can
    // safely restore it only while the same Admin is answering (a replaced Admin
    // triggers a full reload and must not inherit a stale verification).
    upgradeState.preparedAdminInstanceId = authState.adminInstanceId || null;
    upgradeState.preparedReleaseIdentity = upgradeReleaseIdentity(
      upgradeSelectedRelease()
    );
    upgradeState.validation = data;
    upgradeState.status = "ready";
    renderSystemAlignmentStatus(data);
  } catch (err) {
    if (stale()) return;
    upgradeState.prepared = false;
    upgradeState.preparedTag = null;
    upgradeState.status = "failed";
    upgradeState.error =
      (err && err.message) || "System Build verification is unavailable.";
    resetSystemAlignmentPresentation(tag, "validation_failed", upgradeState.error);
  }
  renderUpgradeBadges(upgradeSelectedRelease());
  renderUpgradeAdminAlignment();
  setUpgradeReleaseStatus();
  renderUpgradePlan();
}

if (upgradeEls.form) {
  upgradeEls.form.addEventListener("submit", (event) => {
    event.preventDefault();
    prepareUpgradeTarget();
  });
}
if (upgradeEls.select) {
  upgradeEls.select.addEventListener("change", onUpgradeReleaseChange);
}
if (upgradeEls.reload) {
  upgradeEls.reload.addEventListener("click", () =>
    loadUpgradeReleases(upgradeState.selected)
  );
}
for (const el of upgradeEls.options) {
  el.addEventListener("change", () => {
    invalidateUpgradePlan();
    renderUpgradePlan();
  });
}
if (upgradeEls.planBtn) {
  upgradeEls.planBtn.addEventListener("click", planUpgrade);
}
if (upgradeEls.executeBtn) {
  upgradeEls.executeBtn.addEventListener("click", executeUpgrade);
}

// --- Admin reconnect (shared with Guided Setup) -------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showReconnectOverlay(message) {
  const els = adminUpdateOverlayEls;
  if (els.title) els.title.textContent = "Reconnecting to the Admin Console…";
  if (els.message && message) els.message.textContent = message;
  if (els.hint) els.hint.hidden = true;
  if (els.overlay) els.overlay.hidden = false;
}

function hideReconnectOverlay() {
  if (adminUpdateOverlayEls.overlay) adminUpdateOverlayEls.overlay.hidden = true;
}

// A replaced Admin serves newer assets than this already-running page. Reload so
// the browser runs them; the guided workflow resumes from the durable
// server-side transition on the fresh load.
function reloadForReplacedAdmin() {
  showReconnectOverlay("Loading the updated Admin Console…");
  window.location.reload();
}

function showManualReloadHint() {
  if (adminUpdateOverlayEls.hint) adminUpdateOverlayEls.hint.hidden = false;
}

let adminReconnectInFlight = null;

// A stale process may keep returning 200 while its replacement starts. Only a
// different process identity proves that reconnect can hand off to auth state.
// A 200 from the *old* Admin instance is never a successful reconnect. When an
// operation id is bound, the still-running transition is read only to detect an
// update that failed (pull/compose/recreate) while the old instance kept
// answering, so the operator sees the failure at once instead of at the timeout.
function classifyReconnectTransition(transition, operationId) {
  if (!transition || !transition.stage) return "running";
  if (
    operationId &&
    transition.operation_id &&
    transition.operation_id !== operationId
  ) {
    return "wrong_operation";
  }
  if (transition.stage === "failed_recoverable") return "failed";
  if (transition.stage === "cancelled") return "cancelled";
  return "running";
}

async function readReconnectTransition() {
  try {
    const res = await rawFetch("/api/admin/system-alignment/status", {
      cache: "no-store",
    });
    if (!res.ok) return null;
    const data = await res.json().catch(() => null);
    if (data) renderSystemAlignmentStatus(data);
    return (data && data.transition) || null;
  } catch (_) {
    return null;
  }
}

function surfaceReconnectTransitionFailure(transition) {
  hideReconnectOverlay();
  systemBuildMutationLocked = false;
  systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
  systemBuildState.failedAction = "align";
  systemBuildState.error =
    (transition && transition.error_message) ||
    "The Admin update failed. Review the recovery options below.";
  renderSystemAlignmentStatus({ transition });
  applySystemBuildAlignment();
}

async function surfaceReconnectTransitionCancelled() {
  hideReconnectOverlay();
  systemBuildMutationLocked = false;
  clearSetupOperationContext();
  await restoreSelectedSystemBuild();
}

function surfaceReconnectWrongOperation() {
  hideReconnectOverlay();
  systemBuildMutationLocked = false;
  clearSetupOperationContext();
  systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
  systemBuildState.failedAction = "align";
  systemBuildState.error =
    "The Admin is running a different operation than the one started here. " +
    "Confirm Fresh Setup again before retrying.";
  applySystemBuildAlignment();
}

async function waitForAdminReconnect(
  previousAdminInstanceId = authState.adminInstanceId,
  operationId = null
) {
  if (adminReconnectInFlight) return await adminReconnectInFlight;
  adminReconnectInFlight = (async () => {
    showReconnectOverlay();
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      try {
        const res = await rawFetch("/api/admin/auth/status", { cache: "no-store" });
        const status = res.ok ? await res.json().catch(() => ({})) : {};
        if (
          status.admin_instance_id &&
          status.admin_instance_id !== previousAdminInstanceId
        ) {
          reloadForReplacedAdmin();
          return;
        }
        // The old instance still answers. Only a failed/cancelled/wrong-operation
        // transition ends the wait early; a still-running transition keeps polling.
        if (operationId && status.admin_instance_id === previousAdminInstanceId) {
          const transition = await readReconnectTransition();
          const outcome = classifyReconnectTransition(transition, operationId);
          if (outcome === "wrong_operation") {
            surfaceReconnectWrongOperation();
            return;
          }
          if (outcome === "failed") {
            surfaceReconnectTransitionFailure(transition);
            return;
          }
          if (outcome === "cancelled") {
            await surfaceReconnectTransitionCancelled();
            return;
          }
        }
      } catch (_) {
        // A connection failure is expected while the Admin is being replaced.
      }
      await sleep(1500);
    }
    showManualReloadHint();
  })();
  try {
    return await adminReconnectInFlight;
  } finally {
    adminReconnectInFlight = null;
  }
}

// A Guided Upgrade transition is one that owns an active (non-terminal) system
// build transition in the guided_upgrade mode.
function upgradeTransitionIsActive(transition) {
  return Boolean(
    transition &&
      transition.mode === "guided_upgrade" &&
      transition.stage &&
      !["completed", "cancelled"].includes(transition.stage)
  );
}

// Continue the Guided Upgrade from its durable operation using ONLY the
// operation id. The server verifies the reconnected Admin, imports resources,
// restores the saved options + backup state, and starts the single EMS job; the
// browser resends no target, options, or plan. Several calls are idempotent —
// the server returns the same job — so overlapping reconnect/login events cannot
// start a second upgrade.
async function resumeGuidedUpgrade(operationId) {
  if (!operationId) return;
  setUpgradeRunning(true);
  try {
    const res = await fetch("/api/admin/maintenance/upgrade/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation_id: operationId }),
    });
    const data = await res.json().catch(() => ({}));
    if (
      data &&
      (data.transition ||
        SYSTEM_ALIGNMENT_TRANSITION_STAGES.has(resolveSystemAlignmentStage(data)))
    ) {
      renderSystemAlignmentStatus(data);
    }
    if (
      res.ok &&
      data &&
      (data.reconnect ||
        data.stage === "admin_reconnect_pending" ||
        data.stage === "admin_update_pending")
    ) {
      // The replacement Admin is not ready yet; keep the reconnect overlay up.
      showReconnectOverlay(data.message);
      setUpgradeRunning(false);
      return;
    }
    if (!res.ok || !data.ok || !data.job_id) {
      renderUpgradeResult(data);
      setUpgradeRunning(false);
      return;
    }
    renderUpgradeSteps(data.steps);
    pollUpgradeJob(data.job_id);
  } catch (err) {
    renderUpgradeValidation([{ tone: "error", text: err.message || String(err) }], false);
    setUpgradeRunning(false);
  }
}

// After an Admin reconnect, a page reload, or a login that followed the Admin
// container being replaced, restore the Guided Upgrade from its server-side
// transition (the source of truth) and continue automatically. Backup and
// current-state preflight completed before the Admin was replaced and are never
// repeated; the durable transition + context carry that forward.
async function resumeGuidedUpgradeFromTransition(alignment) {
  const transition = (alignment && alignment.transition) || {};
  if (!upgradeTransitionIsActive(transition)) return false;
  const transitionTag = transition.system_tag;
  revealWorkspace();
  window.location.hash = "maintenance-upgrade";
  setAdminView("maintenance");
  // Open the panel and load the catalogue to completion with the transition tag
  // PINNED, so a server default/prepared release can never overwrite it. The
  // load is awaited (shared in-flight promise dedupes any concurrent hash-route
  // load), then the exact transition tag is confirmed before resuming.
  await setMaintenancePath("upgrade", transitionTag);
  if (!transitionTag || upgradeState.selected !== transitionTag) {
    // Fail closed: never resume against a build we could not deterministically
    // select from the catalogue.
    setUpgradeReleaseStatus();
    return true;
  }
  upgradeState.preparedTag = transitionTag;
  upgradeState.prepared = true;
  // Continue the whole upgrade from its durable operation id — Admin identity,
  // resource import and EMS deployment happen server-side without re-entering
  // options or re-running the backup.
  await resumeGuidedUpgrade(transition.operation_id);
  return true;
}

// --- backup / restore ----------------------------------------------------
// Admin orchestrates EMS-owned backups: it lists/inspects/verifies normal EMS
// archives, previews restores (dry-run first) and applies them behind a rollback
// backup. Every dynamic value (names, paths, messages) is escaped before it is
// written into innerHTML so backup content can never inject markup.

const backupEls = {
  message: document.getElementById("backup-message"),
  dir: document.getElementById("backup-dir"),
  count: document.getElementById("backup-count"),
  latest: document.getElementById("backup-latest"),
  statusWarnings: document.getElementById("backup-status-warnings"),
  refreshBtn: document.getElementById("backup-refresh"),
  scopeInputs: Array.from(document.querySelectorAll("[data-backup-scope]")),
  influxDesc: document.getElementById("backup-scope-influxdb-desc"),
  createBtn: document.getElementById("backup-create"),
  createSteps: document.getElementById("backup-create-steps"),
  list: document.getElementById("backup-list"),
  detailsStage: document.getElementById("backup-details-stage"),
  passwordForm: document.getElementById("backup-password-form"),
  passwordInput: document.getElementById("backup-password-input"),
  detailsFacts: document.getElementById("backup-details-facts"),
  detailsFiles: document.getElementById("backup-details-files"),
  restoreStage: document.getElementById("backup-restore-stage"),
  rollback: document.getElementById("backup-rollback"),
  autoRollback: document.getElementById("backup-auto-rollback"),
  restoreSummary: document.getElementById("backup-restore-summary"),
  restoreFiles: document.getElementById("backup-restore-files"),
  rollbackWarning: document.getElementById("backup-rollback-warning"),
  executeBtn: document.getElementById("backup-execute"),
  restoreSteps: document.getElementById("backup-restore-steps"),
};

const backupState = {
  backups: [],
  sets: [],
  selectedId: null,
  selectedKind: null,
  selectedType: null,
  selectedDetails: null,
  restorePlan: null,
  running: false,
  // Bumped per preview request so a slower earlier preview can never overwrite a
  // newer one after restore options changed.
  previewToken: 0,
};

const BACKUP_POLL_INTERVAL_MS = 1200;
const BACKUP_STEP_ICON = {
  done: "✓", failed: "×", running: "…", skipped: "–", pending: "○",
};
let backupPollTimer = null;

function backupValidationItem(tone, text, icon) {
  const mark = icon || (tone === "error" ? "×" : tone === "warn" ? "!" : "✓");
  return (
    '<div class="config-validation-item config-validation-item-' + tone +
    '"><span class="config-validation-icon" aria-hidden="true">' + mark +
    "</span><span>" + escapeHtml(text) + "</span></div>"
  );
}

function backupFact(label, value) {
  return (
    '<div class="control-pipeline-fact"><span class="maintenance-fact-label">' +
    escapeHtml(label) + '</span><span class="maintenance-fact-value">' +
    escapeHtml(value) + "</span></div>"
  );
}

function backupRowFact(label, value) {
  return (
    '<span class="backup-row-fact"><span>' + escapeHtml(label) +
    "</span><strong>" + escapeHtml(value) + "</strong></span>"
  );
}

function formatBackupBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / (1024 * 1024)).toFixed(1) + " MB";
}

function renderBackupMessage(items) {
  if (!backupEls.message) return;
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    backupEls.message.hidden = true;
    backupEls.message.textContent = "";
    return;
  }
  backupEls.message.hidden = false;
  backupEls.message.innerHTML = list
    .map((item) => backupValidationItem(item.tone || "info", item.text))
    .join("");
}

function setBackupBusy(running) {
  backupState.running = running;
  const buttons = [backupEls.refreshBtn, backupEls.createBtn];
  for (const btn of buttons) if (btn) btn.disabled = running;
  if (backupEls.executeBtn) {
    backupEls.executeBtn.disabled = running || !backupState.restorePlan ||
      backupState.restorePlan.blocked;
  }
  backupEls.list.querySelectorAll("button[data-backup-action]").forEach((btn) => {
    // Restore buttons disabled by markup (invalid archive) stay disabled after a
    // busy state clears; they only re-enable on a list reload.
    btn.disabled = running || btn.dataset.backupRestoreDisabled === "true";
  });
}

async function loadBackups() {
  try {
    const res = await fetch("/api/admin/maintenance/backups");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Backup list unavailable.");
    }
    backupState.backups = Array.isArray(data.backups) ? data.backups : [];
    backupState.sets = Array.isArray(data.sets) ? data.sets : [];
    renderBackupSummary(data);
    renderBackupList(data);
    renderBackupMessage([]);
  } catch (err) {
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
  }
}

function renderBackupSummary(data) {
  const summary = data.summary || {};
  backupEls.dir.textContent = data.backup_dir || "unknown";
  backupEls.count.textContent = String(summary.total || 0);
  backupEls.latest.textContent = summary.latest_created_at || "—";

  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  if (data.safe_location === false) {
    warnings.push("The backup directory is outside the EMS install root.");
  }
  backupEls.statusWarnings.innerHTML = warnings
    .map((text) => backupValidationItem("warn", text))
    .join("");

  const influx = data.influxdb || {};
  const influxInput = backupEls.scopeInputs.find(
    (el) => el.dataset.backupScope === "influxdb"
  );
  if (influxInput) {
    influxInput.disabled = !influx.supported;
    if (!influx.supported) influxInput.checked = false;
  }
  if (backupEls.influxDesc) {
    backupEls.influxDesc.textContent = influx.supported
      ? "Bundled InfluxDB data will be included."
      : influx.message || "Bundled InfluxDB is not enabled — skipped.";
  }
}

function backupSelectedScope() {
  const selected = backupEls.scopeInputs
    .filter((el) => el.checked && !el.disabled)
    .map((el) => el.dataset.backupScope);
  if (!selected.length) return null;
  if (selected.length === 1) return selected[0];
  return "system";
}

function renderBackupList(data) {
  const sets = Array.isArray(data.sets) ? data.sets : [];
  const backups = Array.isArray(data.backups) ? data.backups : [];
  if (!sets.length && !backups.length) {
    backupEls.list.innerHTML =
      '<p class="maintenance-note">No backups yet. Create one above.</p>';
    return;
  }
  const rows = [];
  for (const set of sets) rows.push(renderBackupSetRow(set));
  for (const backup of backups) rows.push(renderBackupRow(backup));
  backupEls.list.innerHTML = rows.join("");
}

function renderBackupRow(backup) {
  const facts = [
    backupRowFact("Created", backup.created_at || backup.mtime || "—"),
    backupRowFact("Size", formatBackupBytes(backup.size_bytes)),
    backupRowFact("Files", String(backup.files_count || 0)),
    backupRowFact("EMS", backup.source_version || "—"),
    backupRowFact("Build", backup.source_build || backup.source_commit || "—"),
    backupRowFact("Enc", backup.encrypted ? "yes" : "no"),
  ];
  const isInflux = backup.backup_type === "influxdb";
  const flags = [];
  if (!backup.valid) flags.push(backupValidationItem("error", backup.error || "invalid archive"));
  if (backup.locked) flags.push(backupValidationItem("warn", "encrypted — password required"));
  if (isInflux) {
    flags.push(backupValidationItem(
      "info",
      "InfluxDB restore uses the EMS CLI restore flow and replaces bundled " +
      "analytics data after confirmation."
    ));
  }
  const id = escapeHtml(backup.id);
  const backupName = backup.name || backup.id || "backup";
  const backupType = backup.backup_type || "backup";
  // An invalid archive cannot be restored; the marker keeps the button disabled
  // through the busy-state toggle (see setBackupBusy).
  const restoreDisabled = !backup.valid;
  const restoreAttrs = restoreDisabled
    ? ' disabled data-backup-restore-disabled="true"' : "";
  return (
    '<div class="backup-row" role="listitem">' +
    '<div class="backup-row-main">' +
    '<span class="backup-row-type source-badge source-mdns">' +
    escapeHtml(backupType) + "</span>" +
    '<span class="backup-row-name" title="' + escapeHtml(backupName) + '">' +
    escapeHtml(backupName) + "</span></div>" +
    '<div class="backup-row-meta" aria-label="Backup metadata">' + facts.join("") + "</div>" +
    '<div class="backup-row-actions">' +
    '<button type="button" class="secondary-button compact" data-backup-action="details" data-backup-id="' + id + '" data-backup-kind="archive">Details</button>' +
    '<button type="button" class="secondary-button compact" data-backup-action="restore" data-backup-id="' + id + '" data-backup-kind="archive" data-backup-type="' + escapeHtml(backup.backup_type || "config") + '"' + restoreAttrs + ">Restore preview</button>" +
    '<button type="button" class="secondary-button compact" data-backup-action="delete" data-backup-id="' + id + '" data-backup-kind="archive" data-backup-name="' + escapeHtml(backup.name) + '">Delete</button>' +
    "</div>" +
    (flags.length ? '<div class="backup-row-flags config-validation-list">' + flags.join("") + "</div>" : "") +
    "</div>"
  );
}

function renderBackupSetRow(set) {
  const members = (set.archives || [])
    .map((a) => escapeHtml(a.type || "") + (a.present ? "" : " (missing)"))
    .join(", ");
  const id = escapeHtml(set.id);
  // A set may include an InfluxDB member; its restore runs through the EMS CLI
  // flow (the preview validates it) rather than the generic file restore path.
  const hasInflux = (set.archives || []).some((a) => a.type === "influxdb");
  const flags = hasInflux
    ? backupValidationItem(
        "info",
        "Includes InfluxDB — restore uses the EMS CLI flow and replaces " +
        "bundled analytics data after confirmation."
      )
    : "";
  const restoreAttrs = "";
  const setName = set.label || set.id || "backup set";
  return (
    '<div class="backup-row backup-row-set" role="listitem">' +
    '<div class="backup-row-main">' +
    '<span class="backup-row-type source-badge source-scan">set</span>' +
    '<span class="backup-row-name" title="' + escapeHtml(setName) + '">' +
    escapeHtml(setName) + "</span></div>" +
    '<div class="backup-row-meta" aria-label="Backup metadata">' +
    backupRowFact("Created", set.created_at || "—") +
    backupRowFact("Status", set.status || "—") +
    backupRowFact("Members", members || "—") +
    "</div>" +
    '<div class="backup-row-actions">' +
    '<button type="button" class="secondary-button compact" data-backup-action="restore" data-backup-id="' + id + '" data-backup-kind="set" data-backup-type="system"' + restoreAttrs + ">Restore preview</button>" +
    '<button type="button" class="secondary-button compact" data-backup-action="delete" data-backup-id="' + id + '" data-backup-kind="set" data-backup-name="' + escapeHtml(set.label || set.id) + '">Delete</button>' +
    "</div>" +
    (flags ? '<div class="backup-row-flags config-validation-list">' + flags + "</div>" : "") +
    "</div>"
  );
}

function selectBackup(id, kind, type) {
  backupState.selectedId = id;
  backupState.selectedKind = kind || "archive";
  backupState.selectedType = type || "config";
  backupState.selectedDetails = null;
  backupState.restorePlan = null;
  // Clear the restore stage content without forcing it open; the restore action
  // opens it, the details action does not.
  backupEls.restoreSummary.innerHTML = "";
  backupEls.restoreFiles.innerHTML = "";
  backupEls.restoreSteps.innerHTML = "";
  backupEls.rollbackWarning.hidden = true;
  backupEls.executeBtn.disabled = true;
}

async function inspectSelectedBackup(password) {
  if (!backupState.selectedId) return;
  backupEls.detailsStage.hidden = false;
  try {
    const res = await fetch("/api/admin/maintenance/backups/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: backupState.selectedId, password: password || null }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Backup could not be inspected.");
    }
    backupState.selectedDetails = data;
    renderBackupDetails(data);
  } catch (err) {
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
  }
}

function renderBackupDetails(data) {
  const locked = Boolean(data.locked);
  backupEls.passwordForm.hidden = !locked;
  if (locked) {
    backupEls.detailsFacts.innerHTML = "";
    backupEls.detailsFiles.innerHTML =
      '<p class="maintenance-note">This backup is encrypted. Enter its password to inspect it.</p>';
    return;
  }
  const manifest = data.manifest || {};
  const source = manifest.source || {};
  const facts = [
    backupFact("Type", manifest.backup_type || "—"),
    backupFact("Purpose", manifest.backup_purpose || "—"),
    backupFact("Created", manifest.created_at || "—"),
    backupFact("Source version", source.ems_version || "—"),
    backupFact("Source build", source.build_label || source.git_describe || source.git_commit_short || "—"),
    backupFact("Source branch", source.git_branch || "—"),
    backupFact("Files", String(manifest.files_count || 0)),
    backupFact("Sensitive files", String(manifest.sensitive_count || 0)),
  ];
  backupEls.detailsFacts.innerHTML = facts.join("");
  const files = Array.isArray(manifest.files) ? manifest.files : [];
  backupEls.detailsFiles.innerHTML = files
    .map((file) => {
      const tags = [];
      if (file.sensitive) tags.push("sensitive");
      if (file.privacy_relevant) tags.push("privacy");
      if (!file.has_checksum) tags.push("no checksum");
      const suffix = tags.length ? " — " + tags.join(", ") : "";
      return (
        '<div class="backup-file" role="listitem"><span class="backup-file-path">' +
        escapeHtml(file.path) + '</span><span class="backup-file-kind">' +
        escapeHtml((file.kind || "") + suffix) + "</span></div>"
      );
    })
    .join("");
}

async function createBackup() {
  if (backupState.running) return;
  const scope = backupSelectedScope();
  if (!scope) {
    renderBackupMessage([{ tone: "warn", text: "Select at least one backup scope." }]);
    return;
  }
  setBackupBusy(true);
  backupEls.createSteps.innerHTML = "";
  renderBackupMessage([{ tone: "info", text: "Creating backup…" }]);
  try {
    const res = await fetch("/api/admin/maintenance/backups/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: scope }),
    });
    const data = await res.json();
    if (!res.ok || !data.job_id) {
      throw new Error((data && data.error) || "Backup could not be started.");
    }
    renderBackupJobSteps(data.steps, backupEls.createSteps);
    pollBackupJob(data.job_id, "create");
  } catch (err) {
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
    setBackupBusy(false);
  }
}

async function previewRestore() {
  if (!backupState.selectedId) {
    renderBackupMessage([{ tone: "warn", text: "Select a backup first." }]);
    return;
  }
  const token = ++backupState.previewToken;
  setBackupBusy(true);
  renderBackupMessage([]);
  try {
    const res = await fetch("/api/admin/maintenance/backups/restore/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: backupState.selectedId,
        scope: backupState.selectedType || "config",
        password: (backupEls.passwordInput && backupEls.passwordInput.value) || null,
        conflict_policy: "replace",
        rollback: backupEls.rollback.checked,
        auto_rollback: backupEls.autoRollback.checked,
      }),
    });
    const data = await res.json();
    // A newer preview (options changed mid-flight) already superseded this one.
    if (token !== backupState.previewToken) return;
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Restore preview failed.");
    }
    backupState.restorePlan = data;
    renderRestorePlan(data);
  } catch (err) {
    if (token !== backupState.previewToken) return;
    backupState.restorePlan = null;
    renderRestorePlan(null);
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
  } finally {
    if (token === backupState.previewToken) setBackupBusy(false);
  }
}

// Restore options changed, so any existing preview no longer matches the request
// the user would apply. Drop the plan (and its plan_id) and re-preview so Restore
// stays disabled until a fresh, matching preview succeeds.
function refreshRestorePreviewFromOptions() {
  if (!backupState.selectedId || backupEls.restoreStage.hidden) return;
  backupState.restorePlan = null;
  backupEls.executeBtn.disabled = true;
  previewRestore();
}

function renderRestorePlan(plan) {
  backupEls.restoreStage.hidden = false;
  if (!plan) {
    backupEls.restoreSummary.innerHTML = "";
    backupEls.restoreFiles.innerHTML = "";
    backupEls.rollbackWarning.hidden = true;
    backupEls.executeBtn.disabled = true;
    return;
  }
  const summary = plan.summary || {};
  backupEls.restoreSummary.innerHTML = [
    backupFact("Will restore", String(summary.would_restore || 0)),
    backupFact("Will replace", String(summary.would_replace || 0)),
    backupFact("Will skip", String(summary.would_skip || 0)),
  ].join("");
  const files = Array.isArray(plan.files) ? plan.files : [];
  backupEls.restoreFiles.innerHTML = files
    .map((file) => (
      '<div class="backup-file" role="listitem"><span class="backup-file-path">' +
      escapeHtml(file.path) + '</span><span class="backup-file-kind">' +
      escapeHtml(file.action || "") + "</span></div>"
    ))
    .join("");

  const notes = [];
  if (plan.blocked) {
    notes.push("Restore is blocked: " + (plan.block_reason || "resolve the issues above") + ".");
  }
  if (!backupEls.rollback.checked) {
    notes.push("Rollback backup is disabled — the current state will not be captured.");
  }
  (plan.warnings || []).forEach((warning) => notes.push(warning));
  if (notes.length) {
    backupEls.rollbackWarning.hidden = false;
    backupEls.rollbackWarning.innerHTML = notes.map(escapeHtml).join("<br>");
  } else {
    backupEls.rollbackWarning.hidden = true;
  }
  backupEls.executeBtn.disabled = Boolean(plan.blocked) || backupState.running;
}

async function executeRestore() {
  const plan = backupState.restorePlan;
  if (!plan || plan.blocked || !plan.plan_id) return;
  const involvesInflux = (plan.targets || []).some(
    (t) => t.backup_type === "influxdb"
  );
  const confirmMessage = involvesInflux
    ? "Restore this backup? Bundled InfluxDB analytics data may be replaced. " +
      "A rollback backup is created first when enabled."
    : "Restore this backup? Existing files may be overwritten. A rollback " +
      "backup is created first when enabled.";
  if (!window.confirm(confirmMessage)) return;
  setBackupBusy(true);
  backupEls.restoreSteps.innerHTML = "";
  renderBackupMessage([{ tone: "info", text: "Restoring…" }]);
  try {
    const res = await fetch("/api/admin/maintenance/backups/restore/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: plan.plan_id, confirm: true }),
    });
    const data = await res.json();
    if (!res.ok || !data.job_id) {
      throw new Error((data && data.error) || "Restore could not be started.");
    }
    renderBackupJobSteps(data.steps, backupEls.restoreSteps);
    pollBackupJob(data.job_id, "restore");
  } catch (err) {
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
    setBackupBusy(false);
  }
}

function renderBackupJobSteps(steps, container) {
  if (!container) return;
  const list = Array.isArray(steps) ? steps : [];
  container.innerHTML = list
    .map((step) => {
      const state = step.state || "pending";
      const icon = BACKUP_STEP_ICON[state] || "○";
      const tone = state === "failed" ? "error" : state === "skipped" ? "warn" : "info";
      const label = step.label || step.key || "";
      const text = step.message ? label + " — " + step.message : label;
      return backupValidationItem(tone, text, icon);
    })
    .join("");
}

function stopBackupPolling() {
  if (backupPollTimer) {
    clearTimeout(backupPollTimer);
    backupPollTimer = null;
  }
}

async function pollBackupJob(jobId, kind) {
  const container = kind === "restore" ? backupEls.restoreSteps : backupEls.createSteps;
  try {
    const res = await fetch(
      "/api/admin/maintenance/backups/jobs/" + encodeURIComponent(jobId)
    );
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Backup status unavailable.");
    }
    renderBackupJobSteps(data.steps, container);
    if (data.status === "running") {
      backupPollTimer = setTimeout(() => pollBackupJob(jobId, kind), BACKUP_POLL_INTERVAL_MS);
      return;
    }
    stopBackupPolling();
    const result = data.result || {};
    renderBackupJobResult(result, kind);
    setBackupBusy(false);
    loadBackups();
  } catch (err) {
    stopBackupPolling();
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
    setBackupBusy(false);
  }
}

function renderBackupJobResult(result, kind) {
  if (result.ok) {
    const text = kind === "restore"
      ? "Restore completed. EMS may need a restart/recreate to use restored files."
      : "Backup created and verified.";
    renderBackupMessage([{ tone: "info", text: text }]);
    if (kind === "restore") backupState.restorePlan = null;
    return;
  }
  const tone = result.status === "rollback_failed" ? "error" : "warn";
  renderBackupMessage([
    { tone: tone, text: result.message || "The backup job did not complete." },
  ]);
}

async function deleteBackup(id, kind, name) {
  if (backupState.running) return;
  const label = name || "this backup";
  if (!window.confirm("Delete " + label + "? This cannot be undone.")) return;
  const body = { id: id, confirm: true };
  if (kind === "set") {
    const alsoArchives = window.confirm(
      "Also delete the backup archive files in this set?\n\n" +
      "OK = delete metadata and archives, Cancel = delete metadata only."
    );
    body.mode = alsoArchives ? "metadata_and_archives" : "metadata_only";
  }
  try {
    const res = await fetch("/api/admin/maintenance/backups/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Backup could not be deleted.");
    }
    if (backupState.selectedId === id) {
      backupState.selectedId = null;
      backupEls.detailsStage.hidden = true;
      backupEls.restoreStage.hidden = true;
    }
    renderBackupMessage([{ tone: "info", text: "Deleted " + label + "." }]);
    loadBackups();
  } catch (err) {
    renderBackupMessage([{ tone: "error", text: err.message || String(err) }]);
  }
}

if (backupEls.list) {
  backupEls.list.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-backup-action]");
    if (!button) return;
    const id = button.dataset.backupId;
    const kind = button.dataset.backupKind;
    const action = button.dataset.backupAction;
    if (action === "details") {
      selectBackup(id, kind, button.dataset.backupType);
      inspectSelectedBackup(null);
    } else if (action === "restore") {
      selectBackup(id, kind, button.dataset.backupType);
      backupEls.restoreStage.hidden = false;
      previewRestore();
    } else if (action === "delete") {
      deleteBackup(id, kind, button.dataset.backupName);
    }
  });
}
if (backupEls.refreshBtn) backupEls.refreshBtn.addEventListener("click", loadBackups);
if (backupEls.createBtn) backupEls.createBtn.addEventListener("click", createBackup);
if (backupEls.rollback) backupEls.rollback.addEventListener("change", refreshRestorePreviewFromOptions);
if (backupEls.autoRollback) backupEls.autoRollback.addEventListener("change", refreshRestorePreviewFromOptions);
if (backupEls.executeBtn) backupEls.executeBtn.addEventListener("click", executeRestore);
if (backupEls.passwordForm) {
  backupEls.passwordForm.addEventListener("submit", (event) => {
    event.preventDefault();
    inspectSelectedBackup(backupEls.passwordInput.value || null);
  });
}

// --- EMS diagnostics (read-only) -----------------------------------------
// User-triggered allowlisted read-only checks from the installed EMS, run via
// the backend bridge. The frontend only POSTs to the run endpoint (it never
// sends command input) and renders every value through textContent/createElement
// so EMS output cannot inject markup. No Apply/Fix/Restart/Upgrade action is
// exposed here; the card stays collapsed and is never auto-run.

const diagnosticsEls = {
  summary: document.getElementById("maintenance-diagnostics-summary"),
  mode: document.getElementById("maintenance-diagnostics-mode"),
  checks: document.getElementById("maintenance-diagnostics-checks"),
  note: document.getElementById("maintenance-diagnostics-note"),
  run: document.getElementById("maintenance-diagnostics-run"),
};

const DIAGNOSTICS_MODE_LABELS = {
  container: "container mode",
  local: "local emsctl.py",
  unavailable: "unavailable",
};

const DIAGNOSTICS_STATUS_TONE = {
  ok: "ok",
  // A subsystem disabled by config is expected, not a problem: neutral tone.
  disabled: "info",
  warning: "warn",
  failed: "error",
  timeout: "error",
  unavailable: null,
  not_run: null,
};

function diagnosticsFirstLine(text) {
  if (!text) return "";
  const lines = String(text).split("\n").map((line) => line.trim()).filter(Boolean);
  return lines.length ? lines[0] : "";
}

function diagnosticsCheckMessage(check) {
  // A backend-supplied message (e.g. the disabled-InfluxDB note) is the
  // user-facing text; the raw stderr stays behind the raw-output toggle.
  if (check.message) return check.message;
  if (check.status === "timeout") return "Check timed out.";
  if (check.status === "unavailable") {
    return diagnosticsFirstLine(check.stderr) || "Check could not run.";
  }
  if (check.status === "ok") return "";
  return (
    diagnosticsFirstLine(check.stderr) ||
    diagnosticsFirstLine(check.stdout) ||
    (typeof check.exit_code === "number" ? "Exit code " + check.exit_code : "")
  );
}

function diagnosticsRawText(check) {
  const parts = [];
  if (check.stdout) parts.push(String(check.stdout));
  if (check.stderr) parts.push("stderr:\n" + String(check.stderr));
  return parts.join("\n\n");
}

function renderDiagnosticsCheck(check) {
  const row = document.createElement("div");
  row.className = "maintenance-check";

  const head = document.createElement("div");
  head.className = "maintenance-check-head";

  const label = document.createElement("span");
  label.className = "maintenance-check-label";
  label.textContent = check.label || check.id || "check";
  head.appendChild(label);

  if (typeof check.duration_ms === "number") {
    const duration = document.createElement("span");
    duration.className = "maintenance-check-duration";
    duration.textContent = check.duration_ms + " ms";
    head.appendChild(duration);
  }

  const pill = document.createElement("span");
  pill.className = "maintenance-check-pill";
  pill.textContent = check.status || "not_run";
  const tone = DIAGNOSTICS_STATUS_TONE[check.status];
  if (tone) pill.dataset.tone = tone;
  head.appendChild(pill);
  row.appendChild(head);

  const message = diagnosticsCheckMessage(check);
  if (message) {
    const note = document.createElement("p");
    note.className = "maintenance-check-message";
    note.textContent = message;
    row.appendChild(note);
  }

  const raw = diagnosticsRawText(check);
  if (raw) {
    const details = document.createElement("details");
    details.className = "maintenance-check-raw";
    const summary = document.createElement("summary");
    summary.textContent = "Raw output" + (check.truncated ? " (truncated)" : "");
    details.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = raw;
    details.appendChild(pre);
    row.appendChild(details);
  }
  return row;
}

function diagnosticsSummaryLine(data) {
  const modeLabel = DIAGNOSTICS_MODE_LABELS[data.mode] || data.mode || "unavailable";
  if (!data.available) {
    return { text: "EMS CLI unavailable · " + modeLabel, tone: "warn" };
  }
  const summary = data.summary || {};
  const parts = ["EMS CLI available", modeLabel];
  if (summary.ok) parts.push(summary.ok + " ok");
  if (summary.disabled) parts.push(summary.disabled + " disabled");
  if (summary.warning) parts.push(summary.warning + " warning");
  if (summary.failed) parts.push(summary.failed + " failed");
  if (summary.unavailable) parts.push(summary.unavailable + " unavailable");
  return { text: parts.join(" · "), tone: summary.status === "ok" ? "ok" : "warn" };
}

function renderDiagnostics(data) {
  const available = Boolean(data.available);
  const modeLabel = DIAGNOSTICS_MODE_LABELS[data.mode] || data.mode || "unavailable";
  setMaintenanceFact(diagnosticsEls.mode, modeLabel, available ? null : "muted");

  if (diagnosticsEls.checks) {
    diagnosticsEls.checks.textContent = "";
    const checks = Array.isArray(data.checks) ? data.checks : [];
    for (const check of checks) {
      diagnosticsEls.checks.appendChild(renderDiagnosticsCheck(check));
    }
  }

  if (diagnosticsEls.note) {
    diagnosticsEls.note.textContent = available
      ? ""
      : data.message ||
        "EMS CLI diagnostics are not available for this installation state.";
  }

  const line = diagnosticsSummaryLine(data);
  setMaintenanceFact(diagnosticsEls.summary, line.text, line.tone);
}

function renderDiagnosticsError() {
  if (diagnosticsEls.note) {
    diagnosticsEls.note.textContent =
      "Could not run EMS diagnostics. The Admin server may be unavailable.";
  }
  setMaintenanceFact(diagnosticsEls.summary, "EMS CLI diagnostics failed", "warn");
}

let diagnosticsRunning = false;

async function runDiagnostics() {
  if (diagnosticsRunning) return;
  diagnosticsRunning = true;
  const button = diagnosticsEls.run;
  if (button) {
    button.disabled = true;
    button.textContent = "Running…";
  }
  try {
    const resp = await fetch("/api/admin/maintenance/diagnostics/run", {
      method: "POST",
    });
    if (!resp.ok) throw new Error("diagnostics request failed");
    renderDiagnostics(await resp.json());
  } catch (err) {
    renderDiagnosticsError();
  } finally {
    diagnosticsRunning = false;
    if (button) {
      button.disabled = false;
      button.textContent = "Run diagnostics";
    }
  }
}

if (diagnosticsEls.run) {
  diagnosticsEls.run.addEventListener("click", runDiagnostics);
}

// --- maintenance config & hardware ---------------------------------------
// Loads the real resolved config, lets the operator edit an in-memory draft,
// and previews a validation + diff. No Apply/Save/Backup/Restart control is
// wired: this step never writes. Every dynamic value is inserted via
// textContent/DOM nodes so config-derived text cannot inject markup.

const mconfigEls = {
  summary: document.getElementById("maintenance-config-summary"),
  source: document.getElementById("maintenance-config-source"),
  message: document.getElementById("maintenance-config-message"),
  editor: document.getElementById("maintenance-config-editor"),
  gridMeter: document.getElementById("maintenance-config-gridmeter"),
  inverters: document.getElementById("maintenance-config-inverters"),
  addInverter: document.getElementById("maintenance-config-add-inverter"),
  addMqttDevice: document.getElementById("maintenance-config-add-mqtt-device"),
  maintenanceManualBrokerForm: document.getElementById("maintenance-manual-mqtt-broker-form"),
  maintenanceManualBrokerName: document.getElementById("maintenance-manual-mqtt-broker-name"),
  maintenanceManualBrokerHost: document.getElementById("maintenance-manual-mqtt-broker-host"),
  maintenanceManualBrokerPort: document.getElementById("maintenance-manual-mqtt-broker-port"),
  maintenanceManualBrokerSecurity: document.getElementById("maintenance-manual-mqtt-broker-security"),
  maintenanceManualBrokerUsername: document.getElementById("maintenance-manual-mqtt-broker-username"),
  maintenanceManualBrokerPassword: document.getElementById("maintenance-manual-mqtt-broker-password"),
  brokerHelp: document.getElementById("maintenance-mqtt-broker-help"),
  brokerHost: document.getElementById("maintenance-mqtt-broker-host"),
  brokerPort: document.getElementById("maintenance-mqtt-broker-port"),
  brokerSecurity: document.getElementById("maintenance-mqtt-broker-security"),
  brokerUsername: document.getElementById("maintenance-mqtt-broker-username"),
  brokerPassword: document.getElementById("maintenance-mqtt-broker-password"),
  brokerClearField: document.getElementById("maintenance-mqtt-broker-clear-field"),
  brokerClear: document.getElementById("maintenance-mqtt-broker-clear"),
  brokerCard: document.getElementById("maintenance-broker-card"),
  brokerBody: document.getElementById("maintenance-broker-body"),
  brokerModel: document.getElementById("maintenance-broker-model"),
  brokerMeta: document.getElementById("maintenance-broker-meta"),
  brokerStatus: document.getElementById("maintenance-broker-status"),
  discoveryStart: document.getElementById("maintenance-discovery-start"),
  discoveryCount: document.getElementById("maintenance-discovery-count"),
  discoveryStatus: document.getElementById("maintenance-discovery-status"),
  discoveryReview: document.getElementById("maintenance-discovery-review"),
  discoveryResults: document.getElementById("maintenance-discovery-results"),
  discoveryReset: document.getElementById("maintenance-discovery-reset"),
  discoveryManualForm: document.getElementById("maintenance-discovery-manual-form"),
  discoveryManualInput: document.getElementById("maintenance-discovery-manual-input"),
  discoveryManualScan: document.getElementById("maintenance-discovery-manual-scan"),
  discoveryError: document.getElementById("maintenance-discovery-error"),
  discoveryProgress: document.getElementById("maintenance-discovery-progress"),
  discoveryProgressBar: document.getElementById("maintenance-discovery-progress-bar"),
  discoveryProgressText: document.getElementById("maintenance-discovery-progress-text"),
  features: document.getElementById("maintenance-config-features"),
  advanced: document.getElementById("maintenance-config-advanced"),
  previewBtn: document.getElementById("maintenance-config-preview-btn"),
  resetBtn: document.getElementById("maintenance-config-reset-btn"),
  resetRuntimeBtn: document.getElementById("maintenance-config-reset-runtime-btn"),
  result: document.getElementById("maintenance-config-result"),
  validation: document.getElementById("maintenance-config-validation"),
  changeSummary: document.getElementById("maintenance-config-change-summary"),
  changes: document.getElementById("maintenance-config-changes"),
  warnings: document.getElementById("maintenance-config-warnings"),
  raw: document.getElementById("maintenance-config-raw"),
  rawPre: document.getElementById("maintenance-config-raw-pre"),
  applyPanel: document.getElementById("maintenance-config-apply-panel"),
  backup: document.getElementById("maintenance-config-backup"),
  applyBtn: document.getElementById("maintenance-config-apply-btn"),
  applyStatus: document.getElementById("maintenance-config-apply-status"),
  applyRollback: document.getElementById("maintenance-config-apply-rollback"),
  postApply: document.getElementById("maintenance-config-post-apply"),
  postEmsDesired: document.getElementById("maintenance-post-ems-desired"),
  postInfluxDesired: document.getElementById("maintenance-post-influx-desired"),
  postActionSummary: document.getElementById("maintenance-post-action-summary"),
  containersSync: document.getElementById("maintenance-containers-sync"),
  containersRecheck: document.getElementById("maintenance-containers-recheck"),
  postDiagnostics: document.getElementById("maintenance-post-diagnostics"),
  containersSyncStatus: document.getElementById("maintenance-containers-sync-status"),
};

const mconfigState = {
  loaded: false,
  draft: null,
  pristine: null,
  catalog: null,
  overrides: null,
  revision: null,
  previewFingerprint: null,
  summaryLine: "",
  openHardware: new Set(),
  openFeatures: new Set(),
  discoveryDraftChanges: 0,
};

function mconfigClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function mconfigLabelRow(labelText, control, description, unit) {
  const row = document.createElement("label");
  row.className = "feature-field-row";
  const label = document.createElement("span");
  label.className = "feature-field-label";
  label.textContent = labelText;
  row.appendChild(label);
  const controlWrap = document.createElement("span");
  controlWrap.className = "feature-field-control";
  controlWrap.appendChild(control);
  if (unit) {
    const unitNode = document.createElement("span");
    unitNode.className = "feature-unit";
    unitNode.textContent = unit;
    controlWrap.appendChild(unitNode);
  }
  row.appendChild(controlWrap);
  const help = document.createElement("span");
  help.className = "feature-field-desc";
  help.textContent = description || "";
  row.appendChild(help);
  return row;
}

function mconfigTextControl(value, onChange, type) {
  const input = document.createElement("input");
  input.type = type || "text";
  input.className = "feature-input";
  input.value = value == null ? "" : String(value);
  input.addEventListener("input", () => onChange(input.value));
  return input;
}

function mconfigCheckboxControl(checked, onChange) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.className = "feature-input";
  input.checked = Boolean(checked);
  input.addEventListener("change", () => onChange(input.checked));
  return input;
}

function mconfigSelectControl(value, options, onChange) {
  const select = document.createElement("select");
  select.className = "feature-input";
  for (const option of options) {
    const node = document.createElement("option");
    node.value = option.value;
    node.textContent = option.label;
    if (String(option.value) === String(value)) node.selected = true;
    select.appendChild(node);
  }
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

// --- shared hardware catalog (parity with the setup Config step) ----------
// Maintenance renders hardware fields from catalog.hardware_sections — the
// same central metadata (ems.config_catalog) the setup flow uses — so both
// editors offer identical fields, labels, units and levels.

function mconfigHardwareSection(id) {
  const sections =
    (mconfigState.catalog && mconfigState.catalog.hardware_sections) || [];
  return sections.find((section) => section.id === id) || null;
}

function mconfigDeviceCatalogFields() {
  const section = mconfigHardwareSection("devices");
  return section && Array.isArray(section.fields) ? section.fields : [];
}

function mconfigCatalogControl(field, value, onChange, opts) {
  if (field.type === "boolean") {
    return mconfigCheckboxControl(value, onChange);
  }
  if (Array.isArray(field.options) && field.options.length) {
    const current = value == null ? "" : String(value);
    const options = field.options.map((opt) => ({
      value: String(opt),
      label: String(opt),
    }));
    // Hardware rows show an unset value as "—" instead of silently displaying
    // the first option; picking a real value stays an explicit edit. The
    // effective inherited default is named so the row is never blank.
    if (opts && opts.allowUnset && !options.some((opt) => opt.value === current)) {
      options.unshift({
        value: "",
        label: opts.defaultValue != null ? "— (default: " + opts.defaultValue + ")" : "—",
      });
    }
    return mconfigSelectControl(current, options, onChange);
  }
  const numeric = field.type === "integer" || field.type === "number";
  const display = Array.isArray(value) ? value.join(", ") : value;
  const input = mconfigTextControl(display, onChange, numeric ? "number" : "text");
  if (opts && opts.defaultValue != null && (value == null || value === "")) {
    input.placeholder = String(opts.defaultValue) + " (default)";
  }
  return input;
}

function mconfigCatalogRow(field, value, onChange, opts) {
  return mconfigLabelRow(
    field.label || field.path,
    mconfigCatalogControl(field, value, onChange, opts),
    field.description || "",
    field.unit || ""
  );
}

function mconfigOverrideEntry(path) {
  return (mconfigState.overrides && mconfigState.overrides[path]) || null;
}

function mconfigDeviceOverrideEntry(name, key) {
  if (!name) return null;
  const devices = mconfigState.overrides && mconfigState.overrides.devices;
  const device = devices && devices[name];
  return (device && device[key]) || null;
}

function mconfigFormatOverrideValue(value) {
  if (value === true) return "on";
  if (value === false) return "off";
  return String(value);
}

function mconfigOverrideBadge(entry) {
  if (!entry || entry.source !== "dashboard_override") return null;
  const badge = document.createElement("span");
  badge.className = "mconfig-override-badge";
  badge.textContent =
    "Live override · effective " + mconfigFormatOverrideValue(entry.effective_value);
  badge.title =
    "The live EMS currently uses " +
    mconfigFormatOverrideValue(entry.effective_value) +
    " for this setting (set from the Dashboard control tab), not the installed " +
    "config value " +
    mconfigFormatOverrideValue(entry.config_value) +
    ". Applying this field makes the live value match the config again.";
  return badge;
}

function mconfigAttachOverrideBadge(row, entry) {
  const badge = mconfigOverrideBadge(entry);
  if (badge) {
    const desc = row.querySelector(".feature-field-desc");
    if (desc) desc.insertBefore(badge, desc.firstChild);
    else row.appendChild(badge);
  }
  return row;
}

// One shared level splitter for catalog-driven card bodies: normal fields
// render first, advanced and expert nest in collapsed details — the same
// areas the setup hardware cards use.
function mconfigLevelledFields(fields, renderRow) {
  const levels = { normal: [], advanced: [], expert: [] };
  fields.forEach((field) => {
    if (FEATURE_LEVELS_HIDDEN.has(field.level)) return;
    const level =
      field.level === "advanced" || field.level === "expert"
        ? field.level
        : "normal";
    levels[level].push(field);
  });
  const body = document.createElement("div");
  const normal = document.createElement("div");
  normal.className = "mconfig-fields feature-fields";
  levels.normal.forEach((field) => normal.appendChild(renderRow(field)));
  body.appendChild(normal);
  [
    ["advanced", "Advanced settings", "feature-advanced"],
    ["expert", "Developer / expert settings", "feature-expert"],
  ].forEach(([level, label, cssClass]) => {
    if (!levels[level].length) return;
    const details = document.createElement("details");
    details.className = cssClass;
    const summary = document.createElement("summary");
    summary.textContent = label;
    const list = document.createElement("div");
    list.className = "mconfig-fields feature-fields";
    levels[level].forEach((field) => list.appendChild(renderRow(field)));
    details.append(summary, list);
    body.appendChild(details);
  });
  return body;
}

function mconfigSetExpanded(card, body, caret, buttons, open) {
  card.dataset.open = open ? "true" : "false";
  body.hidden = !open;
  caret.textContent = open ? "▾" : "▸";
  buttons.forEach((button) => button.setAttribute("aria-expanded", open ? "true" : "false"));
}

function mconfigHardwareCard(options) {
  const card = document.createElement("article");
  // Hardware role owns the card class; the transport stays separate metadata.
  card.className = hardwareCardClass(options.role);
  card.dataset.sourceId = options.id;
  if (options.connectionSource) card.dataset.connection = options.connectionSource;

  const head = document.createElement("div");
  head.className = "hardware-card-head";
  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "hardware-card-summary";
  const title = document.createElement("span");
  title.className = "hardware-card-title";
  title.textContent = options.title;
  const model = document.createElement("span");
  model.className = "hardware-card-model";
  model.textContent = options.model;
  const meta = document.createElement("span");
  meta.className = "hardware-card-meta";
  meta.textContent = options.meta;
  summary.append(title, model, meta);

  const actions = document.createElement("div");
  actions.className = "hardware-card-actions";
  const status = document.createElement("span");
  status.className = "hardware-card-status";
  status.textContent =
    options.statusText || (options.enabled ? "Enabled" : "Disabled");
  actions.appendChild(status);
  if (options.connectionSource) {
    const pill = document.createElement("span");
    pill.className = "connection-pill";
    pill.dataset.connection = options.connectionSource;
    pill.textContent = connectionLabelFor(options.connectionSource);
    actions.appendChild(pill);
  }
  (options.actions || []).forEach((node) => actions.appendChild(node));
  if (options.onRemove) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "hardware-card-remove secondary-button compact";
    remove.textContent = "Remove";
    remove.addEventListener("click", options.onRemove);
    actions.appendChild(remove);
  }
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "hardware-card-toggle";
  toggle.setAttribute("aria-label", "Expand " + options.title);
  const caret = document.createElement("span");
  caret.setAttribute("aria-hidden", "true");
  toggle.appendChild(caret);
  actions.appendChild(toggle);
  head.append(summary, actions);
  card.appendChild(head);

  const body = document.createElement("div");
  body.className = "hardware-card-body";
  body.id = "maintenance-hardware-body-" + options.id.replace(/[^a-z0-9]/gi, "-");
  body.appendChild(options.body);
  card.appendChild(body);
  summary.setAttribute("aria-controls", body.id);
  toggle.setAttribute("aria-controls", body.id);
  const buttons = [summary, toggle];
  const setOpen = (open) => {
    if (open) mconfigState.openHardware.add(options.id);
    else mconfigState.openHardware.delete(options.id);
    toggle.setAttribute("aria-label", (open ? "Collapse " : "Expand ") + options.title);
    mconfigSetExpanded(card, body, caret, buttons, open);
  };
  buttons.forEach((button) =>
    button.addEventListener("click", () => setOpen(card.dataset.open !== "true"))
  );
  setOpen(mconfigState.openHardware.has(options.id));
  return { element: card, model, meta, status };
}

// --- grid meter -----------------------------------------------------------
// The grid-meter card renders the same variant-specific fields as the setup
// flow: the shared catalog's grid_meter_variants decides which connection
// fields a meter type shows, and hardware_sections carries their metadata.

const MCONFIG_GRID_MQTT_PREFIX = "grid_meter.mqtt.";
// Connection values owned by a named broker profile (grid_meter.mqtt.broker_ref)
// are never edited inline beside the reference.
const MCONFIG_GRID_MQTT_CONNECTION_PATHS = new Set([
  "grid_meter.mqtt.host",
  "grid_meter.mqtt.port",
  "grid_meter.mqtt.username",
  "grid_meter.mqtt.password",
]);

function mconfigGridMeterVariant(type) {
  const variants =
    (mconfigState.catalog && mconfigState.catalog.grid_meter_variants) || {};
  return variants[type] || null;
}

function mconfigGridMeterCatalogFields(type) {
  const section = mconfigHardwareSection("grid_meter");
  const variant = mconfigGridMeterVariant(type);
  if (!section || !variant) return [];
  const allowed = new Set(variant.fields || []);
  return (section.fields || []).filter((field) => allowed.has(field.path));
}

function mconfigGridMeterIsMqtt(type) {
  const variant = mconfigGridMeterVariant(type);
  return Boolean(
    variant &&
      (variant.fields || []).some((path) =>
        path.startsWith(MCONFIG_GRID_MQTT_PREFIX)
      )
  );
}

function mconfigGridMqtt(meter) {
  if (!meter.mqtt || typeof meter.mqtt !== "object") meter.mqtt = {};
  return meter.mqtt;
}

function mconfigGridMeterValue(meter, path) {
  if (path.startsWith(MCONFIG_GRID_MQTT_PREFIX)) {
    const key = path.slice(MCONFIG_GRID_MQTT_PREFIX.length);
    return meter.mqtt ? meter.mqtt[key] : undefined;
  }
  const key = path.replace("grid_meter.", "");
  if (key === "channels") {
    return Array.isArray(meter.channels)
      ? meter.channels.join(", ")
      : meter.channels;
  }
  return meter[key];
}

function mconfigSetGridMeterValue(meter, path, value) {
  if (path.startsWith(MCONFIG_GRID_MQTT_PREFIX)) {
    const key = path.slice(MCONFIG_GRID_MQTT_PREFIX.length);
    const mqtt = mconfigGridMqtt(meter);
    if (String(value).trim() === "") delete mqtt[key];
    else mqtt[key] = value;
    return;
  }
  const key = path.replace("grid_meter.", "");
  if (key === "channels") {
    const parts = String(value)
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
    if (parts.length) meter.channels = parts;
    else delete meter.channels;
    return;
  }
  // The IP always writes (backend guards empty-ip semantics); other endpoint
  // values fall back to "unset" when cleared.
  if (key !== "ip" && String(value).trim() === "") delete meter[key];
  else meter[key] = value;
}

// The stored MQTT password is never displayed: an empty field keeps it, the
// explicit clear checkbox removes it — the same rules as the broker form.
function mconfigGridMqttPasswordRow(meter) {
  const mqtt = mconfigGridMqtt(meter);
  const input = document.createElement("input");
  input.type = "password";
  input.className = "feature-input";
  input.autocomplete = "new-password";
  input.placeholder = mqtt.has_password
    ? "Leave blank to keep the stored password"
    : "Optional MQTT password";
  input.addEventListener("input", () => {
    mqtt.password = input.value;
  });
  const rows = document.createElement("div");
  rows.appendChild(
    mconfigLabelRow(
      "MQTT password",
      input,
      "Stored passwords are never displayed."
    )
  );
  if (mqtt.has_password) {
    const clear = mconfigCheckboxControl(false, (checked) => {
      mqtt.clear_password = checked;
      input.disabled = checked;
      if (checked) {
        input.value = "";
        mqtt.password = "";
      }
    });
    rows.appendChild(
      mconfigLabelRow(
        "Clear stored password",
        clear,
        "Remove the stored MQTT password on apply."
      )
    );
  }
  return rows;
}

function mconfigD0SerialRow(meter, onTopicChange) {
  const mqtt = mconfigGridMqtt(meter);
  const control = mconfigTextControl(
    zendureD0SerialFromTopic(mqtt.topic),
    (v) => {
      const topic = zendureD0Topic(v);
      setD0TopicMode(meter, "auto");
      if (topic) mqtt.topic = topic;
      else delete mqtt.topic;
      onTopicChange();
    }
  );
  return mconfigLabelRow(
    "D0 serial number",
    control,
    "Used to generate Zendure/sensor/<serial>/totalPower automatically."
  );
}

function renderMaintenanceGridMeter() {
  const host = mconfigEls.gridMeter;
  if (!host) return;
  const cardId = "maintenance-grid-meter";
  host.textContent = "";
  const meter = mconfigState.draft.grid_meter || (mconfigState.draft.grid_meter = {});
  const type = String(meter.type || "");
  const isMqtt = mconfigGridMeterIsMqtt(type);
  const isD0 = type === "zendure_smartmeter_d0";
  let card;

  const variants =
    (mconfigState.catalog && mconfigState.catalog.grid_meter_variants) || {};
  const typeOptions = [{ value: "", label: "— none —" }].concat(
    Object.values(variants).map((variant) => ({
      value: variant.id,
      label: variant.label,
    }))
  );

  const typeWrap = document.createElement("div");
  typeWrap.className = "mconfig-fields feature-fields";
  typeWrap.appendChild(
    mconfigLabelRow(
      "Meter type",
      mconfigSelectControl(type, typeOptions, (v) => {
        meter.type = v;
        meter.present = Boolean(v);
        if (v) mconfigState.openHardware.add(cardId);
        else mconfigState.openHardware.delete(cardId);
        if (v === "zendure_smartmeter_d0") {
          const mqtt = mconfigGridMqtt(meter);
          if (!mqtt.payload_format) mqtt.payload_format = "number";
        }
        renderMaintenanceGridMeter();
      }),
      "Hardware/API family used to read grid import and export."
    )
  );

  const mqtt = meter.mqtt || {};
  const brokerManaged = isMqtt && Boolean(mqtt.broker_ref);
  let fields = mconfigGridMeterCatalogFields(type);
  if (brokerManaged) {
    fields = fields.filter(
      (field) => !MCONFIG_GRID_MQTT_CONNECTION_PATHS.has(field.path)
    );
  }
  if (isD0) {
    // The D0 topic is generated from the serial; keep it as an Advanced
    // read-back/override so the basic flow never asks for a raw MQTT topic.
    fields = fields.map((field) =>
      field.path === "grid_meter.mqtt.topic"
        ? Object.assign({}, field, { level: "advanced" })
        : field
    );
  }

  const updateMeta = () => {
    card.meta.textContent = mconfigGridMeterMeta(meter);
  };
  const renderRow = (field) => {
    if (field.path === "grid_meter.mqtt.password") {
      return mconfigGridMqttPasswordRow(meter);
    }
    const row = mconfigCatalogRow(
      field,
      mconfigGridMeterValue(meter, field.path),
      (v) => {
        mconfigSetGridMeterValue(meter, field.path, v);
        if (isD0 && field.path === "grid_meter.mqtt.topic") {
          setD0TopicMode(meter, "manual");
        }
        updateMeta();
      },
      { allowUnset: true }
    );
    if (field.path !== "grid_meter.ip") return row;
    // Port has no catalog entry; it stays a dedicated endpoint row after the
    // IP, like the setup grid-meter card.
    const wrap = document.createElement("div");
    wrap.appendChild(row);
    wrap.appendChild(
      mconfigLabelRow(
        "Port",
        mconfigTextControl(
          meter.port == null ? "" : meter.port,
          (v) => {
            if (String(v).trim() === "") delete meter.port;
            else meter.port = v;
            updateMeta();
          },
          "number"
        ),
        "Optional HTTP port."
      )
    );
    return wrap;
  };

  const body = document.createElement("div");
  body.appendChild(typeWrap);
  if (isD0) {
    const d0Wrap = document.createElement("div");
    d0Wrap.className = "mconfig-fields feature-fields";
    d0Wrap.appendChild(mconfigD0SerialRow(meter, updateMeta));
    body.appendChild(d0Wrap);
  }
  if (brokerManaged) {
    const note = document.createElement("p");
    note.className = "feature-field-desc mconfig-mqtt-note";
    note.textContent =
      'Connection settings are managed by the MQTT broker profile "' +
      mqtt.broker_ref +
      '". Edit the broker profile to change host or credentials.';
    body.appendChild(note);
  }
  body.appendChild(mconfigLevelledFields(fields, renderRow));

  const variant = mconfigGridMeterVariant(type);
  card = mconfigHardwareCard({
    role: "grid_meter",
    id: cardId,
    title: "Grid meter",
    model: variant ? variant.label : "No grid meter configured",
    meta: mconfigGridMeterMeta(meter),
    enabled: Boolean(meter.type),
    body,
    onRemove:
      meter.present || meter.type || meter.ip
        ? () => {
            mconfigState.openHardware.delete(cardId);
            mconfigState.draft.grid_meter = { present: false, type: "", ip: "" };
            renderMaintenanceGridMeter();
          }
        : null,
  });
  host.appendChild(card.element);
}

function mconfigGridMeterMeta(meter) {
  if (mconfigGridMeterIsMqtt(String(meter.type || ""))) {
    const mqtt = meter.mqtt || {};
    if (mqtt.broker_ref) return "Broker profile " + String(mqtt.broker_ref);
    if (mqtt.host) {
      return String(mqtt.host) + (mqtt.port ? ":" + String(mqtt.port) : "");
    }
    return meter.type ? "Broker missing" : "Not configured";
  }
  if (!meter.ip) return meter.type ? "Address missing" : "Not configured";
  return String(meter.ip) + (meter.port ? ":" + String(meter.port) : "");
}

// --- Zendure MQTT broker (top-level zendure_mqtt) -------------------------
// The stored password is never displayed. An empty password field keeps the
// existing secret; the explicit "Clear" checkbox removes it. No broker field is
// persisted to localStorage.

function mconfigBrokerDraft() {
  if (!mconfigState.draft) return null;
  const broker = mconfigState.draft.zendure_mqtt || (mconfigState.draft.zendure_mqtt = {});
  return broker;
}

function syncMaintenanceBrokerForm() {
  const broker = mconfigBrokerDraft();
  if (!broker || !mconfigEls.brokerHost) return;
  const catalog = mconfigState.catalog || {};
  // Named broker profiles are owned by Setup/Discovery. This top-level form only
  // edits a legacy single-broker config; for a named-broker config it is shown
  // read-only and a no-op apply never injects top-level host/port/tls fields.
  const named = broker.managed === "named";
  if (mconfigEls.brokerHelp) {
    mconfigEls.brokerHelp.textContent = named
      ? "This installation uses named MQTT broker profiles managed by Setup. " +
        "Edit broker connections there; they are shown read-only here."
      : (catalog.zendure_mqtt_broker && catalog.zendure_mqtt_broker.help) || "";
  }
  [
    mconfigEls.brokerHost,
    mconfigEls.brokerPort,
    mconfigEls.brokerSecurity,
    mconfigEls.brokerUsername,
    mconfigEls.brokerPassword,
    mconfigEls.brokerClear,
  ].forEach((el) => {
    if (el) el.disabled = named;
  });
  mconfigEls.brokerHost.value = broker.host || "";
  mconfigEls.brokerPort.value = broker.port == null ? "" : String(broker.port);
  if (mconfigEls.brokerSecurity) {
    mconfigEls.brokerSecurity.value = broker.tls ? "tls" : "plain";
  }
  mconfigEls.brokerUsername.value = broker.username || "";
  // Never prefill the password. Reset the field and the clear toggle on load.
  mconfigEls.brokerPassword.value = "";
  broker.password = "";
  delete broker.clear_password;
  if (mconfigEls.brokerClear) mconfigEls.brokerClear.checked = false;
  if (mconfigEls.brokerClearField) {
    mconfigEls.brokerClearField.hidden = !broker.has_password;
  }
  if (mconfigEls.brokerPassword) {
    mconfigEls.brokerPassword.placeholder = broker.has_password
      ? "Leave blank to keep the stored password"
      : "Optional broker password";
    mconfigEls.brokerPassword.disabled = named;
  }
  // The broker card summary reads like every other configured hardware card.
  if (mconfigEls.brokerModel) {
    mconfigEls.brokerModel.textContent = named
      ? "Managed by Setup (named broker profiles)"
      : broker.present
      ? "Local MQTT broker"
      : "Not configured";
  }
  if (mconfigEls.brokerMeta) {
    mconfigEls.brokerMeta.textContent = broker.host
      ? String(broker.host) + (broker.port == null ? "" : ":" + String(broker.port))
      : "";
  }
  if (mconfigEls.brokerStatus) {
    mconfigEls.brokerStatus.textContent = named
      ? "Read-only"
      : broker.present
      ? broker.enabled
        ? "Enabled"
        : "Disabled"
      : "Optional";
  }
}

function wireMaintenanceBrokerForm() {
  const card = mconfigEls.brokerCard;
  const body = mconfigEls.brokerBody;
  if (card && body) {
    const buttons = Array.from(
      card.querySelectorAll("[data-maintenance-broker-toggle]")
    );
    const caret = card.querySelector(".hardware-card-toggle span");
    buttons.forEach((button) =>
      button.addEventListener("click", () =>
        mconfigSetExpanded(card, body, caret, buttons, card.dataset.open !== "true")
      )
    );
  }
  if (!mconfigEls.brokerHost) return;
  const update = () => {
    const broker = mconfigBrokerDraft();
    if (!broker) return;
    broker.host = (mconfigEls.brokerHost.value || "").trim();
    const port = (mconfigEls.brokerPort.value || "").trim();
    if (port === "") delete broker.port;
    else broker.port = port;
    broker.tls = mconfigEls.brokerSecurity
      ? mconfigEls.brokerSecurity.value === "tls"
      : false;
    broker.username = (mconfigEls.brokerUsername.value || "").trim();
    broker.present = Boolean(broker.host);
  };
  [
    mconfigEls.brokerHost,
    mconfigEls.brokerPort,
    mconfigEls.brokerSecurity,
    mconfigEls.brokerUsername,
  ].forEach((el) => {
    if (el) el.addEventListener("input", update);
    if (el && el.tagName === "SELECT") el.addEventListener("change", update);
  });
  if (mconfigEls.brokerPassword) {
    mconfigEls.brokerPassword.addEventListener("input", () => {
      const broker = mconfigBrokerDraft();
      if (broker) broker.password = mconfigEls.brokerPassword.value;
    });
  }
  if (mconfigEls.brokerClear) {
    mconfigEls.brokerClear.addEventListener("change", () => {
      const broker = mconfigBrokerDraft();
      if (!broker) return;
      const clearing = mconfigEls.brokerClear.checked;
      broker.clear_password = clearing;
      if (mconfigEls.brokerPassword) {
        mconfigEls.brokerPassword.disabled = clearing;
        if (clearing) {
          mconfigEls.brokerPassword.value = "";
          broker.password = "";
        }
      }
    });
  }
}

// --- Zendure MQTT devices --------------------------------------------------

function mconfigGenerations() {
  const list = mconfigState.catalog && mconfigState.catalog.zendure_mqtt_generations;
  return Array.isArray(list) ? list : [];
}

function mconfigGenerationLabel(id) {
  const generation = mconfigGenerations().find((entry) => entry.id === id);
  return generation ? generation.label : "";
}

function mconfigHardwareModels() {
  const list = mconfigState.catalog && mconfigState.catalog.zendure_mqtt_hardware_models;
  return Array.isArray(list) ? list : [];
}

function mconfigHardwareModel(id) {
  return mconfigHardwareModels().find((entry) => entry.id === id) || null;
}

function mconfigHardwareModelLabel(id) {
  const model = mconfigHardwareModel(id);
  return model ? model.label : "Unknown / telemetry only";
}

function mconfigModelsForGeneration(generationId) {
  return mconfigHardwareModels().filter((model) => {
    if (!model.id) return true;
    const compatible = Array.isArray(model.compatible_generations)
      ? model.compatible_generations
      : [model.generation];
    return compatible.includes(generationId);
  });
}

function mconfigMqttDeviceSummary(device) {
  const parts = [device.name || "(unnamed)"];
  const identity = device.serial_number || device.device_id || "";
  parts.push(identity ? "ID " + identity : "Identifier missing");
  const label = mconfigGenerationLabel(device.hardware_generation);
  if (label) {
    parts.push(device.alternative_layout ? label + " · alternative topic layout" : label);
  }
  parts.push(mconfigHardwareModelLabel(device.hardware_model));
  return parts.join(" · ");
}

function mconfigNextInverterName(excludeDevice) {
  const devices =
    mconfigState.draft && Array.isArray(mconfigState.draft.devices)
      ? mconfigState.draft.devices.filter((device) => device !== excludeDevice)
      : [];
  return nextCompactInverterName(
    devices.map((device) => device.name),
    devices.length
  );
}

// Central common defaults for a new inverter, served with the maintenance
// catalog payload (catalog.default_device.common). The backend merge
// re-materializes the same values, so these are a usability prefill, never
// the authority.
function mconfigDeviceCommonDefaults() {
  const catalog = mconfigState.catalog || {};
  const payload = catalog.default_device || {};
  return payload.common && typeof payload.common === "object"
    ? payload.common
    : {};
}

function mconfigApplyCommonDefaults(device) {
  const defaults = mconfigDeviceCommonDefaults();
  for (const key of Object.keys(defaults)) {
    if (device[key] == null) device[key] = defaults[key];
  }
  return device;
}

function renderMaintenanceZendureMqttDevice(device, index) {
  const body = document.createElement("div");
  body.className = "mconfig-fields feature-fields";
  let card;

  body.appendChild(
    mconfigLabelRow(
      "Device name",
      mconfigTextControl(device.name || "", (v) => {
        device.name = v;
        card.meta.textContent = mconfigMqttDeviceSummary(device);
      }),
      "Short unique EMS name used in config, logs, dashboard and Flowchart. " +
        "Model, address and serial remain in the device details."
    )
  );
  body.appendChild(
    mconfigLabelRow(
      "Serial number",
      mconfigTextControl(device.serial_number || "", (v) => {
        // The physical serial and the MQTT route id are independent identities:
        // editing the serial never changes mqtt.device_id (one input must never
        // overwrite an unrelated field).
        device.serial_number = v;
        card.meta.textContent = mconfigMqttDeviceSummary(device);
      }),
      "Physical device serial. Matches telemetry and detects duplicate devices."
    )
  );
  body.appendChild(
    mconfigLabelRow(
      "MQTT device ID",
      mconfigTextControl((device.mqtt && device.mqtt.device_id) || "", (v) => {
        const trimmed = v.trim();
        if (!device.mqtt) device.mqtt = {};
        device.mqtt.device_id = trimmed;
        syncGenerationFields();
        card.meta.textContent = mconfigMqttDeviceSummary(device);
        mconfigMarkDraftChanged("manual");
      }),
      "Exact MQTT route/payload device ID. The physical serial is never used " +
        "as the MQTT route ID."
    )
  );

  const generations = mconfigGenerations();
  const genOptions = generations.map((g) => ({ value: g.id, label: g.label }));
  const productKeyRow = document.createElement("div");
  const controlRow = document.createElement("div");
  const note = document.createElement("p");
  note.className = "feature-field-desc mconfig-mqtt-note";
  const writeProtocol = document.createElement("span");
  writeProtocol.className = "feature-readonly-value";
  const validationMaturity = document.createElement("span");
  validationMaturity.className = "feature-readonly-value";
  const supportedOperations = document.createElement("span");
  supportedOperations.className = "feature-readonly-value";
  const controlReadiness = document.createElement("span");
  controlReadiness.className = "feature-readonly-value";
  let modelSelect;
  let outputControlInput;

  const syncGenerationFields = () => {
    const generation = generations.find((g) => g.id === device.hardware_generation);
    const model = mconfigHardwareModel(device.hardware_model);
    const supported = mconfigMqttControlSupported(device, generation, model);
    const explicitWriteTopic = !!(device.mqtt && device.mqtt.write_topic);
    const trustedWriteTarget = device.trusted_write_target === true;
    // The MQTT route/payload device id is the explicit mqtt.device_id only; the
    // physical serial is never used as the route id.
    const routeDeviceId =
      device.mqtt && typeof device.mqtt.device_id === "string"
        ? device.mqtt.device_id.trim()
        : "";
    productKeyRow.hidden =
      !device.product_key && (!supported || explicitWriteTopic || trustedWriteTarget);
    // A concrete, supported model is required before control can be offered.
    controlRow.hidden = !supported;
    // Output control can never stay enabled without a complete write route: a
    // supported model and the explicit MQTT route device id. Clearing the route
    // id unchecks control rather than leaving a contradictory editor state.
    if (device.output_control && (!supported || !routeDeviceId)) {
      device.output_control = false;
      if (device.capabilities) device.capabilities.write_output_limit = false;
    }
    const hasWriteTarget =
      !!device.product_key || explicitWriteTopic || trustedWriteTarget;
    const routeComplete = !!routeDeviceId && hasWriteTarget;
    if (mconfigMqttShouldDefaultControl(device, supported, routeComplete)) {
      device.output_control = true;
      if (!device.capabilities) {
        device.capabilities = { read_power: true, read_soc: true };
      }
      device.capabilities.write_output_limit = true;
    }
    if (outputControlInput) {
      outputControlInput.checked = device.output_control === true;
    }
    writeProtocol.textContent = model && model.power_write_profile
      ? model.power_write_profile
      : "None (telemetry only)";
    validationMaturity.textContent = model && model.validation_maturity
      ? model.validation_maturity
      : "Unknown";
    supportedOperations.textContent = model && Array.isArray(model.supported_operations)
      ? model.supported_operations.join(", ") || "None"
      : "None";
    const backendReadiness = device.control_readiness || {};
    controlReadiness.textContent = supported
      ? !routeDeviceId
        ? "MQTT device ID is missing"
        : backendReadiness.reason === "write_target_missing" &&
          !device.product_key && !explicitWriteTopic && !trustedWriteTarget
        ? "Product key or write topic required"
        : backendReadiness.ready && device.hardware_model === model.id
        ? "Ready"
        : "Ready after Preview / Validate"
      : model && model.id
        ? "Telemetry only for this transport/model"
        : "Exact model required";
    note.textContent = supported
      ? !routeDeviceId
        ? "MQTT device ID is missing. The physical serial cannot be used " +
          "automatically as the Cloud MQTT route ID. Enter the MQTT device ID " +
          "to enable output control."
        : device.output_control
        ? "Output control is enabled: EMS regulates this inverter over MQTT, " +
          "using the same control loop as a local API device."
        : "This device supports output control. Enable it to let EMS regulate " +
          "output over MQTT, or leave it off to keep telemetry only."
      : "This device's topic family has no verified MQTT write method, so it " +
        "stays telemetry only.";
  };
  body.appendChild(
    mconfigLabelRow(
      "Zendure hardware generation",
      mconfigSelectControl(device.hardware_generation || "", genOptions, (v) => {
        device.hardware_generation = v;
        device.alternative_layout = false;
        const compatible = mconfigModelsForGeneration(v);
        if (!compatible.some((model) => model.id === device.hardware_model)) {
          device.hardware_model = "";
          device.power_write_profile = null;
          device.output_control = false;
          if (device.capabilities) device.capabilities.write_output_limit = false;
        }
        const options = compatible.map((model) => ({
          value: model.id,
          label: model.label,
        }));
        const replacement = mconfigSelectControl(device.hardware_model || "", options, (modelId) => {
          device.hardware_model = modelId;
          const selectedModel = mconfigHardwareModel(modelId);
          device.power_write_profile = selectedModel
            ? selectedModel.power_write_profile
            : null;
          syncGenerationFields();
          card.meta.textContent = mconfigMqttDeviceSummary(device);
          mconfigMarkDraftChanged("manual");
        });
        modelSelect.replaceWith(replacement);
        modelSelect = replacement;
        syncGenerationFields();
        card.meta.textContent = mconfigMqttDeviceSummary(device);
        mconfigMarkDraftChanged("manual");
      }),
      "Determines how EMS reads telemetry for this device."
    )
  );

  const initialModels = mconfigModelsForGeneration(device.hardware_generation);
  modelSelect = mconfigSelectControl(
    device.hardware_model || "",
    initialModels.map((model) => ({ value: model.id, label: model.label })),
    (modelId) => {
      device.hardware_model = modelId;
      const selectedModel = mconfigHardwareModel(modelId);
      device.power_write_profile = selectedModel
        ? selectedModel.power_write_profile
        : null;
      if (!selectedModel || !selectedModel.control_supported) {
        device.output_control = false;
        if (device.capabilities) device.capabilities.write_output_limit = false;
      }
      syncGenerationFields();
      card.meta.textContent = mconfigMqttDeviceSummary(device);
      mconfigMarkDraftChanged("manual");
    }
  );
  body.appendChild(
    mconfigLabelRow(
      "Exact hardware model",
      modelSelect,
      "Select the concrete registry model. Unknown hardware remains telemetry only."
    )
  );

  productKeyRow.appendChild(
    mconfigLabelRow(
      "Product key",
      mconfigTextControl(device.product_key || "", (v) => {
        device.product_key = v;
        if (!device.mqtt) device.mqtt = {};
        device.mqtt.product_key = v;
        syncGenerationFields();
        mconfigMarkDraftChanged("manual");
      }),
      "Write-target identity used to derive this inverter's MQTT command topic."
    )
  );
  body.appendChild(productKeyRow);

  body.appendChild(
    mconfigLabelRow("Write protocol", writeProtocol, "Derived by EMS/Core and read-only.")
  );
  body.appendChild(
    mconfigLabelRow(
      "Validation maturity",
      validationMaturity,
      "Hardware-validation status from the Core registry."
    )
  );
  body.appendChild(
    mconfigLabelRow(
      "Supported operations",
      supportedOperations,
      "Operations implemented for this exact model."
    )
  );
  body.appendChild(
    mconfigLabelRow(
      "Current control readiness",
      controlReadiness,
      "Preview and validation make the final capability decision."
    )
  );

  outputControlInput = mconfigCheckboxControl(device.output_control === true, (checked) => {
    device.output_control = checked;
    device.output_control_user_set = true;
    if (!device.capabilities) {
      device.capabilities = { read_power: true, read_soc: true };
    }
    device.capabilities.write_output_limit = checked;
    syncGenerationFields();
    mconfigMarkDraftChanged("manual");
  });
  controlRow.appendChild(
    mconfigLabelRow(
      "Output control",
      outputControlInput,
      "Let EMS send output control to this inverter over MQTT (needs a product key)."
    )
  );
  body.appendChild(controlRow);
  body.appendChild(note);
  syncGenerationFields();

  body.appendChild(
    mconfigLabelRow(
      "Enabled",
      mconfigCheckboxControl(device.enabled !== false, (checked) => {
        device.enabled = checked;
        card.element.dataset.disabled = checked ? "false" : "true";
        card.status.textContent = checked ? "Enabled" : "Disabled";
      }),
      "Include this MQTT device in the generated EMS config."
    )
  );

  // The MQTT editor renders the same common tuning fields as the Local API
  // editor (one shared renderer) below its transport-specific connection
  // block; a Local API IP field is never shown for an MQTT device.
  const wrapper = document.createElement("div");
  wrapper.append(
    body,
    renderCommonInverterFields(device, () => {
      card.meta.textContent = mconfigMqttDeviceSummary(device);
    })
  );

  const id = "maintenance-mqtt-device-" + index;
  const model = mconfigHardwareModelLabel(device.hardware_model);
  card = mconfigHardwareCard({
    role: "inverter",
    id,
    title: "Inverter " + (index + 1),
    model: device.alternative_layout ? model + " · alternative topic layout detected" : model,
    meta: mconfigMqttDeviceSummary(device),
    enabled: device.enabled !== false,
    connectionSource: mconfigDeviceConnectionSource(device),
    body: wrapper,
    onRemove: () => {
      mconfigState.openHardware.delete(id);
      mconfigState.draft.devices.splice(index, 1);
      renderMaintenanceInverters();
      mconfigRerenderDiscoveryReview();
    },
  });
  card.element.dataset.disabled = device.enabled === false ? "true" : "false";
  return card.element;
}

function mconfigIsMqttDevice(device) {
  return device && (device.kind === "zendure_mqtt" || device.type === "zendure_mqtt");
}

// The MQTT source a configured device uses. Config may omit mqtt.source, so the
// backend resolves it from the referenced broker profile (mqtt.effective_source);
// the current trusted proposals are the last resort. "" means unknown and must
// never be read as a concrete source.
function mconfigDeviceMqttSource(device) {
  const mqtt = (device && device.mqtt) || {};
  const broker = (device && device.broker) || {};
  const known = String(
    mqtt.source || broker.source || mqtt.effective_source || ""
  ).trim();
  if (known) return known;
  const ref = connectionBrokerScope(device);
  if (!ref) return "";
  const match = maintenanceMqttProposals().find(
    (proposal) => String(proposal.broker_ref || "").trim() === ref
  );
  return match ? String(match.connection_source || "").trim() : "";
}

// The concrete connection a configured maintenance device uses. An unresolved
// source stays "" — mqttSourceOfConnection folds every unknown value to
// local_mqtt, which is right for a proposal that states its source but would
// label an unresolved Cloud device as local MQTT.
function mconfigDeviceConnectionSource(device) {
  if (!mconfigIsMqttDevice(device)) return "local_api";
  const source = mconfigDeviceMqttSource(device);
  return source ? mqttSourceOfConnection(source) : "";
}

// Whether output control can be offered for a draft device. The device's own
// backend capability (derived from its actual observed topic family) wins; the
// generation's capability only applies to drafts that never carried one, where
// the selected generation is what determines the topic family.
function mconfigMqttControlSupported(device, generation, model) {
  const customProtocol =
    device && device.mqtt && device.mqtt.write_protocol === "custom_properties_write";
  if (customProtocol && device.control_readiness && device.control_readiness.ready) {
    return true;
  }
  if (!model || !model.id || !model.control_supported) return false;
  if (device.original_name && device.control_readiness && device.hardware_model === model.id) {
    if (typeof device.supports_output_control === "boolean") {
      return device.supports_output_control;
    }
    return device.control_readiness.ready === true;
  }
  return !!(generation && generation.supports_output_control);
}

function mconfigMqttShouldDefaultControl(device, supported, hasWriteTarget) {
  if (!device) return false;
  if (device.original_name) return false;
  if (device.output_control_user_set === true) return false;
  if (device.output_control === true) return false;
  return supported === true && hasWriteTarget === true;
}

function mconfigAddZendureMqttDevice() {
  const devices = mconfigState.draft.devices || (mconfigState.draft.devices = []);
  const generations = mconfigGenerations();
  const preferred = generations.find((g) => g.default) || generations[0];
  devices.push(
    mconfigApplyCommonDefaults({
      kind: "zendure_mqtt",
      original_name: null,
      name: mconfigNextInverterName(),
      enabled: true,
      has_enabled_key: true,
      serial_number: "",
      device_id: "",
      product_key: "",
      hardware_generation: preferred ? preferred.id : "",
      hardware_model: "",
      power_write_profile: null,
      alternative_layout: false,
      output_control: false,
      supports_output_control: false,
      control_readiness: { ready: false, reason: "hardware_profile_missing" },
      capabilities: { read_power: true, read_soc: true, write_output_limit: false },
    })
  );
  mconfigState.openHardware.add("maintenance-mqtt-device-" + (devices.length - 1));
  renderMaintenanceInverters();
}

// --- inverters ------------------------------------------------------------

// name/ip/sn are identity fields: they always write (a cleared IP is a real
// edit), while other catalog values drop back to "unset" on an emptied input.
const MCONFIG_DEVICE_IDENTITY_KEYS = new Set(["name", "ip", "sn"]);

// The device SN key differs between config (sn) and catalog (devices[].sn);
// deviceFieldKey maps catalog paths onto the draft device keys directly.
function mconfigDeviceFieldRow(device, field, updateMeta) {
  const key = deviceFieldKey(field.path);
  const identity = MCONFIG_DEVICE_IDENTITY_KEYS.has(key);
  const defaults = mconfigDeviceCommonDefaults();
  return mconfigAttachOverrideBadge(
    mconfigCatalogRow(
      field,
      device[key],
      (v) => {
        if (!identity && String(v).trim() === "") delete device[key];
        else device[key] = v;
        if (updateMeta) updateMeta();
      },
      {
        allowUnset: !identity,
        defaultValue: identity ? null : defaults[key],
      }
    ),
    mconfigDeviceOverrideEntry(device.original_name, key)
  );
}

// Common (transport-independent) tuning fields: one renderer for the Local
// API and Zendure MQTT editors, driven by the shared hardware catalog so both
// transports always offer the identical common field set.
function renderCommonInverterFields(device, updateMeta) {
  const fields = mconfigDeviceCatalogFields().filter(
    (field) => !MCONFIG_DEVICE_IDENTITY_KEYS.has(deviceFieldKey(field.path))
  );
  return mconfigLevelledFields(fields, (field) =>
    mconfigDeviceFieldRow(device, field, updateMeta)
  );
}

// Local API connection identity (name/ip/sn); never rendered for MQTT devices.
function renderLocalApiConnectionFields(device, updateMeta) {
  const fields = mconfigDeviceCatalogFields().filter((field) =>
    MCONFIG_DEVICE_IDENTITY_KEYS.has(deviceFieldKey(field.path))
  );
  return mconfigLevelledFields(fields, (field) =>
    mconfigDeviceFieldRow(device, field, updateMeta)
  );
}

function mconfigInverterSummary(device) {
  const endpoint = String(device.ip || "") + (device.port ? ":" + String(device.port) : "");
  const parts = [device.name || "(unnamed)", endpoint || "Address missing"];
  parts.push(device.sn ? "SN " + device.sn : "Serial missing");
  if (device.max_power != null && device.max_power !== "") {
    parts.push(device.max_power + " W");
  }
  return parts.join(" · ");
}

function renderMaintenanceInverter(device, index) {
  let card;

  const enabledWrap = document.createElement("div");
  enabledWrap.className = "mconfig-fields feature-fields";
  enabledWrap.appendChild(
    mconfigAttachOverrideBadge(
      mconfigLabelRow(
        "Enabled",
        mconfigCheckboxControl(device.enabled !== false, (checked) => {
          device.enabled = checked;
          card.element.dataset.disabled = checked ? "false" : "true";
          card.status.textContent = checked ? "Enabled" : "Disabled";
        }),
        "Include this inverter in the generated EMS config."
      ),
      mconfigDeviceOverrideEntry(device.original_name, "enabled")
    )
  );

  const updateMeta = () => {
    card.meta.textContent = mconfigInverterSummary(device);
  };
  const body = document.createElement("div");
  body.append(
    enabledWrap,
    renderLocalApiConnectionFields(device, updateMeta),
    renderCommonInverterFields(device, updateMeta)
  );

  const id = "maintenance-inverter-" + index;
  card = mconfigHardwareCard({
    role: "inverter",
    id,
    title: "Inverter " + (index + 1),
    model: "Zendure SolarFlow inverter",
    meta: mconfigInverterSummary(device),
    enabled: device.enabled !== false,
    connectionSource: mconfigDeviceConnectionSource(device),
    body,
    onRemove: () => {
      mconfigState.openHardware.delete(id);
      mconfigState.draft.devices.splice(index, 1);
      renderMaintenanceInverters();
      mconfigRerenderDiscoveryReview();
    },
  });
  card.element.dataset.disabled = device.enabled === false ? "true" : "false";
  return card.element;
}

function renderMaintenanceInverters() {
  const host = mconfigEls.inverters;
  if (!host) return;
  host.textContent = "";
  const devices = mconfigState.draft.devices || (mconfigState.draft.devices = []);
  devices.forEach((device, index) => {
    host.appendChild(
      mconfigIsMqttDevice(device)
        ? renderMaintenanceZendureMqttDevice(device, index)
        : renderMaintenanceInverter(device, index)
    );
  });
}

function mconfigAddInverter() {
  const devices = mconfigState.draft.devices || (mconfigState.draft.devices = []);
  // A new inverter starts from the central defaults, never from the values of
  // another configured device.
  const device = mconfigApplyCommonDefaults({
    original_name: null,
    name: mconfigNextInverterName(),
    ip: "",
    sn: "",
    enabled: true,
  });
  devices.push(device);
  mconfigState.openHardware.add("maintenance-inverter-" + (devices.length - 1));
  renderMaintenanceInverters();
}

// --- discovery review ----------------------------------------------------

function mconfigIdentity(value) {
  return String(value || "").trim().toLowerCase();
}

function mconfigDiscoveryRole(device) {
  return String(device.role_suggestion || "unknown");
}

function mconfigFindInverterMatch(configured, discovered, used) {
  // Physical serial matches across transports (Local API sn, MQTT
  // serial_number); the IP fallback applies only to serial-less Local API
  // devices — a serial-less MQTT device is never merged on weak evidence.
  const serial = physicalInverterIdentity(configured);
  if (serial) {
    const bySerial = discovered.find(
      (device) =>
        !used.has(deviceKey(device)) &&
        mconfigDiscoveryRole(device) === "inverter" &&
        mconfigIdentity(device.serial_number) === serial
    );
    if (bySerial) return { device: bySerial, match: "serial" };
  }
  if (mconfigIsMqttDevice(configured)) return null;
  const ip = mconfigIdentity(configured.ip);
  if (!ip) return null;
  const byIp = discovered.find(
    (device) =>
      !used.has(deviceKey(device)) &&
      mconfigDiscoveryRole(device) === "inverter" &&
      mconfigIdentity(device.ip) === ip
  );
  return byIp ? { device: byIp, match: "ip" } : null;
}

function buildMaintenanceDiscoveryReview(discovered) {
  const supported = (discovered || []).filter(isConfigCandidate);
  const used = new Set();
  const results = [];
  const devices = (mconfigState.draft && mconfigState.draft.devices) || [];
  devices.forEach((configured, index) => {
    const isMqtt = mconfigIsMqttDevice(configured);
    const match = mconfigFindInverterMatch(configured, supported, used);
    if (!match) {
      // A configured MQTT device without a Local API observation is not a
      // "missing inverter": its transport state is reported by its own MQTT
      // proposal row.
      if (!isMqtt) {
        results.push({ role: "inverter", state: "missing", configured, index });
      }
      return;
    }
    used.add(deviceKey(match.device));
    if (isMqtt) {
      // Same physical inverter observed over Local API: offer the alternative
      // transport on the configured device instead of a duplicate add.
      results.push({
        role: "inverter",
        state: "transport",
        configured,
        discovered: match.device,
        index,
        targetSource: "local_api",
      });
      return;
    }
    const ipChanged =
      match.match === "serial" &&
      mconfigIdentity(configured.ip) !== mconfigIdentity(match.device.ip);
    results.push({
      role: "inverter",
      state: ipChanged ? "conflict" : "found",
      configured,
      discovered: match.device,
      index,
    });
  });

  const meter =
    mconfigState.draft &&
    mconfigState.draft.grid_meter &&
    mconfigState.draft.grid_meter.present
      ? mconfigState.draft.grid_meter
      : null;
  if (meter) {
    const found = supported.find(
      (device) =>
        !used.has(deviceKey(device)) &&
        mconfigDiscoveryRole(device) === "grid_meter" &&
        mconfigIdentity(device.ip) === mconfigIdentity(meter.ip)
    );
    if (found) used.add(deviceKey(found));
    results.push({
      role: "grid_meter",
      state: found ? "found" : "missing",
      configured: meter,
      discovered: found || null,
    });
  }

  supported.forEach((device) => {
    if (!used.has(deviceKey(device))) {
      results.push({
        role: mconfigDiscoveryRole(device),
        state: "new",
        discovered: device,
      });
    }
  });

  maintenanceMqttProposals().forEach((proposal) => {
    results.push({
      role: mqttSourceOfConnection(proposal.connection_source),
      state: mconfigMqttProposalReviewState(proposal),
      mqttProposal: proposal,
    });
  });
  return results;
}

// The hardware role picks the state machine: a grid meter resolves against the
// central grid_meter draft, an inverter against the device list.
function mconfigMqttProposalReviewState(proposal) {
  return mqttProposalHardwareRole(proposal) === "grid_meter"
    ? mconfigMqttGridMeterState(proposal)
    : mconfigMqttProposalState(proposal);
}

function maintenanceMqttProposals() {
  const list = discoverySessions.maintenance.mqttProposals;
  return Array.isArray(list) ? list : [];
}

// Rebuild the whole discovery review from the retained trusted session after a
// draft change, so every card, note, action and count describes the current
// draft. No network request: the discovered devices and proposals are the ones
// already held by the session.
function mconfigRerenderDiscoveryReview() {
  const session = discoverySessions.maintenance;
  if (!session || !mconfigEls.discoveryReview || mconfigEls.discoveryReview.hidden) {
    return;
  }
  renderMaintenanceDiscoveryReview(
    buildMaintenanceDiscoveryReview(Array.from(session.devices.values()))
  );
}

function mconfigMqttProposalIdentity(proposal) {
  const fragment = proposal.config_fragment || {};
  return physicalInverterIdentity(proposal) || physicalInverterIdentity(fragment);
}

function mconfigMqttDeviceIdentity(device) {
  return physicalInverterIdentity(device);
}

// A proposal carries its identity material either at the top level or inside its
// config_fragment; merge both into one identity view for alias matching.
function mconfigProposalIdentityView(proposal) {
  const fragment = (proposal && proposal.config_fragment) || {};
  const aliases =
    (Array.isArray(proposal.physical_identity_alias_tokens) &&
      proposal.physical_identity_alias_tokens.length &&
      proposal.physical_identity_alias_tokens) ||
    fragment.physical_identity_alias_tokens ||
    [];
  return {
    sn: proposal.sn || fragment.sn,
    serial_number: proposal.serial_number || fragment.serial_number,
    physical_identity_token:
      proposal.physical_identity_token || fragment.physical_identity_token,
    physical_identity_alias_tokens: aliases,
  };
}

// A configured MQTT device and a proposal are the same concrete connection only
// within one source and broker scope; where scope evidence is missing the
// trusted proposal reference still keeps two brokers apart.
function mconfigSameMqttConnection(device, proposal) {
  const configuredSource = mconfigDeviceMqttSource(device);
  const offeredSource = String((proposal && proposal.connection_source) || "").trim();
  if (
    configuredSource &&
    offeredSource &&
    mqttSourceOfConnection(configuredSource) !== mqttSourceOfConnection(offeredSource)
  ) {
    return false;
  }
  const configured = connectionBrokerScope(device);
  const offered =
    connectionBrokerScope(proposal) ||
    connectionBrokerScope(proposal && proposal.config_fragment);
  if (configured && offered) return configured === offered;
  const configuredRef = String((device && device.proposal_id) || "").trim();
  const offeredRef = String((proposal && proposal.id) || "").trim();
  if (configuredRef && offeredRef) return configuredRef === offeredRef;
  return true;
}

// Draft devices a trusted candidate identifies. A route-only device enriched by
// a later serial (or vice versa) intersects on its surviving alias token, so it
// is recognized as the same inverter. More than one match is ambiguous evidence
// and must never be resolved by picking the first entry.
function mconfigDraftDevicesMatchingCandidate(view) {
  const devices = (mconfigState.draft && mconfigState.draft.devices) || [];
  return devices.filter((device) => inverterIdentitiesMatch(device, view));
}

// The one thing pristine decides: whether the installed config already used this
// exact connection, which separates an unchanged connection from one the
// operator selected in this session.
function mconfigPristineHasCandidateConnection(view, proposal) {
  const devices = (mconfigState.pristine && mconfigState.pristine.devices) || [];
  return devices.some(
    (device) =>
      mconfigIsMqttDevice(device) &&
      inverterIdentitiesMatch(device, view) &&
      mconfigSameMqttConnection(device, proposal)
  );
}

// What a trusted MQTT proposal offers, resolved against the CURRENT draft. The
// draft is what apply writes, so a connection the operator switched away from is
// selectable again immediately — pristine never keeps it disabled.
function mconfigMqttProposalState(proposal) {
  const view = mconfigProposalIdentityView(proposal);
  if (!inverterHasIdentity(view)) return "new";
  const devices = (mconfigState.draft && mconfigState.draft.devices) || [];
  // A route already bound to a different physical serial is a contradiction:
  // never merged, never added as an independent inverter.
  if (devices.some((device) => inverterIdentityConflict(device, view))) {
    return "identity_conflict";
  }
  const matched = mconfigDraftDevicesMatchingCandidate(view);
  if (matched.length > 1) return "identity_conflict";
  if (!matched.length) return "new";
  // The same physical inverter over any other connection — Local API, another
  // MQTT source or another broker scope — is an alternative, never a duplicate.
  if (
    !mconfigIsMqttDevice(matched[0]) ||
    !mconfigSameMqttConnection(matched[0], proposal)
  ) {
    return "transport";
  }
  return mconfigPristineHasCandidateConnection(view, proposal) ? "found" : "added";
}

// Draft entry for a trusted Zendure MQTT proposal; shared by "add to draft"
// and the transport switch so both produce the identical device shape.
function mconfigZendureMqttDraftFromProposal(proposal) {
  const fragment = proposal.config_fragment || {};
  const mqtt = fragment.mqtt || {};
  const caps = fragment.capabilities || {};
  // The backend capability result on the trusted fragment decides output
  // control; the browser never re-derives topic-family write rules.
  const outputControl = caps.write_output_limit === true;
  const serial = proposal.serial_number || fragment.serial_number || "";
  const displayRoute = mqtt.device_id || proposal.device_id || "";
  return mconfigApplyCommonDefaults({
    kind: "zendure_mqtt",
    original_name: null,
    proposal_id: proposal.id || "",
    proposal_broker_ref: proposal.broker_ref || mqtt.broker_ref || "",
    physical_identity_token: proposal.physical_identity_token || "",
    name: mconfigNextInverterName(),
    enabled: true,
    has_enabled_key: true,
    serial_number: serial,
    device_id: displayRoute,
    product_key: mqtt.product_key || "",
    hardware_generation: proposal.hardware_generation || "",
    // The concrete registry model (from the trusted fragment) selects the runtime
    // write adapter; distinct from the display-only hardware generation above.
    hardware_model: proposal.hardware_model || fragment.hardware_profile || "",
    power_write_profile: fragment.power_write_profile || "",
    alternative_layout: Boolean(proposal.alternative_layout),
    output_control: outputControl,
    supports_output_control: proposal.output_control_supported === true || outputControl,
    trusted_write_target:
      outputControl && proposal.control_block_reason !== "write_target_missing",
    mqtt: {
      broker_ref: mqtt.broker_ref || "",
      source: mqtt.source || "",
      topic_family: mqtt.topic_family || "",
      base_topic: mqtt.base_topic == null ? null : mqtt.base_topic,
      device_id: displayRoute,
      product_key: mqtt.product_key || "",
      write_protocol: mqtt.write_protocol || "",
    },
    capabilities: {
      read_power: caps.read_power !== false,
      read_soc: caps.read_soc !== false,
      write_output_limit: outputControl,
    },
    // Trusted proposal broker endpoint, passed through so the backend can
    // persist the broker profile; the browser derives no broker rules.
    broker: mqttProposalBrokerProfile(proposal, mqtt),
  });
}

function mconfigAddZendureMqttProposal(proposal) {
  if (mconfigMqttProposalState(proposal) !== "new") return false;
  const devices = mconfigState.draft.devices || (mconfigState.draft.devices = []);
  devices.push(mconfigZendureMqttDraftFromProposal(proposal));
  // Configuration happens on the configured card: adding opens it there.
  mconfigState.openHardware.add("maintenance-mqtt-device-" + (devices.length - 1));
  renderMaintenanceInverters();
  mconfigMarkDraftChanged("discovery");
  mconfigRerenderDiscoveryReview();
  return true;
}

// A configured meter is the proposal's meter only on the exact same type, topic
// and broker reference: two brokers can bridge the same topic name.
function mconfigGridMeterIsMapping(meter, mapping) {
  if (!meter || !mapping || meter.present === false) return false;
  const mqtt = meter.mqtt || {};
  return (
    String(meter.type || "") === mapping.type &&
    String(mqtt.topic || "") === String(mapping.mqtt.topic || "") &&
    String(mqtt.broker_ref || "") === String(mapping.mqtt.broker_ref || "")
  );
}

// What a grid-meter proposal offers against the current draft. Without a trusted
// mapping it stays "unavailable": the browser never invents a grid-meter topic.
function mconfigMqttGridMeterState(proposal) {
  const mapping = mqttGridMeterConfigFromProposal(proposal);
  if (!mapping) return "unavailable";
  const draft = mconfigState.draft || {};
  if (!mconfigGridMeterIsMapping(draft.grid_meter, mapping)) return "new";
  const pristine = mconfigState.pristine || {};
  return mconfigGridMeterIsMapping(pristine.grid_meter, mapping) ? "found" : "added";
}

function mconfigAdoptMqttGridMeterProposal(proposal) {
  const mapping = mqttGridMeterConfigFromProposal(proposal);
  if (!mapping) return false;
  const current = (mconfigState.draft && mconfigState.draft.grid_meter) || null;
  if (mconfigGridMeterIsMapping(current, mapping)) return false;
  const configured = Boolean(current && (current.present || current.type));
  if (configured && !confirmGridMeterReplacement()) return false;
  // The broker travels with the meter only once the adoption is committed, so a
  // declined replacement can never leave an unreferenced profile in the draft.
  const broker = mqttProposalBrokerProfile(proposal, mapping.mqtt);
  if (broker.ref) mapping.broker = broker;
  mconfigState.draft.grid_meter = mapping;
  // Configuration happens on the configured card: adopting opens it there.
  mconfigState.openHardware.add("maintenance-grid-meter");
  renderMaintenanceGridMeter();
  mconfigMarkDraftChanged("discovery");
  mconfigRerenderDiscoveryReview();
  return true;
}

function mconfigDiscoveryLabel(item) {
  if (item.role === "grid_meter") return "Grid meter";
  if (item.configured) return item.configured.name || "Configured inverter";
  return item.discovered.display_name || item.discovered.model || "Zendure SolarFlow inverter";
}

function mconfigDiscoveredAlreadyInDraft(item) {
  const found = item && item.discovered;
  if (!found || !mconfigState.draft) return false;

  if ((item.role || mconfigDiscoveryRole(found)) === "grid_meter") {
    const meter = mconfigState.draft.grid_meter || {};
    return Boolean(
      meter.present &&
      found.ip &&
      mconfigIdentity(meter.ip) === mconfigIdentity(found.ip)
    );
  }

  const serial = mconfigIdentity(found.serial_number);
  const ip = mconfigIdentity(found.ip);
  const devices = mconfigState.draft.devices || [];
  return devices.some(
    (device) =>
      (serial && physicalInverterIdentity(device) === serial) ||
      (!serial && ip && mconfigIdentity(device.ip) === ip)
  );
}

// Candidate cards the operator dismissed in the current review, kept outside the
// draft because ignoring changes nothing that is applied.
const mconfigIgnoredCandidates = new Set();

function mconfigDiscoveryActionState(item) {
  if (item.state === "found") {
    return { text: "In config", disabled: true, cssClass: "is-in-config" };
  }

  if (item.state === "missing") {
    return {
      text: "Configured",
      disabled: true,
      cssClass: "is-configured-missing",
    };
  }

  if (item.state === "transport") {
    return { text: "Use connection", disabled: false, cssClass: "is-transport" };
  }

  if (item.state === "identity_conflict") {
    return {
      text: "Identity conflict",
      disabled: true,
      cssClass: "is-conflict",
    };
  }

  if (mconfigDiscoveredAlreadyInDraft(item)) {
    return { text: "Added to draft", disabled: true, cssClass: "is-added" };
  }

  if (item.state === "conflict") {
    return { text: "Update draft", disabled: false, cssClass: "is-update" };
  }

  // New candidates add fresh-install style: one role-specific action.
  const role = item.role || mconfigDiscoveryRole(item.discovered || {});
  return {
    text: role === "grid_meter" ? "Add as grid meter" : "Add inverter",
    disabled: false,
    cssClass: "is-add",
  };
}

function mconfigMarkDraftChanged(source) {
  mconfigState.previewFingerprint = null;
  if (mconfigEls.result) mconfigEls.result.hidden = true;
  if (mconfigEls.applyPanel) mconfigEls.applyPanel.hidden = true;
  setMaintenanceFact(mconfigEls.summary, "Draft changed · preview required", "warn");
  if (source === "discovery") {
    mconfigState.discoveryDraftChanges += 1;
    const count = mconfigState.discoveryDraftChanges;
    if (mconfigEls.discoveryStatus) {
      mconfigEls.discoveryStatus.textContent =
        count + " discovery " + (count === 1 ? "change" : "changes") +
        " added to the draft. Preview changes before applying.";
    }
  }
}

function mconfigAddDiscovered(item) {
  const found = item.discovered;
  if (!found) return false;
  if (item.role === "grid_meter") {
    const gridMeter = {
      present: true,
      type: gridMeterType(
        {
          device_type: found.device_type || "",
          api_family: found.api_family || "",
        },
        "shelly"
      ),
      ip: found.ip || "",
    };
    if (found.port != null && found.port !== "") {
      gridMeter.port = found.port;
    }
    mconfigState.draft.grid_meter = gridMeter;
    // Configuration happens on the configured card: adding opens it there.
    mconfigState.openHardware.add("maintenance-grid-meter");
    renderMaintenanceGridMeter();
    mconfigMarkDraftChanged("discovery");
    mconfigRerenderDiscoveryReview();
    return true;
  }
  const devices = mconfigState.draft.devices || (mconfigState.draft.devices = []);
  const serial = mconfigIdentity(found.serial_number);
  if (
    devices.some(
      (device) =>
        (serial && physicalInverterIdentity(device) === serial) ||
        (!serial && mconfigIdentity(device.ip) === mconfigIdentity(found.ip))
    )
  ) return false;
  devices.push(
    mconfigApplyCommonDefaults({
      original_name: null,
      name: mconfigNextInverterName(),
      ip: found.ip || "",
      sn: found.serial_number || "",
      enabled: true,
    })
  );
  // Configuration happens on the configured card: adding opens it there.
  mconfigState.openHardware.add("maintenance-inverter-" + (devices.length - 1));
  renderMaintenanceInverters();
  mconfigMarkDraftChanged("discovery");
  mconfigRerenderDiscoveryReview();
  return true;
}

// Switch a draft inverter to another transport in place: same logical device,
// same name and common tuning values, new connection fields only. The
// original_name reference is preserved so the backend replaces the one
// original entry even after a rename.
function mconfigSwitchInverterTransport(identity, targetSource, context) {
  const devices = (mconfigState.draft && mconfigState.draft.devices) || [];
  const rawIdentity = String(identity == null ? "" : identity).trim();
  if (!rawIdentity) return false;
  const probe = /^opaque:v1:[A-Za-z0-9_-]+$/.test(rawIdentity)
    ? { physical_identity_token: rawIdentity }
    : { serial_number: rawIdentity };
  if (!inverterHasIdentity(probe)) return false;
  // Contradictory evidence is never switched, and an ambiguous match would
  // silently rewrite a different inverter.
  if (devices.some((device) => inverterIdentityConflict(device, probe))) return false;
  const matched = [];
  devices.forEach((device, position) => {
    if (inverterIdentitiesMatch(device, probe)) matched.push(position);
  });
  if (matched.length !== 1) return false;
  const index = matched[0];
  const current = devices[index];
  const preserved = {
    original_name: current.original_name || null,
    enabled: current.enabled !== false,
    has_enabled_key: true,
  };
  if (current.name) preserved.name = current.name;
  for (const field of mconfigDeviceCatalogFields()) {
    const fieldKey = deviceFieldKey(field.path);
    if (MCONFIG_DEVICE_IDENTITY_KEYS.has(fieldKey)) continue;
    if (current[fieldKey] != null) preserved[fieldKey] = current[fieldKey];
  }
  let replacement;
  let cardId;
  if (targetSource === "local_api") {
    const found = (context && context.discovered) || {};
    replacement = Object.assign(
      {
        kind: "local_api",
        ip: found.ip || "",
        sn: found.serial_number || current.serial_number || current.sn || "",
      },
      preserved
    );
    cardId = "maintenance-inverter-" + index;
  } else {
    const proposal = context && context.proposal;
    if (!proposal) return false;
    replacement = Object.assign(
      mconfigZendureMqttDraftFromProposal(proposal),
      preserved
    );
    cardId = "maintenance-mqtt-device-" + index;
  }
  mconfigApplyCommonDefaults(replacement);
  devices[index] = replacement;
  mconfigState.openHardware.add(cardId);
  renderMaintenanceInverters();
  mconfigMarkDraftChanged("discovery");
  mconfigRerenderDiscoveryReview();
  return true;
}

function mconfigAppendSourceBadge(host, label, cssClass) {
  const badge = document.createElement("span");
  badge.className = "source-badge " + cssClass;
  badge.textContent = label;
  host.appendChild(badge);
}

function mconfigAppendSourceBadges(host, device) {
  sourcesOf(device).forEach((source) => {
    const normalized = normalizeDiscoverySource(source);
    const label = DISCOVERY_SOURCE_LABELS[normalized] || normalized;
    const cssClass = normalized === "mdns" ? "source-mdns" : "source-scan";
    mconfigAppendSourceBadge(host, label, cssClass);
  });
}

function mconfigAppendDeviceFact(host, label, value) {
  const fact = document.createElement("span");
  fact.className = "device-fact";
  const key = document.createElement("span");
  key.className = "k";
  key.textContent = label;
  const val = document.createElement("span");
  val.className = value ? "v" : "v missing";
  val.textContent = value || "missing";
  fact.append(key, val);
  host.appendChild(fact);
}

// The connection an inverter discovery candidate represents.
function mconfigCandidateConnectionSource(item) {
  if (item && item.mqttProposal) {
    return mqttSourceOfConnection(item.mqttProposal.connection_source);
  }
  return "local_api";
}

function mconfigConfiguredDeviceForCandidate(item) {
  if (item && item.configured) return item.configured;
  if (!item || !item.mqttProposal) return null;
  const matched = mconfigDraftDevicesMatchingCandidate(
    mconfigProposalIdentityView(item.mqttProposal)
  );
  // An ambiguous alias names no inverter: a note must not claim one of them.
  return matched.length === 1 ? matched[0] : null;
}

// Compact relationship line for an alternative connection: which configured
// inverter it belongs to, and how that one is connected today.
function mconfigConnectionRelationshipNote(item) {
  if (!item || item.state !== "transport") return null;
  const configured = mconfigConfiguredDeviceForCandidate(item);
  if (!configured) return null;
  const note = document.createElement("p");
  note.className = "maintenance-note muted candidate-connection-note";
  note.textContent =
    "Already configured as " +
    (configured.name || "another inverter") +
    " via " +
    connectionLabelFor(mconfigDeviceConnectionSource(configured));
  return note;
}

const MCONFIG_MQTT_PROPOSAL_ACTIONS = {
  found: { text: "In config", disabled: true, cssClass: "is-in-config" },
  added: { text: "Added to draft", disabled: true, cssClass: "is-added" },
  new: { text: "Add inverter", disabled: false, cssClass: "is-add" },
  // Same physical inverter already configured over another transport: the
  // action switches the connection instead of adding a duplicate device.
  transport: { text: "Use connection", disabled: false, cssClass: "is-transport" },
  // This Cloud route is already bound to a different physical serial: blocked,
  // never merged and never added as an independent inverter.
  identity_conflict: {
    text: "Identity conflict",
    disabled: true,
    cssClass: "is-conflict",
  },
};

// Grid-meter hardware is adopted as the central grid meter, never as an
// inverter; without a trusted mapping the action stays visible but inert.
const MCONFIG_MQTT_GRID_METER_ACTIONS = {
  found: { text: "In config", disabled: true, cssClass: "is-in-config" },
  added: { text: "Added to draft", disabled: true, cssClass: "is-added" },
  new: { text: "Use as grid meter", disabled: false, cssClass: "is-add" },
  unavailable: {
    text: "Use as grid meter",
    disabled: true,
    cssClass: "is-unavailable",
  },
};

function renderMaintenanceMqttProposalCard(item) {
  const proposal = item.mqttProposal;
  const card = document.createElement("article");
  const hardwareRole = mqttProposalHardwareRole(proposal);
  card.className =
    hardwareCardClass(hardwareRole) +
    " mconfig-discovery-device-card mconfig-discovery-proposal-card";
  card.dataset.state = item.state;
  const transportSource = mqttSourceOfConnection(proposal.connection_source);
  // Hardware role owns the card colour; the transport stays separate metadata.
  card.dataset.role = hardwareRole;
  card.dataset.connection = transportSource;

  const head = document.createElement("div");
  head.className = "device-card-head";
  const name = document.createElement("span");
  name.className = "device-name";
  name.textContent = proposal.display_name || "Zendure MQTT device";
  const transportPill = document.createElement("span");
  transportPill.className = "connection-pill";
  transportPill.dataset.connection = transportSource;
  transportPill.textContent = mqttTransportLabel(proposal);
  head.append(name, transportPill);
  card.appendChild(head);

  const sources = document.createElement("div");
  sources.className = "device-sources";
  mconfigAppendSourceBadge(sources, "mqtt proposal", "source-scan");
  card.appendChild(sources);

  const facts = document.createElement("div");
  facts.className = "device-facts";
  mconfigAppendDeviceFact(
    facts,
    "Device/SN",
    proposal.serial_number || proposal.device_id || ""
  );
  mconfigAppendDeviceFact(facts, "Hardware generation", mqttGenerationLabel(proposal));
  mconfigAppendDeviceFact(facts, "Transport", mqttTransportLabel(proposal));
  const isGridMeter = hardwareRole === "grid_meter";
  const controllable = !isGridMeter && !!proposal.output_control_supported;
  const note = document.createElement("p");
  note.className = "maintenance-note muted";
  if (isGridMeter) {
    mconfigAppendDeviceFact(facts, "Role", "Grid meter");
    mconfigAppendDeviceFact(facts, "Topic", mqttGridMeterProposalTopic(proposal));
    note.textContent =
      item.state === "unavailable"
        ? "Grid meter without a trusted totalPower topic on this connection: " +
          "EMS never derives one, so it cannot be adopted here."
        : "Grid meter: EMS reads the grid signal from this MQTT topic. It is " +
          "read-only and never written to.";
  } else {
    mconfigAppendDeviceFact(
      facts,
      "Output control",
      controllable ? "Supported" : "Not available"
    );
    if (controllable) {
      mconfigAppendDeviceFact(
        facts,
        "Write protocol",
        mqttWriteProtocolLabel(mqttProposalWriteProtocol(proposal))
      );
    } else {
      mconfigAppendDeviceFact(
        facts,
        "Reason",
        mqttControlReasonLabel(mqttProposalControlReason(proposal))
      );
    }
    note.textContent = controllable
      ? "Supported inverter: EMS regulates its output over MQTT using the same " +
        "control loop as a local API device."
      : "Telemetry only: this device's topic family has no verified MQTT write " +
        "method, so EMS reads values but does not send output control.";
  }
  card.appendChild(facts);
  card.appendChild(note);

  if (item.state === "identity_conflict") {
    const conflict = document.createElement("p");
    conflict.className = "maintenance-note is-conflict";
    conflict.textContent =
      "Identity conflict: this Cloud route is already configured with a " +
      "different physical serial. Resolve the existing device before adding it.";
    card.appendChild(conflict);
  }

  const relationship = mconfigConnectionRelationshipNote(item);
  if (relationship) card.appendChild(relationship);

  const actions = document.createElement("div");
  actions.className = "mconfig-discovery-item-actions";
  const actionState = isGridMeter
    ? MCONFIG_MQTT_GRID_METER_ACTIONS[item.state] ||
      MCONFIG_MQTT_GRID_METER_ACTIONS.unavailable
    : MCONFIG_MQTT_PROPOSAL_ACTIONS[item.state] || MCONFIG_MQTT_PROPOSAL_ACTIONS.new;
  const accept = document.createElement("button");
  accept.type = "button";
  accept.className =
    "primary-button compact mconfig-discovery-add-button " + actionState.cssClass;
  accept.textContent = actionState.text;
  accept.disabled = actionState.disabled;
  if (!actionState.disabled) {
    // The mutation rebuilds the whole review, so this card is replaced by one
    // that describes the new draft; it is never patched by hand.
    accept.addEventListener("click", () => {
      if (isGridMeter) {
        mconfigAdoptMqttGridMeterProposal(proposal);
        return;
      }
      if (item.state === "transport") {
        mconfigSwitchInverterTransport(
          mconfigMqttProposalIdentity(proposal),
          transportSource,
          { proposal }
        );
        return;
      }
      mconfigAddZendureMqttProposal(proposal);
    });
  }
  actions.appendChild(accept);
  card.appendChild(actions);
  return card;
}

// Discovery candidates render as the same collapsible hardware cards the
// setup "Add more devices" row uses (renderConfigAvailableCard); the
// maintenance-only extra is the match state against the existing config
// (In config / Not found / IP changed) shown as the card status.
const MCONFIG_DISCOVERY_STATUS_TEXT = {
  found: "In config",
  new: "Detected",
  missing: "Not found",
  conflict: "IP changed",
  transport: "Alternative connection",
};

function renderMaintenanceDiscoveryCard(item) {
  if (item.mqttProposal) return renderMaintenanceMqttProposalCard(item);
  const found = item.discovered || {};
  const configured = item.configured || {};
  const role = item.role || mconfigDiscoveryRole(found) || "unknown";
  const isGridMeter = role === "grid_meter";
  const cardId =
    "maintenance-candidate-" +
    String(found.id || deviceKey(found) || configured.name || role);

  const endpoint = String(found.ip || configured.ip || "");
  const serial = found.serial_number || configured.sn || "";
  const apiFamily = found.api_family || configured.api_family || "";
  const deviceType =
    found.device_type || configured.type || configured.grid_meter_type || "";
  const meta = [
    endpoint,
    serial ? "SN " + serial : "SN missing",
    apiFamily,
    deviceType,
  ]
    .filter(Boolean)
    .join(" · ");

  const body = document.createElement("div");
  body.className = "device-facts";
  mconfigAppendDeviceFact(body, "IP", endpoint);
  mconfigAppendDeviceFact(body, "Serial", serial);
  mconfigAppendDeviceFact(body, "API family", apiFamily);
  mconfigAppendDeviceFact(body, "Type", deviceType);
  const sources = document.createElement("div");
  sources.className = "device-sources";
  if (item.configured) {
    mconfigAppendSourceBadge(sources, "configured", "source-scan");
  }
  if (item.discovered) {
    mconfigAppendSourceBadges(sources, item.discovered);
  }
  if (item.state === "missing") {
    mconfigAppendSourceBadge(sources, "not found", "source-scan");
  }
  body.appendChild(sources);

  // Ignoring is a review-local choice with no draft effect, so it is remembered
  // per candidate and survives the rebuilds that follow other draft changes.
  const ignored = mconfigIgnoredCandidates.has(cardId);
  const actionState = ignored
    ? { text: "Ignored", disabled: true, cssClass: "is-ignored" }
    : mconfigDiscoveryActionState(item);
  if (ignored) mconfigAppendSourceBadge(sources, "ignored", "source-scan");
  const accept = document.createElement("button");
  accept.type = "button";
  accept.className =
    "primary-button compact " +
    "mconfig-discovery-add-button " + actionState.cssClass;
  accept.textContent = actionState.text;
  accept.disabled = actionState.disabled;

  const actions = [accept];
  if (item.state === "new" || item.state === "conflict") {
    const ignore = document.createElement("button");
    ignore.type = "button";
    ignore.className =
      "secondary-button compact mconfig-discovery-ignore-button";
    ignore.textContent = "Ignore";
    ignore.disabled = actionState.disabled;
    ignore.addEventListener("click", () => {
      mconfigIgnoredCandidates.add(cardId);
      mconfigRerenderDiscoveryReview();
    });
    actions.push(ignore);
  }

  if (!actionState.disabled) {
    // The mutation rebuilds the whole review, so this card is replaced by one
    // that describes the new draft; it is never patched by hand.
    accept.addEventListener("click", () => {
      if (item.state === "transport") {
        mconfigSwitchInverterTransport(
          physicalInverterIdentity(item.configured),
          item.targetSource || "local_api",
          { discovered: item.discovered }
        );
        return;
      }
      if (item.state !== "conflict") {
        mconfigAddDiscovered(item);
        return;
      }
      const target = mconfigState.draft.devices[item.index];
      if (!target) return;
      target.ip = item.discovered.ip || target.ip;
      mconfigState.openHardware.add("maintenance-inverter-" + item.index);
      renderMaintenanceInverters();
      mconfigMarkDraftChanged("discovery");
      mconfigRerenderDiscoveryReview();
    });
  }

  const relationship = mconfigConnectionRelationshipNote(item);
  if (relationship) body.appendChild(relationship);

  const card = mconfigHardwareCard({
    role: isGridMeter ? "grid_meter" : "inverter",
    id: cardId,
    title: isGridMeter ? "Grid meter candidate" : "Inverter candidate",
    model: mconfigDiscoveryLabel(item),
    meta,
    statusText: MCONFIG_DISCOVERY_STATUS_TEXT[item.state] || "Configured",
    connectionSource: isGridMeter ? "" : mconfigCandidateConnectionSource(item),
    body,
    actions,
  });
  card.element.classList.add("mconfig-discovery-device-card");
  card.element.dataset.state = item.state;
  card.element.dataset.role = role;
  return card.element;
}

function renderMaintenanceDiscoveryReview(results) {
  if (!mconfigEls.discoveryResults || !mconfigEls.discoveryReview) return;
  mconfigEls.discoveryResults.textContent = "";
  mconfigEls.discoveryResults.className = "mconfig-discovery-results";
  const counts = { found: 0, new: 0, missing: 0, conflict: 0, transport: 0 };
  results.forEach((item) => {
    counts[item.state] = (counts[item.state] || 0) + 1;
  });
  const configured =
    counts.found + counts.missing + counts.conflict + counts.transport;
  const summary = document.createElement("div");
  summary.className = "mconfig-discovery-summary";
  const summaryTitle = document.createElement("strong");
  const session = discoverySessions.maintenance;
  summaryTitle.textContent = session.active
    ? "Scanning…"
    : session.progress.failed
    ? "Discovery completed with warnings"
    : "Discovery completed";
  const summaryCounts = document.createElement("span");
  summaryCounts.textContent =
    "Configured: " + configured +
    " · Found: " + counts.found +
    " · New: " + counts.new +
    " · Not found: " + counts.missing +
    " · Conflict: " + counts.conflict +
    " · Failed scans: " + session.progress.failed;
  const summaryNote = document.createElement("span");
  const supportedFound = results.some((item) => Boolean(item.discovered));
  summaryNote.textContent =
    (supportedFound
      ? counts.new === 0
        ? "No new devices found. "
        : ""
      : "No supported devices found. ") +
    "Configured devices were kept in the draft. No changes applied yet.";
  summary.append(summaryTitle, summaryCounts, summaryNote);
  mconfigEls.discoveryResults.appendChild(summary);

  const grid = document.createElement("div");
  grid.className = "config-available-list-style";
  results.forEach((item) => {
    grid.appendChild(renderMaintenanceDiscoveryCard(item));
  });
  mconfigEls.discoveryResults.appendChild(grid);
  mconfigEls.discoveryReview.hidden = false;
}

let mconfigDiscovering = false;

async function maintenanceScanNetwork(
  cidr,
  onProgress,
  session = discoverySessions.maintenance
) {
  const start = await discoveryFetch("/api/discovery/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cidr }),
  }, session.mode);
  const started = await start.json();
  if (!start.ok || !started.scan_id) {
    throw new Error(started.error || "scan request failed");
  }
  const deadline = Date.now() + POLL_MAX_MS;
  while (Date.now() < deadline) {
    const response = await fetch(
      "/api/discovery/result/" + encodeURIComponent(started.scan_id)
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "scan result unavailable");
    if (onProgress && result.progress) onProgress(result.progress);
    if (result.status !== "running") {
      return Array.isArray(result.devices) ? result.devices : [];
    }
    await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
  }
  throw new Error("scan timed out");
}

function renderMaintenanceDiscoveryProgress(session) {
  if (!mconfigEls.discoveryProgress) return;
  const progress = session.progress;
  const completed = progress.done + progress.failed;
  mconfigEls.discoveryProgress.hidden = progress.total === 0;
  mconfigEls.discoveryProgressBar.style.width =
    discoveryProgressPercent(session) + "%";
  mconfigEls.discoveryProgressText.textContent =
    completed + " of " + progress.total + " work units checked · Active: " +
    progress.active + activeScanHostDetail(session) + " · Found: " +
    session.devices.size + " · Failed: " + progress.failed;
  if (mconfigEls.discoveryCount) {
    mconfigEls.discoveryCount.textContent = session.devices.size + " found";
  }
  if (mconfigState.loaded && (session.devices.size || progress.total)) {
    renderMaintenanceDiscoveryReview(
      buildMaintenanceDiscoveryReview(Array.from(session.devices.values()))
    );
  }
}

function maintenanceConfiguredCidrs() {
  if (!mconfigState.draft) return [];
  const addresses = (mconfigState.draft.devices || []).map((device) => device.ip);
  if (mconfigState.draft.grid_meter && mconfigState.draft.grid_meter.present) {
    addresses.push(mconfigState.draft.grid_meter.ip);
  }
  return addresses
    .map((address) => validateManualScanInput(String(address || "") + "/24"))
    .filter((result) => !result.error)
    .map((result) => result.cidr);
}

function completeDiscoveryWork(session, failed, generation) {
  if (generation !== undefined && generation !== session.generation) return;
  session.progress.active = Math.max(0, session.progress.active - 1);
  if (failed) session.progress.failed += 1;
  else session.progress.done += 1;
  session.active = session.progress.active > 0;
  renderMaintenanceDiscoveryProgress(session);
}

async function startMaintenanceDiscovery() {
  if (mconfigDiscovering) return;
  mconfigDiscovering = true;
  mconfigEls.discoveryStart.disabled = true;
  mconfigEls.discoveryStart.textContent = "Discovering…";
  mconfigEls.discoveryStatus.textContent =
    "Searching mDNS and recommended LAN networks…";
  try {
    if (!mconfigState.loaded) {
      const loaded = await loadMaintenanceConfig();
      if (!loaded || loaded.status !== "ok") throw new Error("config unavailable");
    }
    mconfigState.discoveryDraftChanges = 0;
    mconfigIgnoredCandidates.clear();
    const session = discoverySessions.maintenance;
    const generation = session.generation;
    session.active = true;
    session.startedAt = session.startedAt || Date.now();
    session.mqttProposals = [];
    session.progress.total += 5;
    session.progress.active += 5;
    renderMaintenanceDiscoveryProgress(session);
    let cloudSkippedWithoutKey = false;

    const mqttWork = (async () => {
      // Local-broker re-listen and cloud refresh are their own work units: a
      // failure marks that unit failed but never blocks reading the proposals
      // the remaining sources produced.
      let brokersFailed = false;
      try {
        const refresh = await fetch("/api/discovery/mqtt-brokers/refresh", { method: "POST" });
        if (!refresh.ok) throw new Error("mqtt broker refresh failed");
      } catch (err) {
        brokersFailed = true;
      }
      completeDiscoveryWork(session, brokersFailed, generation);

      let cloudFailed = false;
      try {
        const settingsResponse = await fetch(ZENDURE_CLOUD_BASE + "/settings");
        const settings = await settingsResponse.json();
        if (!settingsResponse.ok) {
          throw new Error(settings.error || "cloud settings unavailable");
        }
        if (settings.token_saved) {
          const refresh = await fetch(ZENDURE_CLOUD_BASE + "/refresh", { method: "POST" });
          if (!refresh.ok) throw new Error("cloud refresh failed");
        } else {
          cloudSkippedWithoutKey = true;
        }
      } catch (err) {
        cloudFailed = true;
      }
      completeDiscoveryWork(session, cloudFailed, generation);

      let failed = false;
      try {
        const response = await fetch("/api/discovery/mqtt-proposals");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "mqtt proposals unavailable");
        if (generation !== session.generation) return;
        session.mqttProposals = Array.isArray(data.proposals) ? data.proposals : [];
      } catch (err) {
        failed = true;
      }
      completeDiscoveryWork(session, failed, generation);
    })();

    const knownScans = queueDiscoveryScans(
      session,
      maintenanceConfiguredCidrs(),
      "active_scan",
      renderMaintenanceDiscoveryProgress
    );
    const mdnsWork = (async () => {
      let failed = false;
      try {
        const refresh = await fetch("/api/discovery/mdns/refresh", { method: "POST" });
        if (!refresh.ok) throw new Error("mDNS refresh failed");
        const response = await fetch("/api/discovery/devices");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "mDNS results unavailable");
        if (generation !== session.generation) return;
        (Array.isArray(data.devices) ? data.devices : []).forEach((device) =>
          mergeDiscoveryDevice(session, device, "mdns")
        );
      } catch (err) {
        failed = true;
      }
      completeDiscoveryWork(session, failed, generation);
    })();
    const networkWork = (async () => {
      let failed = false;
      try {
        const response = await fetch("/api/discovery/networks");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "network discovery failed");
        if (generation !== session.generation) return;
        const cidrs = (Array.isArray(data.networks) ? data.networks : [])
          .filter((network) => network.scan_recommended && !network.is_docker_like)
          .map((network) => network.cidr)
          .filter(Boolean);
        cidrs.forEach((cidr) => session.networks.set(cidr, { cidr }));
        completeDiscoveryWork(session, false, generation);
        await queueDiscoveryScans(
          session,
          cidrs,
          "active_scan",
          renderMaintenanceDiscoveryProgress
        );
        return;
      } catch (err) {
        failed = true;
      }
      completeDiscoveryWork(session, failed, generation);
    })();
    await Promise.all([knownScans, mdnsWork, networkWork, mqttWork]);
    if (generation !== session.generation) return;

    const results = buildMaintenanceDiscoveryReview(
      Array.from(session.devices.values())
    );
    renderMaintenanceDiscoveryReview(results);
    mconfigEls.discoveryStatus.textContent = session.progress.failed
      ? "Discovery completed with warnings. Retained results and the in-memory draft are unchanged."
      : "Discovery completed. Results are retained until you reset them.";
    if (cloudSkippedWithoutKey) {
      mconfigEls.discoveryStatus.textContent +=
        " Zendure cloud was skipped: save an API key under Discovery sources to include cloud devices.";
    }
  } catch (err) {
    mconfigEls.discoveryStatus.textContent =
      "Discovery failed. The current in-memory draft and config.json are unchanged.";
  } finally {
    mconfigDiscovering = false;
    mconfigEls.discoveryStart.disabled = false;
    mconfigEls.discoveryStart.textContent = "Start discovery";
  }
}

async function runMaintenanceManualScan(event) {
  event.preventDefault();
  const checked = validateManualScanInput(mconfigEls.discoveryManualInput.value);
  if (checked.error) {
    mconfigEls.discoveryError.hidden = false;
    mconfigEls.discoveryError.textContent = checked.error;
    return;
  }
  if (!mconfigState.loaded) {
    const loaded = await loadMaintenanceConfig();
    if (!loaded || loaded.status !== "ok") return;
  }
  mconfigEls.discoveryError.hidden = true;
  mconfigEls.discoveryError.textContent = "";
  mconfigEls.discoveryManualScan.disabled = true;
  mconfigEls.discoveryStatus.textContent = "Running manual scan…";
  const generation = discoverySessions.maintenance.generation;
  await queueDiscoveryScans(
    discoverySessions.maintenance,
    [checked.cidr],
    "manual_scan",
    renderMaintenanceDiscoveryProgress
  );
  mconfigEls.discoveryManualScan.disabled = false;
  if (generation !== discoverySessions.maintenance.generation) return;
  renderMaintenanceDiscoveryProgress(discoverySessions.maintenance);
  mconfigEls.discoveryStatus.textContent = discoverySessions.maintenance.progress.failed
    ? "Manual scan completed with warnings. Previous results were retained."
    : "Manual scan completed. Previous results were retained.";
}

function resetMaintenanceDiscovery() {
  resetDiscoverySession(discoverySessions.maintenance);
  mconfigIgnoredCandidates.clear();
  if (mconfigEls.discoveryReview) mconfigEls.discoveryReview.hidden = true;
  if (mconfigEls.discoveryProgress) mconfigEls.discoveryProgress.hidden = true;
  if (mconfigEls.discoveryError) mconfigEls.discoveryError.hidden = true;
  if (mconfigEls.discoveryCount) mconfigEls.discoveryCount.textContent = "0 found";
  mconfigEls.discoveryStatus.textContent =
    "Discovery results reset. The in-memory config draft was not changed.";
}

async function addManualMaintenanceInverter() {
  if (!mconfigState.loaded) {
    const loaded = await loadMaintenanceConfig();
    if (!loaded || loaded.status !== "ok") return;
  }
  mconfigAddInverter();
  mconfigMarkDraftChanged("manual");
  if (mconfigEls.discoveryStatus) {
    mconfigEls.discoveryStatus.textContent =
      "Manual inverter added to the in-memory draft. Complete its fields on its card, then preview the changes.";
  }
}

function deriveLocalBrokerRef(name) {
  const slug = String(name == null ? "" : name)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "")
    .replace(/^[_-]+/, "")
    .replace(/[_-]+$/, "");
  if (!slug || slug === "local_mqtt") return "local_mqtt";
  const suffix = (
    slug.indexOf("local_mqtt_") === 0
      ? slug.slice("local_mqtt_".length)
      : slug
  )
    .replace(/^[_-]+/, "")
    .replace(/[_-]+$/, "");
  return suffix ? "local_mqtt_" + suffix : "local_mqtt";
}

function mconfigManualBrokerBlock() {
  const els = mconfigEls;
  const host = (
    (els.maintenanceManualBrokerHost && els.maintenanceManualBrokerHost.value) || ""
  ).trim();
  if (!host) return null;
  const ref = deriveLocalBrokerRef(
    els.maintenanceManualBrokerName && els.maintenanceManualBrokerName.value
  );
  const tls = Boolean(
    els.maintenanceManualBrokerSecurity &&
      els.maintenanceManualBrokerSecurity.value === "tls"
  );
  let port = parseInt(
    (
      (els.maintenanceManualBrokerPort && els.maintenanceManualBrokerPort.value) || ""
    ).trim(),
    10
  );
  if (!Number.isFinite(port) || port <= 0) port = tls ? 8883 : 1883;
  const username = (
    (els.maintenanceManualBrokerUsername && els.maintenanceManualBrokerUsername.value) || ""
  ).trim();
  const password =
    (els.maintenanceManualBrokerPassword && els.maintenanceManualBrokerPassword.value) || "";
  return {
    ref,
    host,
    port,
    tls,
    username,
    password,
    hasAuth: Boolean(username && password),
    authPartial: Boolean(username) !== Boolean(password),
  };
}

function mconfigManualBrokerError(text) {
  if (!mconfigEls.discoveryError) return;
  mconfigEls.discoveryError.hidden = !text;
  mconfigEls.discoveryError.textContent = text || "";
}

async function addManualMaintenanceMqttDevice() {
  if (!mconfigState.loaded) {
    const loaded = await loadMaintenanceConfig();
    if (!loaded || loaded.status !== "ok") return;
  }
  const broker = mconfigManualBrokerBlock();
  if (broker && broker.authPartial) {
    mconfigManualBrokerError(
      "Enter both a username and password, or leave both blank."
    );
    return;
  }
  mconfigManualBrokerError("");
  let credentialsRef = "";
  if (broker && broker.hasAuth) {
    if (mconfigEls.addMqttDevice) mconfigEls.addMqttDevice.disabled = true;
    if (mconfigEls.discoveryStatus) {
      mconfigEls.discoveryStatus.textContent = "Saving broker credential…";
    }
    try {
      const res = await discoveryFetch(
        "/api/discovery/connections/mqtt-credentials",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            label: broker.ref,
            username: broker.username,
            password: broker.password,
          }),
        },
        discoveryContextFor(mconfigEls.maintenanceManualBrokerForm)
      );
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.message || data.error || "credential save failed");
      }
      const credentials = (data.local_mqtt || {}).credentials || [];
      const match = credentials.find((entry) => String(entry.id) === broker.ref);
      if (!match) throw new Error("credential reference unresolved");
      credentialsRef = String(match.id);
    } catch (err) {
      mconfigManualBrokerError(
        "Could not save the broker credential: " + (err.message || String(err))
      );
      return;
    } finally {
      if (mconfigEls.addMqttDevice) mconfigEls.addMqttDevice.disabled = false;
    }
  }
  mconfigAddZendureMqttDevice();
  const devices = mconfigState.draft.devices;
  const device = devices[devices.length - 1];
  if (broker) {
    device.mqtt = {
      broker_ref: broker.ref,
      topic_family: "",
      base_topic: null,
      device_id: "",
    };
    device.broker = {
      ref: broker.ref,
      host: broker.host,
      port: broker.port,
      tls: broker.tls,
      tls_insecure: false,
      tls_mode: "",
      source: "local_mqtt",
    };
    if (credentialsRef) device.broker.credentials_ref = credentialsRef;
    if (mconfigEls.maintenanceManualBrokerForm) {
      mconfigEls.maintenanceManualBrokerForm.reset();
    }
    renderMaintenanceInverters();
  }
  mconfigMarkDraftChanged("manual");
  if (mconfigEls.discoveryStatus) {
    mconfigEls.discoveryStatus.textContent = broker
      ? "Local MQTT device added to the in-memory draft. " +
        "Complete its fields on its card, then preview the changes."
      : "Zendure MQTT device added to the in-memory draft. " +
        "Complete its fields on its card, then preview the changes.";
  }
}

// --- features -------------------------------------------------------------

function mconfigFeatureBody(section) {
  const enabledPath = featureEnabledPath(section);
  const features = mconfigState.draft.features || (mconfigState.draft.features = {});
  const fields = (section.fields || []).filter(
    (field) => field.path !== enabledPath
  );
  return mconfigLevelledFields(fields, (field) =>
    mconfigAttachOverrideBadge(
      mconfigCatalogRow(field, features[field.path], (v) => {
        features[field.path] = v;
      }),
      mconfigOverrideEntry(field.path)
    )
  );
}

function renderMaintenanceFeatureSection(section) {
  const id = String(section.id || "feature");
  const enabledPath = featureEnabledPath(section);
  const features = mconfigState.draft.features || (mconfigState.draft.features = {});
  const enabled = enabledPath ? Boolean(features[enabledPath]) : null;
  const card = document.createElement("div");
  card.className = "feature-row mconfig-feature";
  card.setAttribute("role", "listitem");
  card.dataset.featureId = id;
  if (enabled === false) card.dataset.disabled = "true";
  const head = document.createElement("div");
  head.className = "feature-row-head";
  let status;
  if (enabledPath) {
    const checkbox = mconfigCheckboxControl(enabled, (checked) => {
      features[enabledPath] = checked;
      status.textContent = checked ? "Enabled" : "Disabled";
      card.dataset.disabled = checked ? "false" : "true";
    });
    checkbox.className = "feature-enable";
    checkbox.setAttribute("aria-label", "Enable " + (section.title || id));
    head.appendChild(checkbox);
  }
  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "feature-row-summary";
  const title = document.createElement("span");
  title.className = "feature-title";
  title.textContent = section.title || id;
  const description = document.createElement("span");
  description.className = "feature-desc";
  description.textContent = section.description || section.summary || "";
  status = document.createElement("span");
  status.className = "feature-status";
  status.textContent = enabled === null ? "Configured" : enabled ? "Enabled" : "Disabled";
  const caret = document.createElement("span");
  caret.className = "feature-caret";
  caret.setAttribute("aria-hidden", "true");
  const enabledBadge = enabledPath
    ? mconfigOverrideBadge(mconfigOverrideEntry(enabledPath))
    : null;
  summary.append(title, description, status);
  if (enabledBadge) summary.append(enabledBadge);
  summary.append(caret);
  head.appendChild(summary);
  card.appendChild(head);
  const body = document.createElement("div");
  body.className = "feature-body";
  body.id = "maintenance-feature-body-" + id.replace(/[^a-z0-9]/gi, "-");
  body.appendChild(mconfigFeatureBody(section));
  card.appendChild(body);
  summary.setAttribute("aria-controls", body.id);
  const setOpen = (open) => {
    if (open) mconfigState.openFeatures.add(id);
    else mconfigState.openFeatures.delete(id);
    mconfigSetExpanded(card, body, caret, [summary], open);
  };
  summary.addEventListener("click", () => setOpen(card.dataset.open !== "true"));
  setOpen(mconfigState.openFeatures.has(id));
  return card;
}

function renderMaintenanceFeatures() {
  const sections = (mconfigState.catalog && mconfigState.catalog.feature_sections) || [];
  if (mconfigEls.features) mconfigEls.features.textContent = "";
  if (mconfigEls.advanced) mconfigEls.advanced.textContent = "";
  for (const section of sections) {
    const target = section.setup_group === "advanced" ? mconfigEls.advanced : mconfigEls.features;
    if (target) target.appendChild(renderMaintenanceFeatureSection(section));
  }
}

// --- load / render --------------------------------------------------------

function mconfigSummaryLine(summary) {
  const devices = summary.device_count || 0;
  const parts = [devices + (devices === 1 ? " inverter" : " inverters")];
  parts.push(summary.grid_meter_type ? summary.grid_meter_type + " grid meter" : "grid meter missing");
  return parts.join(" · ");
}

function renderMaintenanceConfig(data) {
  if (data.status !== "ok") {
    mconfigState.loaded = false;
    if (mconfigEls.editor) mconfigEls.editor.hidden = true;
    setMaintenanceFact(mconfigEls.source, data.config_path || "—", "muted");
    if (mconfigEls.message) mconfigEls.message.textContent = data.message || "";
    const label = data.status === "missing" ? "Config not found" : "Config invalid";
    setMaintenanceFact(mconfigEls.summary, label, "warn");
    setMaintenanceCardTone("maintenance-config-card", "warn");
    return;
  }

  mconfigState.loaded = true;
  mconfigState.catalog = data.catalog || {
    feature_sections: [],
    hardware_sections: [],
    grid_meter_variants: {},
  };
  mconfigState.overrides = data.overrides || {};
  mconfigState.revision = data.revision || null;
  mconfigState.previewFingerprint = null;
  mconfigState.discoveryDraftChanges = 0;
  mconfigState.pristine = mconfigClone(data.draft || {});
  mconfigState.draft = mconfigClone(data.draft || {});
  mconfigState.openHardware.clear();
  mconfigState.openFeatures.clear();
  seedDefaultOpenFeatureSections(
    mconfigState.catalog.feature_sections,
    mconfigState.openFeatures,
  );

  setMaintenanceFact(mconfigEls.source, data.config_path || "—", "muted");
  if (mconfigEls.message) mconfigEls.message.textContent = "";
  if (mconfigEls.editor) mconfigEls.editor.hidden = false;
  if (mconfigEls.result) mconfigEls.result.hidden = true;
  if (mconfigEls.applyPanel) mconfigEls.applyPanel.hidden = true;
  if (mconfigEls.applyBtn) mconfigEls.applyBtn.hidden = false;
  if (mconfigEls.applyStatus) mconfigEls.applyStatus.textContent = "";
  showCredentialRollbackWarning(mconfigEls.applyRollback, null);
  if (mconfigEls.postApply) mconfigEls.postApply.hidden = true;
  if (mconfigEls.containersSyncStatus) mconfigEls.containersSyncStatus.textContent = "";

  renderMaintenanceGridMeter();
  syncMaintenanceBrokerForm();
  renderMaintenanceInverters();
  renderMaintenanceFeatures();
  mconfigUpdateResetRuntimeButton();

  const line = mconfigSummaryLine(data.summary || {});
  mconfigState.summaryLine = line;
  setMaintenanceFact(mconfigEls.summary, line + " · preview not run", null);
  setMaintenanceCardTone("maintenance-config-card", "ok");
}

let mconfigLoading = false;

async function loadMaintenanceConfig() {
  if (mconfigLoading) return null;
  mconfigLoading = true;
  try {
    const resp = await fetch("/api/admin/maintenance/config");
    if (!resp.ok) throw new Error("maintenance config request failed");
    const data = await resp.json();
    renderMaintenanceConfig(data);
    return data;
  } catch (err) {
    if (mconfigEls.editor) mconfigEls.editor.hidden = true;
    setMaintenanceFact(mconfigEls.summary, "Could not load config", "warn");
    if (mconfigEls.message) {
      mconfigEls.message.textContent =
        "Could not load the current config. The Admin server may be unavailable.";
    }
    return null;
  } finally {
    mconfigLoading = false;
  }
}

// --- preview --------------------------------------------------------------

function renderMaintenanceConfigChange(entry, kind) {
  const row = document.createElement("div");
  row.className = "mconfig-change";
  row.dataset.kind = kind;
  const path = document.createElement("span");
  path.className = "mconfig-change-path";
  path.textContent = entry.path;
  row.appendChild(path);
  const value = document.createElement("span");
  value.className = "mconfig-change-value";
  if (kind === "changed") {
    value.textContent = mconfigDisplayValue(entry.before) + " → " + mconfigDisplayValue(entry.after);
  } else if (kind === "added") {
    value.textContent = "+ " + mconfigDisplayValue(entry.after);
  } else {
    value.textContent = "− " + mconfigDisplayValue(entry.before);
  }
  row.appendChild(value);
  return row;
}

function mconfigDisplayValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function renderMaintenanceConfigPreview(data) {
  if (!mconfigEls.result) return;
  if (data.status !== "ok") {
    mconfigState.previewFingerprint = null;
    if (mconfigEls.applyPanel) mconfigEls.applyPanel.hidden = true;
    mconfigEls.result.hidden = false;
    setMaintenanceFact(mconfigEls.validation, data.status || "error", "warn");
    setMaintenanceFact(mconfigEls.changeSummary, "—", "muted");
    if (mconfigEls.changes) mconfigEls.changes.textContent = "";
    if (mconfigEls.warnings) mconfigEls.warnings.textContent = data.message || "";
    if (mconfigEls.raw) mconfigEls.raw.hidden = true;
    return;
  }

  mconfigEls.result.hidden = false;
  const validation = data.validation || {};
  const ok = Boolean(validation.ok);
  mconfigState.previewFingerprint = ok
    ? JSON.stringify(mconfigState.draft)
    : null;
  if (mconfigEls.applyPanel) {
    mconfigEls.applyPanel.hidden = !ok || !data.changed;
  }
  setMaintenanceFact(
    mconfigEls.validation,
    ok ? "valid" : (validation.errors || []).length + " error(s)",
    ok ? "ok" : "warn"
  );

  const diff = data.diff || { changes: [], added: [], removed: [] };
  const total = (diff.changes || []).length + (diff.added || []).length + (diff.removed || []).length;
  setMaintenanceFact(
    mconfigEls.changeSummary,
    data.changed ? total + " change(s)" : "no changes",
    data.changed ? "ok" : "muted"
  );

  if (mconfigEls.changes) {
    mconfigEls.changes.textContent = "";
    (diff.changes || []).forEach((e) => mconfigEls.changes.appendChild(renderMaintenanceConfigChange(e, "changed")));
    (diff.added || []).forEach((e) => mconfigEls.changes.appendChild(renderMaintenanceConfigChange(e, "added")));
    (diff.removed || []).forEach((e) => mconfigEls.changes.appendChild(renderMaintenanceConfigChange(e, "removed")));
  }

  if (mconfigEls.warnings) {
    const notes = []
      .concat((validation.errors || []).map((i) => i.message))
      .concat((validation.warnings || []).map((i) => i.message))
      .filter(Boolean);
    mconfigEls.warnings.textContent = notes.join(" · ");
  }

  if (mconfigEls.raw && mconfigEls.rawPre) {
    if (data.preview) {
      mconfigEls.rawPre.textContent = JSON.stringify(data.preview, null, 2);
      mconfigEls.raw.hidden = false;
    } else {
      mconfigEls.raw.hidden = true;
    }
  }

  setMaintenanceFact(
    mconfigEls.summary,
    (ok ? "config valid" : "config invalid") + " · " + (data.changed ? total + " change(s)" : "no changes"),
    ok ? "ok" : "warn"
  );
}

let mconfigPreviewing = false;

async function previewMaintenanceConfig() {
  if (mconfigPreviewing || !mconfigState.loaded) return;
  mconfigPreviewing = true;
  const button = mconfigEls.previewBtn;
  if (button) {
    button.disabled = true;
    button.textContent = "Previewing…";
  }
  try {
    const resp = await fetch("/api/admin/maintenance/config/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: mconfigState.draft }),
    });
    const payload = await resp.json();
    // A refused draft (e.g. a connection selection the server could not resolve
    // against current discovery) answers with a status code and a validation
    // body. Rendering it keeps the reason and the next step visible instead of
    // collapsing to a generic transport failure.
    if (!resp.ok && !(payload && payload.validation)) {
      throw new Error("preview request failed");
    }
    renderMaintenanceConfigPreview(payload);
  } catch (err) {
    if (mconfigEls.result) mconfigEls.result.hidden = false;
    setMaintenanceFact(mconfigEls.validation, "preview failed", "warn");
    if (mconfigEls.warnings) {
      mconfigEls.warnings.textContent = "Could not preview the config draft.";
    }
  } finally {
    mconfigPreviewing = false;
    if (button) {
      button.disabled = false;
      button.textContent = "Preview changes";
    }
  }
}

function resetMaintenanceConfigDraft() {
  if (!mconfigState.pristine) return;
  mconfigState.draft = mconfigClone(mconfigState.pristine);
  renderMaintenanceGridMeter();
  syncMaintenanceBrokerForm();
  renderMaintenanceInverters();
  renderMaintenanceFeatures();
  if (mconfigEls.result) mconfigEls.result.hidden = true;
  if (mconfigEls.applyPanel) mconfigEls.applyPanel.hidden = true;
  mconfigState.previewFingerprint = null;
  setMaintenanceFact(
    mconfigEls.summary,
    (mconfigState.summaryLine || "Config loaded") + " · preview not run",
    null
  );
}

function mconfigCollectOverrideTargets() {
  const overrides = mconfigState.overrides || {};
  const targets = [];
  for (const path of Object.keys(overrides)) {
    if (path === "devices") continue;
    const entry = overrides[path];
    if (!entry || entry.source !== "dashboard_override") continue;
    const dot = path.indexOf(".");
    if (dot < 0) continue;
    const head = path.slice(0, dot);
    const key = path.slice(dot + 1);
    if (head === "system") targets.push({ scope: "system", key });
    else targets.push({ scope: "section", section: head, key });
  }
  const devices = overrides.devices || {};
  for (const name of Object.keys(devices)) {
    const fields = devices[name] || {};
    for (const key of Object.keys(fields)) {
      const entry = fields[key];
      if (entry && entry.source === "dashboard_override") {
        const target = { scope: "device", name, key };
        const token = String(entry.physical_identity_token || "").trim();
        if (token.startsWith("opaque:v1:")) {
          target.physical_identity_token = token;
        }
        targets.push(target);
      }
    }
  }
  return targets;
}

function mconfigUpdateResetRuntimeButton() {
  if (mconfigEls.resetRuntimeBtn) {
    mconfigEls.resetRuntimeBtn.hidden = mconfigCollectOverrideTargets().length === 0;
  }
}

let mconfigResettingRuntime = false;

async function resetMaintenanceRuntimeOverrides() {
  if (mconfigResettingRuntime || !mconfigState.loaded) return;
  const targets = mconfigCollectOverrideTargets();
  if (!targets.length) return;
  if (
    !window.confirm(
      "Reset " +
        targets.length +
        " live override(s) to the installed config values? This writes the " +
        "config values into the live runtime state immediately."
    )
  ) {
    return;
  }
  mconfigResettingRuntime = true;
  const button = mconfigEls.resetRuntimeBtn;
  if (button) {
    button.disabled = true;
    button.textContent = "Resetting…";
  }
  try {
    const resp = await fetch("/api/admin/maintenance/config/reset-runtime", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targets }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data && data.error ? data.error : "reset failed");
    }
    await loadMaintenanceConfig();
  } catch (err) {
    if (mconfigEls.warnings) {
      mconfigEls.warnings.textContent =
        "Could not reset live overrides: " + (err && err.message ? err.message : err);
    }
  } finally {
    mconfigResettingRuntime = false;
    if (button) {
      button.disabled = false;
      button.textContent = "Reset live overrides";
    }
  }
}

if (mconfigEls.addInverter) {
  mconfigEls.addInverter.addEventListener("click", addManualMaintenanceInverter);
}
if (mconfigEls.addMqttDevice) {
  mconfigEls.addMqttDevice.addEventListener("click", addManualMaintenanceMqttDevice);
}
wireMaintenanceBrokerForm();
if (mconfigEls.discoveryStart) {
  mconfigEls.discoveryStart.addEventListener("click", startMaintenanceDiscovery);
}
if (mconfigEls.discoveryManualForm) {
  mconfigEls.discoveryManualForm.addEventListener("submit", runMaintenanceManualScan);
}
if (mconfigEls.discoveryReset) {
  mconfigEls.discoveryReset.addEventListener("click", resetMaintenanceDiscovery);
}
// Opening a Discovery sources row mounts the shared parked config node into
// its slot; closing it parks the node back.
document.querySelectorAll("[data-maintenance-source]").forEach((row) => {
  row.addEventListener("toggle", () => {
    const source = row.getAttribute("data-maintenance-source");
    if (row.open) mountMaintenanceSourceConfig(source);
    else parkMaintenanceSourceConfig(source);
  });
});
if (mconfigEls.previewBtn) mconfigEls.previewBtn.addEventListener("click", previewMaintenanceConfig);
if (mconfigEls.resetBtn) mconfigEls.resetBtn.addEventListener("click", resetMaintenanceConfigDraft);
if (mconfigEls.resetRuntimeBtn) {
  mconfigEls.resetRuntimeBtn.addEventListener("click", resetMaintenanceRuntimeOverrides);
}

let mconfigApplying = false;

async function applyMaintenanceConfig() {
  if (mconfigApplying || !mconfigState.loaded) return;
  if (JSON.stringify(mconfigState.draft) !== mconfigState.previewFingerprint) {
    mconfigEls.applyStatus.textContent =
      "The draft changed after preview. Preview the current draft before applying.";
    return;
  }
  const backup = !mconfigEls.backup || mconfigEls.backup.checked;
  const warning = backup
    ? "Apply the reviewed draft to config/config.json? A backup will be created first."
    : "Apply without a backup? You will not have an Admin rollback copy for this change.";
  if (!window.confirm(warning)) return;
  mconfigApplying = true;
  mconfigEls.applyBtn.disabled = true;
  mconfigEls.applyBtn.textContent = "Applying…";
  mconfigEls.applyStatus.textContent = "Validating and applying the reviewed draft…";
  showCredentialRollbackWarning(mconfigEls.applyRollback, null);
  try {
    const resp = await fetch("/api/admin/maintenance/config/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft: mconfigState.draft,
        revision: mconfigState.revision,
        confirm: true,
        backup,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    showCredentialRollbackWarning(mconfigEls.applyRollback, data);
    if (!resp.ok || !data.ok) {
      throw new Error(data.message || data.error || "Could not apply the config draft.");
    }
    const successMessage =
      "Config updated at " + data.path +
      (data.backup_path ? " · backup: " + data.backup_path : " · no backup created");
    await loadMaintenanceConfig();
    // Refresh the overview facts only: config + container plan are handled
    // explicitly below so the guided post-apply panel is not reset.
    await loadMaintenanceOverview({
      refreshConfig: false,
      refreshContainerPlan: false,
    });
    if (mconfigEls.result) mconfigEls.result.hidden = false;
    mconfigEls.applyPanel.hidden = false;
    mconfigEls.applyBtn.hidden = true;
    if (data.changed === false) {
      // A no-op apply must not push the guided restart step: nothing was written.
      if (mconfigEls.postApply) mconfigEls.postApply.hidden = true;
      mconfigEls.applyStatus.textContent =
        "No config changes were written. Container restart is not required.";
      setMaintenanceFact(mconfigEls.summary, "No config changes written", "muted");
    } else {
      mconfigEls.applyStatus.textContent = successMessage;
      setMaintenanceFact(mconfigEls.summary, "Config updated · container sync recommended", "ok");
      setMaintenanceCardTone("maintenance-config-card", "action");
      // Do not auto-run diagnostics here: they would still hit the old container
      // and config. The user runs diagnostics after the container sync.
      await loadMaintenanceContainerPlan({ showPostApply: true });
      if (mconfigEls.postApply) {
        mconfigEls.postApply.hidden = false;
        mconfigEls.postApply.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
  } catch (err) {
    mconfigEls.applyStatus.textContent = err.message || String(err);
  } finally {
    mconfigApplying = false;
    mconfigEls.applyBtn.disabled = false;
    mconfigEls.applyBtn.textContent = "Apply reviewed draft";
  }
}

const CONTAINER_SYNC_LABEL = "Restart / sync containers";
const CONTAINER_SYNC_CONFIRM =
  "Restart / sync containers with the current config? This may recreate EMS and " +
  "start or stop optional feature containers. It will not delete config, data, " +
  "containers, volumes, or backups.";

// Both host panels (post-apply completion block and the always-visible Runtime
// containers section) render from the same plan; keep their target handles here.
function containerPlanTargets() {
  return [
    {
      emsDesired: mconfigEls.postEmsDesired,
      influxDesired: mconfigEls.postInfluxDesired,
      actionSummary: mconfigEls.postActionSummary,
      syncButton: mconfigEls.containersSync,
    },
    {
      emsDesired: maintenanceEls.runtimeEmsDesired,
      influxDesired: maintenanceEls.runtimeInfluxDesired,
      actionSummary: maintenanceEls.runtimeActionSummary,
      syncButton: maintenanceEls.runtimeContainersSync,
    },
  ];
}

async function loadMaintenanceContainerPlan(options = {}) {
  const showPostApply = options.showPostApply === true;
  if (showPostApply && mconfigEls.postApply) {
    mconfigEls.postApply.hidden = false;
  }
  containerPlanTargets().forEach((targets) => {
    if (targets.actionSummary) targets.actionSummary.textContent = "Checking container state…";
  });
  try {
    const resp = await fetch("/api/admin/maintenance/containers/plan");
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      setContainerPlanUnavailable(data.message || "Container plan unavailable");
      return;
    }
    renderMaintenanceContainerPlan(data);
  } catch (err) {
    setContainerPlanUnavailable("Container plan unavailable");
  }
}

function setContainerPlanUnavailable(message) {
  containerPlanTargets().forEach((targets) => {
    setMaintenanceFact(targets.actionSummary, message, "warn");
    if (targets.syncButton) targets.syncButton.disabled = true;
  });
  setMaintenanceFact(maintenanceEls.ems, "unavailable", "muted");
  setMaintenanceFact(maintenanceEls.emsDetail, "", "muted");
  setMaintenanceFact(maintenanceEls.influx, "unavailable", "muted");
  setMaintenanceFact(maintenanceEls.influxDetail, "", "muted");
  setMaintenanceFact(maintenanceEls.containersSummary, message, "warn");
  setMaintenanceCardTone("maintenance-containers", "warn");
}

function renderContainerPlanInto(plan, targets) {
  const services = plan.services || {};
  const ems = services.ems || {};
  const influx = services.influxdb || {};
  const emsDesired = ems.desired_state || "unknown";
  const influxDesired = influx.desired_state || "unknown";
  setMaintenanceFact(
    targets.emsDesired,
    emsDesired,
    emsDesired === "running" ? "ok" : "muted"
  );
  setMaintenanceFact(
    targets.influxDesired,
    influxDesired,
    influxDesired === "running" ? "ok" : "muted"
  );
  setMaintenanceFact(targets.actionSummary, plan.summary || "No action required", null);
  const hasActions =
    Array.isArray(plan.actions) && plan.actions.some((item) => item.action !== "none");
  if (targets.syncButton) {
    targets.syncButton.disabled = !hasActions || !plan.available;
  }
}

// The Runtime containers card (02) surfaces the derived feature/container status
// so a disabled feature reads as "Disabled", not a broken "missing" container.
function renderRuntimeServiceStatus(plan) {
  const services = plan.services || {};
  const ems = services.ems || {};
  const influx = services.influxdb || {};
  setMaintenanceFact(maintenanceEls.ems, ems.display_label || "unknown", ems.tone || "muted");
  setMaintenanceFact(maintenanceEls.emsDetail, ems.display_detail || "", "muted");
  setMaintenanceFact(
    maintenanceEls.influx,
    influx.display_label || "unknown",
    influx.tone || "muted"
  );
  setMaintenanceFact(maintenanceEls.influxDetail, influx.display_detail || "", "muted");

  const summaryText = plan.available
    ? plan.status_summary || "Container status unavailable"
    : plan.message || "Docker unavailable";
  const tone = !plan.available
    ? "warn"
    : containerSummaryTone(ems.tone, influx.tone);
  setMaintenanceFact(maintenanceEls.containersSummary, summaryText, tone);
  setMaintenanceCardTone("maintenance-containers", tone);
}

function containerSummaryTone(...tones) {
  if (tones.includes("warn")) return "warn";
  if (tones.includes("info")) return "info";
  if (tones.includes("ok")) return "ok";
  return "muted";
}

function renderMaintenanceContainerPlan(plan) {
  containerPlanTargets().forEach((targets) => renderContainerPlanInto(plan, targets));
  renderRuntimeServiceStatus(plan);
}

const CONTAINER_STEP_LABELS = {
  "influxdb:init": "InfluxDB init",
  "influxdb:start": "InfluxDB start",
  "influxdb:stop": "InfluxDB stop",
  "influxdb:sync": "InfluxDB sync",
  "ems:recreate": "EMS recreate",
};

function formatContainerSyncSteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) return "";
  return steps
    .map((step) => {
      const label =
        CONTAINER_STEP_LABELS[`${step.service}:${step.action}`] ||
        `${step.service} ${step.action}`;
      return `${label} ${step.status}`;
    })
    .join(", ");
}

function setContainerSyncBusy(isBusy) {
  const buttons = [mconfigEls.containersSync, maintenanceEls.runtimeContainersSync].filter(Boolean);
  buttons.forEach((button) => {
    button.disabled = isBusy;
    button.textContent = isBusy ? "Syncing…" : CONTAINER_SYNC_LABEL;
  });
}

async function syncMaintenanceContainers(statusEl, reason = "manual") {
  if (!window.confirm(CONTAINER_SYNC_CONFIRM)) return;
  setContainerSyncBusy(true);
  if (statusEl) statusEl.textContent = "Synchronizing containers…";
  try {
    const resp = await fetch("/api/admin/maintenance/containers/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true, reason }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      throw new Error(data.message || data.error || "Container sync failed.");
    }
    const stepSummary = formatContainerSyncSteps(data.steps);
    if (statusEl) {
      statusEl.textContent = stepSummary
        ? `Container sync completed: ${stepSummary}.`
        : "Container sync completed.";
    }
    // Refresh facts only; the container plan (and any visible post-apply panel)
    // is reloaded explicitly so the guided view is preserved.
    const keepPostApply = Boolean(mconfigEls.postApply && !mconfigEls.postApply.hidden);
    await loadMaintenanceOverview({ refreshConfig: false, refreshContainerPlan: false });
    await loadMaintenanceContainerPlan({ showPostApply: keepPostApply });
  } catch (err) {
    if (statusEl) statusEl.textContent = err.message || String(err);
  } finally {
    setContainerSyncBusy(false);
  }
}

if (mconfigEls.applyBtn) {
  mconfigEls.applyBtn.addEventListener("click", applyMaintenanceConfig);
}
if (mconfigEls.containersSync) {
  mconfigEls.containersSync.addEventListener("click", () =>
    syncMaintenanceContainers(mconfigEls.containersSyncStatus, "config_apply")
  );
}
if (mconfigEls.containersRecheck) {
  mconfigEls.containersRecheck.addEventListener("click", async () => {
    await loadMaintenanceOverview({ refreshConfig: false, refreshContainerPlan: false });
    await loadMaintenanceContainerPlan({ showPostApply: true });
  });
}
if (mconfigEls.postDiagnostics) {
  mconfigEls.postDiagnostics.addEventListener("click", runDiagnostics);
}
if (maintenanceEls.runtimeContainersSync) {
  maintenanceEls.runtimeContainersSync.addEventListener("click", () =>
    syncMaintenanceContainers(maintenanceEls.runtimeContainersStatus, "manual")
  );
}
if (maintenanceEls.runtimeContainersRecheck) {
  maintenanceEls.runtimeContainersRecheck.addEventListener("click", async () => {
    await loadMaintenanceOverview({ refreshConfig: false, refreshContainerPlan: false });
    await loadMaintenanceContainerPlan({ showPostApply: false });
  });
}
if (maintenanceEls.runtimeDiagnostics) {
  maintenanceEls.runtimeDiagnostics.addEventListener("click", runDiagnostics);
}

// The Admin UI opens on a router screen that detects the install state and
// recommends the safest of the only two flows (set up new / manage existing).
// The setup wizard must not auto-run when an install already exists, so its
// network-touching init is deferred until the user chooses "Set up a new
// system". Every server-provided path/message passes through escapeHtml.

const RECOMMEND_LABELS = {
  setup_new: "Guided setup",
  manage_existing: "Maintenance",
};

const startEls = {
  gate: document.getElementById("view-start"),
  recommend: document.getElementById("start-recommend"),
  error: document.getElementById("start-path-error"),
};

let workspaceRevealed = false;
let startPathBusy = false;
let setupIntentId = null;

function setStartError(message) {
  if (!startEls.error) return;
  if (!message) {
    startEls.error.hidden = true;
    startEls.error.textContent = "";
    return;
  }
  startEls.error.hidden = false;
  startEls.error.textContent = message;
}

function renderRecommendation(state) {
  const recommended = state.recommended_path;
  const label = escapeHtml(RECOMMEND_LABELS[recommended] || "Maintenance");
  const notes = []
    .concat(Array.isArray(state.reasons) ? state.reasons : [])
    .concat(Array.isArray(state.warnings) ? state.warnings : []);
  let html = '<p class="start-recommend-line">Recommended: <strong>' + label + "</strong></p>";
  if (notes.length) {
    html +=
      '<ul class="start-recommend-notes">' +
      notes.map((note) => "<li>" + escapeHtml(note) + "</li>").join("") +
      "</ul>";
  }
  startEls.recommend.innerHTML = html;
  highlightRecommendedChoice(recommended);
}

// Highlight the recommended landing card. Falls back to leaving the static
// default (Guided setup) highlighted if the recommendation is unknown.
function highlightRecommendedChoice(recommended) {
  const cards = document.querySelectorAll(".start-choice-nav");
  if (!cards.length || !RECOMMEND_LABELS[recommended]) return;
  cards.forEach((card) => {
    card.classList.toggle("is-recommended", card.dataset.startPath === recommended);
  });
}

async function loadInstallState() {
  try {
    const resp = await fetch("/api/admin/install-state");
    if (!resp.ok) throw new Error("install-state request failed");
    const state = await resp.json();
    renderRecommendation(state);
  } catch (err) {
    startEls.recommend.textContent =
      "Could not detect the current installation. Choose an option to continue.";
  }
}

function revealWorkspace() {
  if (startEls.gate) startEls.gate.hidden = true;
  workspaceRevealed = true;
}

// Return to the landing gate from any workspace page. Clears the hash without a
// route so applyHashRoute (guarded on workspaceRevealed) can't re-open a panel.
function showLanding() {
  document.querySelectorAll("[data-admin-view-panel]").forEach((panel) => {
    panel.hidden = true;
  });
  if (startEls.gate) startEls.gate.hidden = false;
  workspaceRevealed = false;
  if (window.location.hash) {
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  // No task is open: drop any synthetic preview, park the pipeline and stop
  // task-local polling. The durable backend transition is untouched, so a later
  // resume reopens the right task.
  rescopeSystemBuildForNavigation();
}

function enterSetup() {
  revealWorkspace();
  if (!setupInitialized) initSetupWizard();
  window.location.hash = "setup";
  setAdminView("setup");
}

function enterMaintenance() {
  revealWorkspace();
  window.location.hash = "maintenance";
  setAdminView("maintenance");
  setMaintenancePath("hub");
}

// Forward navigation only sets the hash; applyHashRoute drives the panel switch
// and the single overview load so opening a panel never double-fetches.
document.querySelectorAll("[data-open-maintenance-path]").forEach((button) => {
  button.addEventListener("click", () => {
    const path = button.dataset.openMaintenancePath;
    if (!MAINTENANCE_PATHS.includes(path) || path === "hub") return;
    window.location.hash = "maintenance-" + path;
  });
});

// Shared "← Back" navigation: landing returns to the start gate, maintenance-hub
// returns to the hub. Every workspace page carries one of these controls.
function navigateBack(target) {
  if (target === "maintenance-hub") {
    window.location.hash = "maintenance";
    return;
  }
  showLanding();
}

document.querySelectorAll("[data-back]").forEach((button) => {
  button.addEventListener("click", () => navigateBack(button.dataset.back));
});

async function migrateLegacyConfig() {
  const resp = await fetch("/api/admin/config/migrate-legacy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  const result = await resp.json().catch(() => ({}));
  if (!resp.ok || !result.ok) {
    const message = result.message || "Could not migrate the legacy config.json.";
    setStartError(message);
    return false;
  }
  return true;
}

async function postStartPath(choice, confirm) {
  const resp = await fetch("/api/admin/start-path", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choice, confirm: Boolean(confirm) }),
  });
  const result = await resp.json().catch(() => ({}));
  return { status: resp.status, result };
}

function setupIntentHeaders(initial) {
  const headers = new Headers(initial || {});
  if (setupIntentId) {
    headers.set("X-Setup-Intent-ID", setupIntentId);
  }
  return headers;
}

// Open a landing path (Guided setup / Maintenance) directly from its card. The
// busy guard prevents a double-click from firing two start-path requests.
async function startPath(choice) {
  if (startPathBusy || !choice) return;
  setupIntentId = null;
  freshSetupConfirmationRequired = false;
  setStartError("");
  startPathBusy = true;
  try {
    let { result } = await postStartPath(choice, false);
    if (result.requires_confirmation) {
      const proceed = window.confirm(
        "An existing installation was detected. Setting up a new system can " +
          "replace its configuration. Continue anyway?"
      );
      if (!proceed) return;
      ({ result } = await postStartPath(choice, true));
      if (result.requires_confirmation || !result.ok) {
        setStartError(result.message || "Could not start setup.");
        return;
      }
    }
    if (!result.ok) {
      setStartError(result.message || "Could not continue.");
      return;
    }
    if (result.route === "setup") {
      if (!result.setup_intent_id || !result.setup_workflow_id) {
        setStartError("Fresh Setup confirmation was not recorded. Try again.");
        return;
      }
      setupIntentId = result.setup_intent_id;
      setSetupWorkflowId(result.setup_workflow_id);
      enterSetup();
      return;
    }
    if (result.migrate_legacy_config) {
      const proceed = window.confirm(
        "A previous root config.json was found. Migrate it to the standard " +
          "config/config.json layout now? A backup is created first."
      );
      if (proceed) {
        const migrated = await migrateLegacyConfig();
        if (!migrated) return;
      }
    }
    enterMaintenance();
  } finally {
    startPathBusy = false;
  }
}

document.querySelectorAll("[data-start-path]").forEach((card) => {
  card.addEventListener("click", () => startPath(card.dataset.startPath));
});

// --- Paired System Build selection / recovery ------------------------------
// The browser supplies one immutable tag only. Image repositories, digests and
// pull targets remain server-owned; every dynamic response value is written via
// textContent so a malicious tag or registry error can never become markup.

const systemAlignmentEls = {
  workflow: document.getElementById("system-alignment-workflow"),
  tag: document.getElementById("system-alignment-tag"),
  buildId: document.getElementById("system-alignment-build-id"),
  revision: document.getElementById("system-alignment-revision"),
  adminImage: document.getElementById("system-alignment-admin-image"),
  emsImage: document.getElementById("system-alignment-ems-image"),
  message: document.getElementById("system-alignment-message"),
  warning: document.getElementById("system-alignment-warning"),
  reconnect: document.getElementById("system-alignment-reconnect"),
  partial: document.getElementById("system-alignment-partial"),
  partialMessage: document.getElementById("system-alignment-partial-message"),
  resume: document.getElementById("system-alignment-resume"),
  returnToRunning: document.getElementById("system-alignment-return"),
  abandon: document.getElementById("system-alignment-abandon"),
  retryCleanup: document.getElementById("system-alignment-retry-cleanup"),
};

// The recovery action is chosen by the transition's owner, never by the panel:
// Setup-owned transitions discard their artifacts, an upgrade only cancels.
const SETUP_TRANSITION_MODES = new Set(["fresh_install", "automated_setup"]);

const DISCARD_SETUP_CONFIRM =
  "Discard this setup?\n\n" +
  "The current setup draft, generated configuration, deployment plan and " +
  "setup progress will be removed.\n\n" +
  "The installed EMS system, live configuration, runtime data, containers, " +
  "volumes and backups will not be changed.";

const CANCEL_UPGRADE_CONFIRM =
  "Cancel this upgrade?\n\n" +
  "The System Build transition stops here and the console returns to the " +
  "normal setup and upgrade choices.\n\n" +
  "The running EMS build, live configuration and backups are left as they are.";

const SETUP_CLEANUP_PENDING_MESSAGE =
  "Setup has stopped. Temporary files remain. No new Setup or Upgrade can " +
  "start until cleanup succeeds. The live config and the running EMS were not " +
  "changed by the failed cleanup.";

const SETUP_CLEANUP_REVIEW_MESSAGE =
  "Setup has stopped. Files remain that cannot be proven to belong to this " +
  "setup, so they were kept for review. No new Setup or Upgrade can start " +
  "until they are resolved. The live config and the running EMS were not " +
  "changed.";

// Which unresolved cleanup state a backend response describes, or null when the
// response reports none. "pending" is a failed removal a retry can converge;
// "review_required" needs an operator, so a retry is not offered as a fix.
function setupCleanupStateFor(data) {
  if (!data) return null;
  if (data.error === "abandon_cleanup_incomplete") return "pending";
  if (data.error === "setup_artifact_review_required") return "review_required";
  const cleanup =
    (data.workflow && data.workflow.cleanup) ||
    (typeof data.state === "string" ? data : null) ||
    (data.cleanup_state ? { state: data.cleanup_state, blocking: true } : null);
  if (!cleanup) return null;
  if (data.error !== "setup_cleanup_required" && cleanup.blocking !== true) {
    return null;
  }
  return cleanup.state === "review_required" ? "review_required" : "pending";
}

// An unfinished cleanup is durable backend state, so it outlives a transition
// status poll: the renderer must not erase it, and the card that carries the
// retry has to stay reachable once the transition itself is already terminal.
let setupCleanupState = null;

function setupCleanupWarningText() {
  if (setupCleanupState === "review_required") return SETUP_CLEANUP_REVIEW_MESSAGE;
  if (setupCleanupState === "pending") return SETUP_CLEANUP_PENDING_MESSAGE;
  return null;
}

// Truthful partial cleanup: the transition may already be gone while files
// remain, so the panel says so and offers a retry instead of "done".
function showSetupCleanupIncomplete(data) {
  setupCleanupState = setupCleanupStateFor(data);
  renderSetupCleanupRecovery();
}

function renderSetupCleanupRecovery() {
  const text = setupCleanupWarningText();
  if (systemAlignmentEls.warning && text !== null) {
    systemAlignmentEls.warning.textContent = text;
    systemAlignmentEls.warning.hidden = false;
  } else if (systemAlignmentEls.warning && setupCleanupState === null) {
    systemAlignmentEls.warning.hidden = true;
  }
  if (systemAlignmentEls.partial && setupCleanupState !== null) {
    systemAlignmentEls.partial.hidden = false;
  }
  if (systemAlignmentEls.partialMessage && text !== null) {
    systemAlignmentEls.partialMessage.textContent = text;
  }
  if (systemAlignmentEls.retryCleanup) {
    // Only a failed removal converges on a retry; an unknown owner needs an
    // operator, so the action is not offered as if it would help.
    systemAlignmentEls.retryCleanup.hidden = setupCleanupState !== "pending";
  }
  // While cleanup owns the workflow, no other recovery action applies.
  for (const element of [
    systemAlignmentEls.resume,
    systemAlignmentEls.returnToRunning,
    systemAlignmentEls.abandon,
  ]) {
    if (element && setupCleanupState !== null) element.hidden = true;
  }
}

async function retrySetupCleanup() {
  try {
    // The retry must name the workflow that owns the failed cleanup; the server
    // record is the authority for which one that is.
    const workflowId = await fetchOwningSetupWorkflowId();
    if (!workflowId) {
      showSetupCleanupIncomplete(null);
      loadSystemAlignmentStatus();
      return;
    }
    const res = await fetch("/api/setup/abandon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setup_workflow_id: workflowId }),
    });
    const data = await res.json().catch(() => ({}));
    if (isSetupOperationInProgress(data)) {
      if (systemAlignmentEls.warning) {
        systemAlignmentEls.warning.textContent =
          setupOperationInProgressMessage(data);
        systemAlignmentEls.warning.hidden = false;
      }
      return;
    }
    if (setupCleanupStateFor(data) !== null) {
      showSetupCleanupIncomplete(data);
      return;
    }
    if (!res.ok || data.ok !== true) {
      throw new Error(data.message || data.error || "Cleanup did not finish.");
    }
    showSetupCleanupIncomplete(null);
    setSetupWorkflowId(null);
    loadSystemAlignmentStatus();
  } catch (err) {
    if (systemAlignmentEls.warning) {
      systemAlignmentEls.warning.textContent = err.message || String(err);
      systemAlignmentEls.warning.hidden = false;
    }
  }
}

function recoveryActionFor(mode) {
  if (SETUP_TRANSITION_MODES.has(mode)) {
    return {
      owner: "guided_setup",
      label: "Discard setup",
      endpoint: "/api/setup/abandon",
      confirm: DISCARD_SETUP_CONFIRM,
    };
  }
  if (mode === "guided_upgrade") {
    return {
      owner: "guided_upgrade",
      label: "Cancel upgrade",
      endpoint: "/api/admin/system-alignment/cancel",
      confirm: CANCEL_UPGRADE_CONFIRM,
    };
  }
  return null;
}

const SYSTEM_ALIGNMENT_STAGE_ORDER = [
  "select",
  "validate",
  "align-admin",
  "reconnect",
  "verify-resources",
  "install-ems",
  "verify-system",
];

const SYSTEM_ALIGNMENT_STAGE_INDEX = {
  selection_started: 0,
  validation_running: 1,
  validation_failed: 1,
  validated: 1,
  admin_update_pending: 2,
  admin_alignment_started: 2,
  admin_reconnect_pending: 3,
  admin_aligned: 4,
  resources_verified: 5,
  ems_operation_pending: 5,
  ems_operation_running: 5,
  healthcheck_pending: 6,
  completed: 7,
};

const SYSTEM_ALIGNMENT_TRANSITION_STAGES = new Set([
  "admin_update_pending",
  "admin_alignment_started",
  "admin_reconnect_pending",
  "admin_aligned",
  "resources_verified",
  "ems_operation_pending",
  "ems_operation_running",
  "healthcheck_pending",
  "failed_recoverable",
]);
const SYSTEM_ALIGNMENT_TERMINAL_STAGES = new Set([
  "completed",
  "cancelled",
  "failed_unrecoverable",
]);
const SYSTEM_ALIGNMENT_POLL_INTERVAL_MS = 1800;

let systemAlignmentState = null;
let systemAlignmentPollTimer = null;
// Stale-response guard for the single status poller: bumped on every stop, so a
// status response from a superseded poll is dropped instead of rendering.
let systemAlignmentPollGeneration = 0;

// --- Guided Setup Step 1: one System Build selection drives alignment --------
// Button state consumes the server verdict; the browser never infers alignment.
const setupSystemBuildEls = {
  status: document.getElementById("setup-system-build-status"),
  error: document.getElementById("setup-system-build-error"),
  // The two alternative Step 1 actions share one footer; only the valid one is
  // ever the active primary. Both stay visible so the alternative is legible.
  actions: document.getElementById("setup-system-build-actions"),
  align: document.getElementById("setup-system-build-align"),
  next: document.getElementById("setup-system-build-next"),
  recreateNotice: document.getElementById("setup-system-build-recreate-notice"),
};

let selectedSystemBuildTag = null;
// When a reconnect/reload resumes an in-progress operation, the restored build
// must be re-verified (not merely previewed) so its aligned/verified state is
// restored. This one-shot flag tells the next loadReleases to validate the
// resumed build instead of showing a fresh, side-effect-free preview.
let systemBuildResumeValidationTag = null;
const SYSTEM_BUILD_STATUS = {
  IDLE: "idle",
  // A build is selected and previewed from the local catalogue but not yet
  // verified. Selection is side-effect free: no pull, no full validation runs
  // until the user explicitly verifies it.
  SELECTED: "selected",
  VALIDATING: "validating",
  VALID: "valid",
  INVALID: "invalid",
  CONFIRMING: "confirming",
  UPDATING: "updating",
  RECONNECTING: "reconnecting",
  FAILED: "failed",
};
const systemBuildState = {
  status: SYSTEM_BUILD_STATUS.IDLE,
  // Monotonic counter: only the newest validation's response is applied, so a
  // slow earlier response can never overwrite a newer selection's verdict.
  validationGeneration: 0,
  result: null,
  error: null,
  lastAction: null,
  // Which action's failure is currently surfaced (validate | align | confirm).
  // Owned by the mutation that failed; an internal revalidation never sets it.
  failedAction: null,
};
let setupOperationId = null;
// A confirmed, tag-bound Fresh Setup operation. Only this authorizes the later
// setup steps; a prepared release cache never does.
let setupOperationContext = null;
function bindConfirmedSetupOperation(operationId, systemTag) {
  setupOperationId = operationId;
  setupOperationContext = { operationId, systemTag };
}
function clearSetupOperationContext() {
  setupOperationId = null;
  setupOperationContext = null;
}
let systemBuildMutationLocked = false;
// A one-shot setup intent authorizes exactly one mutation. Once the server
// reports it stale (consumed/expired/changed/required) the flow must return to
// Step 1 and demand a new Fresh Setup confirmation instead of resending.
let freshSetupConfirmationRequired = false;

const STALE_SETUP_INTENT_REASONS = new Set([
  "setup_intent_consumed",
  "setup_intent_expired",
  "setup_state_changed",
  "setup_intent_required",
]);

// Handle a stale-intent rejection: drop the id, block Next/Update and reopen
// Step 1 with the shared error surface. Returns true when it handled the error
// so callers stop treating the response as a normal failure.
function handleSetupIntentRejection(data) {
  const reason = data && data.error;
  if (!STALE_SETUP_INTENT_REASONS.has(reason)) return false;
  setupIntentId = null;
  clearSetupOperationContext();
  freshSetupConfirmationRequired = true;
  systemBuildMutationLocked = false;
  systemBuildState.status = SYSTEM_BUILD_STATUS.IDLE;
  systemBuildState.error = null;
  setSystemBuildError(
    (data && data.message) ||
      "Confirm Fresh Setup again before starting another operation."
  );
  setActiveStep("release");
  applySystemBuildAlignment();
  return true;
}

// A System Build mutation was refused because this tab is not the workflow the
// server owns. Reuse the task-local workflow conflict panel (no new visual
// system), unlock Step 1 and stop treating the response as a build failure.
// Returns true once handled so the caller does not also report a generic error.
function handleSystemBuildWorkflowConflict(data) {
  if (!isSetupWorkflowConflict(data)) return false;
  handleSetupWorkflowConflict(data);
  clearSetupOperationContext();
  systemBuildMutationLocked = false;
  systemBuildState.status = SYSTEM_BUILD_STATUS.IDLE;
  systemBuildState.error = null;
  systemBuildState.failedAction = null;
  setSystemBuildError((data && data.message) || SETUP_WORKFLOW_STALE_MESSAGE);
  setActiveStep("release");
  applySystemBuildAlignment();
  return true;
}

// User-facing status lines for the Admin Server update action. The concrete
// technical cause (retag/recreate/image) stays in the validation checklist and
// diagnostics, never in this plain-language status line.
const SYSTEM_BUILD_ALIGNMENT_TEXT = {
  aligned: "The Admin Server is ready for the selected System Build.",
  legacy_ready:
    "The current Admin Server can install this legacy EMS release. " +
    "Continue to prepare the selected release resources.",
  update_required: "The Admin Server must be updated before you can continue.",
};

// The update-admin start returns the operation id at the top level; the nested
// transition copy is kept only as a compatible fallback.
function reconnectOperationIdFromStart(data) {
  return (
    (data && data.operation_id) ||
    (data && data.transition && data.transition.operation_id) ||
    null
  );
}

function systemBuildIsUpdating() {
  return (
    systemBuildState.status === SYSTEM_BUILD_STATUS.UPDATING ||
    systemBuildState.status === SYSTEM_BUILD_STATUS.RECONNECTING
  );
}

function systemBuildMutationInProgress() {
  return (
    systemBuildMutationLocked ||
    systemBuildState.status === SYSTEM_BUILD_STATUS.CONFIRMING ||
    systemBuildState.status === SYSTEM_BUILD_STATUS.UPDATING ||
    systemBuildState.status === SYSTEM_BUILD_STATUS.RECONNECTING
  );
}

function setSystemBuildError(message) {
  if (!setupSystemBuildEls.error) return;
  setupSystemBuildEls.error.textContent = message || "";
  setupSystemBuildEls.error.hidden = !message;
}

// A live transition for a different build owns the flow. The canonical tag is
// accepted too so a floating selection tag never reads as foreign.
function foreignTransitionActive(result) {
  if (!result || result.transition_in_progress !== true) return false;
  const activeTag = result.active_transition_tag;
  if (!activeTag) return false;
  const canonical = result.system_build && result.system_build.canonical_tag;
  return activeTag !== selectedSystemBuildTag && activeTag !== canonical;
}

function systemBuildActionState() {
  const result = systemBuildState.result;
  return result && result.action_state && typeof result.action_state === "object"
    ? result.action_state
    : null;
}

function systemBuildNextAllowed() {
  const action = systemBuildActionState();
  return (
    !freshSetupConfirmationRequired &&
    systemBuildState.status === SYSTEM_BUILD_STATUS.VALID &&
    Boolean(action) &&
    action.continue_allowed === true &&
    action.admin_update_allowed !== true
  );
}

// Present a locally-selected System Build without any registry side effect:
// show the catalogue preview, drop any previous verification, and require an
// explicit Verify. No image is pulled and no full validation runs here — those
// only happen when the user clicks Verify System Build.
function presentSelectedSystemBuild(tag) {
  selectedSystemBuildTag = tag || null;
  systemBuildState.result = null;
  systemBuildState.error = null;
  systemBuildState.lastAction = null;
  systemBuildState.failedAction = null;
  systemBuildState.status = selectedSystemBuildTag
    ? SYSTEM_BUILD_STATUS.SELECTED
    : SYSTEM_BUILD_STATUS.IDLE;
  applySystemBuildAlignment();
}

// Verify is the single explicit action that starts full verification. It is
// offered only for a freshly selected, selectable build.
function systemBuildVerifyAllowed() {
  if (systemBuildState.status !== SYSTEM_BUILD_STATUS.SELECTED) return false;
  if (!selectedSystemBuildTag || freshSetupConfirmationRequired) return false;
  const release = (setupState.release.releases || []).find(
    (item) => item.tag === selectedSystemBuildTag
  );
  return !release || release.selectable !== false;
}

function systemBuildUpdateAllowed() {
  const action = systemBuildActionState();
  return (
    !freshSetupConfirmationRequired &&
    systemBuildState.status === SYSTEM_BUILD_STATUS.VALID &&
    Boolean(action) &&
    action.admin_update_allowed === true &&
    action.continue_allowed !== true
  );
}

function systemBuildStatusMessage() {
  const result = systemBuildState.result;
  const action = systemBuildActionState();
  switch (systemBuildState.status) {
    case SYSTEM_BUILD_STATUS.UPDATING:
      return "The Admin Server is being updated. The browser will reconnect automatically.";
    case SYSTEM_BUILD_STATUS.RECONNECTING:
      return "Waiting for the updated Admin Server.";
    case SYSTEM_BUILD_STATUS.SELECTED:
      return "Select Verify System Build to download and verify this System Build.";
    case SYSTEM_BUILD_STATUS.CONFIRMING:
      return "Confirming the selected System Build…";
    case SYSTEM_BUILD_STATUS.VALIDATING:
      return "Downloading and verifying the Admin and EMS images…";
    case SYSTEM_BUILD_STATUS.FAILED:
      // Name the failed action; the concrete detail stays in the error surface.
      if (systemBuildState.failedAction === "validate") {
        return "System Build validation failed. Check the details and try again.";
      }
      if (systemBuildState.failedAction === "confirm") {
        return "System Build confirmation failed. Check the details and try again.";
      }
      return "The Admin Server update failed. Check the details and try again.";
    default:
      break;
  }
  if (action && action.busy === true) {
    return action.progress_message || "System Build work is in progress…";
  }
  if (action && action.terminal_error) {
    return "The selected System Build needs attention before you can continue.";
  }
  if (result && result.alignment) {
    if (result.recovery_required) {
      return "System Build recovery is required before continuing.";
    }
    if (result.transition_in_progress && !result.next_allowed) {
      return "The Admin Server update is already in progress.";
    }
    if (result.alignment === "aligned") {
      if (result.resource_strategy === "release_archive") {
        return SYSTEM_BUILD_ALIGNMENT_TEXT.legacy_ready;
      }
      return SYSTEM_BUILD_ALIGNMENT_TEXT.aligned;
    }
    // An embedded-resource mismatch (admin_recreate_required) is surfaced with
    // the same standard "must be updated" message; the technical embedded-resource
    // detail belongs in diagnostics, not the primary status line.
    return SYSTEM_BUILD_ALIGNMENT_TEXT.update_required;
  }
  if (!selectedSystemBuildTag) {
    return "Select a System Build to continue.";
  }
  return "";
}

// One renderer owns both Step 1 actions so exactly one can be the active
// primary, never both.
function renderSystemBuildActions() {
  const els = setupSystemBuildEls;
  const aligning = systemBuildIsUpdating();
  const busy = aligning || systemBuildMutationInProgress();
  const failed = systemBuildState.status === SYSTEM_BUILD_STATUS.FAILED;
  const failedAction = systemBuildState.failedAction;

  if (els.align) {
    let label = "Update Admin Server";
    let enabled = !busy && !failed && systemBuildUpdateAllowed();
    if (aligning) {
      // Disabled, but doubles as the progress indicator for the running action.
      label =
        systemBuildState.status === SYSTEM_BUILD_STATUS.RECONNECTING
          ? "Reconnecting…"
          : "Updating Admin Server…";
      enabled = false;
    } else if (failed) {
      if (failedAction === "validate") {
        label = "Check again";
        enabled = true;
      } else if (failedAction === "confirm") {
        // A confirm failure retries on the right; the left never confirms.
        enabled = false;
      } else {
        label = "Try again";
        enabled = true;
      }
    }
    els.align.textContent = label;
    els.align.disabled = !enabled;
    els.align.setAttribute("aria-disabled", String(!enabled));
  }
  if (els.next) {
    let label = "Continue";
    let enabled = !busy && !failed && systemBuildNextAllowed();
    if (
      !busy &&
      !failed &&
      systemBuildState.status === SYSTEM_BUILD_STATUS.SELECTED
    ) {
      // The single explicit verification action. It is the only trigger for a
      // full download + identity check; selection alone never pulls.
      label = "Verify System Build";
      enabled = systemBuildVerifyAllowed();
    } else if (!aligning && failed && failedAction === "confirm") {
      label = "Try again";
      enabled = true;
    }
    els.next.textContent = label;
    els.next.disabled = !enabled;
    els.next.setAttribute("aria-disabled", String(!enabled));
  }
}

function applySystemBuildAlignment() {
  const els = setupSystemBuildEls;
  const result = systemBuildState.result;
  const action = systemBuildActionState();
  const updating =
    systemBuildIsUpdating() ||
    systemBuildMutationInProgress() ||
    Boolean(action && action.busy === true);

  const updateVisible = Boolean(action && action.admin_update_required);
  const failed = systemBuildState.status === SYSTEM_BUILD_STATUS.FAILED;
  if (els.recreateNotice) els.recreateNotice.hidden = !updateVisible || failed;

  // One renderer owns both alternative actions, so their enabled/label state can
  // never drift apart or leave both active at once.
  renderSystemBuildActions();

  if (setupEls.releaseSelect) {
    // Prevent a target change after a transition starts.
    setupEls.releaseSelect.disabled = updating
      ? true
      : !(setupState.release.releases && setupState.release.releases.length);
  }

  const terminalMessage =
    action && action.terminal_error && action.terminal_error.message;
  setSystemBuildError(systemBuildState.error || terminalMessage || "");
  if (els.status) els.status.textContent = systemBuildStatusMessage();
  notifySetupStatus();
}

// Changing the selected System Build after a previous choice retires the old
// Setup workflow as ONE backend operation: the server cancels its transition,
// removes its preview and artifacts, marks it superseded and returns the
// replacement workflow with a fresh one-shot intent. The browser never
// composes cancel + reset + new intent itself.
async function supersedeSetupBuild(nextTag, previousTag) {
  if (!nextTag || nextTag === previousTag) return false;
  if (!setupWorkflowId) return false;
  // Only an existing Setup-owned transition for a *different* build has to be
  // retired. Selecting a build before any transition exists, or re-selecting
  // the one the transition already targets, changes nothing on the server.
  const statusRes = await fetch("/api/admin/system-alignment/status", {
    cache: "no-store",
  });
  const status = await statusRes.json().catch(() => ({}));
  if (!statusRes.ok) {
    throw new Error(status.message || status.error || "transition status is unavailable");
  }
  const transition = status && status.transition;
  if (
    !transition ||
    transition.system_tag === nextTag ||
    transition.stage === "completed" ||
    transition.stage === "cancelled" ||
    !SETUP_TRANSITION_MODES.has(transition.mode)
  ) {
    return false;
  }
  const res = await fetch("/api/setup/system-build/supersede", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ setup_workflow_id: setupWorkflowId, tag: nextTag }),
  });
  const data = await res.json().catch(() => ({}));
  if (isSetupWorkflowConflict(data)) {
    handleSetupWorkflowConflict(data);
    throw new Error(data.message || SETUP_WORKFLOW_STALE_MESSAGE);
  }
  if (!res.ok || data.ok !== true || !data.setup_workflow_id) {
    throw new Error(
      data.message ||
        data.error ||
        "the previous System Build could not be superseded"
    );
  }
  clearSetupOperationContext();
  setupIntentId = data.setup_intent_id || null;
  freshSetupConfirmationRequired = false;
  setSetupWorkflowId(data.setup_workflow_id);
  loadSystemAlignmentStatus();
  return true;
}

async function validateSelectedSystemBuild(options = {}) {
  // Bind to the caller's captured tag/epoch when given, else read the current
  // selection and start a fresh epoch; never re-read the DOM after an await.
  const capturedTag = options.tag;
  const internal = options.internal === true;
  const domTag = setupEls.releaseSelect ? setupEls.releaseSelect.value : "";
  const tag = capturedTag != null ? capturedTag : domTag;
  // An explicit tag was already recorded by the caller; re-recording could move
  // the selection backwards under a slower concurrent lifecycle.
  if (capturedTag == null) selectedSystemBuildTag = tag || null;
  if (!tag) {
    systemBuildState.result = null;
    systemBuildState.error = null;
    if (!internal) systemBuildState.failedAction = null;
    systemBuildState.status = SYSTEM_BUILD_STATUS.IDLE;
    renderSystemAlignmentStatus({ active: false, selected_tag: null, status: null });
    applySystemBuildAlignment();
    return;
  }
  const generation =
    typeof options.generation === "number"
      ? options.generation
      : ++systemBuildState.validationGeneration;
  // A superseded reselection lifecycle stops here: it must neither clear a newer
  // selection's result nor paint "validation running" for its stale tag.
  if (
    generation !== systemBuildState.validationGeneration ||
    tag !== selectedSystemBuildTag
  ) {
    return;
  }
  systemBuildState.result = null;
  systemBuildState.error = null;
  // A top-level validation owns any failure it surfaces; an internal safety
  // revalidation leaves the outer mutation's failure ownership untouched.
  if (!internal) systemBuildState.failedAction = null;
  systemBuildState.lastAction = "validate";
  systemBuildState.status = SYSTEM_BUILD_STATUS.VALIDATING;
  renderSystemAlignmentStatus({
    active: true,
    selected_tag: tag,
    status: "validation_running",
  });
  applySystemBuildAlignment();
  try {
    const body = { tag };
    const res = await fetch("/api/admin/system-alignment/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    // Ignore a stale response: a newer validation, or a changed selection, wins.
    if (
      generation !== systemBuildState.validationGeneration ||
      tag !== selectedSystemBuildTag
    ) {
      return;
    }
    if (
      data &&
      data.action_state &&
      data.action_state.terminal_error
    ) {
      const terminal = data.action_state.terminal_error;
      const rateLimited =
        terminal.code === "system_build_registry_rate_limited";
      systemBuildState.result = data;
      // A registry rate-limit is a retryable throttle, not an invalid build:
      // keep the selection, surface the actionable message, and leave
      // Verify/Retry available while Continue/Update stay blocked.
      systemBuildState.status = rateLimited
        ? SYSTEM_BUILD_STATUS.FAILED
        : SYSTEM_BUILD_STATUS.INVALID;
      systemBuildState.error = terminal.message;
      if (rateLimited && !internal) systemBuildState.failedAction = "validate";
      renderSystemAlignmentStatus({
        active: true,
        selected_tag: tag,
        status: "validation_failed",
        message: systemBuildState.error,
      });
      applySystemBuildAlignment();
      return;
    }
    if (!res.ok || !data || data.valid === false) {
      const transient = res.status >= 500 || res.status === 0;
      systemBuildState.status = transient
        ? SYSTEM_BUILD_STATUS.FAILED
        : SYSTEM_BUILD_STATUS.INVALID;
      systemBuildState.error =
        (data && (data.message || data.error)) || "System Build validation failed.";
      if (!internal && transient) systemBuildState.failedAction = "validate";
      renderSystemAlignmentStatus({
        active: true,
        selected_tag: tag,
        status: "validation_failed",
        message: systemBuildState.error,
      });
      applySystemBuildAlignment();
      return;
    }
    if (!data.action_state || typeof data.action_state !== "object") {
      throw new Error(
        "The Admin Server returned no authoritative System Build action state. " +
          "Reload the Admin Server and try again."
      );
    }
    const selected = data.action_state.selected_build || {};
    if (selected.tag !== (data.system_build && data.system_build.canonical_tag)) {
      throw new Error("The System Build action state does not match its validation.");
    }
    systemBuildState.result = data;
    systemBuildState.status = SYSTEM_BUILD_STATUS.VALID;
    systemBuildState.failedAction = null;
    renderDevelopmentBuildChecks(data.checks || {}, data);
    renderSystemAlignmentStatus(data);
    if (
      data.action_state.busy === true &&
      data.action_state.transition_stage === "admin_aligned" &&
      data.action_state.operation_id
    ) {
      await resumeSelectedSystemBuildResources(
        data.action_state.operation_id,
        tag,
        generation
      );
      return;
    }
  } catch (err) {
    if (generation !== systemBuildState.validationGeneration) return;
    systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
    systemBuildState.error = err.message || String(err);
    if (!internal) systemBuildState.failedAction = "validate";
    renderSystemAlignmentStatus({
      active: true,
      selected_tag: tag,
      status: "validation_failed",
      message: systemBuildState.error,
    });
  }
  applySystemBuildAlignment();
}

let selectedSystemBuildResumeInFlight = null;

async function resumeSelectedSystemBuildResources(operationId, tag, generation) {
  if (selectedSystemBuildResumeInFlight) {
    await selectedSystemBuildResumeInFlight;
    return;
  }
  selectedSystemBuildResumeInFlight = (async () => {
    systemBuildMutationLocked = true;
    applySystemBuildAlignment();
    try {
      const res = await fetch("/api/admin/system-alignment/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation_id: operationId, tag }),
      });
      const data = await res.json().catch(() => ({}));
      if (
        generation !== systemBuildState.validationGeneration ||
        tag !== selectedSystemBuildTag
      ) {
        return;
      }
      if (!res.ok) {
        if (data.error === "resource_verification_in_progress") {
          scheduleSystemAlignmentPoll(true);
          return;
        }
        throw new Error(
          data.message || data.error || "System Build resource recovery failed."
        );
      }
      renderSystemAlignmentStatus(data);
      systemBuildMutationLocked = false;
      await validateSelectedSystemBuild({ internal: true });
    } catch (err) {
      if (
        generation !== systemBuildState.validationGeneration ||
        tag !== selectedSystemBuildTag
      ) {
        return;
      }
      systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
      systemBuildState.failedAction = "confirm";
      systemBuildState.error = err.message || String(err);
    } finally {
      systemBuildMutationLocked = false;
      applySystemBuildAlignment();
    }
  })();
  try {
    await selectedSystemBuildResumeInFlight;
  } finally {
    selectedSystemBuildResumeInFlight = null;
  }
}

async function confirmSelectedSystemBuild() {
  if (!selectedSystemBuildTag || !systemBuildNextAllowed()) return;
  if (systemBuildMutationInProgress()) return;
  const tag = selectedSystemBuildTag;
  const result = systemBuildState.result || {};
  if (
    result.resources_verified === true &&
    result.confirmation_allowed === false &&
    result.operation_id
  ) {
    renderSystemAlignmentStatus(result);
    bindConfirmedSetupOperation(result.operation_id, tag);
    setupState.release.version = tag;
    setupState.release.current = tag;
    await loadActiveConfigTemplate(tag);
    setReleaseStatus("ready");
    setActiveStep("devices");
    return;
  }
  if (result.confirmation_allowed !== true) return;
  systemBuildMutationLocked = true;
  systemBuildState.status = SYSTEM_BUILD_STATUS.CONFIRMING;
  systemBuildState.lastAction = "confirm";
  systemBuildState.failedAction = null;
  systemBuildState.error = null;
  applySystemBuildAlignment();
  try {
    const res = await fetch("/api/setup/system-build/confirm", {
      method: "POST",
      headers: setupIntentHeaders({ "Content-Type": "application/json" }),
      // The exact server-issued workflow identity, never a locally chosen one:
      // this request creates the transition that workflow will own.
      body: JSON.stringify({ tag, setup_workflow_id: setupWorkflowId }),
    });
    const data = await res.json().catch(() => ({}));
    if (handleSystemBuildWorkflowConflict(data)) return;
    if (handleSetupIntentRejection(data)) return;
    if (tag !== selectedSystemBuildTag) {
      throw new Error("The selected System Build changed during confirmation.");
    }
    if (!res.ok || !data || data.resources_verified !== true) {
      if (data && data.action_state && data.action_state.terminal_error) {
        systemBuildState.result = data;
        const terminal = new Error(data.action_state.terminal_error.message);
        terminal.systemBuildTerminal = true;
        throw terminal;
      }
      const activeTag = data && data.transition && data.transition.system_tag;
      if (activeTag && activeTag !== tag) {
        selectedSystemBuildTag = activeTag;
        if (setupEls.releaseSelect) setupEls.releaseSelect.value = activeTag;
      }
      throw new Error(
        (data && (data.message || data.error)) || "System Build confirmation failed."
      );
    }
    const confirmedTag =
      (data.system_build && data.system_build.canonical_tag) || data.system_tag || tag;
    if (confirmedTag !== tag || !data.operation_id) {
      throw new Error("The server confirmed a different System Build context.");
    }
    // Render the successful durable resources_verified transition before any
    // template load or navigation can move the wizard to Device Discovery.
    renderSystemAlignmentStatus(data);
    // The confirm consumed the one-shot intent server-side; drop it so a later
    // step never resends a spent id.
    setupIntentId = null;
    bindConfirmedSetupOperation(data.operation_id, confirmedTag);
    setupState.release.version = confirmedTag;
    setupState.release.current = confirmedTag;
    setupState.release.resources = data.resources || {};
    const release = setupState.release.releases.find((item) => item.tag === confirmedTag);
    if (release) {
      release.prepared = true;
      renderReleaseBadges(release);
    }
    await loadActiveConfigTemplate(confirmedTag);
    setReleaseStatus("ready");
    renderReleaseResources();
    systemBuildState.result = { ...systemBuildState.result, ...data };
    systemBuildState.status = SYSTEM_BUILD_STATUS.VALID;
    systemBuildState.failedAction = null;
    systemBuildMutationLocked = false;
    applySystemBuildAlignment();
    setActiveStep("devices");
  } catch (err) {
    systemBuildMutationLocked = false;
    systemBuildState.status = err.systemBuildTerminal
      ? SYSTEM_BUILD_STATUS.INVALID
      : SYSTEM_BUILD_STATUS.FAILED;
    systemBuildState.error = err.message || String(err);
    systemBuildState.failedAction = err.systemBuildTerminal ? null : "confirm";
    applySystemBuildAlignment();
    setActiveStep("release");
  }
}

async function restoreSelectedSystemBuild() {
  // After the Admin reconnects the user must land back in Step 1 with the very
  // build they selected, and Next only opens once alignment is re-confirmed.
  systemBuildState.status = SYSTEM_BUILD_STATUS.IDLE;
  if (setupEls.releaseSelect) {
    setupEls.releaseSelect.disabled = false;
    if (selectedSystemBuildTag && setupEls.releaseSelect.value !== selectedSystemBuildTag) {
      setupEls.releaseSelect.value = selectedSystemBuildTag;
    }
  }
  await validateSelectedSystemBuild();
}

function setupTransitionIsActive(transition) {
  // A non-terminal Fresh/Automated Setup transition means Guided Setup is still
  // mid-flight and Step 1 must be resumed on its target build.
  return Boolean(
    transition &&
      transition.system_tag &&
      (transition.mode === "fresh_install" ||
        transition.mode === "automated_setup") &&
      transition.stage !== "completed" &&
      transition.stage !== "cancelled"
  );
}

async function resumeGuidedSetupFromTransition(alignment) {
  // The server transition — not lost in-memory JS state — is the source of truth.
  // After a reconnect, a page reload, or a login that followed an Admin restart,
  // reopen Guided Setup Step 1 on the very build the transition targets and
  // re-validate; Next opens only once alignment and resources are green again.
  // Discovery is never auto-started from here.
  const transition = (alignment && alignment.transition) || {};
  if (!setupTransitionIsActive(transition)) return false;
  selectedSystemBuildTag = transition.system_tag;
  // Rebuild the operation context only from the server transition, and only once
  // it has confirmed resources — never from a prepared cache or stored state.
  const resourcesConfirmed =
    Boolean(transition.operation_id) &&
    [
      "resources_verified",
      "ems_operation_pending",
      "ems_operation_running",
      "healthcheck_pending",
      "completed",
    ].includes(transition.stage);
  if (resourcesConfirmed) {
    bindConfirmedSetupOperation(transition.operation_id, transition.system_tag);
  } else {
    clearSetupOperationContext();
  }
  revealWorkspace();
  window.location.hash = "setup";
  setAdminView("setup");
  if (
    transition.stage === "admin_reconnect_pending" ||
    transition.stage === "admin_aligned"
  ) {
    await resumeSystemAlignment();
  }
  if (setupInitialized) {
    if (setupEls.releaseSelect) setupEls.releaseSelect.value = selectedSystemBuildTag;
    setActiveStep("release");
    await validateSelectedSystemBuild();
  } else {
    // First entry: loadReleases honors the restored tag and re-verifies it (a
    // resume must restore the verified state, not show a fresh preview).
    systemBuildResumeValidationTag = selectedSystemBuildTag;
    initSetupWizard();
    setActiveStep("release");
  }
  return true;
}

async function updateAdminForSystemBuild() {
  // Only the explicit Admin-update button reaches here, and only one at a time.
  if (!selectedSystemBuildTag) return;
  if (systemBuildIsUpdating()) return;
  if (systemBuildMutationInProgress()) return;
  const tag = selectedSystemBuildTag;
  const previousAdminInstanceId = authState.adminInstanceId;
  systemBuildMutationLocked = true;
  systemBuildState.lastAction = "update";
  systemBuildState.failedAction = null;
  systemBuildState.error = null;
  // Revalidate the selected build (internally, so it never claims failure
  // ownership) and confirm the captured tag is still selected before mutating —
  // a selection change during update-start must never mutate the wrong build.
  await validateSelectedSystemBuild({ internal: true });
  if (tag !== selectedSystemBuildTag || !systemBuildUpdateAllowed()) {
    systemBuildMutationLocked = false;
    if (systemBuildState.status === SYSTEM_BUILD_STATUS.FAILED) {
      systemBuildState.failedAction = "align";
    }
    applySystemBuildAlignment();
    return;
  }
  systemBuildState.status = SYSTEM_BUILD_STATUS.UPDATING;
  applySystemBuildAlignment();
  try {
    const body = { tag, setup_workflow_id: setupWorkflowId };
    const res = await fetch("/api/setup/system-build/update-admin", {
      method: "POST",
      headers: setupIntentHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (handleSystemBuildWorkflowConflict(data)) return;
    if (handleSetupIntentRejection(data)) return;
    if (!res.ok && res.status !== 202) {
      throw new Error((data && (data.message || data.error)) || "Admin update failed.");
    }
    // The update-admin start consumed the one-shot intent; drop it before the
    // reconnect so a resumed flow never resends a spent id.
    setupIntentId = null;
    renderSystemAlignmentStatus(data);
    if (data.reconnect || data.status === "admin_alignment_started") {
      const operationId = reconnectOperationIdFromStart(data);
      if (!operationId) {
        // Never poll for a reconnect without a concrete operation identity.
        throw new Error(
          "The Admin Server update started without an operation id; cannot reconnect safely."
        );
      }
      // Bind reconnect polling to the transition started by this request.
      setupOperationId = operationId;
      systemBuildState.status = SYSTEM_BUILD_STATUS.RECONNECTING;
      applySystemBuildAlignment();
      showReconnectOverlay(data.message || "Updating Admin Server…");
      await waitForAdminReconnect(previousAdminInstanceId, operationId);
      systemBuildMutationLocked = false;
      applySystemBuildAlignment();
      return;
    }
    await validateSelectedSystemBuild({ internal: true });
    systemBuildMutationLocked = false;
    applySystemBuildAlignment();
  } catch (err) {
    systemBuildMutationLocked = false;
    systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
    systemBuildState.error = err.message || String(err);
    systemBuildState.failedAction = "align";
    applySystemBuildAlignment();
  }
}

// The left action is normally "Update Admin Server". After a failure it retries
// only the action it owns — validation or the Admin update — on a fresh click.
// It never confirms.
async function handleAlignAdminClick() {
  if (systemBuildState.status === SYSTEM_BUILD_STATUS.FAILED) {
    if (systemBuildState.failedAction === "validate") {
      await validateSelectedSystemBuild();
    } else if (systemBuildState.failedAction === "confirm") {
      return;
    } else {
      await updateAdminForSystemBuild();
    }
    return;
  }
  await updateAdminForSystemBuild();
}

// The right action is normally "Continue". After a confirm failure it retries
// only the confirmation on a fresh click. It never runs an Admin update.
async function handleContinueClick() {
  // A selected-but-unverified build: the primary is "Verify System Build", and
  // this explicit click is the only trigger for a full download + verification.
  if (systemBuildState.status === SYSTEM_BUILD_STATUS.SELECTED) {
    await validateSelectedSystemBuild();
    return;
  }
  if (
    systemBuildState.status === SYSTEM_BUILD_STATUS.FAILED &&
    systemBuildState.failedAction === "confirm" &&
    systemBuildState.result
  ) {
    systemBuildState.status = SYSTEM_BUILD_STATUS.VALID;
    systemBuildState.failedAction = null;
    applySystemBuildAlignment();
    await confirmSelectedSystemBuild();
    return;
  }
  await confirmSelectedSystemBuild();
}

function wireSetupSystemBuildActions() {
  if (setupSystemBuildEls.align) {
    setupSystemBuildEls.align.addEventListener("click", handleAlignAdminClick);
  }
  if (setupSystemBuildEls.next) {
    setupSystemBuildEls.next.addEventListener("click", handleContinueClick);
  }
}
wireSetupSystemBuildActions();

function isImmutableDevelopmentBuildTag(tag) {
  const value = typeof tag === "string" ? tag.trim() : "";
  return (
    value.length <= 128 &&
    /^dev-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?-[0-9a-f]{7,40}-[1-9][0-9]*-[1-9][0-9]*$/.test(
      value
    )
  );
}

function renderDevelopmentBuildChecks(checks, data) {
  // A legacy release uses the release-archive strategy, so an embedded-resource
  // match is not applicable — render it as a neutral, informational row rather
  // than a failed check. All strings here are static (no interpolation).
  const embeddedNotApplicable = Boolean(
    data && data.embedded_resources_applicable === false
  );
  document.querySelectorAll("[data-system-build-check]").forEach((row) => {
    const key = row.dataset.systemBuildCheck;
    const icon = row.querySelector(".config-validation-icon");
    const label = row.querySelector("span:last-child");
    if (key === "embedded_resources_match") {
      if (embeddedNotApplicable) {
        row.classList.remove("config-validation-item-error");
        if (label) label.textContent = "Resource source: verified release archive";
        if (icon) icon.textContent = "–";
        return;
      }
      if (label) label.textContent = "Embedded resources match";
    }
    const passed = checks && checks[key] === true;
    row.classList.toggle("config-validation-item-error", checks && !passed);
    if (icon) icon.textContent = passed ? "✓" : checks ? "×" : "○";
  });
}

function resolveSystemAlignmentStage(data) {
  const payload = data && typeof data === "object" ? data : {};
  const transition =
    payload.transition && typeof payload.transition === "object"
      ? payload.transition
      : {};
  // The nested transition is the persisted source of truth.  Flat transition
  // stages are compatibility fallbacks; generic endpoint statuses come last.
  return (
    transition.stage ||
    payload.transition_stage ||
    payload.stage ||
    payload.status ||
    null
  );
}

function systemAlignmentAdminRequired(payload, transition, stage) {
  if (typeof transition.admin_alignment_required === "boolean") {
    return transition.admin_alignment_required;
  }
  if (typeof payload.admin_alignment_required === "boolean") {
    return payload.admin_alignment_required;
  }
  if (typeof payload.admin_update_required === "boolean") {
    return payload.admin_update_required;
  }
  if (payload.alignment === "aligned") return false;
  if (
    stage === "admin_update_pending" ||
    stage === "admin_alignment_started" ||
    stage === "admin_reconnect_pending"
  ) {
    return true;
  }
  return null;
}

function systemAlignmentStageStates(data) {
  const payload = data && typeof data === "object" ? data : {};
  const transition =
    payload.transition && typeof payload.transition === "object"
      ? payload.transition
      : {};
  const stage = resolveSystemAlignmentStage(payload);
  const failed = stage === "failed_recoverable";
  const effectiveStage = failed ? transition.resume_stage || stage : stage;
  const adminRequired = systemAlignmentAdminRequired(
    payload,
    transition,
    effectiveStage
  );
  const states = Array(SYSTEM_ALIGNMENT_STAGE_ORDER.length).fill("pending");
  const completeSelectionAndValidation = () => {
    states[0] = "done";
    states[1] = "done";
  };
  const completeOrSkipAdmin = () => {
    states[2] = adminRequired === false ? "skipped" : "done";
    states[3] = adminRequired === false ? "skipped" : "done";
  };

  switch (effectiveStage) {
    case "selection_started":
      states[0] = "active";
      break;
    case "validation_running":
      states[0] = "done";
      states[1] = "active";
      break;
    case "validation_failed":
      states[0] = "done";
      states[1] = "failed";
      break;
    case "validated":
      completeSelectionAndValidation();
      if (adminRequired === false) {
        states[2] = "skipped";
        states[3] = "skipped";
        states[4] = "active";
      } else {
        states[2] = "active";
      }
      break;
    case "admin_update_pending":
    case "admin_alignment_started":
      completeSelectionAndValidation();
      states[2] = "active";
      break;
    case "admin_reconnect_pending":
      completeSelectionAndValidation();
      states[2] = "done";
      states[3] = "active";
      break;
    case "admin_aligned":
      completeSelectionAndValidation();
      completeOrSkipAdmin();
      states[4] = "active";
      break;
    case "resources_verified":
    case "ems_operation_pending":
    case "ems_operation_running":
      completeSelectionAndValidation();
      completeOrSkipAdmin();
      states[4] = "done";
      states[5] = "active";
      break;
    case "healthcheck_pending":
      completeSelectionAndValidation();
      completeOrSkipAdmin();
      states[4] = "done";
      states[5] = "done";
      states[6] = "active";
      break;
    case "completed":
      completeSelectionAndValidation();
      completeOrSkipAdmin();
      states[4] = "done";
      states[5] = "done";
      states[6] = "done";
      break;
    default: {
      const currentIndex = SYSTEM_ALIGNMENT_STAGE_INDEX[effectiveStage];
      if (typeof currentIndex === "number") {
        for (let index = 0; index < states.length; index += 1) {
          states[index] =
            index < currentIndex
              ? "done"
              : index === currentIndex
                ? "active"
                : "pending";
        }
      }
      break;
    }
  }
  if (failed) {
    const activeIndex = states.indexOf("active");
    if (activeIndex >= 0) states[activeIndex] = "failed";
  }
  return { stage, adminRequired, states };
}

function systemAlignmentShouldPoll(data) {
  const payload = data && typeof data === "object" ? data : {};
  const transition =
    payload.transition && typeof payload.transition === "object"
      ? payload.transition
      : {};
  const stage = resolveSystemAlignmentStage(payload);
  if (SYSTEM_ALIGNMENT_TERMINAL_STAGES.has(stage)) return false;
  if (payload.active === false && !payload.transition_in_progress) return false;
  return Boolean(
    payload.active === true ||
      payload.transition_in_progress === true ||
      (SYSTEM_ALIGNMENT_TRANSITION_STAGES.has(stage) &&
        (transition.operation_id || payload.operation_id))
  );
}

// --- System Build task ownership -------------------------------------------
// The seven-stage pipeline is a task subworkflow, not an application-global
// card. The authoritative backend transition mode decides which task owns it; a
// synthetic local validation preview (no persisted transition yet) falls back to
// the task the operator currently has open. The single shared node is moved into
// that task's mount slot and is otherwise parked hidden, so it can never render
// above Login or Task Selection.

const SYSTEM_BUILD_SLOT_IDS = {
  setup: "setup-system-build-slot",
  guided_upgrade: "upgrade-system-build-slot",
};

// Map the authoritative transition mode to its owning task. Fresh/Automated
// Setup belong to Guided Setup; Guided Upgrade owns its own transition. Any
// other or missing mode (including the align-existing rollback) has no owner.
function systemBuildOwner(transition) {
  const mode = transition && transition.mode;
  if (mode === "fresh_install" || mode === "automated_setup") return "setup";
  if (mode === "guided_upgrade") return "guided_upgrade";
  return null;
}

// Which task view is currently open, read from the DOM (not the URL hash), so a
// stale response can only make the pipeline visible inside the task the operator
// is actually looking at.
function currentActiveTask() {
  const setup = document.getElementById("view-setup");
  if (setup && !setup.hidden) return "setup";
  // The upgrade sub-panel's own hidden flag is only reset by setMaintenancePath,
  // so it can stay stale after leaving Maintenance by another route; require the
  // maintenance view ancestor to be visible too.
  const maintenance = document.getElementById("view-maintenance");
  const upgrade = document.getElementById("maintenance-upgrade-panel");
  if (maintenance && !maintenance.hidden && upgrade && !upgrade.hidden) {
    return "guided_upgrade";
  }
  return null;
}

// Move the single shared workflow node into the owning task's slot, or park it
// in the hidden neutral container. The node — with its ids and bound handlers —
// is moved, never copied, so there is exactly one renderer and one control set.
function mountSystemBuildWorkflow(owner) {
  const workflow = systemAlignmentEls.workflow;
  if (!workflow) return;
  const slotId = owner ? SYSTEM_BUILD_SLOT_IDS[owner] : null;
  const target =
    (slotId && document.getElementById(slotId)) ||
    document.getElementById("system-build-parking");
  if (target && workflow.parentElement !== target) {
    target.appendChild(workflow);
  }
}

// The single presentation decision. `owner` is the mount target (null → park in
// the hidden container); `visible` is whether the full pipeline may show inside
// its slot; `poll` is whether task-local status polling may run. The slot's
// ancestor handles view-based hiding, so this only decides in-task visibility
// (authenticated + real progress + not cancelled) and same-task polling.
function systemBuildPresentation({ authenticated, state, activeTask }) {
  const payload = state && typeof state === "object" ? state : {};
  const transition =
    payload.transition && typeof payload.transition === "object"
      ? payload.transition
      : {};
  const hasTransition = Boolean(
    payload.transition && typeof payload.transition === "object"
  );
  const stage = resolveSystemAlignmentStage(payload);
  const terminal = SYSTEM_ALIGNMENT_TERMINAL_STAGES.has(stage);
  const cancelled = stage === "cancelled";
  // Authoritative backend mode owns the pipeline. Only a synthetic local
  // validation preview — one with NO persisted transition — falls back to the
  // open task; a real transition whose mode has no owner (e.g. the
  // align-existing rollback) yields no owner and is parked, never shown in
  // whatever task happens to be open.
  const effectiveOwner =
    systemBuildOwner(transition) ||
    (authenticated && !hasTransition ? activeTask : null);
  const canonicalTag =
    transition.system_tag ||
    payload.system_tag ||
    payload.canonical_tag ||
    payload.selected_tag ||
    null;
  const hasProgress = Boolean(payload.active || canonicalTag || stage);
  const owner =
    authenticated && effectiveOwner && hasProgress ? effectiveOwner : null;
  const visible = Boolean(owner && !cancelled);
  const poll = Boolean(
    authenticated &&
      effectiveOwner &&
      activeTask === effectiveOwner &&
      systemAlignmentShouldPoll(payload)
  );
  return { owner, visible, poll, terminal };
}

// Re-scope the workflow to the current authoritative state: mount it into the
// owning task (or park it), set its in-task visibility, and (re)arm or stop the
// single poll timer. Idempotent — safe to call from every render and from the
// auth/navigation lifecycle.
function applySystemBuildPresentation() {
  const presentation = systemBuildPresentation({
    authenticated: isAuthenticated(),
    state: systemAlignmentState,
    activeTask: currentActiveTask(),
  });
  mountSystemBuildWorkflow(presentation.owner);
  if (systemAlignmentEls.workflow) {
    systemAlignmentEls.workflow.hidden = !presentation.visible;
  }
  scheduleSystemAlignmentPoll(presentation.poll);
}

// A synthetic validation preview (no persisted backend transition) belongs to
// the task that produced it; drop it on task navigation so it can never follow
// the operator into another task. A real mode-owned transition is durable and
// survives — it is re-scoped by applySystemBuildPresentation.
function rescopeSystemBuildForNavigation() {
  const state = systemAlignmentState;
  const hasTransition = Boolean(
    state && state.transition && typeof state.transition === "object"
  );
  if (state && !hasTransition) systemAlignmentState = null;
  applySystemBuildPresentation();
}

// Fully drop the pipeline when entering any unauthenticated state: stop polling,
// forget the cached transition and selection, reset every visible build fact to
// its neutral placeholder, and detach the node to the hidden parking container.
// No release tag, revision, image or operation id survives into the login gate.
function clearSystemBuildProgress() {
  stopSystemAlignmentPolling();
  systemAlignmentState = null;
  selectedSystemBuildTag = null;
  const els = systemAlignmentEls;
  if (els.workflow) els.workflow.hidden = true;
  if (els.tag) els.tag.textContent = "Not selected";
  if (els.buildId) els.buildId.textContent = "Unknown";
  if (els.revision) els.revision.textContent = "Unknown";
  if (els.adminImage) els.adminImage.textContent = "Unknown";
  if (els.emsImage) els.emsImage.textContent = "Unknown";
  if (els.message) els.message.textContent = "";
  if (els.warning) {
    els.warning.textContent = "";
    els.warning.hidden = true;
  }
  if (els.reconnect) els.reconnect.hidden = true;
  if (els.partial) els.partial.hidden = true;
  if (els.resume) els.resume.disabled = true;
  if (els.returnToRunning) els.returnToRunning.disabled = true;
  document.querySelectorAll("[data-system-alignment-stage]").forEach((row) => {
    row.dataset.state = "pending";
    const label = row.querySelector("[data-system-alignment-state-label]");
    if (label) label.textContent = "";
  });
  mountSystemBuildWorkflow(null);
}

function resetSystemAlignmentPresentation(selectedTag, stage, message = "") {
  renderSystemAlignmentStatus({
    active: Boolean(selectedTag && stage),
    selected_tag: selectedTag,
    status: stage,
    message,
  });
}

function renderSystemAlignmentStatus(data) {
  const payload = data && typeof data === "object" ? data : {};
  const transition =
    payload.transition && typeof payload.transition === "object"
      ? payload.transition
      : {};
  const build = transition.system_build || payload.system_build || {};
  const progress = systemAlignmentStageStates(payload);
  const stage = progress.stage;
  const canonicalTag =
    transition.system_tag ||
    build.canonical_tag ||
    payload.system_tag ||
    payload.canonical_tag ||
    payload.selected_tag ||
    null;
  const buildId = transition.build_id || build.build_id || payload.build_id || null;
  const revision = transition.revision || build.revision || payload.revision || null;
  const adminImage =
    transition.admin_image || build.admin_image || payload.admin_image || null;
  const emsImage = transition.ems_image || build.ems_image || payload.ems_image || null;
  const errorMessage =
    transition.error_message || payload.error_message || payload.message || "";
  const warning = transition.warning || payload.warning || "";

  systemAlignmentState = payload;
  applyUpgradeAlignmentTransition();
  if (systemAlignmentEls.tag) {
    systemAlignmentEls.tag.textContent = canonicalTag || "Not selected";
  }
  if (systemAlignmentEls.buildId) {
    systemAlignmentEls.buildId.textContent = buildId || "Unknown";
  }
  if (systemAlignmentEls.revision) {
    systemAlignmentEls.revision.textContent = revision || "Unknown";
  }
  if (systemAlignmentEls.adminImage) {
    systemAlignmentEls.adminImage.textContent = adminImage || "Unknown";
  }
  if (systemAlignmentEls.emsImage) {
    systemAlignmentEls.emsImage.textContent = emsImage || "Unknown";
  }
  if (systemAlignmentEls.message) {
    const stageMessage = {
      selection_started: "Selecting the System Build…",
      validation_running: "Verifying the Admin and EMS images…",
      validation_failed: "System Build verification failed.",
      validated: "System Build verified. Preparing to align…",
      admin_update_pending: "Updating the Admin Console…",
      admin_alignment_started: "Updating the Admin Console…",
      admin_reconnect_pending:
        "Restarting the Admin Console — this page reconnects automatically…",
      admin_aligned: "Admin Console aligned. Verifying target resources…",
      resources_verified: "Target resources verified. Preparing the EMS update…",
      ems_operation_pending: "Preparing the EMS update…",
      ems_operation_running:
        "Installing EMS — this can take a few minutes while the image downloads…",
      healthcheck_pending: "Verifying the running system…",
      completed: "System Build complete.",
      cancelled: "The System Build transition was cancelled.",
      failed_recoverable: "The System Build transition needs recovery.",
    };
    systemAlignmentEls.message.textContent =
      errorMessage ||
      (stage ? stageMessage[stage] || stage.replaceAll("_", " ") : "");
  }
  if (systemAlignmentEls.warning) {
    systemAlignmentEls.warning.textContent = warning;
    systemAlignmentEls.warning.hidden = !warning;
  }

  document.querySelectorAll("[data-system-alignment-stage]").forEach((row) => {
    const index = SYSTEM_ALIGNMENT_STAGE_ORDER.indexOf(
      row.dataset.systemAlignmentStage
    );
    const state = progress.states[index] || "pending";
    row.dataset.state = state;
    const label = row.querySelector("[data-system-alignment-state-label]");
    if (label) {
      label.textContent =
        state === "active"
          ? "Working…"
          : state === "failed"
          ? "Failed"
          : state === "skipped"
          ? index === 2 || index === 3
            ? "Not required"
            : "Skipped"
          : "";
    }
  });

  const failed = stage === "failed_recoverable";
  const expired = transition.expired === true;
  const recoveryAvailable = failed || expired;
  const reconnecting =
    !expired &&
    (stage === "admin_update_pending" ||
      stage === "admin_reconnect_pending" ||
      stage === "admin_alignment_started");
  // Expiry or failure does not prove the operation's worker stopped, and a
  // worker state that could not be verified is not "stopped" either.
  const workerActive = transition.worker_active === true;
  const workerStatusUnknown = transition.worker_status_available === false;
  if (systemAlignmentEls.reconnect) systemAlignmentEls.reconnect.hidden = !reconnecting;
  if (systemAlignmentEls.partial) systemAlignmentEls.partial.hidden = !recoveryAvailable;
  if (systemAlignmentEls.resume) systemAlignmentEls.resume.hidden = false;
  // Returning to the running build is not offered for a Guided Setup transition:
  // the server refuses it because the new align-existing operation would have no
  // durable owner. Setup recovery is Resume or Discard setup.
  const setupOwned = SETUP_TRANSITION_MODES.has(transition.mode);
  if (systemAlignmentEls.returnToRunning) {
    systemAlignmentEls.returnToRunning.hidden = setupOwned;
  }
  if (systemAlignmentEls.partialMessage) {
    systemAlignmentEls.partialMessage.textContent = workerStatusUnknown
      ? "The System Build worker state could not be verified. Abandon is " +
        "temporarily unavailable."
      : workerActive
        ? expired
          ? "The System Build transition has expired, but its operation is still " +
            "running. Wait for it to finish before abandoning the transition."
          : "The System Build operation is still running. Wait for it to finish " +
            "before abandoning the transition."
        : errorMessage ||
          (expired
            ? "The System Build transition has expired. Abandon it to start a new one."
            : setupOwned
              ? "Admin is aligned, but EMS has not completed the matching build " +
                "transition. Retry it, or discard this setup to remove its " +
                "temporary files."
              : "Admin is aligned, but EMS has not completed the matching build transition.");
  }
  if (systemAlignmentEls.resume) {
    systemAlignmentEls.resume.disabled =
      !transition.operation_id || transition.resume_available !== true;
  }
  if (systemAlignmentEls.returnToRunning) {
    systemAlignmentEls.returnToRunning.disabled =
      setupOwned ||
      !transition.operation_id ||
      transition.return_available !== true;
  }
  if (systemAlignmentEls.abandon) {
    // The escape hatch out of a wedged transition, only with the worker
    // proven inactive: an active, unknown or absent worker verdict keeps it
    // closed so a stale worker cannot keep mutating past a new operation. An
    // unknown owner offers no destructive action at all.
    const recovery = recoveryActionFor(transition.mode);
    systemAlignmentEls.abandon.hidden = !recovery;
    if (recovery) systemAlignmentEls.abandon.textContent = recovery.label;
    systemAlignmentEls.abandon.disabled =
      !recovery ||
      !transition.operation_id ||
      transition.cancel_available !== true ||
      transition.worker_active !== false ||
      transition.worker_status_available !== true;
  }
  // A pending Setup cleanup is durable state, not a transition observation: it
  // re-asserts itself after every poll so the poller cannot erase the recovery.
  renderSetupCleanupRecovery();
  // One authority owns visibility, task placement and polling for every render.
  applySystemBuildPresentation();
}

function stopSystemAlignmentPolling() {
  // Invalidate any in-flight status response bound to the previous poll cycle.
  systemAlignmentPollGeneration += 1;
  if (systemAlignmentPollTimer !== null) {
    window.clearTimeout(systemAlignmentPollTimer);
    systemAlignmentPollTimer = null;
  }
}

function scheduleSystemAlignmentPoll(active) {
  if (typeof stopSystemAlignmentPolling === "function") {
    stopSystemAlignmentPolling();
  }
  if (active && isAuthenticated()) {
    systemAlignmentPollTimer = window.setTimeout(
      loadSystemAlignmentStatus,
      SYSTEM_ALIGNMENT_POLL_INTERVAL_MS
    );
  }
}

async function loadSystemAlignmentStatus() {
  if (!isAuthenticated()) return null;
  const pollGeneration = systemAlignmentPollGeneration;
  try {
    const res = await fetch("/api/admin/system-alignment/status", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || "Status unavailable.");
    // Drop a stale status response: polling was stopped or rescheduled (task,
    // owner or selection change, auth loss) while this request was in flight.
    if (pollGeneration !== systemAlignmentPollGeneration) return null;
    renderSystemAlignmentStatus(data);
    return data;
  } catch (_) {
    if (pollGeneration !== systemAlignmentPollGeneration) return null;
    const action = systemBuildActionState();
    if (
      action &&
      action.busy === true &&
      !systemBuildIsUpdating() &&
      !systemBuildMutationInProgress()
    ) {
      stopSystemAlignmentPolling();
      systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;
      systemBuildState.failedAction = "validate";
      systemBuildState.error =
        "The Admin Server could not check System Build progress. " +
        "Check the connection and try again.";
      applySystemBuildAlignment();
      return null;
    }
    // A connection loss is expected while the Admin process itself is being
    // replaced; reconnect polling owns that visible progress state.
    scheduleSystemAlignmentPoll(systemAlignmentShouldPoll(systemAlignmentState));
    return null;
  }
}

async function resumeSystemAlignment() {
  const transition = (systemAlignmentState && systemAlignmentState.transition) || {};
  if (!transition.operation_id) return;
  const previousAdminInstanceId = authState.adminInstanceId;
  try {
    // Resume/reconnect/retry carries only the operation id and tag. The server
    // authorizes it from the transition's own stored, tag-bound acknowledgement
    // — a fresh browser acknowledgement is never trusted during recovery.
    const body = {
      operation_id: transition.operation_id,
      tag: transition.system_tag,
    };
    const res = await fetch("/api/admin/system-alignment/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || "Resume failed.");
    // Render the reconnect/alignment mutation before starting the next durable
    // resource-verification mutation.
    renderSystemAlignmentStatus(data);
    if (resolveSystemAlignmentStage(data) === "admin_aligned") {
      const verifyRes = await fetch("/api/admin/system-alignment/verify-resources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation_id: transition.operation_id }),
      });
      data = await verifyRes.json();
      if (!verifyRes.ok) {
        throw new Error(data.message || data.error || "Resource verification failed.");
      }
      renderSystemAlignmentStatus(data);
    }
    if (data.reconnect || data.status === "admin_alignment_started") {
      showReconnectOverlay(data.message);
      waitForAdminReconnect(previousAdminInstanceId, transition.operation_id);
    } else {
      loadSystemAlignmentStatus();
    }
  } catch (err) {
    if (systemAlignmentEls.warning) {
      systemAlignmentEls.warning.textContent = err.message || String(err);
      systemAlignmentEls.warning.hidden = false;
    }
  }
}

async function returnToRunningSystemBuild() {
  const transition = (systemAlignmentState && systemAlignmentState.transition) || {};
  if (!transition.operation_id) return;
  if (SETUP_TRANSITION_MODES.has(transition.mode)) return;
  const previousAdminInstanceId = authState.adminInstanceId;
  if (!window.confirm("Return the Admin Console to the last known-good running EMS build?")) {
    return;
  }
  try {
    const res = await fetch("/api/admin/system-alignment/return-to-running-build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation_id: transition.operation_id, confirm: true }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.error || "Return failed.");
    renderSystemAlignmentStatus(data);
    if (data.reconnect !== false) {
      showReconnectOverlay(data.message || "Returning to the running System Build…");
      waitForAdminReconnect(previousAdminInstanceId, transition.operation_id);
    }
  } catch (err) {
    if (systemAlignmentEls.warning) {
      systemAlignmentEls.warning.textContent = err.message || String(err);
      systemAlignmentEls.warning.hidden = false;
    }
  }
}

async function abandonSystemAlignment() {
  const transition = (systemAlignmentState && systemAlignmentState.transition) || {};
  const action = recoveryActionFor(transition.mode);
  if (!action || !transition.operation_id) return;
  if (!window.confirm(action.confirm)) return;
  try {
    let body;
    if (action.owner === "guided_setup") {
      // Discard the server's CURRENT workflow explicitly — the panel shows the
      // current state, so a stale locally-cached identity must not block it.
      const current = await fetchOwningSetupWorkflowId();
      body = current ? { setup_workflow_id: current } : {};
    } else {
      body = { operation_id: transition.operation_id, confirm: true };
    }
    const res = await fetch(action.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (isSetupOperationInProgress(data)) {
      // Nothing was discarded: keep the workflow and say which operation owns it.
      if (systemAlignmentEls.warning) {
        systemAlignmentEls.warning.textContent =
          setupOperationInProgressMessage(data);
        systemAlignmentEls.warning.hidden = false;
      }
      loadSystemAlignmentStatus();
      return;
    }
    if (action.owner === "guided_setup" && res.ok && data.ok === true) {
      setSetupWorkflowId(null);
    }
    if (setupCleanupStateFor(data) !== null) {
      showSetupCleanupIncomplete(data);
      loadSystemAlignmentStatus();
      return;
    }
    const succeeded =
      action.owner === "guided_setup" ? data.ok === true : data.stage === "cancelled";
    if (!res.ok || !succeeded) {
      throw new Error(
        data.message ||
          data.error ||
          (action.owner === "guided_setup"
            ? "The setup could not be discarded."
            : "The upgrade could not be cancelled.")
      );
    }
    showSetupCleanupIncomplete(null);
    renderSystemAlignmentStatus(data.transition ? data : data);
    loadSystemAlignmentStatus();
  } catch (err) {
    if (systemAlignmentEls.warning) {
      systemAlignmentEls.warning.textContent = err.message || String(err);
      systemAlignmentEls.warning.hidden = false;
    }
  }
}

if (systemAlignmentEls.resume) {
  systemAlignmentEls.resume.addEventListener("click", resumeSystemAlignment);
}
if (systemAlignmentEls.returnToRunning) {
  systemAlignmentEls.returnToRunning.addEventListener("click", returnToRunningSystemBuild);
}
if (systemAlignmentEls.abandon) {
  systemAlignmentEls.abandon.addEventListener("click", abandonSystemAlignment);
}
if (systemAlignmentEls.retryCleanup) {
  systemAlignmentEls.retryCleanup.addEventListener("click", retrySetupCleanup);
}

// --- Auth gate wiring -------------------------------------------------------
// The Admin Console renders the login/create-password gate first and only runs
// its normal bootstrap (install state + discovery pollers) once authenticated.
// Setup/maintenance/discovery APIs are never called before that.
const authEls = {
  view: document.getElementById("view-auth"),
  createBlock: document.getElementById("auth-create"),
  loginBlock: document.getElementById("auth-login"),
  recoveryBlock: document.getElementById("auth-recovery"),
  recoveryRetry: document.getElementById("auth-recovery-retry"),
  createForm: document.getElementById("auth-create-form"),
  createPassword: document.getElementById("auth-create-password"),
  createConfirm: document.getElementById("auth-create-confirm"),
  createError: document.getElementById("auth-create-error"),
  loginForm: document.getElementById("auth-login-form"),
  loginPassword: document.getElementById("auth-login-password"),
  loginError: document.getElementById("auth-login-error"),
  logout: document.getElementById("auth-logout"),
};

let adminBootstrapped = false;
let pendingAuthenticatedWorkflowResume = false;
let authenticatedWorkflowResumeCompleted = false;
let authenticatedWorkflowResumeInFlight = null;

function setAuthError(el, message) {
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = message;
}

const AUTH_ERROR_MESSAGES = {
  password_required: "Enter a password.",
  password_mismatch: "Passwords do not match.",
  auth_already_configured: "Password is already configured. Please log in.",
  invalid_password: "Incorrect password.",
  login_rate_limited: "Too many attempts. Wait a moment and try again.",
  setup_rate_limited: "Too many attempts. Wait a moment and try again.",
  install_dir_unavailable:
    "Admin install directory is not mounted. Start the Admin Console with install-admin-console.sh.",
};

function authMessage(data) {
  if (data && data.message) return data.message;
  if (data && AUTH_ERROR_MESSAGES[data.error]) return AUTH_ERROR_MESSAGES[data.error];
  return "Something went wrong. Please try again.";
}

// Show the auth gate (create, login, or recovery) and hide every workspace
// surface so no setup/maintenance panel is reachable before authentication.
function showAuthView(mode) {
  workspaceRevealed = false;
  if (startEls.gate) startEls.gate.hidden = true;
  document.querySelectorAll("[data-admin-view-panel]").forEach((panel) => {
    panel.hidden = true;
  });
  if (authEls.view) authEls.view.hidden = false;
  if (authEls.createBlock) authEls.createBlock.hidden = mode !== "create";
  if (authEls.loginBlock) authEls.loginBlock.hidden = mode !== "login";
  if (authEls.recoveryBlock) authEls.recoveryBlock.hidden = mode !== "recovery";
  if (authEls.logout) authEls.logout.hidden = true;
  // The auth gate is the single choke point for every unauthenticated state
  // (login, create, recovery, session expiry, logout, first load): fully drop
  // the System Build pipeline so it never renders above the login gate.
  clearSystemBuildProgress();
  clearUpgradeVerification();
}

function bootstrapAuthenticatedAppOnce() {
  if (adminBootstrapped) return;
  adminBootstrapped = true;
  loadInstallState();
  pollMdns();
  loadMqttBrokers();
  loadZendureCloudSettings();
  window.setInterval(pollMdns, MDNS_POLL_INTERVAL_MS);
}

async function performAuthenticatedWorkflowResume() {
  const alignment = await loadSystemAlignmentStatus();
  const resumedSetup = await resumeGuidedSetupFromTransition(alignment);
  if (!resumedSetup) {
    await resumeGuidedUpgradeFromTransition(alignment);
  }
  pendingAuthenticatedWorkflowResume = false;
  authenticatedWorkflowResumeCompleted = true;
}

async function resumeAuthenticatedWorkflows() {
  if (authenticatedWorkflowResumeInFlight) {
    return await authenticatedWorkflowResumeInFlight;
  }
  authenticatedWorkflowResumeInFlight = performAuthenticatedWorkflowResume();
  try {
    return await authenticatedWorkflowResumeInFlight;
  } finally {
    authenticatedWorkflowResumeInFlight = null;
  }
}

function showAuthenticatedApp() {
  if (authEls.view) authEls.view.hidden = true;
  if (authEls.logout) authEls.logout.hidden = false;
  if (startEls.gate && !workspaceRevealed) startEls.gate.hidden = false;
  bootstrapAuthenticatedAppOnce();
  if (
    authenticatedWorkflowResumeCompleted &&
    !pendingAuthenticatedWorkflowResume
  ) {
    return Promise.resolve(false);
  }
  return resumeAuthenticatedWorkflows();
}

function applyAuthStatus(status) {
  const previousAdminInstanceId = authState.adminInstanceId;
  authState.adminInstanceId = status.admin_instance_id || null;
  if (
    previousAdminInstanceId &&
    authState.adminInstanceId &&
    previousAdminInstanceId !== authState.adminInstanceId &&
    upgradeState.preparedAdminInstanceId
  ) {
    clearUpgradeVerification();
    setUpgradeReleaseStatus();
    renderUpgradePlan();
  }
  authState.configured = Boolean(status.auth_configured);
  authState.authenticated = Boolean(status.authenticated);
  authState.requiresInitialPassword = Boolean(status.requires_initial_password);
  authState.recoveryRequired = Boolean(status.recovery_required);
  authState.csrfToken = status.csrf_token || null;
  if (authState.authenticated) {
    return showAuthenticatedApp();
  }
  if (typeof stopSystemAlignmentPolling === "function") {
    stopSystemAlignmentPolling();
  }
  setupIntentId = null;
  authenticatedWorkflowResumeCompleted = false;
  if (authState.recoveryRequired) {
    showAuthView("recovery");
  } else if (authState.requiresInitialPassword) {
    showAuthView("create");
  } else {
    showAuthView("login");
  }
}

function onAuthLost() {
  authState.authenticated = false;
  authState.csrfToken = null;
  stopSystemAlignmentPolling();
  refreshAuthStatus();
}

async function refreshAuthStatus() {
  try {
    const resp = await rawFetch("/api/admin/auth/status");
    return await applyAuthStatus(await resp.json());
  } catch (err) {
    showAuthView("login");
  }
}

async function submitCreatePassword(event) {
  event.preventDefault();
  setAuthError(authEls.createError, "");
  const password = authEls.createPassword.value;
  const confirm = authEls.createConfirm.value;
  if (password !== confirm) {
    setAuthError(authEls.createError, "Passwords do not match.");
    return;
  }
  try {
    const resp = await rawFetch("/api/admin/auth/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, confirm_password: confirm }),
    });
    const data = await resp.json().catch(() => ({}));
    if (data && data.error === "auth_file_invalid") {
      authState.recoveryRequired = true;
      showAuthView("recovery");
      return;
    }
    if (resp.status === 409) {
      setAuthError(authEls.createError, "Password is already configured. Please log in.");
      showAuthView("login");
      return;
    }
    if (!resp.ok) {
      setAuthError(authEls.createError, authMessage(data));
      return;
    }
    applyAuthStatus(data);
  } catch (err) {
    setAuthError(authEls.createError, "Could not create the password.");
  }
}

async function submitLogin(event) {
  event.preventDefault();
  setAuthError(authEls.loginError, "");
  const password = authEls.loginPassword.value;
  try {
    const resp = await rawFetch("/api/admin/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await resp.json().catch(() => ({}));
    if (data && data.error === "auth_file_invalid") {
      authState.recoveryRequired = true;
      showAuthView("recovery");
      return;
    }
    if (!resp.ok) {
      setAuthError(authEls.loginError, authMessage(data));
      return;
    }
    if (authEls.loginPassword) authEls.loginPassword.value = "";
    applyAuthStatus(data);
  } catch (err) {
    setAuthError(authEls.loginError, "Could not log in.");
  }
}

async function submitLogout() {
  if (typeof stopSystemAlignmentPolling === "function") {
    stopSystemAlignmentPolling();
  }
  clearSetupOperationContext();
  try {
    const resp = await rawFetch("/api/admin/auth/logout", { method: "POST" });
    const data = await resp.json().catch(() => ({}));
    applyAuthStatus(data);
  } catch (err) {
    authState.authenticated = false;
    authState.csrfToken = null;
    showAuthView("login");
  }
}

if (authEls.createForm) authEls.createForm.addEventListener("submit", submitCreatePassword);
if (authEls.loginForm) authEls.loginForm.addEventListener("submit", submitLogin);
if (authEls.logout) authEls.logout.addEventListener("click", submitLogout);
if (authEls.recoveryRetry) authEls.recoveryRetry.addEventListener("click", refreshAuthStatus);

refreshAuthStatus();
