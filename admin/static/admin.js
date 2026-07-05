// SPDX-License-Identifier: AGPL-3.0-or-later
// Vanilla admin discovery UI: list detected networks, scan one or several on
// demand, and render discovered devices. Every server-provided value passes
// through escapeHtml before it reaches the DOM. Polling runs only while a scan
// is active.
"use strict";

const POLL_INTERVAL_MS = 1200;
const POLL_MAX_MS = 120000;

const els = {
  form: document.getElementById("scan-form"),
  cidr: document.getElementById("cidr-input"),
  button: document.getElementById("scan-button"),
  status: document.getElementById("scan-status"),
  error: document.getElementById("scan-error"),
  count: document.getElementById("results-count"),
  empty: document.getElementById("results-empty"),
  list: document.getElementById("results-list"),
  accumulate: document.getElementById("results-accumulate"),
  networksRefresh: document.getElementById("networks-refresh"),
  networksScanAll: document.getElementById("networks-scan-all"),
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
  mqttCount: document.getElementById("mqtt-count"),
  mqttMessage: document.getElementById("mqtt-message"),
  mqttRefresh: document.getElementById("mqtt-refresh"),
  mqttEmpty: document.getElementById("mqtt-empty"),
  mqttList: document.getElementById("mqtt-list"),
  summaryDevices: document.getElementById("setup-summary-devices"),
  summaryNetworks: document.getElementById("setup-summary-networks"),
  summaryMdns: document.getElementById("setup-summary-mdns"),
  summaryMqtt: document.getElementById("setup-summary-mqtt"),
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

let scanSessions = [];
let scanPollTimer = null;
let scanStartedAt = 0;
let scanning = false;
// Detected direct-route networks plus reachable gateway-probe networks share one
// selectable list. Devices kept across scans when "Keep previous results" is on.
let directNetworks = [];
let gatewayNetworks = [];
const keptDevices = new Map();
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

function firstErrorMessage(errors) {
  if (Array.isArray(errors) && errors.length && errors[0] && errors[0].error) {
    return errors[0].error;
  }
  return "Unknown error.";
}

// --- scanning (one or several networks) ----------------------------------

function cancelPolling() {
  if (scanPollTimer) {
    window.clearTimeout(scanPollTimer);
    scanPollTimer = null;
  }
}

function updateBusy() {
  els.button.disabled = scanning;
  els.button.textContent = scanning ? "Scanning…" : "Scan manually";
  els.button.classList.toggle("is-scanning", scanning);
  updateScanAllState();
  notifySetupStatus();
}

// Every detected LAN network (direct + gateway, excluding Docker) is scanned by
// the single "Scan all" action; Docker networks keep their own per-chip button.
function lanCidrs() {
  return combinedNetworks()
    .filter((net) => !net.is_docker_like)
    .map((net) => net.cidr);
}

function updateScanAllState() {
  if (!els.networksScanAll) return;
  const count = lanCidrs().length;
  els.networksScanAll.disabled = scanning || count === 0;
  els.networksScanAll.classList.toggle("is-scanning", scanning);
  els.networksScanAll.textContent = scanning
    ? "Scanning…"
    : count > 1
    ? "Scan all (" + count + ")"
    : "Scan all";
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

async function runScans(cidrs) {
  const unique = [...new Set(cidrs.filter(Boolean))];
  if (!unique.length) {
    showError("Select at least one network, or enter a CIDR.");
    return;
  }
  cancelPolling();
  showError("");
  if (!els.accumulate || !els.accumulate.checked) {
    keptDevices.clear();
  }
  scanning = true;
  scanStartedAt = Date.now();
  scanSessions = unique.map((cidr) => ({
    cidr,
    scanId: null,
    status: "starting",
    devices: [],
    errors: [],
  }));
  updateBusy();
  setStatus("Starting scan of " + unique.length + " network(s)…", "is-running");
  renderAggregate();
  probeMqttNetworks(unique);
  await Promise.all(scanSessions.map(startSession));
  pollSessions();
}

async function startSession(session) {
  try {
    const res = await fetch("/api/discovery/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cidr: session.cidr }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data && data.error ? data.error : "scan request failed");
    }
    session.scanId = data.scan_id;
    session.status = "running";
  } catch (err) {
    session.status = "failed";
    session.errors = [{ error: err.message || String(err) }];
  }
}

async function pollSessions() {
  const running = scanSessions.filter((s) => s.status === "running" && s.scanId);
  await Promise.all(
    running.map(async (session) => {
      try {
        const res = await fetch(
          "/api/discovery/result/" + encodeURIComponent(session.scanId)
        );
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data && data.error ? data.error : "result unavailable");
        }
        session.status = data.status;
        session.devices = Array.isArray(data.devices) ? data.devices : [];
        session.errors = Array.isArray(data.errors) ? data.errors : [];
      } catch (err) {
        session.status = "failed";
        session.errors = [{ error: err.message || String(err) }];
      }
    })
  );
  renderAggregate();

  const stillRunning = scanSessions.some((s) => s.status === "running");
  if (stillRunning && Date.now() - scanStartedAt < POLL_MAX_MS) {
    scanPollTimer = window.setTimeout(pollSessions, POLL_INTERVAL_MS);
    return;
  }
  finishScans(stillRunning);
}

function finishScans(timedOut) {
  scanning = false;
  updateBusy();
  commitDevices();
  const devices = aggregateDevices();
  const failed = scanSessions.filter((s) => s.status === "failed");
  if (timedOut) {
    setStatus("Scan timed out while polling.", null);
  } else {
    setStatus(
      "Scan finished: " +
        devices.length +
        " device(s) across " +
        scanSessions.length +
        " network(s).",
      "is-done"
    );
  }
  if (failed.length) {
    showError(
      failed.length + " network(s) failed: " + firstErrorMessage(failed[0].errors)
    );
  }
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
  // Network-scan hits (kept + current session) are confirmed reachable now.
  for (const device of keptDevices.values()) mergeDevice(seen, device, true);
  for (const session of scanSessions) {
    for (const device of session.devices) mergeDevice(seen, device, true);
  }
  return [...seen.values()];
}

function commitDevices() {
  for (const session of scanSessions) {
    for (const device of session.devices) {
      keptDevices.set(deviceKey(device), device);
    }
  }
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

function renderAggregate() {
  const devices = aggregateDevices();
  els.count.textContent = devices.length + " found";
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
  const running = scanSessions.some(
    (s) => s.status === "running" || s.status === "starting"
  );
  if (running) {
    setStatus(
      "Scanning " + scanSessions.length + " network(s)… " + devices.length + " found",
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

const SOURCE_LABELS = {
  mdns: "mDNS",
  http_probe: "Network scan",
  network_scan: "Network scan",
  manual: "Manual scan",
};

function sourceBadges(device) {
  return sourcesOf(device)
    .map((source) => {
      const label = SOURCE_LABELS[source] || source;
      const cls = source === "mdns" ? "source-mdns" : "source-scan";
      return '<span class="source-badge ' + cls + '">' + escapeHtml(label) + "</span>";
    })
    .join("");
}

function renderDeviceCard(device) {
  const role = String(device.role_suggestion || "unknown");
  const roleClass = "role-" + role.replace(/[^a-z_]/gi, "");
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

// --- network suggestions -------------------------------------------------

async function loadNetworks() {
  els.networksRefresh.disabled = true;
  els.networksEmpty.hidden = false;
  els.networksEmpty.textContent = "Detecting local networks…";
  els.networksList.hidden = true;
  gatewayNetworks = [];
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
  els.networksRefresh.disabled = false;
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
      "No detected networks. Use the gateway probe or enter a CIDR manually below.";
    updateScanAllState();
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
      "No LAN networks detected. See advanced/container networks or enter a CIDR manually.";
  }
  els.networksList.hidden = lan.length === 0;
  // LAN chips are info-only; the header "Scan all" button scans them together.
  els.networksList.innerHTML = lan.map((net) => renderNetworkRow(net, false)).join("");

  els.networksDockerDetails.hidden = docker.length === 0;
  // Docker networks keep a per-chip Scan button (opt-in, not part of "Scan all").
  els.networksDockerList.innerHTML = docker
    .map((net) => renderNetworkRow(net, true))
    .join("");

  updateScanAllState();
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
  // via "Scan all"); Docker chips carry their own opt-in Scan button.
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
    const res = await fetch("/api/discovery/gateway-probe", { method: "POST" });
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

els.networksScanAll.addEventListener("click", () => runScans(lanCidrs()));

els.networksRefresh.addEventListener("click", () => {
  loadNetworks();
  refreshMdns();
  loadMqttBrokers();
});

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const cidr = els.cidr.value.trim();
  if (!cidr) {
    showError("Enter a CIDR range, e.g. 192.168.178.0/24");
    return;
  }
  runScans([cidr]);
});

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
    mdnsDevices.clear();
    for (const device of Array.isArray(result.devices) ? result.devices : []) {
      mdnsDevices.set(deviceKey(device), device);
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
    const res = await fetch(
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
    const res = await fetch("/api/discovery/mdns/refresh", { method: "POST" });
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

function renderMqttBrokers() {
  const candidates = Array.from(mqttBrokers.values());
  els.mqttCount.textContent = candidates.length + " found";
  setSummary(els.summaryMqtt, plural(candidates.length, "candidate"));
  notifySetupStatus();
  els.mqttEmpty.hidden = candidates.length > 0;
  els.mqttList.hidden = candidates.length === 0;
  els.mqttList.innerHTML = candidates
    .map((broker) => {
      const endpoint = String(broker.host || "") + ":" + String(broker.port || "");
      const source =
        broker.source === "mdns" ? "mDNS" : "Network probe";
      const status = String(broker.status || (broker.reachable ? "reachable" : "candidate"))
        .replace(/_/g, " ");
      return (
        '<article class="device-card mqtt-card">' +
        '<div class="device-card-head">' +
        '<span class="device-name">Broker candidate</span>' +
        '<span class="device-role role-unknown">' +
        escapeHtml(status) +
        "</span></div>" +
        '<div class="device-facts">' +
        fact("Endpoint", escapeHtml(endpoint)) +
        fact("Source", escapeHtml(source)) +
        fact("Hostname", escapeHtml(broker.hostname || "—")) +
        fact("Last seen", escapeHtml(broker.last_seen || "—")) +
        "</div>" +
        '<div class="device-card-foot"><span class="confidence">' +
        "Broker candidate only. Topic discovery is not implemented yet." +
        "</span></div></article>"
      );
    })
    .join("");
}

async function loadMqttBrokers() {
  try {
    const res = await fetch("/api/discovery/mqtt-brokers");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "broker discovery failed");
    mqttBrokers.clear();
    for (const broker of Array.isArray(data.candidates) ? data.candidates : []) {
      mqttBrokers.set(String(broker.host) + ":" + String(broker.port), broker);
    }
    renderMqttBrokers();
  } catch (err) {
    els.mqttMessage.textContent =
      "MQTT broker discovery unavailable: " + (err.message || String(err));
  }
}

async function probeMqttNetworks(cidrs) {
  els.mqttMessage.textContent =
    "The network scan is also checking TCP ports 1883 and 8883 for MQTT brokers…";
  const results = await Promise.all(
    cidrs.map(async (cidr) => {
      try {
        const res = await fetch("/api/discovery/mqtt-brokers/probe", {
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
      " open endpoint(s) found. Broker candidates only; topic discovery is not implemented yet.";
}

async function refreshMqttBrokers() {
  els.mqttRefresh.disabled = true;
  els.mqttMessage.textContent = "Refreshing broker discovery…";
  try {
    const res = await fetch("/api/discovery/mqtt-brokers/refresh", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "broker refresh failed");
    els.mqttMessage.textContent =
      "Broker discovery refreshed. " + String(data.reachable || 0) + " candidate(s) reachable.";
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

// --- config draft workflow (Config tab) ----------------------------------
// The Config tab reuses the same aggregated discovery data as the Discovery
// tab. The editable draft lives in frontend state and export actions send it
// to the validated Admin endpoints. Available-device cards re-render on every
// discovery update, but draft cards only redraw on structural changes.

const CONFIG_DRAFT_STORAGE_KEY = "ems-admin-config-draft";
const CONFIG_DISMISSED_STORAGE_KEY = "ems-admin-config-dismissed";
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
  manualHost: document.getElementById("config-manual-host"),
  manualPort: document.getElementById("config-manual-port"),
  manualSerial: document.getElementById("config-manual-serial"),
  manualError: document.getElementById("config-manual-error"),
  gridMeterSelection: document.getElementById("config-grid-meter-selection"),
  validation: document.getElementById("config-validation"),
  draftEmpty: document.getElementById("config-draft-empty"),
  draftList: document.getElementById("config-draft-list"),
  preview: document.getElementById("config-preview"),
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
  applyTarget: document.getElementById("config-apply-target"),
};

let activeConfigTemplate = null;
let activeConfigTemplateTag = null;
let latestConfigPreview = null;
let configPreviewRequest = 0;
let configPreviewTimer = null;

// Flat, ordered list of draft items keyed by their discovery source id. Order
// is display order; inverter numbering and preview grouping derive from it.
let configDraftItems = loadConfigDraft();
// Source ids the user removed/cleared: auto-config must not re-add these, so a
// manual "Remove" or "Clear draft" is not undone by the next discovery poll.
const configDismissed = loadConfigDismissed();
// Source-id -> latest discovered device, refreshed on every available render so
// the add-button handler always has the current device record.
const configAvailableIndex = new Map();

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

function nextInverterName() {
  const used = new Set(inverterItems().map((item) => item.config_name));
  let index = 1;
  while (used.has("inverter_" + index)) index += 1;
  return "inverter_" + index;
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
    config_name: nextInverterName(),
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
  if (role === "grid_meter") {
    selectGridMeter(sourceId);
    return;
  }
  if (draftHasSource(sourceId)) return;
  configDraftItems.push(draftItemFromDevice(device, "inverter"));
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
  configDraftItems.push(item);
  commitDraftChange();
}

function removeDraftItem(sourceId) {
  // Remember the removal so auto-config does not re-add it on the next poll.
  configDismissed.add(sourceId);
  saveConfigDismissed();
  configDraftItems = configDraftItems.filter(
    (item) => item.source_id !== sourceId
  );
  commitDraftChange();
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
  const device = configAvailableIndex.get(sourceId) || item;
  if (item.role === "grid_meter") {
    item.config_name = "grid_meter";
    item.display_name =
      device.display_name || device.model || DEFAULT_GRID_METER_DISPLAY;
  } else {
    // Temporarily drop the item so numbering ignores its old name.
    configDraftItems = configDraftItems.filter(
      (entry) => entry.source_id !== sourceId
    );
    item.config_name = nextInverterName();
    item.display_name = uniqueDisplayName(
      device.display_name || device.model || DEFAULT_INVERTER_DISPLAY,
      "inverter"
    );
    configDraftItems.push(item);
  }
  commitDraftChange();
}

// Structural change: persist, then redraw draft + available (added-state) + preview.
function commitDraftChange() {
  saveConfigDraft();
  renderConfigDraft();
  renderConfigAvailable();
}

// Auto-add every verified inverter that is not already in the draft and was not
// removed by the user. Stale-but-present inverters are kept, never re-added.
function autoAddInverters() {
  let changed = false;
  for (const device of availableConfigDevices()) {
    if (String(device.role_suggestion) !== "inverter") continue;
    if (!isAutoConfigReady(device)) continue;
    const sourceId = deviceKey(device);
    if (draftHasSource(sourceId) || configDismissed.has(sourceId)) continue;
    configDraftItems.push(draftItemFromDevice(device, "inverter"));
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
  configDraftItems.push(item);
  return true;
}

function applyAutoConfig() {
  let changed = autoAddInverters();
  if (autoSelectGridMeter()) changed = true;
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

// Build a draft item from hand-entered details for devices discovery can't reach.
// Keyed by host:port so re-adding the same endpoint is a no-op, and flagged
// `manual` so auto-config and staleness leave it alone.
function addManualDevice() {
  const host = (configEls.manualHost.value || "").trim();
  if (!host) {
    showManualError("Host / IP is required.");
    return;
  }
  const role = configEls.manualRole.value === "grid_meter" ? "grid_meter" : "inverter";
  const port = (configEls.manualPort.value || "").trim();
  const sourceId = "manual:" + host + ":" + (port || "");
  if (draftHasSource(sourceId)) {
    showManualError("A device with this host is already in the draft.");
    return;
  }
  const displayBase =
    (configEls.manualName.value || "").trim() ||
    (role === "grid_meter" ? DEFAULT_GRID_METER_DISPLAY : DEFAULT_INVERTER_DISPLAY);
  if (role === "grid_meter") {
    configDraftItems = configDraftItems.filter((item) => item.role !== "grid_meter");
  }
  configDismissed.delete(sourceId);
  saveConfigDismissed();
  configDraftItems.push({
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
  });
  showManualError("");
  configEls.manualForm.reset();
  commitDraftChange();
}

function roleLabel(role) {
  return role === "grid_meter" ? "grid meter" : "inverter";
}

function renderConfigAvailable() {
  if (!configEls.availableList) return;
  const devices = availableConfigDevices();
  configAvailableIndex.clear();
  for (const device of devices) {
    configAvailableIndex.set(deviceKey(device), device);
  }
  configEls.availableCount.textContent = devices.length + " ready";
  if (!devices.length) {
    configEls.availableList.hidden = true;
    configEls.availableList.innerHTML = "";
    configEls.availableEmpty.hidden = false;
    return;
  }
  configEls.availableEmpty.hidden = true;
  configEls.availableList.hidden = false;
  configEls.availableList.innerHTML = devices
    .map(renderConfigAvailableCard)
    .join("");
}

function renderConfigAvailableCard(device) {
  const sourceId = deviceKey(device);
  const role = String(device.role_suggestion || "unknown");
  const roleClass = "role-" + role.replace(/[^a-z_]/gi, "");
  const serial = device.serial_number
    ? '<span class="v">' + escapeHtml(device.serial_number) + "</span>"
    : '<span class="v missing">missing</span>';
  const ready =
    device.usable_for_config !== undefined
      ? device.usable_for_config
      : device.config_ready;
  const endpoint =
    escapeHtml(device.ip) + (device.port ? ":" + escapeHtml(device.port) : "");
  const added = draftHasSource(sourceId);
  const addLabel = role === "grid_meter" ? "Add as grid meter" : "Add as inverter";
  const button = added
    ? '<button type="button" class="secondary-button compact" disabled>Added</button>'
    : '<button type="button" class="primary-button compact config-add"' +
      ' data-source-id="' +
      escapeHtml(sourceId) +
      '" data-add-role="' +
      escapeHtml(role) +
      '">' +
      addLabel +
      "</button>";

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
    "</div>" +
    '<div class="device-facts">' +
    fact("Endpoint", endpoint) +
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
    button +
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
  if (!configDraftItems.length) {
    configEls.draftList.hidden = true;
    configEls.draftList.innerHTML = "";
    configEls.draftEmpty.hidden = false;
    return;
  }
  configEls.draftEmpty.hidden = true;
  configEls.draftList.hidden = false;
  configEls.draftList.innerHTML = configDraftItems
    .map(renderConfigDraftCard)
    .join("");
}

function renderConfigDraftCard(item) {
  const roleClass = "role-" + String(item.role).replace(/[^a-z_]/gi, "");
  const id = escapeHtml(item.source_id);
  const inverterIndex = inverterItems().findIndex(
    (entry) => entry.source_id === item.source_id
  );
  const kind =
    item.role === "grid_meter" ? "Grid meter" : "Inverter " + (inverterIndex + 1);
  const title = item.display_name || item.device_type || item.config_name;
  const endpoint =
    String(item.ip || "") + (item.port ? ":" + String(item.port) : "");
  const metadata = [
    endpoint,
    item.serial_number || "Serial missing",
    item.api_family,
    item.discovery_source,
  ].filter(Boolean);
  const autoBadge =
    item.role === "grid_meter" && item.auto_selected
      ? '<span class="config-auto-badge">Auto-selected</span>'
      : "";
  const staleBadge =
    item.role === "grid_meter" && selectedGridMeterStale()
      ? '<span class="stale-badge">stale</span>'
      : "";
  return (
    '<article class="config-draft-card" data-source-id="' +
    id +
    '">' +
    '<div class="config-draft-head">' +
    '<div class="config-draft-identity"><span class="config-draft-kind ' +
    escapeHtml(roleClass) +
    '">' +
    escapeHtml(kind) +
    '</span><span class="config-draft-separator" aria-hidden="true">·</span>' +
    '<span class="config-draft-title">' +
    escapeHtml(title) +
    "</span></div>" +
    autoBadge +
    staleBadge +
    '<div class="config-draft-buttons">' +
    '<button type="button" class="secondary-button compact config-draft-action config-draft-move" data-move="up" aria-label="Move up" title="Move up">↑</button>' +
    '<button type="button" class="secondary-button compact config-draft-action config-draft-move" data-move="down" aria-label="Move down" title="Move down">↓</button>' +
    '<button type="button" class="secondary-button compact config-draft-action config-draft-reset">Reset name</button>' +
    '<button type="button" class="secondary-button compact config-draft-action config-draft-remove">Remove</button>' +
    "</div>" +
    "</div>" +
    '<div class="config-draft-fields">' +
    '<label class="field"><span class="field-label">Config name</span>' +
    '<input type="text" data-field="config_name" value="' +
    escapeHtml(item.config_name) +
    '"></label>' +
    '<label class="field"><span class="field-label">Display name</span>' +
    '<input type="text" data-field="display_name" value="' +
    escapeHtml(item.display_name) +
    '"></label>' +
    '<label class="field"><span class="field-label">Role</span>' +
    '<select data-field="role">' +
    '<option value="inverter"' +
    (item.role === "inverter" ? " selected" : "") +
    ">inverter</option>" +
    '<option value="grid_meter"' +
    (item.role === "grid_meter" ? " selected" : "") +
    ">grid_meter</option>" +
    "</select></label>" +
    '<label class="config-draft-toggle"><input type="checkbox" data-field="enabled"' +
    (item.enabled ? " checked" : "") +
    "><span>Enabled</span></label>" +
    "</div>" +
    '<details class="config-device-details"><summary><span>Device details</span>' +
    '<span class="config-device-meta-preview">' +
    escapeHtml(metadata.join(" · ")) +
    "</span></summary>" +
    '<div class="config-device-details-grid">' +
    fact("IP", escapeHtml(item.ip)) +
    fact("Port", escapeHtml(item.port)) +
    fact(
      "Serial",
      item.serial_number
        ? '<span class="v">' + escapeHtml(item.serial_number) + "</span>"
        : '<span class="v missing">missing</span>',
      true
    ) +
    fact("Type", escapeHtml(item.device_type)) +
    fact("API family", escapeHtml(item.api_family)) +
    fact("Source", escapeHtml(item.discovery_source)) +
    '</div><p class="future-note">Advanced parameters coming later.</p></details>' +
    "</article>"
  );
}

function configValidationHints() {
  const hints = [];
  const names = configDraftItems.map((item) => item.config_name);
  const seen = new Set();
  const dupes = new Set();
  for (const name of names) {
    if (!name) continue;
    if (seen.has(name)) dupes.add(name);
    seen.add(name);
  }
  if (configDraftItems.some((item) => !String(item.config_name).trim())) {
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
  if (gridMeterItem() || meters.length === 1) {
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

function gridMeterType(item, fallback) {
  const description = (item.device_type + " " + item.api_family).toLowerCase();
  if (description.includes("ecotracker")) return "ecotracker";
  if (description.includes("3em") && description.includes("gen1")) {
    return "shelly_3em_gen1";
  }
  return fallback || "shelly";
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
  configEls.preview.textContent = "{}";
  setConfigExportReady(false);
  if (configEls.exportStatus) configEls.exportStatus.hidden = true;
  if (configEls.validationCard) configEls.validationCard.dataset.tone = "pending";
  if (configEls.previewReady) configEls.previewReady.textContent = "Checking…";
  if (configPreviewTimer) window.clearTimeout(configPreviewTimer);
  configPreviewTimer = window.setTimeout(requestConfigPreview, 100);
}

async function requestConfigPreview() {
  const requestId = ++configPreviewRequest;
  try {
    const res = await fetch("/api/setup/config-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        devices: configDraftItems,
        supported_grid_meter_count: supportedGridMeters().length,
      }),
    });
    const data = await res.json();
    if (requestId !== configPreviewRequest) return;
    if (!res.ok) throw new Error(data.error || "Config preview unavailable.");
    latestConfigPreview = data;
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
    if (requestId !== configPreviewRequest) return;
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
  return {
    devices: configDraftItems,
    supported_grid_meter_count: supportedGridMeters().length,
    overwrite: Boolean(overwrite),
  };
}

function setConfigExportReady(ready) {
  if (configEls.download) configEls.download.disabled = !ready;
  if (configEls.apply) configEls.apply.disabled = !ready;
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
    configEls.download.disabled = !(latestConfigPreview && latestConfigPreview.ready);
  }
}

async function applyGeneratedConfig() {
  if (!configEls.apply || configEls.apply.disabled) return;
  configEls.apply.disabled = true;
  showConfigApplyStatus("Applying config to the EMS installation…", "info");
  try {
    const res = await fetch("/api/setup/config/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configExportBody(false)),
    });
    const data = await res.json();
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
    configEls.apply.disabled = !(latestConfigPreview && latestConfigPreview.ready);
  }
}

function findDraftItem(sourceId) {
  return configDraftItems.find((item) => item.source_id === sourceId) || null;
}

if (configEls.availableList) {
  configEls.availableList.addEventListener("click", (event) => {
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
    const button = event.target.closest(".config-grid-use");
    if (!button) return;
    selectGridMeter(button.getAttribute("data-source-id"));
  });
}

if (configEls.manualForm) {
  configEls.manualForm.addEventListener("submit", (event) => {
    event.preventDefault();
    addManualDevice();
  });
}

if (configEls.download) {
  configEls.download.addEventListener("click", downloadGeneratedConfig);
}

if (configEls.apply) {
  configEls.apply.addEventListener("click", applyGeneratedConfig);
}

if (configEls.draftList) {
  configEls.draftList.addEventListener("click", (event) => {
    const card = event.target.closest(".config-draft-card");
    if (!card) return;
    const sourceId = card.getAttribute("data-source-id");
    if (event.target.closest(".config-draft-remove")) {
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

  // Text inputs update state without redraw so focus is kept while typing;
  // only the preview and validation refresh. Role changes redraw (badge/order).
  configEls.draftList.addEventListener("input", (event) => {
    const field = event.target.getAttribute("data-field");
    if (!field || field === "role" || field === "enabled") return;
    const card = event.target.closest(".config-draft-card");
    const item = card && findDraftItem(card.getAttribute("data-source-id"));
    if (!item) return;
    item[field] = event.target.value;
    saveConfigDraft();
    renderConfigPreview();
    renderConfigValidation();
    if (field === "display_name") {
      const title = card.querySelector(".config-draft-title");
      if (title) title.textContent = item.display_name || item.device_type || item.config_name;
    }
  });

  configEls.draftList.addEventListener("change", (event) => {
    const field = event.target.getAttribute("data-field");
    const card = event.target.closest(".config-draft-card");
    const item = card && findDraftItem(card.getAttribute("data-source-id"));
    if (!item || !field) return;
    if (field === "enabled") {
      item.enabled = event.target.checked;
      saveConfigDraft();
      renderConfigPreview();
    } else if (field === "role") {
      if (event.target.value === "grid_meter") {
        // Keep a single primary grid meter: demote any other one to nothing by
        // removing it before this item takes the grid_meter role.
        configDraftItems = configDraftItems.filter(
          (entry) => entry === item || entry.role !== "grid_meter"
        );
        item.config_name = "grid_meter";
        item.auto_selected = false;
      }
      item.role = event.target.value;
      commitDraftChange();
    }
  });
}

if (configEls.clearDraft) {
  configEls.clearDraft.addEventListener("click", () => {
    // Dismiss everything currently discovered so a cleared draft stays cleared
    // until the user re-adds a device; auto-config skips dismissed sources.
    for (const device of availableConfigDevices()) {
      configDismissed.add(deviceKey(device));
    }
    saveConfigDismissed();
    configDraftItems = [];
    try {
      window.localStorage.removeItem(CONFIG_DRAFT_STORAGE_KEY);
    } catch (err) {
      /* ignore */
    }
    renderConfigDraft();
    renderConfigAvailable();
  });
}

const ADMIN_VIEWS = ["setup", "advanced"];

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
  if (next === "setup") {
    syncConfigFromDiscovery();
  }
}

function currentHashView() {
  return (window.location.hash || "").replace(/^#/, "");
}

document.querySelectorAll("[data-admin-view]").forEach((button) => {
  button.addEventListener("click", () => {
    window.location.hash = button.dataset.adminView;
  });
});
window.addEventListener("hashchange", () => setAdminView(currentHashView()));

// --- setup wizard --------------------------------------------------------
// Compact stepper: only the active step's panel is shown. Devices and Config
// stay locked until the Release step reports "ready". Lightweight in-memory
// state model backed by the release-resource API.

const SETUP_STEPS = ["release", "devices", "config", "deployment", "start"];
const SETUP_STEP_STORAGE_KEY = "ems-admin-setup-step";

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
  devices: { status: "idle", supported_count: 0, ignored_count: 0, mqtt_broker_count: 0 },
  config: {
    status: "empty",
    auto_added_count: 0,
    warnings: [],
    template_loaded: false,
    template_tag: null,
  },
  deployment: {
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
    conflict: false,
    existing_conflict: null,
    docker: null,
    auto_prepare_attempted: false,
  },
  start: {
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
  },
};

let deploymentJobTimer = null;
let startJobTimer = null;

let setupInitialized = false;
let devicesDiscoveryStarted = false;

const setupEls = {
  back: document.getElementById("setup-back"),
  next: document.getElementById("setup-next"),
  navError: document.getElementById("setup-nav-error"),
  releaseForm: document.getElementById("release-form"),
  releaseSelect: document.getElementById("release-select"),
  releaseDownload: document.getElementById("release-download"),
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

// Devices and Config cannot be opened until release resources are prepared;
// Deployment additionally needs a saved generated config.
function stepLocked(step) {
  if (step === "devices" || step === "config") return !releaseReady();
  if (step === "deployment") {
    return !releaseReady() || !setupState.deployment.generated_ready;
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
  setupEls.next.hidden = isLast;
  if (isLast) return;
  // Config commits the generated config on Continue; other steps just unlock.
  const onConfig = setupState.activeStep === "config";
  setupEls.next.textContent = onConfig ? "Continue to deployment" : "Next";
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
  dep.conflict = false;
  dep.existing_conflict = null;
  dep.auto_prepare_attempted = true;
  dep.steps = [];
  renderDeploymentControls();
  try {
    const res = await fetch("/api/setup/deployment/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overwrite: Boolean(overwrite) }),
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
  } else if (job.status === "failed") {
    dep.prepared = false;
    dep.error = (job.error && job.error.message) || "Preparation failed.";
  }
  renderDeployment();
  notifySetupStatus();
}

function pollDeploymentJob(jobId) {
  if (deploymentJobTimer) window.clearTimeout(deploymentJobTimer);
  const tick = async () => {
    try {
      const res = await fetch("/api/setup/deployment/jobs/" + encodeURIComponent(jobId));
      const job = await res.json();
      if (!res.ok) throw new Error(job.error || "Job status unavailable.");
      applyDeploymentJob(job);
      if (job.status === "running") {
        deploymentJobTimer = window.setTimeout(tick, 800);
      } else {
        notifySetupStatus();
        loadDeploymentPlan();
      }
    } catch (err) {
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
  setupEls.startConflictTitle.textContent = replace
    ? "EMS is running with a different image"
    : running
    ? "EMS is already running"
    : "Existing EMS container found";
  setupEls.startConflictMessage.textContent = replace
    ? "A running EMS container already uses this name, but it is not using the selected release. Replacing it will stop and remove the current EMS container, then start the prepared deployment with the selected image. Bind-mounted config/data folders are preserved, but switching releases can change config/runtime compatibility."
    : safe
      ? "A stopped EMS container already uses this name and blocks starting the selected release."
      : running
        ? "A running EMS container already uses this name. Re-check its image and status before taking action."
        : "An existing container uses this name. Re-check its status before taking action.";
  setSummary(setupEls.startConflictName, conflict.container_name || "—");
  setSummary(setupEls.startConflictImage, conflict.image || "—");
  setSummary(setupEls.startConflictSelectedImage, conflict.selected_image || "—");
  if (setupEls.startConflictResolve) {
    setupEls.startConflictResolve.hidden = !safe && !replace;
    setupEls.startConflictResolve.disabled = start.resolving_conflict;
    setupEls.startConflictResolve.textContent = start.resolving_conflict
      ? replace
        ? "Replacing running EMS…"
        : "Removing old container…"
      : replace
        ? "Replace running EMS and continue"
        : "Remove old container and continue";
  }
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
  return service === "influxdb" ? "InfluxDB" : "EMS";
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
  if (start.conflict) return start.error || "An existing container blocks startup.";
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
      body: JSON.stringify({}),
    });
    const data = await res.json();
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
      body: JSON.stringify({}),
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
    start.conflict = null;
    start.resolving_conflict = false;
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
  const tick = async () => {
    try {
      const res = await fetch(
        "/api/setup/deployment/start/jobs/" + encodeURIComponent(jobId)
      );
      const job = await res.json();
      if (!res.ok) throw new Error(job.error || "Job status unavailable.");
      applyStartJob(job);
      if (job.status === "running") {
        startJobTimer = window.setTimeout(tick, 900);
      } else {
        notifySetupStatus();
        refreshStartStatus();
      }
    } catch (err) {
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
  if (devicesDiscoveryStarted) return;
  devicesDiscoveryStarted = true;
  loadNetworks().then(runInitialScan);
}

async function onReleaseSelectChange() {
  const value = setupEls.releaseSelect.value;
  setupState.release.selected = value;
  const release = setupState.release.releases.find((item) => item.tag === value);
  setSummary(setupEls.releaseSelectedVal, release ? release.name : value);
  renderReleaseBadges(release);
  setupState.release.resources = null;
  setupState.release.docker_image = null;
  setupState.release.version = null;
  clearActiveConfigTemplate();
  // A changed selection invalidates any previously prepared resources.
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
    setReleaseStatus("failed", release.reason || "This release cannot be prepared.");
  } else {
    setReleaseStatus("not_started");
  }
  if (release && release.reason && setupEls.releaseStatus) {
    setupEls.releaseStatus.textContent = release.reason;
  }
  renderReleaseResources();
}

function setReleaseStatus(status, error) {
  setupState.release.status = status;
  setupState.release.error = error || null;
  setSummary(setupEls.releaseStatusVal, RELEASE_STATUS_TEXT[status] || status);
  const messages = {
    loading: "Loading EMS releases…",
    not_started: "Not started. Select a release and prepare resources.",
    downloading: "Preparing release resources…",
    ready: "Release resources are ready.",
    failed: "Release preparation failed.",
  };
  if (setupEls.releaseStatus) {
    setupEls.releaseStatus.textContent = messages[status] || "";
  }
  if (setupEls.releaseError) {
    setupEls.releaseError.hidden = !error;
    setupEls.releaseError.textContent = error || "";
  }
  if (setupEls.releaseDownload) {
    const release = setupState.release.releases.find(
      (item) => item.tag === setupState.release.selected
    );
    setupEls.releaseDownload.disabled =
      status === "loading" ||
      status === "downloading" ||
      status === "ready" ||
      !release ||
      !release.selectable;
    setupEls.releaseDownload.textContent =
      status === "failed" ? "Retry" : status === "ready" ? "Resources ready" : "Prepare resources";
  }
  renderStepper();
}

function releaseOptionLabel(release) {
  const labels = [release.name || release.tag];
  if (release.channel === "stable") labels.push("stable");
  if (release.channel === "latest") labels.push("latest", "not stable");
  if (release.prerelease) labels.push("rc", "not stable");
  labels.push(release.docker_supported ? "docker" : "unsupported");
  if (release.prepared) labels.push("prepared");
  if (release.active) labels.push("active");
  return (
    labels[0] +
    (labels.length > 1 ? " — " + labels.slice(1).join(" · ") : "") +
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
      "Prepare release resources to load config.template.json.";
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
    throw new Error("Prepared release returned an invalid config template.");
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
    setupEls.releaseSelect.innerHTML = "";
    for (const release of releases) {
      const option = document.createElement("option");
      option.value = release.tag;
      option.textContent = releaseOptionLabel(release);
      option.disabled = release.selectable === false;
      setupEls.releaseSelect.appendChild(option);
    }
    const selected =
      releases.find((item) => item.tag === data.default_release) ||
      releases.find((item) => item.tag === data.prepared_release && item.selectable) ||
      releases.find((item) => item.selectable !== false);
    if (!selected) {
      throw new Error(
        Array.isArray(data.warnings) && data.warnings.length
          ? data.warnings[0]
          : "No EMS releases are available."
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
  } catch (err) {
    setupState.release.releases = [];
    setupEls.releaseSelect.disabled = true;
    setReleaseStatus("failed", err.message || String(err));
  }
}

async function prepareRelease() {
  const tag = setupEls.releaseSelect.value;
  if (!tag) {
    setReleaseStatus("failed", "Select a release first.");
    return;
  }
  setReleaseStatus("downloading");
  try {
    const res = await fetch("/api/setup/releases/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: tag }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data && data.error ? data.error : "release preparation failed");
    }
    setupState.release.version = data.tag;
    setupState.release.current = data.tag;
    setupState.release.resources = data.resources || null;
    setupState.release.docker_image = data.docker_image || null;
    await loadActiveConfigTemplate(data.tag);
    const release = setupState.release.releases.find((item) => item.tag === data.tag);
    if (release) release.prepared = true;
    renderReleaseBadges(release);
    setReleaseStatus("ready");
    if (setupEls.releaseStatus) {
      setupEls.releaseStatus.textContent = data.reused
        ? "Cached release resources are ready."
        : "Release resources downloaded and ready.";
    }
    renderReleaseResources();
  } catch (err) {
    setupState.release.resources = null;
    clearActiveConfigTemplate();
    setReleaseStatus("failed", err.message || String(err));
    renderReleaseResources();
  }
}

document.querySelectorAll("[data-setup-step]").forEach((button) => {
  button.addEventListener("click", () => setActiveStep(button.dataset.setupStep));
});
if (setupEls.back) setupEls.back.addEventListener("click", () => goToStep(-1));
if (setupEls.next) {
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
if (setupEls.releaseForm) {
  setupEls.releaseForm.addEventListener("submit", (event) => {
    event.preventDefault();
    prepareRelease();
  });
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
  loadReleases();
  refreshDeploymentStatus();
}

// --- start gate ----------------------------------------------------------
// The Admin UI opens on a router screen that detects the install state and
// recommends the safest of the only two flows (set up new / manage existing).
// The setup wizard must not auto-run when an install already exists, so its
// network-touching init is deferred until the user chooses "Set up a new
// system". Every server-provided path/message passes through escapeHtml.

const RECOMMEND_LABELS = {
  setup_new: "Set up a new system",
  manage_existing: "Manage my existing system",
};

const startEls = {
  gate: document.getElementById("view-start"),
  tabs: document.querySelector(".admin-view-tabs"),
  recommend: document.getElementById("start-recommend"),
  form: document.getElementById("start-path-form"),
  error: document.getElementById("start-path-error"),
  continue: document.getElementById("start-continue"),
};

let workspaceRevealed = false;

function selectedStartChoice() {
  const checked = startEls.form.querySelector('input[name="start-path"]:checked');
  return checked ? checked.value : null;
}

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
  const label = escapeHtml(RECOMMEND_LABELS[recommended] || "Manage my existing system");
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

  const preselect = startEls.form.querySelector(
    'input[name="start-path"][value="' + recommended + '"]'
  );
  if (preselect) preselect.checked = true;
  if (startEls.continue) startEls.continue.disabled = false;
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
    if (startEls.continue) startEls.continue.disabled = false;
  }
}

function revealWorkspace() {
  if (startEls.gate) startEls.gate.hidden = true;
  if (startEls.tabs) startEls.tabs.hidden = false;
  workspaceRevealed = true;
}

function enterSetup() {
  revealWorkspace();
  if (!setupInitialized) initSetupWizard();
  window.location.hash = "setup";
  setAdminView("setup");
}

function enterMaintenance() {
  revealWorkspace();
  window.location.hash = "advanced";
  setAdminView("advanced");
}

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

async function submitStartPath(event) {
  event.preventDefault();
  setStartError("");
  const choice = selectedStartChoice();
  if (!choice) {
    setStartError("Choose how you want to continue.");
    return;
  }
  startEls.continue.disabled = true;
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
    if (!workspaceRevealed) startEls.continue.disabled = false;
  }
}

if (startEls.form) {
  startEls.form.addEventListener("submit", submitStartPath);
  startEls.form.querySelectorAll('input[name="start-path"]').forEach((input) => {
    input.addEventListener("change", () => setStartError(""));
  });
}

loadInstallState();

// Discovery pollers can run before the workspace is revealed; they only feed the
// devices step once setup is entered.
pollMdns();
loadMqttBrokers();
window.setInterval(pollMdns, MDNS_POLL_INTERVAL_MS);
