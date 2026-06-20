// SPDX-License-Identifier: AGPL-3.0-or-later
const state = {
  snapshot: null,
  range: "24h",
  // Lightweight SQLite-backed history shown in the Aggregate/Devices views.
  // Independent of the InfluxDB analytics state below: it always works with no
  // external dependency and is the default experience.
  history: {
    range: "24h",
    device: "",
    data: null,
    chart: null,
    chartSignature: null,
    deviceOptions: [],
  },
  // InfluxDB-backed long-term analytics shown in the dedicated Analytics tab.
  // ``available`` is null until probed; false renders the "not configured"
  // state instead of a broken chart.
  analytics: {
    tab: "overview",
    device: "",
    data: null,
    chart: null,
    available: null,
    deviceOptions: [],
    overlays: { soc: false, target: false, grid: false },
    custom: { active: false, start: null, end: null },
    // Zoom viewport (epoch seconds) when the user has zoomed into the chart;
    // null means live mode. applyingScale guards programmatic scale changes so
    // they are not mistaken for a user zoom.
    zoom: null,
    applyingScale: false,
    // Cached uPlot instance signature; when unchanged across refreshes the
    // chart is updated in place (setData) instead of destroyed/recreated.
    chartSignature: null,
    // Cached series-based KPI values, keyed by a stable data key. Live snapshot
    // updates reuse this cache instead of re-integrating the full series.
    kpiCache: { dataKey: null, values: {} },
  },
  flowView: "aggregated",
  demoMode: isDemoMode(),
  liveTransport: "sse",
  deviceSocValues: new Map(),
  auth: {
    configured: false,
    authenticated: false,
    csrfToken: null,
  },
  runtime: null,
  runtimeEditorDirty: false,
  runtimeEditorFocused: false,
  flowActivity: new Map(),
  deviceFlowSignature: null,
  diagnose: {
    profile: "install",
    report: null,
    running: false,
  },
  logs: {
    cursor: 0,
    lines: [],
    follow: true,
    level: "INFO",
    serviceLevel: "INFO",
    timerId: null,
  },
};

const MAX_LOG_ROWS = 1000;
const LOG_POLL_INTERVAL_MS = 2000;

const SOC_ANIMATION_EPSILON = 0.1;
const SSE_TELEMETRY_TIMEOUT_MS = 3000;
let pollingStarted = false;
let pollingIntervalId = null;
let pendingDeviceFlowBatteryAnimation = false;

// Single-chart philosophy: one combined chart, configurable series. Colors are
// resolved from the existing CSS tokens (styles.css), never re-hardcoded here.
const ANALYTICS_SERIES_META = {
  pv: { label: "PV Input", colorVar: "--pv", unit: "W" },
  output: { label: "Inverter Output", colorVar: "--output", unit: "W" },
  battery: { label: "Battery Power", colorVar: "--battery", unit: "W" },
  soc: { label: "SoC", colorVar: "--accent", unit: "%", scaleId: "pct" },
  home: { label: "Home Load", colorVar: "--accent2", unit: "W" },
  grid: { label: "Grid Power", colorVar: "--grid", unit: "W" },
  target: { label: "EMS Target", colorVar: "--accent2", unit: "W" },
};

// Optional overlays toggled on top of the active tab's base series. SoC uses a
// secondary (percentage) axis; the rest share the watts axis. Grid Power is the
// meter exchange power (positive import / negative export); EMS Target is the
// controller's effective output target. All overlays are data-backed.
const ANALYTICS_OVERLAYS = [
  { id: "soc", label: "SoC" },
  { id: "target", label: "EMS Target" },
  { id: "grid", label: "Grid Power" },
];

// Analytics sub-tabs. Each tab reuses the same chart + API; only the visible
// series and KPI cards change (no chart explosion, no extra chart pages).
const ANALYTICS_TABS = [
  { id: "overview", label: "Overview", series: ["pv", "output", "battery"], kpis: ["pv", "output", "charge", "discharge", "soc", "role"] },
  { id: "devices", label: "Devices", series: ["pv", "output", "battery"], kpis: ["pv", "output", "charge", "discharge", "soc", "role"] },
  { id: "grid", label: "Grid", series: ["grid", "home"], kpis: ["gridImport", "gridExport", "home", "soc"] },
  { id: "battery", label: "Battery", series: ["battery"], kpis: ["charge", "discharge", "soc", "role"] },
  { id: "pv", label: "PV", series: ["pv"], kpis: ["pv", "pvPeak", "output", "soc"] },
];

const _kpiPos = (value) => Math.max(0, value);
const _kpiNeg = (value) => Math.max(0, -value);

// KPI registry: id -> { label(range), tone, compute(data, snapshot) }. Energy
// KPIs integrate the fetched series; soc/role read the live snapshot.
const ANALYTICS_KPIS = {
  pv: { label: (r) => `PV · ${r}`, tone: "pv", compute: (d) => energyLabel(integrateSeries(d, "pv", _kpiPos)) },
  output: { label: (r) => `Output · ${r}`, tone: "output", compute: (d) => energyLabel(integrateSeries(d, "output", _kpiPos)) },
  charge: { label: (r) => `Charge · ${r}`, tone: "battery", compute: (d) => energyLabel(integrateSeries(d, "battery", _kpiPos)) },
  discharge: { label: (r) => `Discharge · ${r}`, tone: "battery", compute: (d) => energyLabel(integrateSeries(d, "battery", _kpiNeg)) },
  gridImport: { label: (r) => `Grid Import · ${r}`, tone: "grid", compute: (d) => energyLabel(integrateSeries(d, "grid", _kpiPos)) },
  gridExport: { label: (r) => `Grid Export · ${r}`, tone: "grid", compute: (d) => energyLabel(integrateSeries(d, "grid", _kpiNeg)) },
  home: { label: (r) => `Home · ${r}`, tone: "output", compute: (d) => energyLabel(integrateSeries(d, "home", _kpiPos)) },
  pvPeak: { label: () => "PV Peak", tone: "pv", compute: (d) => powerLabel(seriesPeak(d, "pv")) },
  // ``live`` KPIs read the cheap live snapshot (not the integrated series), so
  // they can be refreshed on every SSE/poll update without re-integrating.
  soc: { label: () => "Current SoC", tone: "accent", live: true, compute: (_d, s) => (s ? `${Math.round(Number(s.average_soc || 0))}%` : "--") },
  role: { label: () => "Runtime Role", tone: "output", live: true, compute: (_d, s) => runtimeRoleLabel(s) },
};

const FLOW_ACTIVATE_THRESHOLD_W = 8;
const FLOW_DEACTIVATE_THRESHOLD_W = 3;
const FLOW_THRESHOLD_W = FLOW_ACTIVATE_THRESHOLD_W;
const FLOW_SPEED_BUCKETS = {
  idle: { alpha: 0.12, width: 3, glow: 0.08 },
  low: { alpha: 0.48, width: 4, glow: 0.16 },
  medium: { alpha: 0.68, width: 5, glow: 0.26 },
  high: { alpha: 0.90, width: 6, glow: 0.40 },
};
const DEVICE_FLOW_LAYOUT = {
  width: 900,
  rowHeight: 244,
  firstRowY: 28,
  rowBottomPadding: 28,
  pvX: 44,
  batteryX: 44,
  inverterX: 342,
  sharedX: 690,
  pvOffsetY: 0,
  inverterOffsetY: 76,
  batteryOffsetY: 150,
  inverterPvPortOffsetY: 32,
  inverterBatteryPortOffsetY: 60,
  sharedVisualHeight: 76,
  sharedHomeGridGapY: 164,
};

function $(id) {
  return document.getElementById(id);
}

function watts(value) {
  const number = Number(value || 0);
  if (Math.abs(number) >= 1000) {
    return `${(number / 1000).toFixed(2)} kW`;
  }
  return `${Math.round(number)} W`;
}

function signedWatts(value) {
  const number = Number(value || 0);
  if (number === 0) return watts(0);
  return `${number > 0 ? "+" : "-"}${watts(Math.abs(number))}`;
}

function pct(value) {
  return `${Math.round(Number(value || 0))}%`;
}

function normalizeBatteryPowerForDisplay(rawBatteryPowerW) {
  const number = Number(rawBatteryPowerW);
  const valueW = Number.isFinite(number) ? number : 0;
  const absW = Math.abs(valueW);

  if (valueW > 0) {
    return {
      valueW,
      absW,
      state: "charging",
      isCharging: true,
      isDischarging: false,
      isIdle: false,
    };
  }

  if (valueW < 0) {
    return {
      valueW,
      absW,
      state: "discharging",
      isCharging: false,
      isDischarging: true,
      isIdle: false,
    };
  }

  return {
    valueW: 0,
    absW: 0,
    state: "idle",
    isCharging: false,
    isDischarging: false,
    isIdle: true,
  };
}

function batteryStateLabel(batteryFlow) {
  const labels = {
    charging: "Charging",
    discharging: "Discharging",
    idle: "Idle",
  };
  return labels[batteryFlow.state] || labels.idle;
}

function batteryPipeDirection(batteryFlow) {
  return batteryFlow.isCharging ? "reverse" : "forward";
}

function gridDirectionLabel(gridPower) {
  if (gridPower > FLOW_THRESHOLD_W) return "Import";
  if (gridPower < -FLOW_THRESHOLD_W) return "Export";
  return "Neutral";
}

function aggregatedBatteryPowerW(snapshot) {
  const entries = normalizeDeviceEntries(snapshot?.devices || {});
  if (!entries.length) {
    return normalizeBatteryPowerForDisplay(snapshot?.battery_power_w).valueW;
  }

  return entries.reduce(
    (total, [, device]) => total + normalizeBatteryPowerForDisplay(device?.battery_power_w).valueW,
    0
  );
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setConnection(text, connected) {
  const el = $("connectionState");
  el.textContent = text;
  el.className = connected ? "pill" : "pill muted";
}

function updateSnapshot(snapshot) {
  state.snapshot = snapshot;
  const status = state.demoMode
    ? "Demo"
    : state.liveTransport === "polling" ? "Polling" : "Live";
  setConnection(status, true);
  renderSnapshot(snapshot);
}

// View-aware live rendering. Every SSE/poll snapshot updates only the globally
// required sections (header metrics, rules, device selector) plus the section
// belonging to the currently visible view. Hidden views are not rebuilt, so the
// browser does not waste CPU recreating device cards, the device-flow SVG,
// energy stats, control explain, or animating the aggregated pipes while another
// tab (e.g. Analytics) is on screen. setFlowView() is no longer called here; it
// runs on initial setup and on actual view changes only.
function renderSnapshot(snapshot) {
  renderGlobalSnapshotMetrics(snapshot);
  renderRules(snapshot.rules || {});
  populateDeviceSelector(snapshot);
  renderViewSnapshot(state.flowView, snapshot);
}

// Dispatch the view-specific live render for a single view. Shared by the live
// snapshot path and by setFlowView() (so switching to a view immediately shows
// fresh data from the latest snapshot without waiting for the next update).
function renderViewSnapshot(view, snapshot) {
  if (!snapshot) return;
  if (view === "aggregated") {
    renderAggregatedSnapshot(snapshot);
  } else if (view === "devices") {
    renderDevicesSnapshot(snapshot);
  } else if (view === "control") {
    renderControlSnapshot(snapshot);
  } else if (view === "energy") {
    renderEnergySnapshot(snapshot);
  } else if (view === "analytics") {
    renderAnalyticsLiveSnapshot(snapshot);
  }
  // diagnose/logs keep their own render/polling paths and are not rebuilt here.
}

// Header summary metrics shown on every view.
function renderGlobalSnapshotMetrics(snapshot) {
  const batteryFlow = normalizeBatteryPowerForDisplay(aggregatedBatteryPowerW(snapshot));
  setText("metricPv", watts(snapshot.pv_total_w));
  setText("metricHome", watts(snapshot.home_load_w));
  setText("metricGrid", watts(snapshot.grid_power_w));
  setText("metricBattery", signedWatts(batteryFlow.valueW));
  setText("metricSoc", pct(snapshot.average_soc));
  setText("lastUpdated", new Date(snapshot.timestamp).toLocaleTimeString());
}

// Aggregated energy-flow SVG (texts, battery fill, visual states, animated
// pipes). Only run while the aggregated view is on screen.
function renderAggregatedSnapshot(snapshot) {
  const batteryFlow = normalizeBatteryPowerForDisplay(aggregatedBatteryPowerW(snapshot));
  const gridPower = Number(snapshot.grid_power_w || 0);
  const pvPower = Number(snapshot.pv_total_w || 0);
  const inverterPower = Number(snapshot.inverter_output_w || 0);
  const homeLoad = Number(snapshot.home_load_w || 0);
  const soc = clamp(Number(snapshot.average_soc || 0), 0, 100);

  setText("flowPv", watts(snapshot.pv_total_w));
  setText("flowBattery", signedWatts(batteryFlow.valueW));
  setText("flowInverter", watts(snapshot.inverter_output_w));
  setText("flowHome", watts(snapshot.home_load_w));
  setText("flowGrid", watts(snapshot.grid_power_w));
  setText("flowBatterySoc", pct(soc));
  setText("flowBatteryState", batteryStateLabel(batteryFlow));
  setText("flowGridDirection", gridDirectionLabel(gridPower));

  setBatteryFill("flowBatteryFill", soc);
  setVisualState("visualPv", flowActive("aggregate:visualPv", pvPower), "active");
  setVisualState(
    "visualBattery",
    flowActive("aggregate:visualBattery", batteryFlow.absW),
    batteryFlow.state
  );
  setVisualState("visualInverter", flowActive("aggregate:visualInverter", inverterPower), "active");
  setVisualState("visualHome", flowActive("aggregate:visualHome", homeLoad), "active");
  setVisualState(
    "visualGrid",
    flowActive("aggregate:visualGrid", Math.abs(gridPower)),
    gridPower > FLOW_THRESHOLD_W ? "importing" : gridPower < -FLOW_THRESHOLD_W ? "exporting" : "neutral"
  );

  setPipe("pipePvInverter", pvPower, "forward");
  // The battery path is drawn battery -> inverter; in the current SVG dash
  // animation, reverse visibly flows inverter -> battery for charging.
  setPipe("pipeBatteryInverter", batteryFlow.absW, batteryPipeDirection(batteryFlow));
  setPipe("pipeInverterHome", inverterPower, "forward");
  setPipe("pipeGridHome", Math.abs(gridPower), gridPower < -FLOW_THRESHOLD_W ? "reverse" : "forward");
}

function renderDevicesSnapshot(snapshot) {
  renderDevices(snapshot.devices || {});
  renderDeviceFlow(snapshot);
}

function renderControlSnapshot(snapshot) {
  renderControlExplain(snapshot);
}

function renderEnergySnapshot(snapshot) {
  renderEnergyStats(snapshot.energy_stats);
}

// Analytics live update is intentionally cheap: it only refreshes the live KPI
// cards (Current SoC, Runtime Role) from the snapshot. The series-based KPIs and
// the chart are not recomputed here -- they change only when analytics data is
// (re)loaded (see renderAnalytics / the KPI cache).
function renderAnalyticsLiveSnapshot(snapshot) {
  renderAnalyticsLiveKpis(snapshot);
}

function flowActive(key, value) {
  const wattsValue = Math.abs(Number(value || 0));
  const wasActive = Boolean(state.flowActivity.get(key));
  const active = wasActive
    ? wattsValue > FLOW_DEACTIVATE_THRESHOLD_W
    : wattsValue >= FLOW_ACTIVATE_THRESHOLD_W;
  state.flowActivity.set(key, active);
  return active;
}

function flowSpeedBucket(value, active) {
  if (!active) return "idle";
  const wattsValue = Math.abs(Number(value || 0));
  if (wattsValue >= 600) return "high";
  if (wattsValue >= 150) return "medium";
  return "low";
}

function applyFlowClasses(el, active, direction, speedBucket) {
  if (!el) return;
  const previousBucket = typeof el.getAttribute === "function"
    ? el.getAttribute("data-flow-speed") || ""
    : "";
  el.classList.toggle("active", active);
  el.classList.toggle("idle", !active);
  el.classList.toggle("reverse", direction === "reverse");
  ["idle", "low", "medium", "high"].forEach((bucket) => {
    el.classList.toggle(`flow-speed-${bucket}`, speedBucket === bucket);
  });
  if (previousBucket !== speedBucket && typeof el.setAttribute === "function") {
    el.setAttribute("data-flow-speed", speedBucket);
  }
}

function applyPipeStyleBucket(el, speedBucket) {
  if (!el) return;
  if (typeof el.getAttribute === "function" && el.getAttribute("data-flow-style") === speedBucket) {
    return;
  }
  const style = FLOW_SPEED_BUCKETS[speedBucket] || FLOW_SPEED_BUCKETS.idle;
  el.style.setProperty("--pipe-alpha", String(style.alpha));
  el.style.setProperty("--pipe-width", `${style.width}px`);
  el.style.setProperty("--pipe-glow", String(style.glow));
  if (typeof el.setAttribute === "function") {
    el.setAttribute("data-flow-style", speedBucket);
  }
}

function setPipe(id, value, direction = "forward") {
  const el = $(id);
  if (!el) return;
  const wattsValue = Math.abs(Number(value || 0));
  const active = flowActive(`aggregate:${id}`, wattsValue);
  const speedBucket = flowSpeedBucket(wattsValue, active);

  applyFlowClasses(el, active, direction, speedBucket);
  applyPipeStyleBucket(el, speedBucket);
}

function setVisualState(id, active, mode) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle("active", Boolean(active));
  el.classList.toggle("charging", Boolean(active) && mode === "charging");
  el.classList.toggle("discharging", Boolean(active) && mode === "discharging");
  el.classList.toggle("importing", Boolean(active) && mode === "importing");
  el.classList.toggle("exporting", Boolean(active) && mode === "exporting");
}

function afterNextPaint(callback) {
  if (
    typeof window !== "undefined"
    && typeof window.requestAnimationFrame === "function"
  ) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(callback);
    });
    return;
  }

  callback();
}

function normalizeSoc(value) {
  const numericSoc = Number(value);
  return Number.isFinite(numericSoc) ? clamp(numericSoc, 0, 100) : 0;
}

function socValuesDiffer(previous, next) {
  return !Number.isFinite(previous)
    || Math.abs(previous - next) > SOC_ANIMATION_EPSILON;
}

function setBatteryFill(id, soc) {
  const el = $(id);
  if (!el) return;
  const clampedSoc = normalizeSoc(soc);
  const fillScale = clampedSoc / 100;
  const previousSoc = Number(
    typeof el.getAttribute === "function"
      ? el.getAttribute("data-soc-current")
      : undefined
  );
  const hasPreviousSoc = Number.isFinite(previousSoc);
  const shouldAnimate = hasPreviousSoc && socValuesDiffer(previousSoc, clampedSoc);

  el.setAttribute("width", "42");
  el.setAttribute("data-soc-target", String(clampedSoc));
  el.classList.toggle("low", clampedSoc < 20);
  el.classList.toggle("full", clampedSoc >= 90);

  if (!shouldAnimate) {
    el.style.transition = "none";
    el.style.transform = `scaleX(${fillScale})`;
    el.setAttribute("data-soc-current", String(clampedSoc));
    afterNextPaint(() => {
      el.style.transition = "";
    });
    return;
  }

  afterNextPaint(() => {
    el.style.transform = `scaleX(${fillScale})`;
    el.setAttribute("data-soc-current", String(clampedSoc));
  });
}

function renderRules(rules) {
  const list = $("rulesList");
  const labels = [
    ["ems_enabled", "EMS enabled", "rule"],
    ["soc_limit_active", "SOC limit", "warning"],
    ["output_limit_active", "Output limit", "charge"],
    ["winter_soc_mode", "Winter mode", "battery"],
    ["full_charge_assist_active", "Full-charge assist", "charge"],
    ["pv_priority_balancing", "PV priority", "solar"],
    ["battery_balancing", "Battery balance", "battery"],
    ["night_min_soc_idle", "Night idle", "gauge"],
    ["offline_devices", "Offline devices", "warning"],
  ];

  list.innerHTML = "";
  labels.forEach(([key, label, iconName]) => {
    const rule = rules[key] || { active: false, reason: "inactive" };
    const row = document.createElement("div");
    row.className = `rule-row ${rule.active ? "active" : ""}`;
    row.innerHTML = `
      <span class="rule-icon" aria-hidden="true">${icon(iconName)}</span>
      <span>
        <span class="rule-title">${label}</span>
        <span class="rule-reason">${rule.active ? "active" : "inactive"} - ${escapeHtml(rule.reason || "")}</span>
      </span>
    `;
    list.appendChild(row);
  });
}

function renderDevices(devices) {
  const grid = $("deviceGrid");
  const previousSocWidths = readDeviceSocFillWidths(grid);
  grid.innerHTML = "";
  const entries = normalizeDeviceEntries(devices);
  const activeDeviceNames = new Set(entries.map(([name]) => name));

  entries.forEach(([name, device]) => {
    const card = document.createElement("article");
    card.className = "device-card";
    const soc = clamp(deviceSoc(device), 0, 100);
    const safeDeviceKey = escapeHtml(name || "Unknown");
    const previousSocFromState = state.deviceSocValues.get(name);
    const previousSocFromDom = previousSocWidths.has(name)
      ? previousSocWidths.get(name)
      : previousSocWidths.get(safeDeviceKey);
    const previousKnownSoc = Number.isFinite(previousSocFromState)
      ? previousSocFromState
      : previousSocFromDom;
    const shouldAnimateSoc = Number.isFinite(previousKnownSoc)
      && socValuesDiffer(previousKnownSoc, soc);
    const previousSoc = Number.isFinite(previousKnownSoc) ? previousKnownSoc : soc;
    const batteryFlow = normalizeBatteryPowerForDisplay(device.battery_power_w);
    const deviceBatteryState = batteryStateLabel(batteryFlow);
    const socClass = soc < 20 ? "low" : soc >= 90 ? "full" : "";
    card.innerHTML = `
      <div class="device-head">
        <span class="device-name">${escapeHtml(name)}</span>
        <span class="pill ${device.online ? "" : "muted"}">${icon(device.online ? "live" : "warning")}${device.online ? "Online" : "Offline"}</span>
      </div>
      <div class="soc-block ${socClass}" aria-label="Battery state of charge">
        <div class="soc-row">
          <span class="soc-title">${icon("battery")} Battery SOC</span>
          <strong class="soc-percent">${pct(soc)}</strong>
        </div>
        <div class="soc-bar"><div class="soc-fill" data-device-soc-fill="${safeDeviceKey}" data-soc-start="${previousSoc}" data-soc-target="${soc}" data-soc-animate="${shouldAnimateSoc ? "true" : "false"}"></div></div>
        <div class="soc-mode">${deviceBatteryState} ${signedWatts(batteryFlow.valueW)}</div>
      </div>
      <div class="device-values">
        ${deviceValue("PV", watts(devicePvPower(device)), "solar")}
        ${deviceValue("Output", watts(deviceOutputPower(device)), "inverter")}
        ${deviceValue("Battery", signedWatts(batteryFlow.valueW), batteryFlow.isCharging ? "charge" : "battery")}
        ${deviceValue("Target", watts(device.target_w), "gauge")}
        ${deviceValue("Limit", watts(device.output_limit_w), "warning")}
      </div>
      ${renderDeviceFirmwareStatus(device)}
      ${renderFullChargeAssist(device)}
    `;
    grid.appendChild(card);
    state.deviceSocValues.set(name, soc);
  });

  state.deviceSocValues.forEach((_, name) => {
    if (!activeDeviceNames.has(name)) {
      state.deviceSocValues.delete(name);
    }
  });

  if (!entries.length) {
    grid.innerHTML = `<article class="device-card"><span class="device-label">Waiting for EMS telemetry</span></article>`;
  }

  applyDeviceSocFillStarts(grid);
  animateDeviceSocFills(grid);
}

function readDeviceSocFillWidths(grid) {
  const widths = new Map();
  if (!grid || typeof grid.querySelectorAll !== "function") {
    return widths;
  }

  grid.querySelectorAll("[data-device-soc-fill]").forEach((el) => {
    const key = el.getAttribute("data-device-soc-fill");
    const target = Number(el.getAttribute("data-soc-target"));
    if (key && Number.isFinite(target)) {
      widths.set(key, clamp(target, 0, 100));
    }
  });
  return widths;
}

function applyDeviceSocFillStarts(grid) {
  if (!grid || typeof grid.querySelectorAll !== "function") {
    return;
  }

  grid.querySelectorAll("[data-device-soc-fill][data-soc-start]").forEach((el) => {
    const start = Number(el.getAttribute("data-soc-start"));
    if (Number.isFinite(start)) {
      el.style.width = `${clamp(start, 0, 100)}%`;
    }
  });
}

function animateDeviceSocFills(grid) {
  if (!grid || typeof grid.querySelectorAll !== "function") {
    return;
  }

  const fills = Array.from(
    grid.querySelectorAll('[data-device-soc-fill][data-soc-target][data-soc-animate="true"]')
  );
  if (!fills.length) return;

  afterNextPaint(() => {
    fills.forEach((el) => {
      const target = Number(el.getAttribute("data-soc-target"));
      if (Number.isFinite(target)) {
        el.style.width = `${clamp(target, 0, 100)}%`;
      }
    });
  });
}

function renderEnergyStats(stats) {
  const container = $("energyStats");
  if (!container) return;

  if (!stats) {
    container.innerHTML = `<div class="energy-empty control-empty compact">Energy statistics not available yet.</div>`;
    return;
  }

  if (stats.enabled === false) {
    container.innerHTML = `<div class="energy-empty control-empty compact">Energy statistics are disabled.</div>`;
    return;
  }

  const currency = stats.currency || "EUR";
  const monthly = normalizeMonthlyEnergy(stats.monthly_current_year);
  const yearly = normalizeYearlyEnergy(stats.yearly);
  const lifetime = stats.lifetime || {};
  const hasCollectedStats = Boolean(lifetime.since_date) || [
    stats.today,
    stats.yesterday,
    stats.last_7_days,
    stats.last_4_weeks,
    stats.last_12_months,
    stats.best_day,
    lifetime,
    ...monthly,
    ...yearly,
  ].some((item) => energyKwh(item) > 0);

  if (!hasCollectedStats) {
    container.innerHTML = `<div class="energy-empty control-empty compact">Waiting for the first measured inverter output sample.</div>`;
    return;
  }

  const periods = [
    energyPeriodStage("Today", stats.today, currency, {
      kind: "today",
      subtitle: "Current day output",
    }),
    energyPeriodStage("Yesterday", stats.yesterday, currency, {
      kind: "yesterday",
      subtitle: "Previous day output",
    }),
    energyPeriodStage("Last 7 Days", stats.last_7_days, currency, {
      kind: "week",
      subtitle: "Rolling week total",
    }),
    energyPeriodStage("Last 4 Weeks", stats.last_4_weeks, currency, {
      kind: "month",
      subtitle: "Rolling 28-day output",
    }),
    energyPeriodStage("Last 12 Months", stats.last_12_months, currency, {
      kind: "year",
      subtitle: "Rolling annual total",
    }),
    energyPeriodStage("Best Day", stats.best_day, currency, {
      kind: "best",
      subtitle: "Highest measured day",
      detailLabel: "Date",
      detailValue: stats.best_day?.date ? formatEnergyDate(stats.best_day.date) : null,
    }),
  ].join("");

  container.innerHTML = `
    <div class="energy-report-board">
      <section class="energy-stage-row energy-kpi-row" aria-label="Energy period overview">
        <div class="energy-period-pipeline energy-kpi-grid">${periods}</div>
      </section>
      ${energyContextRail(stats, monthly, yearly, lifetime, currency)}
      ${energyReportSection("Monthly Summary", "Current calendar year delivered output", `
      <div class="energy-month-grid">
        ${monthly.map((month) => energyMonthCard(month, currency)).join("")}
      </div>
      `)}
      ${energyReportSection("Yearly Summary", "Calendar-year totals from daily aggregates", `
      <div class="energy-year-grid">
        ${yearly.map((year, index) => energyYearCard(year, currency, { latest: index === yearly.length - 1 })).join("")}
      </div>
      `)}
      <section class="energy-report-section energy-lifetime-section">
        ${energyLifetimeCard(lifetime, currency)}
      </section>
    </div>
  `;
}

function energyKpiCard(label, values, currency, options = {}) {
  return energyPeriodStage(label, values, currency, options);
}

function energyPeriodStage(label, values, currency, options = {}) {
  const detail = options.detailValue
    ? energyFact(options.detailLabel || "Detail", options.detailValue, "history", "neutral")
    : "";

  return `
    <article class="energy-period-stage energy-kpi-card energy-period-${escapeHtml(options.kind || "period")}">
      <div class="energy-stage-head">
        <div class="energy-stage-title-block">
          <h3 class="energy-stage-title">${escapeHtml(label)}</h3>
          ${options.subtitle ? `<span class="energy-stage-subtitle">${escapeHtml(options.subtitle)}</span>` : ""}
        </div>
      </div>
      <div class="energy-stage-values">
        ${energyFact("Energy", formatEnergyKwh(values), "inverter", "output")}
        ${energyFact("Savings", formatSavings(values, currency), "charge", "savings")}
        ${detail}
      </div>
    </article>
  `;
}

function energyMonthCard(month, currency) {
  const isZero = energyKwh(month) <= 0;
  const isCurrent = Number(month.month) === new Date().getMonth() + 1;

  return energySummaryCard({
    title: month.label || monthName(month.month),
    subtitle: isCurrent ? "Current month" : "Month total",
    values: month,
    currency,
    className: `energy-month-card ${isZero ? "energy-zero" : ""}`,
    current: isCurrent,
  });
}

function energyYearCard(year, currency, options = {}) {
  const currentYear = new Date().getFullYear();
  const isCurrent = Number(year.year) === currentYear;
  const isLatest = Boolean(options.latest);

  return energySummaryCard({
    title: year.year || "--",
    subtitle: isCurrent ? "Current year" : isLatest ? "Latest year" : "Calendar year",
    values: year,
    currency,
    className: "energy-year-card",
    current: isCurrent || isLatest,
  });
}

function energyLifetimeCard(values, currency) {
  return energySummaryCard({
    title: "Result / Lifetime",
    subtitle: "All stored daily totals",
    values,
    currency,
    className: "energy-lifetime-card",
    details: values?.since_date
      ? [{ label: "Date", value: formatEnergyDate(values.since_date) }]
      : [],
  });
}

function energySummaryCard({ title, subtitle, values, currency, className = "", current = false, details = [] }) {
  const detailFacts = details
    .filter((detail) => detail?.value)
    .map((detail) => energyFact(detail.label || "Detail", detail.value, detail.iconName || "history", detail.tone || "neutral"))
    .join("");

  return `
    <article class="energy-summary-card ${escapeHtml(className)} ${current ? "energy-current" : ""}">
      <div class="energy-summary-head">
        <h4>${escapeHtml(title)}</h4>
        ${subtitle ? `<span>${escapeHtml(subtitle)}</span>` : ""}
      </div>
      <div class="energy-summary-values">
        ${energyFact("Energy", formatEnergyKwh(values), "inverter", "output")}
        ${energyFact("Savings", formatSavings(values, currency), "charge", "savings")}
        ${detailFacts}
      </div>
    </article>
  `;
}

function energyFact(label, value, iconName = "rule", tone = "") {
  return `
    <span class="energy-fact ${tone ? `role-${escapeHtml(tone)}` : ""}">
      <span class="value-icon" aria-hidden="true">${icon(iconName)}</span>
      <span class="energy-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </span>
  `;
}

function energyContextRail(stats, monthly, yearly, lifetime, currency) {
  return `
    <aside class="energy-context-rail" aria-label="Energy statistics context">
      <div class="energy-context-title">Context</div>
      <div class="energy-context-items">
        ${energyContextItem("Currency", currency || "EUR", "rule")}
        ${energyContextItem("Months", String(monthly.length), "history")}
        ${energyContextItem("With data", String(monthly.filter((month) => energyKwh(month) > 0).length), "gauge")}
        ${energyContextItem("Years", energyYearRange(yearly), "history")}
        ${stats.best_day?.date ? energyContextItem("Best Day", formatEnergyDate(stats.best_day.date), "charge") : ""}
        ${energyContextItem("Lifetime", formatEnergyKwh(lifetime), "inverter")}
      </div>
    </aside>
  `;
}

function energyContextItem(label, value, iconName = "rule") {
  if (value === undefined || value === null || value === "") return "";
  return `
    <span class="energy-context-item">
      <span class="value-icon" aria-hidden="true">${icon(iconName)}</span>
      <span class="energy-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </span>
  `;
}

function energyReportSection(title, subtitle, content) {
  return `
    <section class="energy-report-section energy-subsection">
      <div class="energy-report-section-head">
        <h3 class="energy-report-section-title energy-section-title">${escapeHtml(title)}</h3>
        <span class="energy-report-section-subtitle">${escapeHtml(subtitle)}</span>
      </div>
      ${content}
    </section>
  `;
}

function energyYearRange(yearly) {
  if (!yearly.length) return "None";
  if (yearly.length === 1) return String(yearly[0].year);
  return `${yearly[0].year}-${yearly[yearly.length - 1].year}`;
}

function normalizeMonthlyEnergy(months) {
  const byMonth = new Map((Array.isArray(months) ? months : []).map((item) => [Number(item.month), item]));
  return Array.from({ length: 12 }, (_, index) => {
    const month = index + 1;
    return {
      month,
      label: monthName(month),
      ...(byMonth.get(month) || {}),
    };
  });
}

function normalizeYearlyEnergy(years) {
  return (Array.isArray(years) ? years : [])
    .filter((item) => item && item.year !== undefined && item.year !== null)
    .sort((a, b) => Number(a.year) - Number(b.year))
    .slice(-8);
}

function monthName(month) {
  const labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return labels[Number(month) - 1] || "--";
}

function energyKwh(values) {
  const kwh = Number(values?.inverter_output_kwh);
  if (Number.isFinite(kwh)) return kwh;
  const wh = Number(values?.inverter_output_wh);
  if (Number.isFinite(wh)) return wh / 1000;
  return 0;
}

function formatEnergyKwh(values) {
  const value = energyKwh(values);
  const digits = Math.abs(value) >= 1000 ? 0 : 1;
  return `${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} kWh`;
}

function formatSavings(values, currency) {
  const value = Number(values?.savings_value);
  if (!Number.isFinite(value)) return "--";
  const code = String(currency || "EUR").toUpperCase();
  if (code === "EUR") return `€${value.toFixed(2)}`;
  return `${value.toFixed(2)} ${code}`;
}

function formatEnergyDate(value) {
  if (!value) return "";
  const raw = String(value);
  const dateOnly = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dateOnly) return `${dateOnly[1]}-${dateOnly[2]}-${dateOnly[3]}`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toISOString().slice(0, 10);
}

function renderDeviceFlow(snapshotOrDevices) {
  const container = $("deviceFlowView");
  if (!container) return;

  const snapshot = snapshotOrDevices && (
    snapshotOrDevices.devices !== undefined
    || snapshotOrDevices.home_load_w !== undefined
    || snapshotOrDevices.grid_power_w !== undefined
  )
    ? snapshotOrDevices
    : { devices: snapshotOrDevices || {} };
  const devices = snapshot.devices || {};
  const entries = normalizeDeviceEntries(devices);
  if (!entries.length) {
    container.innerHTML = `<div class="device-flow-empty">No per-device telemetry available.</div>`;
    state.deviceFlowSignature = null;
    return;
  }

  const layout = DEVICE_FLOW_LAYOUT;
  const rowsBottomY = layout.firstRowY + ((entries.length - 1) * layout.rowHeight) + layout.batteryOffsetY + 76;
  const rowsCenterY = (layout.firstRowY + rowsBottomY) / 2;
  const homeY = rowsCenterY - layout.sharedVisualHeight / 2;
  const gridY = homeY + layout.sharedHomeGridGapY;
  const viewHeight = Math.max(rowsBottomY, gridY + layout.sharedVisualHeight) + layout.rowBottomPadding;
  const homeLoad = Number(snapshot.home_load_w || 0);
  const gridPower = Number(snapshot.grid_power_w || 0);
  const signature = deviceFlowSignature(entries, layout, viewHeight);
  if (
    state.deviceFlowSignature === signature
    && typeof container.querySelector === "function"
    && container.querySelector("[data-device-flow-root]")
  ) {
    updateDeviceFlowSnapshot(container, snapshot, entries);
    animateDeviceFlowIfVisible(container);
    return;
  }

  const previousBatteryScales = readDeviceBatteryFillScales(container);
  const rows = entries
    .map(([name, device], index) => {
      const key = String(index);
      const previousScale = previousBatteryScales.has(key)
        ? previousBatteryScales.get(key)
        : null;
      return deviceFlowRow(
        name,
        device || {},
        layout.firstRowY + index * layout.rowHeight,
        layout,
        homeY,
        index,
        previousScale
      );
    })
    .join("");

  container.innerHTML = `
    <svg class="device-flow-svg" data-device-flow-root viewBox="0 0 ${layout.width} ${viewHeight}" role="img" aria-label="Per-device PV, battery, inverter, home and grid energy flow">
      <g class="device-flow-layer" aria-hidden="true">
        ${rows}
      </g>
      ${deviceSharedVisuals(layout.sharedX, homeY, gridY, homeLoad, gridPower)}
    </svg>
  `;
  state.deviceFlowSignature = signature;
  applyDeviceFlowInitialStyles(container);
  animateDeviceFlowIfVisible(container);
}

function animateDeviceFlowIfVisible(container) {
  if (state.flowView === "devices" && !container.hidden) {
    pendingDeviceFlowBatteryAnimation = false;
    animateDeviceBatteryFills(container);
  } else {
    pendingDeviceFlowBatteryAnimation = true;
  }
}

function deviceFlowSignature(entries, layout, viewHeight) {
  const names = entries.map(([name]) => String(name || "")).join("|");
  return `${layout.width}:${viewHeight}:${entries.length}:${names}`;
}

function readDeviceBatteryFillScales(container) {
  const scales = new Map();
  if (!container || typeof container.querySelectorAll !== "function") {
    return scales;
  }

  container.querySelectorAll("[data-device-battery-fill]").forEach((el) => {
    const key = el.getAttribute("data-device-battery-fill");
    const target = Number(el.getAttribute("data-battery-fill-target"));
    if (key !== null && Number.isFinite(target)) {
      scales.set(key, clamp(target, 0, 1));
    }
  });
  return scales;
}

function animateDeviceBatteryFills(container) {
  if (!container || typeof container.querySelectorAll !== "function") {
    return;
  }

  const fills = Array.from(container.querySelectorAll("[data-battery-fill-target]"));
  if (!fills.length) return;

  const applyTargets = () => {
    fills.forEach((el) => {
      const target = Number(el.getAttribute("data-battery-fill-target"));
      if (Number.isFinite(target)) {
        el.style.transform = `scaleX(${clamp(target, 0, 1)})`;
      }
    });
  };

  afterNextPaint(applyTargets);
}

function applyDeviceFlowInitialStyles(container) {
  if (!container || typeof container.querySelectorAll !== "function") {
    return;
  }

  container.querySelectorAll("[data-flow-speed]").forEach((el) => {
    applyPipeStyleBucket(el, el.getAttribute("data-flow-speed") || "idle");
  });

  container.querySelectorAll("[data-battery-fill-start]").forEach((el) => {
    const start = Number(el.getAttribute("data-battery-fill-start"));
    if (Number.isFinite(start)) {
      el.style.transform = `scaleX(${clamp(start, 0, 1)})`;
    }
  });
}

function dataElementMap(container, attribute) {
  const result = new Map();
  if (!container || typeof container.querySelectorAll !== "function") return result;
  container.querySelectorAll(`[${attribute}]`).forEach((el) => {
    const key = el.getAttribute(attribute);
    if (key !== null) result.set(key, el);
  });
  return result;
}

function setMappedText(map, key, value) {
  const el = map.get(key);
  if (el) el.textContent = value;
}

function setSvgClass(el, className) {
  if (!el) return;
  if (typeof el.setAttribute === "function") {
    el.setAttribute("class", className);
  } else {
    el.className = className;
  }
}

function updateDevicePipeElement(el, key, kind, value, direction = "forward") {
  if (!el) return;
  const wattsValue = Math.abs(Number(value || 0));
  const active = flowActive(`device:${key}:${kind}`, wattsValue);
  const speedBucket = flowSpeedBucket(wattsValue, active);
  setSvgClass(el, devicePipeClass(kind, active, direction, speedBucket));
  applyPipeStyleBucket(el, speedBucket);
}

function updateDeviceFlowSnapshot(container, snapshot, entries) {
  const texts = dataElementMap(container, "data-flow-text");
  const visuals = dataElementMap(container, "data-flow-visual");
  const pipes = dataElementMap(container, "data-flow-pipe");
  const fills = dataElementMap(container, "data-device-battery-fill");
  const homeLoad = Number(snapshot.home_load_w || 0);
  const gridPower = Number(snapshot.grid_power_w || 0);

  entries.forEach(([name, device], index) => {
    const key = deviceFlowKey(name, index);
    const pvPower = devicePvPower(device);
    const outputPower = deviceOutputPower(device);
    const batteryFlow = normalizeBatteryPowerForDisplay(device?.battery_power_w);
    const soc = clamp(deviceSoc(device), 0, 100);
    const fill = fills.get(String(index));

    updateDevicePipeElement(pipes.get(`${key}:pv`), key, "pv", pvPower);
    updateDevicePipeElement(pipes.get(`${key}:battery`), key, "battery", batteryFlow.absW, batteryPipeDirection(batteryFlow));
    updateDevicePipeElement(pipes.get(`${key}:output`), key, "output", outputPower);
    setSvgClass(visuals.get(`${key}:pv`), deviceVisualClasses("solar-visual", flowActive(`device:${key}:visualPv`, pvPower)));
    setSvgClass(visuals.get(`${key}:battery`), deviceVisualClasses("battery-visual", flowActive(`device:${key}:visualBattery`, batteryFlow.absW), batteryFlow.state));
    setSvgClass(visuals.get(`${key}:inverter`), deviceVisualClasses("inverter-visual", flowActive(`device:${key}:visualInverter`, outputPower)));

    setMappedText(texts, `${key}:pv-label`, `${name || "Unknown"} PV`);
    setMappedText(texts, `${key}:pv-value`, watts(pvPower));
    setMappedText(texts, `${key}:inverter-label`, name || "Unknown");
    setMappedText(texts, `${key}:inverter-value`, watts(outputPower));
    setMappedText(texts, `${key}:battery-state`, batteryStateLabel(batteryFlow));
    setMappedText(texts, `${key}:battery-value`, signedWatts(batteryFlow.valueW));
    setMappedText(texts, `${key}:battery-soc`, pct(soc));

    if (fill) {
      const fillScale = soc / 100;
      fill.setAttribute("data-battery-fill-target", String(fillScale));
      setSvgClass(fill, `battery-fill${soc < 20 ? " low" : soc >= 90 ? " full" : ""}`);
    }
  });

  const gridActive = flowActive("device:shared:visualGrid", Math.abs(gridPower));
  updateDevicePipeElement(pipes.get("shared:grid"), "shared", "grid", Math.abs(gridPower), gridPower < -FLOW_THRESHOLD_W ? "reverse" : "forward");
  setSvgClass(visuals.get("shared:home"), deviceVisualClasses("home-visual", flowActive("device:shared:visualHome", homeLoad)));
  setSvgClass(visuals.get("shared:grid"), deviceVisualClasses("grid-visual", gridActive, gridPower > FLOW_THRESHOLD_W ? "importing" : gridPower < -FLOW_THRESHOLD_W ? "exporting" : "neutral"));
  setMappedText(texts, "shared:home-value", watts(homeLoad));
  setMappedText(texts, "shared:grid-state", gridDirectionLabel(gridPower));
  setMappedText(texts, "shared:grid-value", watts(gridPower));
}

function renderControlExplain(snapshot, options = {}) {
  const container = $("controlExplainView");
  if (!container) return;

  if (!ensureControlExplainShell(container)) {
    container.innerHTML = `
      <div class="control-decision-board">
        ${runtimeControlPanel()}
        ${controlExplainHtml(snapshot)}
      </div>
    `;
    return;
  }

  if (options.forceRuntimeEditor || !isRuntimeEditorEditing()) {
    renderRuntimeEditorMount();
  }
  renderControlExplainMount(snapshot);
}

function ensureControlExplainShell(container) {
  if (!container.querySelector) return false;
  if (
    container.querySelector("#runtimeEditorMount")
    && container.querySelector("#controlExplainMount")
  ) {
    return true;
  }

  container.innerHTML = `
    <div class="control-decision-board">
      <div id="runtimeEditorMount"></div>
      <div id="controlExplainMount"></div>
    </div>
  `;
  return Boolean(
    container.querySelector("#runtimeEditorMount")
    && container.querySelector("#controlExplainMount")
  );
}

function renderRuntimeEditorMount() {
  const mount = $("runtimeEditorMount");
  if (!mount) return;
  mount.innerHTML = runtimeControlPanel();
}

function renderControlExplainMount(snapshot) {
  const mount = $("controlExplainMount");
  if (!mount) return;
  mount.innerHTML = controlExplainHtml(snapshot);
}

function controlExplainHtml(snapshot) {
  const explain = snapshot?.control_explain;
  if (!explain || typeof explain !== "object") {
    return `<div class="control-empty">No control explanation data available yet.</div>`;
  }

  const notes = Array.isArray(explain.notes)
    ? explain.notes.filter(hasExplainValue)
    : [];
  const devices = normalizeControlDeviceEntries(explain.devices);
  const weightContext = controlWeightContext(devices);
  const deviceFlows = devices.length
    ? devices.map(([name, device]) => controlDeviceCard(name, device || {}, explain, weightContext)).join("")
    : `<div class="control-empty compact">No device explanation data available.</div>`;

  return `
    ${controlGlobalPipeline(explain, devices, snapshot)}
    ${controlContextRail(explain, devices, notes)}
    <div class="control-device-list">${deviceFlows}</div>
  `;
}

function controlGlobalPipeline(explain, devices, snapshot) {
  const writeSummary = controlGlobalWriteSummary(devices, explain);
  const stages = [
    {
      title: "Measurements",
      kind: "measurements",
      subtitle: "Live values define the demand basis",
      facts: [
        controlPipelineFact("Filtered load", explain.filtered_load_w, "home", watts, "output"),
        controlPipelineFact("PV total", snapshot?.pv_total_w, "solar", watts, "solar"),
        controlPipelineFact("Output total", snapshot?.inverter_output_w, "inverter", watts, "output"),
      ],
      resultLabel: "Demand basis",
      resultValue: explain.filtered_load_w,
      resultFormatter: watts,
    },
    {
      title: "Target",
      kind: "target",
      subtitle: "Request and limits become the effective target",
      facts: [
        controlPipelineFact("Requested", explain.requested_total_w, "gauge", watts, "output"),
        controlPipelineFact("Strategy", explain.mode, "rule", controlReason, "context"),
      ],
      resultLabel: "Effective target",
      resultValue: explain.effective_target_total_w,
      resultFormatter: watts,
    },
    {
      title: "Distribution",
      kind: "distribution",
      subtitle: "The target is allocated across devices",
      facts: [
        controlPipelineFact("Target split", controlDeviceTargetSummary(devices, (device) => device.allocated_target_w), "rule", controlText, "output"),
        controlPipelineFact("Undistributed", explain.undistributed_target_w, "warning", watts, "neutral"),
      ],
      resultLabel: "Allocated total",
      resultValue: explain.allocated_target_total_w,
      resultFormatter: watts,
    },
    {
      title: "Limits / Gates",
      kind: "gates",
      subtitle: "Limits and write gates shape commandable power",
      facts: [
        controlPipelineFact("Active limits", controlActiveLimitSummary(explain.limits), "warning", controlText, "neutral"),
        controlPipelineFact("Write gate", writeSummary.label, writeSummary.icon, controlText, "neutral", writeSummary.tone),
      ],
      resultLabel: "Commandable total",
      resultValue: controlCommandableTotal(explain),
      resultFormatter: watts,
    },
    {
      title: "Commands",
      kind: "commands",
      subtitle: "Command state decides whether writes are needed",
      facts: [
        controlPipelineFact("Commanded", explain.commanded_total_w, "rule", watts, "output"),
        controlPipelineFact("Writes", controlWriteDecisionSummary(devices, explain), writeSummary.icon, controlText, "neutral", writeSummary.tone),
      ],
      resultLabel: "Command decision",
      resultValue: writeSummary.label,
      resultFormatter: controlText,
      resultTone: writeSummary.tone === "blocked" ? "blocked" : "",
    },
    {
      title: "Result",
      kind: "result",
      subtitle: "Final targets become the active control state",
      facts: [
        controlPipelineFact("Final split", controlDeviceTargetSummary(devices, controlFinalTarget), "charge", controlText, "output"),
      ],
      resultLabel: "Final total",
      resultValue: controlFinalTotalTarget(explain),
      resultFormatter: watts,
    },
  ];
  const renderedStages = stages
    .map((stage, index) => controlPipelineStage({ ...stage, step: index + 1 }))
    .join("");

  if (!renderedStages) return "";

  return `
    <section class="control-stage-row control-global-row" aria-label="Global control decision pipeline">
      <div class="control-global-pipeline">
        ${renderedStages}
      </div>
    </section>
  `;
}

function controlPipelineStage({ title, kind, subtitle, step, facts, resultLabel, resultValue, resultFormatter = controlText, resultTone = "" }) {
  const content = facts.filter(Boolean).join("");

  return `
    <section class="control-pipeline-stage control-pipeline-${escapeHtml(kind)}">
      <div class="control-stage-head control-stage-header">
        <div class="control-stage-kicker">
          ${controlStageStep(step)}
          <span class="control-stage-dot" aria-hidden="true">${icon(controlStageIcon(kind))}</span>
        </div>
        <div class="control-stage-title-block">
          <h3 class="control-stage-title">${escapeHtml(title)}</h3>
          ${controlStageSubtitle(subtitle)}
        </div>
      </div>
      <div class="control-stage-body control-pipeline-values">${content}</div>
      ${controlResult(resultLabel, resultValue, "charge", resultFormatter, resultTone)}
    </section>
  `;
}

function controlStageSubtitle(subtitle) {
  if (!hasExplainValue(subtitle)) return "";
  return `<span class="control-stage-subtitle">${escapeHtml(subtitle)}</span>`;
}

function controlStageStep(step) {
  if (!hasExplainValue(step)) return "";
  return `<span class="control-stage-step">${String(step).padStart(2, "0")}</span>`;
}

function controlPipelineFact(label, value, iconName = "rule", formatter = controlText, role = "input", tone = "") {
  if (!hasExplainValue(value)) return "";
  return `
    <span class="control-pipeline-fact role-${escapeHtml(role)} ${tone ? `tone-${escapeHtml(tone)}` : ""}">
      <span class="value-icon" aria-hidden="true">${icon(iconName)}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatter(value))}</strong>
    </span>
  `;
}

function controlContextRail(explain, devices, notes) {
  const limits = Array.isArray(explain.limits) ? explain.limits : [];
  const contextItems = [
    controlContextItem("Mode", explain.mode, "rule", controlText),
    controlContextItem("Max power", explain.max_total_power_w, "warning", watts),
    controlContextItem("Min output", explain.min_output_limit_w, "warning", watts),
    controlContextItem("Undistributed", explain.undistributed_target_w, "warning", watts),
    controlContextItem("Devices", devices.length, "inverter", controlText),
    controlContextItem("Active gates", controlLimitNames(limits, true), "charge", controlText),
    controlContextItem("Inactive gates", controlLimitNames(limits, false), "rule", controlText),
  ].filter(Boolean).join("");
  const noteItems = notes
    .map((note) => `<span class="control-note">${escapeHtml(controlText(note))}</span>`)
    .join("");

  if (!contextItems && !noteItems) return "";

  return `
    <aside class="control-context-rail" aria-label="Control configuration context">
      <div class="control-context-title">Context</div>
      <div class="control-context-items">${contextItems}${noteItems}</div>
    </aside>
  `;
}

function controlContextItem(label, value, iconName = "rule", formatter = controlText) {
  if (!hasExplainValue(value)) return "";
  return `
    <span class="control-context-item role-config">
      <span class="value-icon" aria-hidden="true">${icon(iconName)}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatter(value))}</strong>
    </span>
  `;
}

function controlLimitNames(limits, active) {
  const names = (Array.isArray(limits) ? limits : [])
    .filter((limit) => limit && Boolean(limit.active) === active && hasExplainValue(limit.name))
    .map((limit) => controlReason(limit.name));
  return names.length ? names.join(", ") : null;
}

function controlActiveLimitSummary(limits) {
  return controlLimitNames(limits, true) || "None";
}

function controlDeviceTargetSummary(devices, targetResolver) {
  const parts = devices
    .map(([name, device]) => {
      const target = targetResolver(device || {});
      if (!hasExplainValue(target)) return "";
      return `${device?.device || name}: ${watts(target)}`;
    })
    .filter(Boolean);
  return parts.length ? parts.join(" / ") : null;
}

function controlFinalTotalTarget(explain) {
  return firstExplainValue(
    explain.final_target_total_w,
    explain.effective_target_total_w,
    explain.allocated_target_total_w,
    explain.commanded_total_w,
    explain.requested_total_w
  );
}

function controlCommandableTotal(explain) {
  return firstExplainValue(
    explain.commandable_total_w,
    explain.adjusted_commandable_total_w,
    explain.effective_command_total_w,
    explain.commanded_total_w,
    explain.allocated_target_total_w,
    explain.effective_target_total_w
  );
}

function controlWriteDecisionSummary(devices, explain) {
  if (!devices.length) return null;
  const counts = devices.reduce((result, [, device]) => {
    const decision = controlDeviceWriteDecision(device || {}, explain);
    result[decision.label] = (result[decision.label] || 0) + 1;
    return result;
  }, {});
  return Object.entries(counts)
    .map(([label, count]) => `${label} ${count}`)
    .join(" / ");
}

function normalizeControlDeviceEntries(devices) {
  if (Array.isArray(devices)) {
    return devices.map((device, index) => [deviceName(device, index), device || {}]);
  }
  return Object.entries(devices || {}).map(([name, device]) => [name || deviceName(device, 0), device || {}]);
}

function controlDeviceCard(name, device, explain, weightContext) {
  const safeName = escapeHtml(device.device || name || "Device");
  const writeDecision = controlDeviceWriteDecision(device, explain);
  const reasonNote = controlDeviceReasonNote(safeName, device, explain, writeDecision);
  const online = device.online;
  const onlinePill = hasExplainValue(online)
    ? `<span class="pill ${online ? "" : "muted"}">${icon(online ? "live" : "warning")}${online ? "Online" : "Offline"}</span>`
    : "";

  return `
    <article class="control-device-card" data-control-device="${safeName}">
      <div class="control-device-head">
        <div>
          <span class="device-name">${safeName}</span>
        </div>
        <div class="control-device-status">
          ${onlinePill}
          <span class="control-write-pill tone-${escapeHtml(writeDecision.tone)}">${escapeHtml(writeDecision.label)}</span>
        </div>
      </div>
      ${reasonNote}
      <div class="control-device-panels">
        ${controlDeviceMeasurementPanel(device)}
        ${controlDeviceContextPanel(device)}
      </div>
      ${controlDeviceDecisionFlow(device, explain, weightContext, writeDecision)}
    </article>
  `;
}

function controlDeviceReasonNote(name, device, explain, writeDecision) {
  const reason = controlDeviceDecisionReason(name, device, explain, writeDecision);
  const tone = writeDecision.tone === "blocked"
    ? "blocked"
    : writeDecision.tone === "warn"
      ? "warn"
      : "neutral";

  return `
    <div class="control-device-reason tone-${escapeHtml(tone)}">
      <span class="value-icon" aria-hidden="true">${icon(tone === "blocked" ? "warning" : "rule")}</span>
      <span>${escapeHtml(reason)}</span>
    </div>
  `;
}

function controlDeviceDecisionReason(name, device, explain, writeDecision) {
  const explicitReason = firstExplainValue(device.decision_reason, device.reason);
  if (hasExplainValue(explicitReason)) {
    return controlReadableDecisionReason(explicitReason, name);
  }

  const writeReason = controlReason(firstExplainValue(writeDecision.reason, device.write_reason, ""));
  if (writeDecision.tone === "blocked") {
    return `${name} is blocked because ${writeReason || "the device cannot be controlled"}.`;
  }
  if (writeDecision.tone === "skip" && writeReason.toLowerCase().includes("deadband")) {
    return "Write skipped because the target change is inside the deadband.";
  }

  const limitingReason = firstExplainValue(device.limiting_reason, device.capability_reason);
  if (hasExplainValue(limitingReason)) {
    return `${name} target is limited by ${controlReason(limitingReason)}.`;
  }

  const pvPriority = numericOrNull(device.pv_priority_factor);
  const chargeBalance = numericOrNull(device.charge_balance_multiplier);
  const allocated = numericOrNull(firstExplainValue(device.allocated_target_w, device.effective_target_w));
  const outputLimit = numericOrNull(device.output_limit_w);
  const requestedTotal = numericOrNull(explain.requested_total_w);

  if (pvPriority !== null && pvPriority > 1.05) {
    return `${name} target is reduced because local PV priority keeps more PV available for battery charging.`;
  }
  if (chargeBalance !== null && chargeBalance > 1.05) {
    return `${name} target is adjusted by charge balancing, SOC, PV availability, and configured limits.`;
  }
  if (allocated !== null && outputLimit !== null && allocated >= outputLimit && requestedTotal !== null) {
    return `${name} carries its allocated share up to the configured output limit.`;
  }

  return "Target is distributed according to device weight, SOC, PV availability, and configured limits.";
}

function controlReadableDecisionReason(reason, name) {
  const normalized = String(reason).trim().toLowerCase();
  const map = {
    pv_first_allocation: `${name} target is shaped by PV-first allocation and local solar availability.`,
    pv_priority_applied: `${name} keeps more PV available for charging because local PV priority is active.`,
    remaining_demand_assigned: `${name} carries the remaining house load after PV-priority allocation.`,
    battery_discharge: `${name} supports the load from battery output according to the current target split.`,
    deadband: "Write skipped because the target change is inside the deadband.",
    output_limit_update: `${name} receives a write because the target differs from the current output limit.`,
  };
  return map[normalized] || controlReason(reason);
}

function controlDeviceMeasurementPanel(device) {
  return controlDevicePanel("Measurements", "measurements", [
    controlFact("PV", device.pv_input_w, "solar", watts, "solar"),
    controlFact("SOC", device.soc, "battery", pct, "battery"),
    controlFact("Output", device.output_w, "inverter", watts, "output"),
  ]);
}

function controlDeviceContextPanel(device) {
  return controlDevicePanel("Context", "context", [
    controlSocRange(device),
    controlFact("Output limit", device.output_limit_w, "rule", watts, "config"),
    controlFact("Device max", device.max_output_w, "inverter", watts, "output"),
    controlFact("Capacity", device.capacity_weight, "battery", decimal, "config"),
    controlFact("PV priority", device.pv_priority_factor, "solar", factor, "config"),
  ]);
}

function controlDevicePanel(title, kind, rows) {
  const content = rows.filter(Boolean).join("");
  if (!content) return "";

  return `
    <section class="control-device-panel control-device-panel-${escapeHtml(kind)}">
      <div class="control-stage-head">
        <span class="control-stage-dot" aria-hidden="true">${icon(controlStageIcon(kind))}</span>
        <h3>${escapeHtml(title)}</h3>
      </div>
      <div class="control-stage-values">${content}</div>
    </section>
  `;
}

function controlDeviceDecisionFlow(device, explain, weightContext, writeDecision) {
  const stages = [
    {
      title: "Inputs",
      kind: "measurements",
      subtitle: "Live values from this inverter",
      facts: [
        controlFact("PV", device.pv_input_w, "solar", watts, "solar"),
        controlFact("SOC", device.soc, "battery", pct, "battery"),
        controlFact("Output", device.output_w, "inverter", watts, "output"),
      ],
      resultLabel: "Input state",
      resultValue: device.online === false ? "offline" : "ready",
      resultTone: device.online === false ? "blocked" : "",
    },
    {
      title: "Weighting",
      kind: "weighting",
      subtitle: "PV, SOC and balance produce the weight",
      facts: [
        controlFact("Base weight", controlBaseWeight(device), "rule", decimal, "config"),
        controlFact("PV priority", device.pv_priority_factor, "solar", factor, "solar"),
        controlFact("Charge balance", device.charge_balance_multiplier, "charge", factor, "battery"),
      ],
      resultLabel: "Effective weight",
      resultValue: controlDeviceWeight(device),
      resultFormatter: decimal,
    },
    {
      title: "Raw Target",
      kind: "raw",
      subtitle: "Weight share is applied to the requested target",
      facts: [
        controlFact("Weight", controlDeviceWeight(device), "charge", decimal, "output"),
        controlFact("Share", controlDeviceShare(device, explain, weightContext), "gauge", formatShare, "output"),
        controlFact("Requested", explain.requested_total_w, "gauge", watts, "output"),
        controlFact("Formula", controlRawFormula(device, explain, weightContext), "rule", controlText, "context"),
      ],
      resultLabel: "Raw target",
      resultValue: controlRawTarget(device),
      resultFormatter: watts,
    },
    {
      title: "Adjustments / Limits",
      kind: "limits",
      subtitle: "Limits modify the raw device target",
      facts: [
        controlFact("PV-only limit", device.pv_only_limit_w, "solar", watts, "solar"),
        controlFact("Output limit", device.output_limit_w, "rule", watts, "config"),
        controlFact("Delta", controlAdjustmentDelta(device), "charge", signedWatts, "output"),
        controlFact("Limited by", device.limiting_reason, "warning", controlReason, "warning"),
        controlFact("Capability", device.capability_reason, "warning", controlReason, "warning"),
      ],
      resultLabel: "Adjusted target",
      resultValue: controlAdjustedTarget(device),
      resultFormatter: watts,
      resultTone: device.capability_reason ? "blocked" : "",
    },
    {
      title: "Final Target",
      kind: "final",
      subtitle: "Adjusted target and write gate finish the decision",
      facts: [
        controlFact("Adjusted", controlAdjustedTarget(device), "charge", watts, "output"),
        controlFact("Final", controlFinalTarget(device), "inverter", watts, "output"),
        controlFact("Delta output", controlOutputDelta(device), "charge", signedWatts, "output"),
        controlFact("Write", writeDecision.label, writeDecision.icon, controlText, "neutral", writeDecision.tone),
        controlFact("Reason", writeDecision.reason, writeDecision.icon, controlReason, "neutral", writeDecision.tone),
      ],
      resultLabel: "Final / write",
      resultValue: controlWriteOutput(writeDecision),
      resultFormatter: controlText,
      resultTone: writeDecision.tone === "blocked" ? "blocked" : "",
    },
  ];
  return `
    <div class="control-stage-row control-device-stage-row">
      <div class="control-flow control-device-decision-flow">
        ${stages.map((stage, index) => controlStage({ ...stage, step: index + 1 })).join("")}
      </div>
    </div>
  `;
}

function controlStage({ title, kind, subtitle, step, facts, resultLabel, resultValue, resultFormatter = controlText, resultTone = "" }) {
  const content = facts.filter(Boolean).join("");

  return `
    <section class="control-stage control-stage-${escapeHtml(kind)}" data-stage-title="${escapeHtml(title)}">
      <div class="control-stage-head control-stage-header">
        <div class="control-stage-kicker">
          ${controlStageStep(step)}
          <span class="control-stage-dot" aria-hidden="true">${icon(controlStageIcon(kind))}</span>
        </div>
        <div class="control-stage-title-block">
          <h3 class="control-stage-title">${escapeHtml(title)}</h3>
          ${controlStageSubtitle(subtitle)}
        </div>
      </div>
      <div class="control-stage-body control-stage-values">${content}</div>
      ${controlResult(resultLabel, resultValue, "charge", resultFormatter, resultTone)}
    </section>
  `;
}

function controlStageIcon(kind) {
  const map = {
    inputs: "solar",
    measurements: "solar",
    context: "rule",
    target: "gauge",
    distribution: "charge",
    gates: "warning",
    commands: "rule",
    result: "inverter",
    weighting: "charge",
    share: "gauge",
    raw: "rule",
    limits: "warning",
    final: "inverter",
    write: "live",
  };
  return map[kind] || "rule";
}

function controlFact(label, value, iconName = "rule", formatter = controlText, role = "input", tone = "") {
  if (!hasExplainValue(value)) return "";
  const wideClass = ["Formula", "Reason"].includes(label) ? "control-fact-wide" : "";
  return `
    <span class="control-fact ${wideClass} role-${escapeHtml(role)} ${tone ? `tone-${escapeHtml(tone)}` : ""}">
      <span class="value-icon" aria-hidden="true">${icon(iconName)}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatter(value))}</strong>
    </span>
  `;
}

function controlResult(label, value, iconName = "charge", formatter = controlText, tone = "") {
  const text = hasExplainValue(value) ? formatter(value) : "--";
  return `
    <div class="control-result control-stage-result ${tone ? `tone-${escapeHtml(tone)}` : ""}">
      <span class="value-icon" aria-hidden="true">${icon(iconName)}</span>
      <span class="control-label control-stage-result-label">Result / ${escapeHtml(label)}</span>
      <strong class="control-stage-result-value">${escapeHtml(text)}</strong>
    </div>
  `;
}

function controlSocRange(device) {
  if (!hasExplainValue(device.min_soc) && !hasExplainValue(device.max_soc)) {
    return "";
  }
  const minSoc = hasExplainValue(device.min_soc) ? pct(device.min_soc) : "n/a";
  const maxSoc = hasExplainValue(device.max_soc) ? pct(device.max_soc) : "n/a";
  return controlFact("SOC range", `${minSoc} - ${maxSoc}`, "gauge", controlText, "config");
}

function controlWeightContext(deviceEntries) {
  const weights = deviceEntries.map(([, device]) => numericOrNull(controlDeviceWeight(device)));
  const totalWeight = weights.reduce((total, value) => total + (value || 0), 0);
  return {
    totalWeight: totalWeight > 0 ? totalWeight : null,
    weights,
  };
}

function controlBaseWeight(device) {
  const explicit = firstExplainValue(device.base_weight, device.base_pv_weight);
  if (hasExplainValue(explicit)) return explicit;

  const pvWeight = numericOrNull(device.pv_weight);
  if (pvWeight !== null) {
    const priority = numericOrNull(device.pv_priority_factor) || 1;
    const balance = numericOrNull(device.charge_balance_multiplier) || 1;
    const denominator = priority * balance;
    if (denominator > 0) return pvWeight / denominator;
  }

  return firstExplainValue(device.capacity_weight, device.pv_only_limit_w);
}

function controlDeviceWeight(device) {
  return firstExplainValue(
    device.effective_weight,
    device.pv_weight,
    device.capacity_weight
  );
}

function controlDeviceShare(device, explain, weightContext) {
  const explicit = numericOrNull(firstExplainValue(device.share, device.share_percent));
  if (explicit !== null) {
    return explicit > 1 ? explicit / 100 : explicit;
  }

  const rawTarget = numericOrNull(controlRawTarget(device));
  const requestedTotal = numericOrNull(explain.requested_total_w);
  if (rawTarget !== null && requestedTotal && requestedTotal > 0) {
    return rawTarget / requestedTotal;
  }

  const weight = numericOrNull(controlDeviceWeight(device));
  if (weight !== null && weightContext.totalWeight) {
    return weight / weightContext.totalWeight;
  }

  return null;
}

function controlRawFormula(device, explain, weightContext) {
  const share = controlDeviceShare(device, explain, weightContext);
  const requestedTotal = numericOrNull(explain.requested_total_w);
  const rawTarget = numericOrNull(controlRawTarget(device));
  if (share === null || requestedTotal === null || rawTarget === null) {
    return null;
  }
  return `${watts(requestedTotal)} x ${formatShare(share)} = ${watts(rawTarget)}`;
}

function controlRawTarget(device) {
  return firstExplainValue(device.raw_target_w, device.raw_allocation_w);
}

function controlFinalTarget(device) {
  return firstExplainValue(
    device.final_target_w,
    device.effective_target_w,
    device.adjusted_target_w,
    device.allocated_target_w,
    device.raw_target_w
  );
}

function controlAdjustedTarget(device) {
  return firstExplainValue(
    device.adjusted_target_w,
    device.effective_target_w,
    device.allocated_target_w,
    controlFinalTarget(device)
  );
}

function controlAdjustmentDelta(device) {
  const explicit = firstExplainValue(device.adjustment_delta_w, device.adjustment_delta);
  if (hasExplainValue(explicit)) return explicit;

  const rawTarget = numericOrNull(controlRawTarget(device));
  const finalTarget = numericOrNull(controlFinalTarget(device));
  if (rawTarget === null || finalTarget === null) return null;
  return finalTarget - rawTarget;
}

function controlOutputDelta(device) {
  const finalTarget = numericOrNull(controlFinalTarget(device));
  const currentOutput = numericOrNull(device.output_w);
  if (finalTarget === null || currentOutput === null) return null;
  return finalTarget - currentOutput;
}

function controlLimitDelta(device) {
  const finalTarget = numericOrNull(controlFinalTarget(device));
  const currentLimit = numericOrNull(device.output_limit_w);
  if (finalTarget === null || currentLimit === null) return null;
  return finalTarget - currentLimit;
}

function controlDeviceWriteDecision(device, explain) {
  const explicit = firstExplainValue(device.write_decision, device.write_state, device.command_decision);
  const finalTarget = firstExplainValue(device.command_target_w, controlFinalTarget(device));

  if (hasExplainValue(explicit)) {
    const normalized = String(explicit).toLowerCase();
    const tone = normalized.includes("send")
      ? "send"
      : normalized.includes("block") || normalized.includes("error")
        ? "blocked"
        : normalized.includes("skip")
          ? "skip"
          : "warn";
    return {
      label: controlWriteLabel(normalized),
      reason: firstExplainValue(device.write_reason, device.deadband_reason, device.decision_reason),
      target: finalTarget,
      icon: tone === "blocked" ? "warning" : tone === "send" ? "charge" : "rule",
      tone,
    };
  }

  if (device.online === false) {
    return {
      label: "Blocked",
      reason: "offline",
      target: finalTarget,
      icon: "warning",
      tone: "blocked",
    };
  }

  const reference = numericOrNull(firstExplainValue(device.deadband_reference_w, device.output_limit_w, device.output_w));
  const target = numericOrNull(finalTarget);
  const deadband = numericOrNull(firstExplainValue(device.deadband_w, explain.deadband_w));

  if (target === null) {
    return {
      label: "Unavailable",
      reason: "missing target",
      target: null,
      icon: "warning",
      tone: "warn",
    };
  }

  if (reference !== null && deadband !== null && Math.abs(target - reference) < deadband) {
    return {
      label: "Skip",
      reason: "deadband",
      target,
      icon: "rule",
      tone: "skip",
    };
  }

  if (reference !== null && Math.abs(target - reference) < 0.5) {
    return {
      label: "Skip",
      reason: "already at target",
      target,
      icon: "rule",
      tone: "skip",
    };
  }

  return {
    label: "Send",
    reason: "target differs from current limit",
    target,
    icon: "charge",
    tone: "send",
  };
}

function controlWriteOutput(writeDecision) {
  const target = hasExplainValue(writeDecision.target) ? watts(writeDecision.target) : "--";
  return `${writeDecision.label} / ${target}`;
}

function controlGlobalWriteSummary(devices, explain) {
  if (!devices.length) {
    return { label: "No devices", icon: "warning", tone: "warn" };
  }

  const decisions = devices.map(([, device]) => controlDeviceWriteDecision(device || {}, explain));
  if (decisions.some((decision) => decision.tone === "blocked")) {
    return { label: "Blocked", icon: "warning", tone: "blocked" };
  }
  if (decisions.some((decision) => decision.tone === "send")) {
    return { label: "Send", icon: "charge", tone: "send" };
  }
  if (decisions.every((decision) => decision.tone === "skip")) {
    return { label: "Skip", icon: "rule", tone: "skip" };
  }
  return { label: "Inferred", icon: "rule", tone: "warn" };
}

function controlWriteLabel(value) {
  if (value.includes("send")) return "Send";
  if (value.includes("skip")) return "No write";
  if (value.includes("block")) return "Blocked";
  if (value.includes("error")) return "Blocked";
  return controlText(value);
}

function hasExplainValue(value) {
  return value !== undefined && value !== null && value !== "";
}

function controlText(value) {
  return String(value);
}

// Known firmware/EMS reason codes get an explicit readable phrase. Anything
// not in the map falls back to the previous underscore-to-space behavior, so
// unknown reasons stay legible without a localization framework.
const CONTROL_REASON_LABELS = {
  ac_input_runtime_role: "AC input mode is active; normal output writes are blocked",
  charge_inhibit: "Firmware reports Max-SoC / charge cutoff",
  discharge_inhibit: "Firmware reports Min-SoC / discharge protection",
  dc_inactive: "DC path is inactive",
  ac_inactive: "AC path is inactive",
  pack_standby: "Battery pack is in standby",
  fault_observed: "Firmware reports a fault signal",
  pv_evidence: "PV input detected",
};

function controlReason(value) {
  const text = controlText(value);
  const normalized = text.trim().toLowerCase();
  if (Object.prototype.hasOwnProperty.call(CONTROL_REASON_LABELS, normalized)) {
    return CONTROL_REASON_LABELS[normalized];
  }
  return text.replaceAll("_", " ");
}

function decimal(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return controlText(value);
  return number.toFixed(2).replace(/\.?0+$/, "");
}

function factor(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return controlText(value);
  return `${number.toFixed(2)}x`;
}

function formatShare(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return controlText(value);
  return `${(number * 100).toFixed(1).replace(/\.0$/, "")}%`;
}

function numericOrNull(value) {
  if (!hasExplainValue(value)) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function firstExplainValue(...values) {
  return values.find(hasExplainValue);
}

function deviceFlowRow(name, device, y, layout, homeY, rowIndex = 0, previousBatteryScale = 0) {
  const safeName = escapeHtml(name || "Unknown");
  const key = deviceFlowKey(name, rowIndex);
  const pvPower = devicePvPower(device);
  const outputPower = deviceOutputPower(device);
  const batteryFlow = normalizeBatteryPowerForDisplay(device.battery_power_w);
  const soc = clamp(deviceSoc(device), 0, 100);
  const batteryStateText = batteryStateLabel(batteryFlow);
  const pvX = layout.pvX;
  const batteryX = layout.batteryX;
  const inverterX = layout.inverterX;
  const sharedX = layout.sharedX;
  const pvY = y + layout.pvOffsetY;
  const inverterY = y + layout.inverterOffsetY;
  const batteryY = y + layout.batteryOffsetY;
  const pvMidY = pvY + 38;
  const inverterMidY = inverterY + 38;
  const inverterPvPortY = inverterY + layout.inverterPvPortOffsetY;
  const inverterBatteryPortY = inverterY + layout.inverterBatteryPortOffsetY;
  const batteryMidY = batteryY + 38;
  const homeMidY = homeY + 38;
  const leftJoinX = inverterX - 50;
  const homeJoinX = sharedX - 72;

  return `
    <g class="device-flow-device" data-device="${safeName}" data-device-flow-row="${key}">
      ${devicePipeGroup("pv", pvPower, `M${pvX + 184} ${pvMidY} H${leftJoinX} V${inverterPvPortY} H${inverterX}`, "forward", key)}
      ${devicePipeGroup("battery", batteryFlow.absW, `M${batteryX + 184} ${batteryMidY} H${leftJoinX} V${inverterBatteryPortY} H${inverterX}`, batteryPipeDirection(batteryFlow), key)}
      ${devicePipeGroup("output", outputPower, `M${inverterX + 196} ${inverterMidY} H${homeJoinX} V${homeMidY} H${sharedX}`, "forward", key)}
      ${deviceSolarVisual(pvX, pvY, `${safeName} PV`, watts(pvPower), flowActive(`device:${key}:visualPv`, pvPower), key)}
      ${deviceBatteryVisual(batteryX, batteryY, batteryStateText, signedWatts(batteryFlow.valueW), soc, flowActive(`device:${key}:visualBattery`, batteryFlow.absW), batteryFlow.state, rowIndex, previousBatteryScale, key)}
      ${deviceInverterVisual(inverterX, inverterY, safeName, watts(outputPower), flowActive(`device:${key}:visualInverter`, outputPower), key)}
    </g>
  `;
}

function deviceFlowKey(name, index) {
  return `row-${index}-${String(name || "unknown").replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
}

function devicePipeClass(kind, active, direction, speedBucket) {
  return [
    "energy-pipe",
    kind,
    active ? "active" : "idle",
    direction === "reverse" ? "reverse" : "",
    `flow-speed-${speedBucket}`,
  ].filter(Boolean).join(" ");
}

function devicePipeGroup(kind, value, path, direction = "forward", key = "") {
  const wattsValue = Math.abs(Number(value || 0));
  const stateKey = key ? `device:${key}:${kind}` : `device:shared:${kind}`;
  const active = flowActive(stateKey, wattsValue);
  const speedBucket = flowSpeedBucket(wattsValue, active);
  const classes = devicePipeClass(kind, active, direction, speedBucket);
  const pipeKey = key ? `${key}:${kind}` : `shared:${kind}`;

  return `
    <g class="${classes}" data-flow-pipe="${pipeKey}" data-flow-speed="${speedBucket}">
      <path class="pipe-base" d="${path}"></path>
      <path class="pipe-glow" d="${path}"></path>
      <path class="pipe-energy" d="${path}"></path>
    </g>
  `;
}

function deviceVisualClasses(baseClass, active, mode = "active") {
  return [
    "device-visual",
    baseClass,
    active ? mode : "",
  ].filter(Boolean).join(" ");
}

function deviceSolarVisual(x, y, label, value, active, key = "") {
  const attrs = key ? ` data-flow-visual="${key}:pv"` : "";
  return `
    <g class="${deviceVisualClasses("solar-visual", active)}"${attrs} transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="184" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="72" height="56" rx="24"></rect>
      <circle class="solar-sun" cx="46" cy="27" r="8"></circle>
      <g class="solar-panel" transform="translate(27 39) scale(.52)">
        <path class="panel-face" d="M0 0h72l20 48H18Z"></path>
        <path class="panel-grid" d="M17 0 24 48M36 0 46 48M55 0 68 48M8 16h72M14 32h72"></path>
        <path class="panel-reflect" d="M7 3h32l8 16H14Z"></path>
      </g>
      <text class="visual-label" data-flow-text="${key}:pv-label" x="166" y="32" text-anchor="end">${label}</text>
      <text class="visual-value" data-flow-text="${key}:pv-value" x="166" y="56" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceInverterVisual(x, y, label, value, active, key = "") {
  const attrs = key ? ` data-flow-visual="${key}:inverter"` : "";
  return `
    <g class="${deviceVisualClasses("inverter-visual", active)}"${attrs} transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="196" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="14" y="10" width="76" height="56" rx="26"></rect>
      <rect class="inverter-body" x="34" y="20" width="38" height="40" rx="10"></rect>
      <path class="inverter-wave" d="M41 41c5-12 10 12 15 0s10 12 15 0"></path>
      <circle class="inverter-led" cx="65" cy="29" r="3"></circle>
      <text class="visual-label" data-flow-text="${key}:inverter-label" x="174" y="32" text-anchor="end">${label}</text>
      <text class="visual-value" data-flow-text="${key}:inverter-value" x="174" y="56" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceBatteryVisual(
  x,
  y,
  stateText,
  value,
  soc,
  active,
  mode,
  rowIndex = 0,
  previousFillScale = null,
  key = ""
) {
  const clampedSoc = normalizeSoc(soc);
  const fillScale = clampedSoc / 100;
  const numericPreviousScale = Number(previousFillScale);
  const initialFillScale = previousFillScale !== null && Number.isFinite(numericPreviousScale)
    ? clamp(numericPreviousScale, 0, 1)
    : fillScale;
  const numericRowIndex = Number(rowIndex);
  const safeRowIndex = Number.isFinite(numericRowIndex)
    ? String(Math.max(0, Math.floor(numericRowIndex)))
    : "0";
  const fillClass = clampedSoc < 20 ? " low" : clampedSoc >= 90 ? " full" : "";
  const attrs = key ? ` data-flow-visual="${key}:battery"` : "";
  return `
    <g class="${deviceVisualClasses("battery-visual", active, mode)}"${attrs} transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="184" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="72" height="56" rx="24"></rect>
      <rect class="battery-case" x="24" y="27" width="52" height="23" rx="7"></rect>
      <rect class="battery-cap" x="76" y="35" width="5" height="8" rx="2"></rect>
      <rect class="battery-fill${fillClass}" x="29" y="32" width="42" height="13" rx="4" data-device-battery-fill="${safeRowIndex}" data-battery-fill-start="${initialFillScale}" data-battery-fill-target="${fillScale}"></rect>
      <text class="battery-soc" data-flow-text="${key}:battery-soc" x="50" y="43" text-anchor="middle">${pct(clampedSoc)}</text>
      <text class="visual-state" data-flow-text="${key}:battery-state" x="166" y="20" text-anchor="end">${stateText}</text>
      <text class="visual-label" x="166" y="39" text-anchor="end">Battery</text>
      <text class="visual-value" data-flow-text="${key}:battery-value" x="166" y="61" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceSharedVisuals(x, homeY, gridY, homeLoad, gridPower) {
  const gridMidY = gridY + 38;
  const homeMidY = homeY + 38;
  const gridDirection = gridDirectionLabel(gridPower);

  return `
    <g class="device-flow-shared-home">
      ${devicePipeGroup("grid", Math.abs(gridPower), `M${x + 88} ${gridMidY} H${x + 128} V${homeMidY} H${x + 88}`, gridPower < -FLOW_THRESHOLD_W ? "reverse" : "forward")}
      ${deviceHomeVisual(x, homeY, watts(homeLoad), flowActive("device:shared:visualHome", homeLoad))}
      ${deviceGridVisual(x, gridY, gridDirection, watts(gridPower), flowActive("device:shared:visualGrid", Math.abs(gridPower)), gridPower > FLOW_THRESHOLD_W ? "importing" : gridPower < -FLOW_THRESHOLD_W ? "exporting" : "neutral")}
    </g>
  `;
}

function deviceHomeVisual(x, y, value, active) {
  return `
    <g class="${deviceVisualClasses("home-visual", active)}" data-flow-visual="shared:home" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="176" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="68" height="56" rx="24"></rect>
      <path class="home-roof" d="M27 39 46 24l19 15"></path>
      <path class="home-body" d="M32 38v18h28V38"></path>
      <path class="home-door" d="M43 56V45h8v11"></path>
      <text class="visual-label" x="158" y="32" text-anchor="end">Home</text>
      <text class="visual-value" data-flow-text="shared:home-value" x="158" y="56" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceGridVisual(x, y, stateText, value, active, mode) {
  return `
    <g class="${deviceVisualClasses("grid-visual", active, mode)}" data-flow-visual="shared:grid" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="176" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="68" height="56" rx="24"></rect>
      <path class="grid-tower" d="M46 20v40M31 60h30M34 34h24M29 47h34M37 34 29 60M55 34l8 26M39 26h14"></path>
      <text class="visual-state" data-flow-text="shared:grid-state" x="158" y="20" text-anchor="end">${stateText}</text>
      <text class="visual-label" x="158" y="39" text-anchor="end">Grid</text>
      <text class="visual-value" data-flow-text="shared:grid-value" x="158" y="61" text-anchor="end">${value}</text>
    </g>
  `;
}

function normalizeDeviceEntries(devices) {
  if (Array.isArray(devices)) {
    return devices.map((device, index) => [deviceName(device, index), device || {}]);
  }
  return Object.entries(devices || {}).map(([name, device]) => [name || deviceName(device, 0), device || {}]);
}

function deviceName(device, index) {
  return String(device?.device || device?.name || `Device ${index + 1}`);
}

function devicePvPower(device) {
  return Number(device?.pv_input_w ?? device?.pv_power_w ?? 0);
}

function deviceOutputPower(device) {
  return Number(device?.output_w ?? device?.inverter_output_w ?? 0);
}

function deviceSoc(device) {
  return Number(device?.soc ?? device?.soc_percent ?? 0);
}

// Translate a raw firmware enum value into a readable label. Unknown values
// never collapse to a bare "Unknown" — the raw value is preserved as secondary
// debug detail, e.g. "Unknown AC state (value 9)".
function firmwareEnumLabel(value, labels, fallbackPrefix = "Unknown") {
  if (value === undefined || value === null || value === "") {
    return `${fallbackPrefix} (value unknown)`;
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric) && Object.prototype.hasOwnProperty.call(labels, numeric)) {
    return labels[numeric];
  }
  return `${fallbackPrefix} (value ${value})`;
}

function socLimitStatusLabel(value) {
  return firmwareEnumLabel(value, {
    0: "Normal",
    1: "Max-SoC reached",
    2: "Min-SoC protection",
  }, "Unknown");
}

function packStateLabel(value) {
  return firmwareEnumLabel(value, {
    0: "Standby",
    1: "Charging",
    2: "Discharging",
  }, "Unknown battery state");
}

function acModeLabel(value) {
  return firmwareEnumLabel(value, {
    1: "AC input / charge mode",
    2: "AC output mode",
  }, "Unknown AC mode");
}

function acStatusLabel(value) {
  return firmwareEnumLabel(value, {
    0: "AC standby",
    1: "AC output active",
    2: "AC charge active",
  }, "Unknown AC state");
}

function dcStatusLabel(value) {
  return firmwareEnumLabel(value, {
    0: "DC standby",
    1: "DC battery input path",
    2: "DC battery output path",
  }, "Unknown DC state");
}

function gridStateLabel(value) {
  return firmwareEnumLabel(value, {
    0: "Grid disconnected",
    1: "Grid connected",
  }, "Unknown grid state");
}

function socStatusLabel(value) {
  return firmwareEnumLabel(value, {
    0: "No calibration",
    1: "Calibration running",
  }, "Unknown calibration state");
}

function gridOffModeOptionLabel(value) {
  const labels = {
    standard: "Standard",
    eco: "Eco",
    off: "Off / closed",
  };
  return Object.prototype.hasOwnProperty.call(labels, value)
    ? labels[value]
    : String(value);
}

// Combined AC-path label for a device card. Uses acStatus as the source of
// truth for "active" states and only falls back to acMode to clarify the
// standby direction, so the card never claims charging/output that firmware
// does not report as running.
function acPathLabel(device) {
  const acMode = Number(device?.ac_mode ?? 0);
  const acStatus = Number(device?.ac_status ?? 0);

  if (acStatus === 2) return "AC charge active";
  if (acStatus === 1) return "AC output active";
  if (acStatus === 0) {
    if (acMode === 1) return "AC charge standby";
    if (acMode === 2) return "AC output standby";
    return "AC standby";
  }

  return acStatusLabel(device?.ac_status);
}

function acPathIcon(device) {
  const acStatus = Number(device?.ac_status ?? 0);
  if (acStatus === 2) return "charge";
  if (acStatus === 1) return "inverter";
  return "rule";
}

// Compact "Firmware status" block below the main power tiles. Translates the
// selected Zendure firmware status enums into readable labels while keeping the
// raw value visible (via the *_label helpers) for unknown codes.
function deviceFirmwareStatusFacts(device) {
  const facts = [
    deviceValue("AC path", acPathLabel(device), acPathIcon(device)),
    deviceValue("SOC guard", socLimitStatusLabel(device?.soc_limit), "gauge"),
    deviceValue("Battery state", packStateLabel(device?.pack_state), "battery"),
    deviceValue("DC path", dcStatusLabel(device?.dc_status), "rule"),
    deviceValue("Grid", gridStateLabel(device?.grid_state), "grid"),
  ];

  if (device?.soc_status !== undefined && device?.soc_status !== null) {
    facts.push(deviceValue("SOC calibration", socStatusLabel(device.soc_status), "history"));
  }
  if (Number.isFinite(Number(device?.pack_num)) && Number(device?.pack_num) > 0) {
    facts.push(deviceValue("Packs", String(Number(device.pack_num)), "battery"));
  }
  if (Number.isFinite(Number(device?.input_limit_w)) && Number(device?.input_limit_w) > 0) {
    facts.push(deviceValue("AC input limit", watts(device.input_limit_w), "charge"));
  }

  return facts;
}

function renderDeviceFirmwareStatus(device) {
  return `
    <div class="device-firmware">
      <div class="device-firmware-head">
        <span class="device-firmware-title">${icon("rule")} Firmware status</span>
      </div>
      <div class="device-values device-firmware-values">
        ${deviceFirmwareStatusFacts(device).join("")}
      </div>
    </div>
  `;
}

function setFlowView(view, persist = true) {
  const nextView = ["aggregated", "devices", "analytics", "control", "energy", "diagnose", "logs"].includes(view)
    ? view
    : "aggregated";
  const previousView = state.flowView;
  state.flowView = nextView;

  const svg = $("flowSvg");
  const deviceView = $("deviceFlowView");
  const controlView = $("controlExplainView");
  const energyView = $("energyStatsView");
  const diagnoseView = $("diagnoseView");
  const logsView = $("logsView");
  const analyticsView = $("analyticsView");
  const wrap = document.querySelector ? document.querySelector(".flow-wrap") : null;
  const shell = document.querySelector ? document.querySelector(".shell") : null;

  if (svg) svg.hidden = nextView !== "aggregated";
  if (deviceView) deviceView.hidden = nextView !== "devices";
  if (controlView) controlView.hidden = nextView !== "control";
  if (energyView) energyView.hidden = nextView !== "energy";
  if (diagnoseView) diagnoseView.hidden = nextView !== "diagnose";
  if (logsView) logsView.hidden = nextView !== "logs";
  if (analyticsView) analyticsView.hidden = nextView !== "analytics";
  if (wrap?.classList) {
    wrap.classList.toggle("view-devices", nextView === "devices");
    wrap.classList.toggle("view-aggregated", nextView === "aggregated");
    wrap.classList.toggle("view-analytics", nextView === "analytics");
    wrap.classList.toggle("view-control", nextView === "control");
    wrap.classList.toggle("view-energy", nextView === "energy");
    wrap.classList.toggle("view-diagnose", nextView === "diagnose");
    wrap.classList.toggle("view-logs", nextView === "logs");
  }
  if (shell?.classList) {
    shell.classList.toggle("view-analytics", nextView === "analytics");
    shell.classList.toggle("view-energy", nextView === "energy");
    shell.classList.toggle("view-diagnose", nextView === "diagnose");
    shell.classList.toggle("view-logs", nextView === "logs");
  }

  // The lightweight SQLite History panel only belongs to the operational views.
  // Use a distinct local name so this boolean is never confused with the
  // module-level historyVisible() helper (a function object is always truthy).
  const historyPanel = document.querySelector
    ? document.querySelector(".history-panel")
    : null;
  const isHistoryPanelVisible =
    nextView === "aggregated" || nextView === "devices";
  if (historyPanel) historyPanel.hidden = !isHistoryPanelVisible;

  document.querySelectorAll("[data-flow-view]").forEach((button) => {
    const active = button.dataset.flowView === nextView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });

  if (persist && window.localStorage) {
    try {
      window.localStorage.setItem("dashboard.flowView", nextView);
    } catch {
      // Ignore unavailable storage; the default aggregated view remains intact.
    }
  }

  if (nextView === "devices" && pendingDeviceFlowBatteryAnimation && deviceView) {
    pendingDeviceFlowBatteryAnimation = false;
    animateDeviceBatteryFills(deviceView);
  }

  if (nextView === "diagnose") {
    renderDiagnoseView();
  }

  // On an actual view change, immediately render the now-visible view from the
  // latest snapshot so switching tabs shows fresh data without waiting for the
  // next live update (live updates only render the active view).
  if (previousView !== nextView && state.snapshot) {
    renderViewSnapshot(nextView, state.snapshot);
  }

  // Lazy-load each data source only when its view becomes visible.
  if (isHistoryPanelVisible && previousView !== nextView) {
    loadHistory();
  }
  if (nextView === "analytics" && previousView !== nextView) {
    loadAnalytics();
  }

  if (nextView === "logs") {
    startLogsPolling();
  } else {
    stopLogsPolling();
  }
}

function fullChargeAssistMeta(status) {
  const meta = {
    active: { label: "Assist active", tone: "tone-send", icon: "charge" },
    window: { label: "Assist window active", tone: "tone-warn", icon: "history" },
    restore_pending: { label: "Restore pending", tone: "tone-warn", icon: "warning" },
    overdue: { label: "Assist overdue", tone: "tone-warn", icon: "warning" },
    completed: { label: "Assist completed", tone: "tone-send", icon: "rule" },
    ok: { label: "Assist scheduled", tone: "tone-skip", icon: "history" },
    unknown: { label: "Assist pending", tone: "tone-skip", icon: "rule" },
  };
  return meta[status] || meta.unknown;
}

function isAssistAcChargeActive(assist) {
  return assist.ac_mode === 1 && assist.ac_status === 2;
}

function fullChargeAssistDescription(assist) {
  switch (assist.status) {
    case "active":
      return isAssistAcChargeActive(assist)
        ? "EMS is helping this device reach firmware Max-SoC and is currently AC-charging for monthly battery calibration support."
        : "EMS is helping this device reach firmware Max-SoC.";
    case "window":
      return "EMS may start a short assist charge before the due date to reach firmware Max-SoC.";
    case "overdue":
      return "Assist is overdue. EMS will start an assist charge as soon as conditions allow.";
    case "restore_pending":
      return assist.ac_mode_restore_pending
        ? "EMS will restore the configured Max-SoC and normal AC output mode (firmware acMode=2) when writes are available."
        : "EMS will restore the configured Max-SoC when writes are available.";
    default:
      return "";
  }
}

function formatAssistTimestamp(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fullChargeAssistFirmwareSummary(assist) {
  return `SOC guard: ${socLimitStatusLabel(assist.soc_limit)} · AC path: ${acPathLabel(assist)}`;
}

function renderFullChargeAssist(device) {
  const assist = device?.battery_full_charge_assist;
  if (!assist || !assist.enabled || !assist.has_battery) return "";

  const meta = fullChargeAssistMeta(assist.status);
  const rows = [];

  if (assist.status === "active") {
    rows.push(deviceValue("Started", formatAssistTimestamp(assist.assist_started_at), "history"));
    if (isAssistAcChargeActive(assist)) {
      rows.push(deviceValue("AC charge", "Running", "charge"));
    }
    rows.push(deviceValue("Firmware", fullChargeAssistFirmwareSummary(assist), "gauge"));
    // restore_pending / ac_mode_restore_pending while assist is active are
    // planned follow-up actions for after charging finishes, not a current
    // restore problem.
    if (assist.restore_pending || assist.ac_mode_restore_pending) {
      rows.push(deviceValue("After charge", "Restore planned", "history"));
    }
  } else if (assist.status === "overdue") {
    const overdueDays = Number.isFinite(assist.days_until_due) ? Math.abs(assist.days_until_due) : null;
    rows.push(deviceValue(
      "Overdue by",
      overdueDays !== null ? `${overdueDays} d` : "--",
      "warning"
    ));
    rows.push(deviceValue("Next due", formatAssistTimestamp(assist.next_due_at), "history"));
  } else if (assist.status === "window") {
    rows.push(deviceValue(
      "Due in",
      Number.isFinite(assist.days_until_due) ? `${assist.days_until_due} d` : "--",
      "history"
    ));
    rows.push(deviceValue("Window starts", formatAssistTimestamp(assist.window_starts_at), "history"));
  } else {
    rows.push(deviceValue("Last full charge", formatAssistTimestamp(assist.last_full_charge_at), "history"));
    rows.push(deviceValue("Next due", formatAssistTimestamp(assist.next_due_at), "history"));
  }

  // restore_pending / ac_mode_restore_pending are also set while assist is
  // still active (pending confirmation of the initial socSet/acMode write),
  // so only surface them as restore facts once assist has finished and a
  // restore-to-config is actually pending.
  if (assist.status === "restore_pending") {
    if (assist.restore_pending) {
      rows.push(deviceValue("Max-SoC restore", "Pending", "warning"));
    }
    if (assist.ac_mode_restore_pending) {
      rows.push(deviceValue("AC output mode", "Restore pending", "warning"));
    }
  }

  const description = fullChargeAssistDescription(assist);
  const message = assist.message || "";
  const messageText = description
    ? `${message}${message && !/[.!?]$/.test(message) ? "." : ""} ${description}`
    : message;

  return `
    <div class="device-assist" data-assist-status="${escapeHtml(assist.status)}">
      <div class="device-assist-head">
        <span class="device-assist-title">${icon("battery")} Full-charge assist</span>
        <span class="pill ${meta.tone}">${icon(meta.icon)}${escapeHtml(meta.label)}</span>
      </div>
      <div class="control-note device-assist-message">${escapeHtml(messageText)}</div>
      <div class="device-values device-assist-values">
        ${rows.join("")}
      </div>
    </div>
  `;
}

function deviceValue(label, value, iconName = "rule") {
  return `
    <span class="device-value">
      <span class="value-top"><span class="value-icon" aria-hidden="true">${icon(iconName)}</span><span class="device-label">${label}</span></span>
      <strong>${escapeHtml(value)}</strong>
    </span>
  `;
}

function icon(name) {
  const map = {
    solar: "icon-solar",
    battery: "icon-battery",
    inverter: "icon-inverter",
    home: "icon-home",
    grid: "icon-grid",
    gauge: "icon-gauge",
    rule: "icon-rule",
    warning: "icon-warning",
    charge: "icon-charge",
    live: "icon-rule",
    history: "icon-history",
  };
  const id = map[name] || map.rule;
  return `<svg viewBox="0 0 24 24"><use href="#${id}"></use></svg>`;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function initFlowViewSwitch() {
  let initialView = "aggregated";
  if (window.localStorage) {
    try {
      initialView = window.localStorage.getItem("dashboard.flowView") || initialView;
    } catch {
      initialView = "aggregated";
    }
  }

  setFlowView(initialView, false);
  document.querySelectorAll("[data-flow-view]").forEach((button) => {
    button.addEventListener("click", () => setFlowView(button.dataset.flowView));
  });
}

const DIAGNOSE_STATUS_TONE = {
  ok: "tone-send",
  warning: "tone-warn",
  error: "tone-blocked",
  unknown: "tone-skip",
};

function diagnoseStatusTone(status) {
  return DIAGNOSE_STATUS_TONE[status] || "tone-skip";
}

function diagnoseAuthState() {
  if (!state.auth.configured) {
    return `
      <div class="diagnose-empty">
        Configure a dashboard password to enable diagnostics.
      </div>`;
  }
  if (!state.auth.authenticated) {
    return `
      <div class="diagnose-empty">
        Login required to run diagnostics.
      </div>`;
  }
  return "";
}

function renderDiagnoseSection(section) {
  const status = String(section.status || "unknown");
  const messages = []
    .concat(section.errors || [])
    .concat(section.warnings || []);
  const lines = messages.length
    ? messages
        .map((message) => `<li>${escapeHtml(message)}</li>`)
        .join("")
    : "<li class=\"diagnose-line-ok\">No issues reported.</li>";
  return `
    <div class="diagnose-section control-pipeline-stage">
      <div class="diagnose-section-head">
        <span class="diagnose-section-title">${escapeHtml(section.title || section.id || "Section")}</span>
        <span class="pill ${diagnoseStatusTone(status)}">${escapeHtml(status.toUpperCase())}</span>
      </div>
      <ul class="diagnose-lines">${lines}</ul>
    </div>`;
}

function renderDiagnoseMetrics(metrics) {
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return "";
  const entries = Object.entries(metrics);
  if (!entries.length) return "";
  const items = entries
    .map(([key, value]) => `
      <span class="diagnose-metric control-fact role-input">
        <span class="value-icon" aria-hidden="true">${icon("rule")}</span>
        <span class="control-label">${escapeHtml(String(key).replaceAll("_", " "))}</span>
        <strong>${escapeHtml(String(value))}</strong>
      </span>`)
    .join("");
  return `<div class="diagnose-metrics control-stage-values">${items}</div>`;
}

function renderDiagnoseGlobalList(title, items, tone) {
  if (!Array.isArray(items) || !items.length) return "";
  const lines = items
    .map((item) => `<li>${escapeHtml(String(item))}</li>`)
    .join("");
  return `
    <div class="diagnose-global-list diagnose-global-${tone} control-pipeline-stage">
      <div class="diagnose-section-head">
        <span class="diagnose-section-title">${escapeHtml(title)}</span>
        <span class="pill ${diagnoseStatusTone(tone)}">${escapeHtml(tone.toUpperCase())}</span>
      </div>
      <ul class="diagnose-lines">${lines}</ul>
    </div>`;
}

function renderDiagnoseRootCauses(rootCauses) {
  if (!Array.isArray(rootCauses) || !rootCauses.length) return "";
  const items = rootCauses
    .map((cause) => {
      const severity = String(cause.severity || "info");
      return `
        <div class="diagnose-root-cause">
          <span class="pill ${diagnoseStatusTone(severity === "info" ? "ok" : severity)}">${escapeHtml(severity.toUpperCase())}</span>
          <div>
            <div class="diagnose-root-title">${escapeHtml(cause.title || cause.code || "")}</div>
            <div class="diagnose-root-message">${escapeHtml(cause.message || "")}</div>
            ${cause.suggested_next_check ? `<div class="diagnose-root-hint">${escapeHtml(cause.suggested_next_check)}</div>` : ""}
          </div>
        </div>`;
    })
    .join("");
  return `<div class="diagnose-root-causes">${items}</div>`;
}

function renderDiagnoseReport(report) {
  if (!report || typeof report !== "object") {
    return `<div class="diagnose-empty">No diagnosis available.</div>`;
  }
  const diagnosis = report.diagnosis || {};
  const status = String(diagnosis.status || report.status || "unknown");
  const sections = Array.isArray(diagnosis.sections) ? diagnosis.sections : [];
  const profile = String(report.profile || state.diagnose.profile || "install");

  const header = `
    <div class="diagnose-report-head">
      <span class="diagnose-section-title">${escapeHtml(profile.toUpperCase())}</span>
      <span class="pill ${diagnoseStatusTone(status)}">${escapeHtml(status.toUpperCase())}</span>
    </div>`;
  const rootCauses = renderDiagnoseRootCauses(diagnosis.root_causes);
  const metrics = renderDiagnoseMetrics(diagnosis.metrics);
  const warnings = renderDiagnoseGlobalList("Global warnings", diagnosis.warnings, "warning");
  const errors = renderDiagnoseGlobalList("Global errors", diagnosis.errors, "error");
  const body = sections.length
    ? sections.map(renderDiagnoseSection).join("")
    : `<div class="diagnose-empty">No sections reported.</div>`;

  return `${header}${metrics}${errors}${warnings}${rootCauses}<div class="diagnose-sections">${body}</div>`;
}

function renderDiagnoseView() {
  const results = $("diagnoseResults");
  const status = $("diagnoseStatus");
  const copyButton = $("diagnoseCopy");
  if (!results) return;

  const authState = diagnoseAuthState();
  if (authState) {
    results.innerHTML = authState;
    if (status) status.textContent = "";
    if (copyButton) copyButton.hidden = true;
    return;
  }

  if (state.diagnose.report) {
    results.innerHTML = renderDiagnoseReport(state.diagnose.report);
    if (copyButton) copyButton.hidden = false;
  } else {
    results.innerHTML = `<div class="diagnose-empty">Select a profile and press Run.</div>`;
    if (copyButton) copyButton.hidden = true;
  }
}

function setDiagnoseStatus(message) {
  const status = $("diagnoseStatus");
  if (status) status.textContent = message;
}

async function runDiagnose(profile) {
  if (state.diagnose.running) return;
  if (!state.auth.authenticated) {
    renderDiagnoseView();
    return;
  }
  state.diagnose.running = true;
  setDiagnoseStatus(`Running ${profile}…`);
  try {
    const response = await fetch(
      `/api/diagnose?profile=${encodeURIComponent(profile)}`,
      { credentials: "same-origin" }
    );
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      setDiagnoseStatus(`Diagnose failed (${response.status}${detail.error ? `: ${detail.error}` : ""}).`);
      return;
    }
    state.diagnose.report = await response.json();
    renderDiagnoseView();
    setDiagnoseStatus("");
  } catch {
    setDiagnoseStatus("Diagnose request failed.");
  } finally {
    state.diagnose.running = false;
  }
}

async function downloadSupportBundle() {
  if (!state.auth.authenticated) {
    renderDiagnoseView();
    return;
  }
  setDiagnoseStatus("Building support bundle…");
  try {
    const response = await fetch("/api/diagnose/support-bundle", {
      credentials: "same-origin",
    });
    if (!response.ok) {
      setDiagnoseStatus(`Support bundle failed (${response.status}).`);
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "ems-support-bundle.zip";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    setDiagnoseStatus("");
  } catch {
    setDiagnoseStatus("Support bundle request failed.");
  }
}

function copyDiagnoseJson() {
  if (!state.diagnose.report) return;
  const text = JSON.stringify(state.diagnose.report, null, 2);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => setDiagnoseStatus("Copied report JSON to clipboard."),
      () => setDiagnoseStatus("Could not copy to clipboard.")
    );
  }
}

function initDiagnose() {
  document.querySelectorAll("[data-diagnose-profile]").forEach((button) => {
    button.addEventListener("click", () => {
      const profile = button.dataset.diagnoseProfile;
      state.diagnose.profile = profile;
      document.querySelectorAll("[data-diagnose-profile]").forEach((other) => {
        const active = other === button;
        other.classList.toggle("active", active);
        other.setAttribute("aria-selected", active ? "true" : "false");
      });
    });
  });

  const runButton = $("diagnoseRun");
  if (runButton) {
    runButton.addEventListener("click", () => runDiagnose(state.diagnose.profile));
  }
  const bundleButton = $("diagnoseBundle");
  if (bundleButton) {
    bundleButton.addEventListener("click", () => downloadSupportBundle());
  }
  const copyButton = $("diagnoseCopy");
  if (copyButton) {
    copyButton.addEventListener("click", () => copyDiagnoseJson());
  }
}

const LOG_LEVEL_TONE = {
  DEBUG: "log-debug",
  INFO: "log-info",
  WARNING: "log-warning",
  ERROR: "log-error",
  CRITICAL: "log-error",
};

function logsAuthState() {
  if (!state.auth.configured) {
    return `<div class="logs-empty">Configure a dashboard password to view logs.</div>`;
  }
  if (!state.auth.authenticated) {
    return `<div class="logs-empty">Login required to view logs.</div>`;
  }
  return "";
}

function formatLogTimestamp(ts) {
  if (typeof ts !== "number") return "";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().slice(11, 19);
}

function renderLogRows(lines) {
  return lines
    .map((line) => {
      const level = String(line.level || "INFO");
      const tone = LOG_LEVEL_TONE[level] || "log-info";
      return `<div class="logs-row ${tone}">`
        + `<span class="logs-time">${escapeHtml(formatLogTimestamp(line.ts))}</span>`
        + `<span class="logs-level">${escapeHtml(level)}</span>`
        + `<span class="logs-message">${escapeHtml(line.message)}</span>`
        + `</div>`;
    })
    .join("");
}

function trimLogRows(existing, incoming, max = MAX_LOG_ROWS) {
  const combined = existing.concat(incoming);
  return combined.length > max ? combined.slice(combined.length - max) : combined;
}

function applyLogs() {
  const output = $("logsOutput");
  if (!output) return;

  updateServiceLevelControl();

  const authState = logsAuthState();
  if (authState) {
    output.innerHTML = authState;
    return;
  }

  output.innerHTML = renderLogRows(state.logs.lines);
  if (state.logs.follow && typeof output.scrollTop === "number") {
    output.scrollTop = output.scrollHeight;
  }
}

function ingestLogLines(lines) {
  if (!Array.isArray(lines) || !lines.length) return;
  state.logs.lines = trimLogRows(state.logs.lines, lines);
  applyLogs();
}

function setLogsStatus(message) {
  const status = $("logsStatus");
  if (status) status.textContent = message;
}

async function pollLogs() {
  if (!state.auth.authenticated) {
    applyLogs();
    return;
  }
  try {
    const params = new URLSearchParams({ after: String(state.logs.cursor) });
    if (state.logs.level) params.set("level", state.logs.level);
    const response = await fetch(`/api/logs?${params.toString()}`, {
      credentials: "same-origin",
    });
    if (!response.ok) {
      setLogsStatus(`Logs unavailable (${response.status}).`);
      return;
    }
    const payload = await response.json();
    state.logs.cursor = payload.cursor || state.logs.cursor;
    ingestLogLines(payload.lines || []);
    updateServiceLevelControl(payload.service_level);
    setLogsStatus(payload.dropped ? "Some older lines were dropped." : "");
  } catch {
    setLogsStatus("Log request failed.");
  }
}

function updateServiceLevelControl(serviceLevel) {
  const select = $("logsServiceLevel");
  if (!select) return;
  // Changing the service verbosity is a write action -> only when authenticated.
  select.disabled = !state.auth.authenticated;
  if (serviceLevel) {
    state.logs.serviceLevel = serviceLevel;
    select.value = serviceLevel;
  }
}

async function setServiceLogLevel(level) {
  if (!state.auth.authenticated || !state.auth.csrfToken) return;
  setLogsStatus(`Setting service log level to ${level}…`);
  try {
    const response = await fetch("/api/logs/level", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.auth.csrfToken,
      },
      body: JSON.stringify({ level }),
    });
    if (!response.ok) {
      setLogsStatus(`Could not set level (${response.status}).`);
      return;
    }
    const payload = await response.json();
    state.logs.serviceLevel = payload.service_level;
    setLogsStatus(`Service log level set to ${payload.service_level}.`);
  } catch {
    setLogsStatus("Set level request failed.");
  }
}

function resetLogs() {
  state.logs.cursor = 0;
  state.logs.lines = [];
  applyLogs();
}

function startLogsPolling() {
  stopLogsPolling();
  applyLogs();
  if (!state.auth.authenticated) return;
  pollLogs();
  if (typeof setInterval === "function") {
    state.logs.timerId = setInterval(pollLogs, LOG_POLL_INTERVAL_MS);
  }
}

function stopLogsPolling() {
  if (state.logs.timerId && typeof clearInterval === "function") {
    clearInterval(state.logs.timerId);
  }
  state.logs.timerId = null;
}

function initLogs() {
  const levelSelect = $("logsLevel");
  if (levelSelect) {
    state.logs.level = levelSelect.value;
    levelSelect.addEventListener("change", () => {
      state.logs.level = levelSelect.value;
      resetLogs();
      if (state.flowView === "logs") pollLogs();
    });
  }
  const follow = $("logsFollow");
  if (follow) {
    state.logs.follow = Boolean(follow.checked);
    follow.addEventListener("change", () => {
      state.logs.follow = Boolean(follow.checked);
      if (state.logs.follow) applyLogs();
    });
  }
  const serviceSelect = $("logsServiceLevel");
  if (serviceSelect) {
    serviceSelect.addEventListener("change", () => {
      setServiceLogLevel(serviceSelect.value);
    });
  }
  const clear = $("logsClear");
  if (clear) {
    clear.addEventListener("click", () => {
      state.logs.lines = [];
      applyLogs();
    });
  }
}

function runtimeControlPanel() {
  const runtime = state.runtime || {};
  if (!state.auth.configured) {
    return `
      <section class="runtime-editor-panel control-stage-row" aria-label="Runtime write controls">
        <div class="control-empty compact">Read-only mode. Dashboard authentication is not configured.</div>
      </section>
    `;
  }

  if (!state.auth.authenticated) {
    return `
      <section class="runtime-editor-panel control-stage-row" aria-label="Runtime write controls">
        <div class="control-empty compact">Read-only mode. Login required to change runtime values.</div>
      </section>
    `;
  }

  const system = runtime.system || {};
  const ha = runtime.ha || {};
  const winter = runtime.winter || {};
  const devices = Object.entries(runtime.devices || {});
  const limits = runtime._limits || {};
  const systemLimits = limits.system || {};
  const deviceLimits = limits.devices || {};
  const fallbackDeviceMax = Number(limits.fallback_device_max_power || 5000);
  const deviceForms = devices.map(([name, device], index) => runtimeDeviceForm(
    name,
    device || {},
    Number(deviceLimits[name] || fallbackDeviceMax),
    index + 2
  )).join("");
  const winterStep = devices.length + 2;
  const haStep = devices.length + 3;

  return `
    <section class="runtime-editor-panel control-stage-row" aria-label="Runtime write controls">
      <div class="runtime-editor-head control-context-rail">
        <div class="control-context-title">Runtime Settings</div>
        <span id="runtimeWriteFeedback" class="runtime-feedback"></span>
      </div>
      <div class="runtime-editor-grid control-global-pipeline">
        ${runtimeStageCard({
          endpoint: "/api/runtime/system",
          title: "EMS / System",
          subtitle: "Global runtime limits and loop control",
          step: 1,
          kind: "target",
          iconName: "gauge",
          submitLabel: "Save EMS settings",
          fields: `
          ${runtimeToggle("enabled", "EMS enabled", system.enabled)}
          ${runtimeNumber("max_total_power", "Max total power", system.max_total_power, 0, Number(systemLimits.max_total_power || 5000), "W", "50")}
          ${runtimeNumber("min_output_limit", "Min output limit", system.min_output_limit, 0, Number(systemLimits.min_output_limit || 5000), "W", "5")}
          ${runtimeNumber("loop_interval", "Loop interval", system.loop_interval, 1, 3600, "s", "1")}
        `})}
        ${deviceForms}
        ${runtimeStageCard({
          endpoint: "/api/runtime/winter",
          title: "Winter Mode",
          subtitle: "Seasonal charging behavior",
          step: winterStep,
          kind: "gates",
          iconName: "battery",
          submitLabel: "Save winter mode",
          fields: `
          ${runtimeToggle("enabled", "Winter mode", winter.enabled)}
        `})}
        ${runtimeStageCard({
          endpoint: "/api/runtime/ha",
          title: "Home Assistant",
          subtitle: "External publishing and helper control",
          step: haStep,
          kind: "write",
          iconName: "live",
          submitLabel: "Save HA settings",
          fields: `
          ${runtimeToggle("enabled", "HA publishing", ha.enabled)}
          ${runtimeToggle("control_enabled", "HA helper control", ha.control_enabled)}
        `})}
      </div>
    </section>
  `;
}

function runtimeStageCard({ endpoint, title, subtitle, step, kind, iconName, fields, submitLabel }) {
  return `
    <form class="runtime-form control-pipeline-stage runtime-stage-card runtime-stage-${escapeHtml(kind)}" data-runtime-endpoint="${escapeHtml(endpoint)}">
      <div class="control-stage-head control-stage-header">
        <div class="control-stage-kicker">
          ${controlStageStep(step)}
          <span class="control-stage-dot" aria-hidden="true">${icon(iconName)}</span>
        </div>
        <div class="control-stage-title-block">
          <h3 class="control-stage-title">${escapeHtml(title)}</h3>
          ${controlStageSubtitle(subtitle)}
        </div>
      </div>
      <div class="control-stage-body runtime-stage-values control-pipeline-values">${fields}</div>
      ${runtimeSubmit(submitLabel)}
    </form>
  `;
}

function runtimeDeviceForm(name, device, maxPower = 5000, step = 1) {
  const endpoint = `/api/runtime/device/${encodeURIComponent(name)}`;
  return runtimeStageCard({
    endpoint,
    title: name,
    subtitle: "Device runtime write values",
    step,
    kind: "distribution",
    iconName: "inverter",
    submitLabel: `Save ${name} settings`,
    fields: `
      ${runtimeToggle("enabled", "Device enabled", device.enabled)}
      ${runtimeNumber("max_power", "Max power", device.max_power, 0, maxPower, "W", "50")}
      ${runtimeNumber("pv_priority_factor", "PV priority", device.pv_priority_factor, 0.01, 100, "x", "0.01")}
      ${runtimeSelect("offgrid_socket_mode", "Offgrid socket", device.offgrid_socket_mode, [
        { value: "off", label: gridOffModeOptionLabel("off") },
        { value: "eco", label: gridOffModeOptionLabel("eco") },
        { value: "standard", label: gridOffModeOptionLabel("standard") },
      ])}
    `,
  });
}

function runtimeToggle(name, label, value) {
  return `
    <label class="runtime-toggle control-pipeline-fact role-config">
      <span class="value-icon" aria-hidden="true">${icon("rule")}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <input type="checkbox" name="${escapeHtml(name)}" ${value ? "checked" : ""}>
    </label>
  `;
}

function runtimeNumber(name, label, value, min, max, unit, step = "1") {
  const rendered = value === undefined || value === null ? "" : escapeHtml(value);
  return `
    <label class="runtime-field control-pipeline-fact role-config">
      <span class="value-icon" aria-hidden="true">${icon("gauge")}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <span class="runtime-number-wrap">
        <input type="number" name="${escapeHtml(name)}" value="${rendered}" min="${min}" max="${max}" step="${step}">
        <span>${escapeHtml(unit)}</span>
      </span>
    </label>
  `;
}

// `options` may be plain string values or {value, label} objects. The
// submitted/stored `value` is always preserved exactly; only the visible
// option text is made readable (via the object label or `optionLabelFormatter`).
function runtimeSelect(name, label, selectedValue, options, optionLabelFormatter) {
  const normalized = options.map((option) => {
    if (option && typeof option === "object") {
      return { value: option.value, label: option.label ?? String(option.value) };
    }
    const optionLabel = typeof optionLabelFormatter === "function"
      ? optionLabelFormatter(option)
      : String(option);
    return { value: option, label: optionLabel };
  });

  return `
    <label class="runtime-field control-pipeline-fact role-config">
      <span class="value-icon" aria-hidden="true">${icon("rule")}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <select name="${escapeHtml(name)}">
        ${normalized.map(({ value, label: optionLabel }) => `
          <option value="${escapeHtml(value)}" ${selectedValue === value ? "selected" : ""}>${escapeHtml(optionLabel)}</option>
        `).join("")}
      </select>
    </label>
  `;
}

function runtimeSubmit(label = "Apply") {
  return `<button class="primary-button compact" type="submit">${escapeHtml(label)}</button>`;
}

function activeRuntimeEditorElement() {
  const active = typeof document !== "undefined" ? document.activeElement : null;
  if (!active || !active.closest) return null;
  const editor = active.closest(".runtime-form, .runtime-editor-panel");
  const container = $("controlExplainView");
  if (editor && container?.contains && !container.contains(editor)) return null;
  return editor;
}

function isRuntimeEditorEditing() {
  return Boolean(activeRuntimeEditorElement())
    || Boolean(state.runtimeEditorFocused)
    || Boolean(state.runtimeEditorDirty);
}

function clearRuntimeEditorState() {
  state.runtimeEditorDirty = false;
  state.runtimeEditorFocused = false;
}

function initRuntimeForms() {
  const container = $("controlExplainView");
  if (!container) return;

  container.addEventListener("input", (event) => {
    if (event.target?.closest?.(".runtime-form")) {
      state.runtimeEditorDirty = true;
    }
  });

  container.addEventListener("change", (event) => {
    if (event.target?.closest?.(".runtime-form")) {
      state.runtimeEditorDirty = true;
    }
  });

  container.addEventListener("focusin", (event) => {
    if (event.target?.closest?.(".runtime-form, .runtime-editor-panel")) {
      state.runtimeEditorFocused = true;
    }
  });

  container.addEventListener("focusout", () => {
    const defer = typeof window !== "undefined" && typeof window.setTimeout === "function"
      ? window.setTimeout.bind(window)
      : (callback) => callback();
    defer(() => {
      state.runtimeEditorFocused = Boolean(activeRuntimeEditorElement());
    }, 0);
  });

  container.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches(".runtime-form")) return;
    event.preventDefault();
    await submitRuntimeForm(form);
  });
}

async function submitRuntimeForm(form) {
  if (!state.auth.authenticated || !state.auth.csrfToken) {
    setRuntimeFeedback("Login required.", true);
    return;
  }

  const payload = {};
  Array.from(form.elements).forEach((element) => {
    if (!element.name) return;
    if (element.type === "checkbox") {
      payload[element.name] = element.checked;
      return;
    }
    if (element.type === "number") {
      payload[element.name] = element.value.includes(".")
        ? Number.parseFloat(element.value)
        : Number.parseInt(element.value, 10);
      return;
    }
    payload[element.name] = element.value;
  });

  try {
    const response = await fetch(form.dataset.runtimeEndpoint, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.auth.csrfToken,
      },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        await loadAuthStatus();
      }
      throw new Error(result.message || result.error || "Runtime update failed");
    }
    await loadRuntimeState({ forceRuntimeEditor: true });
    setRuntimeFeedback("Saved.", false);
  } catch (error) {
    setRuntimeFeedback(error.message || "Runtime update failed.", true);
  }
}

function setRuntimeFeedback(message, isError) {
  const el = $("runtimeWriteFeedback");
  if (!el) return;
  el.textContent = message;
  el.className = `runtime-feedback ${isError ? "error" : "ok"}`;
}

async function loadRuntimeState(options = {}) {
  const forceRuntimeEditor = Boolean(options.forceRuntimeEditor) || !isRuntimeEditorEditing();
  if (state.demoMode) {
    state.runtime = demoRuntimeState();
    if (forceRuntimeEditor) clearRuntimeEditorState();
    if (state.snapshot) renderControlExplain(state.snapshot, { forceRuntimeEditor });
    return;
  }
  try {
    const response = await fetch("/api/runtime");
    state.runtime = await response.json();
    if (forceRuntimeEditor) clearRuntimeEditorState();
    if (state.snapshot) renderControlExplain(state.snapshot, { forceRuntimeEditor });
  } catch {
    state.runtime = null;
  }
}

async function loadAuthStatus() {
  if (state.demoMode) {
    state.auth = { configured: false, authenticated: false, csrfToken: null };
    clearRuntimeEditorState();
    renderAuthState();
    if (state.snapshot) renderControlExplain(state.snapshot, { forceRuntimeEditor: true });
    return;
  }
  const previousConfigured = state.auth.configured;
  const previousAuthenticated = state.auth.authenticated;
  try {
    const response = await fetch("/api/auth/status");
    const payload = await response.json();
    state.auth.configured = Boolean(payload.auth_configured);
    state.auth.authenticated = Boolean(payload.authenticated);
    if (payload.csrf_token) {
      state.auth.csrfToken = payload.csrf_token;
    }
    if (!state.auth.authenticated) {
      state.auth.csrfToken = null;
    }
    renderAuthState();
    const authChanged = previousConfigured !== state.auth.configured
      || previousAuthenticated !== state.auth.authenticated;
    if (authChanged) clearRuntimeEditorState();
    if (state.snapshot) renderControlExplain(state.snapshot, { forceRuntimeEditor: authChanged });
  } catch {
    state.auth = { configured: false, authenticated: false, csrfToken: null };
    if (previousConfigured || previousAuthenticated) clearRuntimeEditorState();
    renderAuthState();
    if (state.snapshot) {
      renderControlExplain(state.snapshot, {
        forceRuntimeEditor: previousConfigured || previousAuthenticated,
      });
    }
  }
}

function renderAuthState() {
  const statePill = $("writeModeState");
  const button = $("authButton");
  if (statePill) {
    statePill.textContent = state.auth.authenticated ? "Write mode" : "Read-only";
    statePill.className = state.auth.authenticated ? "pill" : "pill muted";
  }
  if (button) {
    button.hidden = !state.auth.configured;
    button.textContent = state.auth.authenticated ? "Logout" : "Login";
  }
  // Keep the Diagnose tab's auth-gated empty state in sync with login/logout.
  if (state.flowView === "diagnose") {
    if (!state.auth.authenticated) state.diagnose.report = null;
    renderDiagnoseView();
  }
  // Keep the Logs tab in sync: start/stop the poll loop on login/logout.
  if (state.flowView === "logs") {
    if (!state.auth.authenticated) {
      stopLogsPolling();
      resetLogs();
    } else if (!state.logs.timerId) {
      startLogsPolling();
    }
  }
}

function initAuthControls() {
  const button = $("authButton");
  const modal = $("loginModal");
  const form = $("loginForm");
  const closeButton = $("loginCloseButton");
  const cancelButton = $("loginCancelButton");

  if (button) {
    button.addEventListener("click", async () => {
      if (state.auth.authenticated) {
        await logout();
      } else {
        openLoginModal();
      }
    });
  }

  [closeButton, cancelButton].forEach((item) => {
    if (item) item.addEventListener("click", closeLoginModal);
  });

  if (modal) {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeLoginModal();
    });
  }

  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await login();
    });
  }
}

function openLoginModal() {
  const modal = $("loginModal");
  const password = $("loginPassword");
  const error = $("loginError");
  if (error) error.hidden = true;
  if (password) password.value = "";
  if (modal) modal.hidden = false;
  if (password) password.focus();
}

function closeLoginModal() {
  const modal = $("loginModal");
  if (modal) modal.hidden = true;
}

async function login() {
  const password = $("loginPassword")?.value || "";
  const error = $("loginError");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.error || "Login failed");
    }
    state.auth.configured = Boolean(payload.auth_configured);
    state.auth.authenticated = true;
    state.auth.csrfToken = payload.csrf_token;
    closeLoginModal();
    renderAuthState();
    clearRuntimeEditorState();
    await loadRuntimeState({ forceRuntimeEditor: true });
  } catch (err) {
    if (error) {
      error.textContent = err.message || "Login failed";
      error.hidden = false;
    }
  }
}

async function logout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } finally {
    state.auth.authenticated = false;
    state.auth.csrfToken = null;
    clearRuntimeEditorState();
    renderAuthState();
    if (state.snapshot) renderControlExplain(state.snapshot, { forceRuntimeEditor: true });
  }
}

function demoModeFromSearch(search) {
  const params = String(search || "").replace(/^\?/, "").split("&");
  return params.some((part) => {
    const [rawKey, rawValue = ""] = part.split("=");
    const key = decodeURIComponent(rawKey || "").toLowerCase();
    const value = decodeURIComponent(rawValue || "").toLowerCase();
    return key === "demo" && (value === "1" || value === "true");
  });
}

function isDemoMode() {
  if (typeof window === "undefined") return false;
  return demoModeFromSearch(window.location?.search || "");
}

const ANIMATION_MODES = ["normal", "reduced", "off"];

// Apply the dashboard animation mode as a root class so the CSS can scale back
// expensive flow animations/filters. Purely visual: never affects control,
// auth, runtime writes or data. Browser prefers-reduced-motion is honored on
// top of this by the stylesheet.
function setAnimationMode(mode) {
  const normalized = ANIMATION_MODES.includes(mode) ? mode : "normal";
  const root = typeof document !== "undefined" ? document.body : null;
  if (!root || !root.classList) return normalized;
  ANIMATION_MODES.forEach((value) => {
    root.classList.toggle(`dashboard-animation-${value}`, value === normalized);
  });
  return normalized;
}

// Fetch the read-only UI bootstrap hints and apply the animation mode. Failure
// is non-fatal: the dashboard keeps the default (normal) animations.
async function applyAnimationMode() {
  if (typeof fetch !== "function") return;
  try {
    const response = await fetch("/api/ui-config");
    if (!response.ok) return;
    const config = await response.json();
    setAnimationMode(config && config.animation_mode);
  } catch {
    // Keep default animations when the hint cannot be loaded.
  }
}

function demoSnapshot() {
  const timestamp = new Date().toISOString();
  return {
    timestamp,
    pv_total_w: 1850,
    inverter_output_w: 800,
    home_load_w: 800,
    grid_power_w: 0,
    battery_power_w: 1050,
    average_soc: 59.5,
    controller: {
      enabled: true,
      max_total_power_w: 800,
      min_output_limit_w: 0,
      allocated_target_total_w: 800,
      effective_target_total_w: 800,
      commanded_total_w: 800,
      filtered_load_w: 800,
    },
    rules: {
      ems_enabled: { active: true, reason: "demo mode static control preview" },
      pv_priority_balancing: { active: true, reason: "WR1 keeps more PV available for charging" },
      battery_balancing: { active: true, reason: "two devices share an 800 W system limit" },
    },
    energy_stats: {
      enabled: true,
      currency: "EUR",
      price_per_kwh: 0.35,
      today: {
        inverter_output_kwh: 3.2,
        savings_value: 1.12,
      },
      yesterday: {
        inverter_output_kwh: 4.2,
        savings_value: 1.47,
        peak_output_w: 780,
      },
      last_7_days: {
        inverter_output_kwh: 18.4,
        savings_value: 6.44,
      },
      last_4_weeks: {
        inverter_output_kwh: 72.1,
        savings_value: 25.24,
      },
      last_12_months: {
        inverter_output_kwh: 520.0,
        savings_value: 182.0,
      },
      best_day: {
        date: "2026-06-14",
        inverter_output_kwh: 8.4,
        savings_value: 2.94,
      },
      monthly_current_year: [
        { month: 1, label: "Jan", inverter_output_kwh: 22.4, savings_value: 7.84 },
        { month: 2, label: "Feb", inverter_output_kwh: 31.2, savings_value: 10.92 },
        { month: 3, label: "Mar", inverter_output_kwh: 43.8, savings_value: 15.33 },
        { month: 4, label: "Apr", inverter_output_kwh: 58.5, savings_value: 20.48 },
        { month: 5, label: "May", inverter_output_kwh: 76.6, savings_value: 26.81 },
        { month: 6, label: "Jun", inverter_output_kwh: 84.2, savings_value: 29.47 },
        { month: 7, label: "Jul", inverter_output_kwh: 91.8, savings_value: 32.13 },
        { month: 8, label: "Aug", inverter_output_kwh: 88.4, savings_value: 30.94 },
        { month: 9, label: "Sep", inverter_output_kwh: 66.9, savings_value: 23.42 },
        { month: 10, label: "Oct", inverter_output_kwh: 44.5, savings_value: 15.58 },
        { month: 11, label: "Nov", inverter_output_kwh: 18.6, savings_value: 6.51 },
        { month: 12, label: "Dec", inverter_output_kwh: 11.2, savings_value: 3.92 },
      ],
      yearly: [
        { year: 2025, inverter_output_kwh: 320.0, savings_value: 112.0 },
        { year: 2026, inverter_output_kwh: 840.0, savings_value: 294.0 },
        { year: 2027, inverter_output_kwh: 910.0, savings_value: 318.5 },
      ],
      lifetime: {
        inverter_output_kwh: 2070.0,
        savings_value: 724.5,
      },
    },
    devices: {
      WR1: {
        online: true,
        enabled: true,
        soc: 62,
        battery_power_w: 880,
        pack_input_w: 20,
        pack_output_w: 900,
        pv_input_w: 1200,
        output_w: 320,
        target_w: 320,
        allocated_target_w: 320,
        output_limit_w: 320,
        mode: "solar",
      },
      WR2: {
        online: true,
        enabled: true,
        soc: 57,
        battery_power_w: 170,
        pack_input_w: 30,
        pack_output_w: 200,
        pv_input_w: 650,
        output_w: 480,
        target_w: 480,
        allocated_target_w: 480,
        output_limit_w: 480,
        mode: "solar",
      },
    },
    control_explain: {
      mode: "pv_first",
      filtered_load_w: 800,
      requested_total_w: 800,
      effective_target_total_w: 800,
      allocated_target_total_w: 800,
      commanded_total_w: 800,
      max_total_power_w: 800,
      min_output_limit_w: 0,
      deadband_w: 5,
      devices: {
        WR1: {
          device: "WR1",
          online: true,
          pv_input_w: 1200,
          output_w: 320,
          output_limit_w: 320,
          soc: 62,
          min_soc: 15,
          max_soc: 100,
          max_output_w: 800,
          pv_only_limit_w: 1180,
          base_weight: 1,
          effective_weight: 1.2,
          pv_priority_factor: 1.2,
          charge_balance_multiplier: 1.05,
          raw_target_w: 330,
          allocated_target_w: 320,
          effective_target_w: 320,
          adjustment_delta_w: -10,
          decision_reason: "WR1 has stronger PV, so output is kept lower to leave more local PV available for battery charging.",
          write_decision: "skip",
          write_reason: "deadband",
          deadband_reference_w: 320,
          command_target_w: 320,
        },
        WR2: {
          device: "WR2",
          online: true,
          pv_input_w: 650,
          output_w: 480,
          output_limit_w: 480,
          soc: 57,
          min_soc: 15,
          max_soc: 100,
          max_output_w: 800,
          pv_only_limit_w: 640,
          base_weight: 1,
          effective_weight: 1.75,
          pv_priority_factor: 1.0,
          charge_balance_multiplier: 1.1,
          raw_target_w: 470,
          allocated_target_w: 480,
          effective_target_w: 480,
          adjustment_delta_w: 10,
          decision_reason: "WR2 carries more house load while WR1 charges more from its stronger PV input.",
          write_decision: "send",
          write_reason: "output_limit_update",
          deadband_reference_w: 460,
          command_target_w: 480,
        },
      },
      limits: [
        {
          name: "System output limit",
          active: true,
          value: "800 W",
          reason: "demo output is capped at the configured 800 W system limit",
        },
      ],
      notes: [
        "Demo mode uses a 2 kWp PV example with an 800 W system output limit.",
      ],
    },
  };
}

function demoRuntimeState() {
  return {
    system: {
      enabled: true,
      max_total_power: 800,
      loop_interval: 5,
      min_output_limit: 0,
    },
    ha: {
      enabled: false,
      control_enabled: false,
    },
    winter: {
      enabled: false,
    },
    devices: {
      WR1: {
        enabled: true,
        max_power: 800,
        offgrid_socket_mode: "off",
        pv_priority_factor: 1.2,
      },
      WR2: {
        enabled: true,
        max_power: 800,
        offgrid_socket_mode: "off",
        pv_priority_factor: 1.0,
      },
    },
    _limits: {
      system: {
        max_total_power: 800,
        min_output_limit: 800,
      },
      devices: {
        WR1: 800,
        WR2: 800,
      },
      fallback_device_max_power: 800,
    },
  };
}

function demoAnalyticsData() {
  const time = [];
  const pv = [];
  const output = [];
  const battery = [];
  const grid = [];
  const home = [];
  const soc = [];
  const target = [];
  const now = Math.floor(Date.now() / 1000);
  for (let index = 0; index < 48; index += 1) {
    time.push(now - (47 - index) * 1800);
    pv.push(Math.round(900 + index * 26));
    output.push(Math.min(800, 420 + index * 13));
    battery.push(Math.round(280 + Math.sin(index / 6) * 300));
    grid.push(Math.round(Math.sin(index / 5) * 180));
    home.push(Math.round(500 + Math.cos(index / 7) * 160));
    soc.push(Math.min(100, 40 + index));
    target.push(Math.min(800, 440 + index * 12));
  }
  return {
    time,
    series: { pv, output, battery, grid, home, soc, target },
    devices: [],
    source: "demo",
  };
}

function ensureDemoBadge() {
  const cluster = document.querySelector ? document.querySelector(".status-cluster") : null;
  if (!cluster || document.getElementById("demoModeBadge")) return;
  const badge = document.createElement("span");
  badge.id = "demoModeBadge";
  badge.className = "pill demo-pill";
  badge.textContent = "Demo mode";
  cluster.appendChild(badge);
}

function initDemoMode() {
  ensureDemoBadge();
  state.runtime = demoRuntimeState();
  const snapshot = demoSnapshot();
  setAnalyticsAvailable(true);
  state.analytics.data = demoAnalyticsData();
  state.history.data = demoAnalyticsData();
  updateSnapshot(snapshot);
  setConnection("Demo", true);
  renderAnalytics();
  renderHistoryChart();
  setFlowView("analytics", false);
}

function currentAnalyticsTab() {
  return ANALYTICS_TABS.find((tab) => tab.id === state.analytics.tab) || ANALYTICS_TABS[0];
}

// Active series = the tab's base series plus any enabled overlays, de-duplicated
// while preserving order (base first, then overlays).
function activeAnalyticsSeries() {
  const series = currentAnalyticsTab().series.slice();
  ANALYTICS_OVERLAYS.forEach((overlay) => {
    if (state.analytics.overlays[overlay.id] && !series.includes(overlay.id)) {
      series.push(overlay.id);
    }
  });
  return series;
}

// Whether the Analytics panel is on screen. The InfluxDB analytics now lives in
// its own dedicated tab, so it only fetches when that tab is active and the
// browser tab is foregrounded (lazy loading).
function analyticsPanelVisible() {
  if (typeof document !== "undefined" && document.hidden) return false;
  return state.flowView === "analytics";
}

function setAnalyticsLoading(active) {
  const node = $("analyticsLoading");
  if (node) node.hidden = !active;
  const chart = $("analyticsChart");
  if (chart && chart.setAttribute) chart.setAttribute("aria-busy", active ? "true" : "false");
}

// Build the series request URL. Precedence: an active zoom viewport wins (so a
// zoomed range re-queries the backend, which picks the finer query profile),
// then an explicit custom range, otherwise the live period token.
function analyticsFetchUrl() {
  const params = new URLSearchParams();
  const zoom = state.analytics.zoom;
  const custom = state.analytics.custom;
  if (zoom && zoom.start && zoom.end) {
    params.set("start", String(zoom.start));
    params.set("end", String(zoom.end));
  } else if (custom.active && custom.start && custom.end) {
    params.set("start", String(custom.start));
    params.set("end", String(custom.end));
  } else {
    params.set("range", state.range);
  }
  params.set("series", activeAnalyticsSeries().join(","));
  if (state.analytics.device) {
    params.set("devices", state.analytics.device);
  }
  return `/api/analytics/series?${params.toString()}`;
}

// Auto-refresh runs only in live mode: paused while zoomed and skipped when the
// panel is off-screen or the tab is backgrounded.
function analyticsShouldAutoRefresh() {
  return analyticsPanelVisible() && !state.analytics.zoom;
}

// Toggle the Analytics tab between its "InfluxDB not configured" info state and
// the live chart body. Returns whether analytics is available.
function setAnalyticsAvailable(available, info) {
  state.analytics.available = available;
  const unavailable = $("analyticsUnavailable");
  const body = $("analyticsBody");
  if (unavailable) {
    unavailable.hidden = available;
    // When InfluxDB is configured but unreachable the API sends an actionable
    // hint; show it (with a matching heading) instead of the default
    // "not configured" copy so the operator knows how to fix it.
    const heading = unavailable.querySelector ? unavailable.querySelector("h3") : null;
    const detail = unavailable.querySelector ? unavailable.querySelector("p") : null;
    const hint = info && info.reason === "unreachable" ? info.hint : null;
    if (heading) heading.textContent = hint
      ? "InfluxDB analytics is not reachable"
      : "InfluxDB analytics is not configured";
    if (detail && hint) {
      // The hint carries an explicit newline before the setup command so the
      // whole command always stays on its own line; preserve it on render.
      detail.style.whiteSpace = "pre-line";
      detail.textContent = hint;
    } else if (detail) {
      detail.style.whiteSpace = "";
      detail.textContent =
        "Enable the optional InfluxDB service to use long-term analytics, " +
        "zooming, and custom date ranges. The Aggregate and Devices views " +
        "keep working without it.";
    }
  }
  if (body) body.hidden = !available;
  return available;
}

async function loadAnalytics(showLoading = true) {
  if (state.demoMode) {
    setAnalyticsAvailable(true);
    state.analytics.data = demoAnalyticsData();
    renderAnalytics();
    return;
  }
  if (showLoading) setAnalyticsLoading(true);
  let payload = null;
  try {
    const response = await fetch(analyticsFetchUrl());
    payload = response.ok ? await response.json() : null;
  } catch (error) {
    payload = null;
  } finally {
    setAnalyticsLoading(false);
  }
  // A 200 payload with available:false means InfluxDB is not configured; show
  // the clean info state instead of an empty/broken chart.
  if (payload && payload.available === false) {
    setAnalyticsAvailable(false, payload);
    state.analytics.data = null;
    return;
  }
  setAnalyticsAvailable(true);
  state.analytics.data = payload;
  renderAnalytics();
}

function setAnalyticsTab(tabId) {
  if (!ANALYTICS_TABS.some((tab) => tab.id === tabId)) return;
  state.analytics.tab = tabId;
  clearZoom();
  renderAnalyticsTabs();
  loadAnalytics();
}

function renderAnalyticsTabs() {
  document.querySelectorAll(".analytics-tabs button").forEach((button) => {
    const active = button.dataset.analyticsTab === state.analytics.tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function toggleAnalyticsOverlay(overlayId) {
  if (!(overlayId in state.analytics.overlays)) return;
  state.analytics.overlays[overlayId] = !state.analytics.overlays[overlayId];
  clearZoom();
  renderAnalyticsOverlays();
  loadAnalytics();
}

function renderAnalyticsOverlays() {
  document.querySelectorAll(".analytics-overlays button").forEach((button) => {
    const active = !!state.analytics.overlays[button.dataset.analyticsOverlay];
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function applyCustomRange(fromValue, toValue) {
  const start = Date.parse(fromValue);
  const end = Date.parse(toValue);
  if (Number.isNaN(start) || Number.isNaN(end) || start >= end) return false;
  state.analytics.custom = {
    active: true,
    start: Math.floor(start / 1000),
    end: Math.floor(end / 1000),
  };
  clearZoom();
  // Selector kept in a variable so this literal does not collide with the
  // marker the node frontend tests use to trim the auto-init tail.
  const rangeSelector = ".range-tabs button";
  document.querySelectorAll(rangeSelector).forEach((item) => item.classList.remove("active"));
  loadAnalytics();
  return true;
}

function clearCustomRange() {
  state.analytics.custom = { active: false, start: null, end: null };
}

// -- Zoom (Fix 1-3) --------------------------------------------------------
//
// A user drag-zoom sets a viewport; we re-query the backend for that range so
// it returns the finer query profile, keep the chart zoomed, and pause live
// auto-refresh until the user returns to live.

let analyticsZoomTimer = null;

// Pure: decide whether a uPlot x-scale window is a zoom-in vs the full extent.
function detectZoom(min, max, dataStart, dataEnd) {
  const nums = [min, max, dataStart, dataEnd];
  if (!nums.every((n) => typeof n === "number" && Number.isFinite(n))) return null;
  const span = dataEnd - dataStart;
  if (!(span > 0)) return null;
  const eps = span * 0.005;
  if (min > dataStart + eps || max < dataEnd - eps) {
    return { start: Math.floor(min), end: Math.ceil(max) };
  }
  return null;
}

function scheduleZoomRequery() {
  // Demo mode has no backend: the chart stays zoomed into the loaded data.
  if (state.demoMode) return;
  if (analyticsZoomTimer) clearTimeout(analyticsZoomTimer);
  analyticsZoomTimer = setTimeout(() => {
    analyticsZoomTimer = null;
    loadAnalytics(true);
  }, 180);
}

function onAnalyticsXScale(chart) {
  if (state.analytics.applyingScale) return;
  const xs = chart.data && chart.data[0];
  if (!xs || xs.length < 2) return;
  const zoom = detectZoom(
    chart.scales.x.min,
    chart.scales.x.max,
    xs[0],
    xs[xs.length - 1]
  );
  if (zoom) {
    state.analytics.zoom = zoom;
    renderZoomControls();
    scheduleZoomRequery();
  }
  // No automatic exit from zoom mode. A real-data requery returns data whose
  // extent equals the zoom range, so a visible scale matching the full extent
  // no longer means "live" -- inferring it here would clear the zoom the instant
  // the finer dataset loads. Zoom is left only by explicit user actions: the
  // Back to live button, ESC, changing period/device/tab, or a new custom range.
}

function clearZoom() {
  state.analytics.zoom = null;
  if (analyticsZoomTimer) {
    clearTimeout(analyticsZoomTimer);
    analyticsZoomTimer = null;
  }
}

function backToLive() {
  clearZoom();
  renderZoomControls();
  loadAnalytics(true);
}

function renderZoomControls() {
  const button = $("analyticsBackToLive");
  if (button) button.hidden = !state.analytics.zoom;
}

function renderAnalytics() {
  renderAnalyticsChart();
  renderAnalyticsKpis();
}

// The Analytics tab is a dedicated analysis workspace, so its primary chart is
// intentionally much larger than the lightweight Aggregate/Devices history.
function analyticsChartHeight() {
  if (typeof window !== "undefined" && window.innerWidth && window.innerWidth <= 760) {
    return 360;
  }
  return 560;
}

// Friendly labels for the chart data-source badge. Makes the SQLite (operational)
// vs InfluxDB (analytics) split visible at a glance on each chart.
const SOURCE_LABELS = {
  sqlite: "SQLite",
  influxdb: "InfluxDB",
  preview: "Preview",
  demo: "Demo",
};

function setSourceBadge(elementId, source) {
  const node = $(elementId);
  if (!node) return;
  const label = SOURCE_LABELS[source];
  if (!label) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.textContent = label;
  node.hidden = false;
}

function cssColor(varName, fallback) {
  if (typeof getComputedStyle !== "function") return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(varName)
    .trim();
  return value || fallback;
}

// Analytics chart display-only sign convention. The backend/API/SQLite/KPI sign
// convention is unchanged (charging positive, discharging negative). For the
// Analytics uPlot chart only we invert the battery line so charging reads below
// zero and discharging above zero, visually separating energy into vs out of the
// battery. This must never be used for KPIs, raw state, or any other chart.
function analyticsChartDisplayValue(seriesId, value) {
  if (value === null || value === undefined) return value;
  if (seriesId === "battery") return -value;
  return value;
}

// Build the uPlot data matrix (x row + one row per series) for the Analytics
// chart. Battery is inverted for display only (see analyticsChartDisplayValue);
// all other series pass through unchanged. Kept pure and exported so the
// display-only inversion is testable without uPlot.
function analyticsChartSeriesData(data, seriesIds) {
  const time = (data && data.time) || [];
  const matrix = [time];
  seriesIds.forEach((id) => {
    const values = (data && data.series && data.series[id]) || [];
    matrix.push(
      time.map((_, index) => {
        const value = values[index];
        if (value === null || value === undefined) return null;
        return analyticsChartDisplayValue(id, Number(value));
      })
    );
  });
  return matrix;
}

// Legend/tooltip value formatter. For the battery line (displayed with an
// inverted sign) the displayed value is translated back into a human-readable
// Charge/Discharge label so the inversion never implies the API sign convention
// changed. Displayed negative == charging (raw positive); positive == discharging.
function analyticsSeriesTooltip(seriesId, displayValue, unit) {
  if (displayValue == null) return "--";
  const rounded = Math.round(displayValue);
  if (seriesId === "battery") {
    if (rounded < 0) return `Charge ${Math.abs(rounded)} ${unit}`;
    if (rounded > 0) return `Discharge ${rounded} ${unit}`;
    return `0 ${unit}`;
  }
  return `${rounded} ${unit}`;
}

// Identity of the uPlot structure (series set, axes, scales). When this is
// unchanged across a refresh the chart can be updated in place with setData()
// instead of being destroyed and recreated -- avoiding canvas setup, layout and
// GC churn. A change (tab, series, overlays, device, axis/pct scale) forces a
// rebuild.
function analyticsChartSignature(chartSeries, usesPctScale) {
  return JSON.stringify({
    tab: state.analytics.tab,
    series: chartSeries,
    overlays: state.analytics.overlays,
    device: state.analytics.device || "",
    pct: usesPctScale,
  });
}

// Re-apply an active zoom viewport to the chart (guarded so the programmatic
// scale change is not mistaken for a fresh user zoom).
function applyAnalyticsZoomToChart(chart) {
  if (state.analytics.zoom && chart.setScale) {
    state.analytics.applyingScale = true;
    chart.setScale("x", {
      min: state.analytics.zoom.start,
      max: state.analytics.zoom.end,
    });
    state.analytics.applyingScale = false;
  }
}

function renderAnalyticsChart() {
  const container = $("analyticsChart");
  if (!container || typeof uPlot === "undefined") return;

  const data = state.analytics.data;
  const time = (data && data.time) || [];
  const empty = $("analyticsEmpty");
  setSourceBadge("analyticsSource", data && data.source);

  // No data: tear the chart down cleanly (and drop its signature) so a later
  // refresh with data rebuilds, then show the empty/unavailable state.
  if (!time.length) {
    if (state.analytics.chart) {
      state.analytics.chart.destroy();
      state.analytics.chart = null;
    }
    state.analytics.chartSignature = null;
    container.innerHTML = "";
    if (empty) {
      const unavailable = !data || (data.meta && data.meta.unavailable);
      empty.textContent = unavailable
        ? "History data is currently unavailable."
        : "No samples in this period.";
      empty.hidden = false;
    }
    return;
  }
  if (empty) empty.hidden = true;

  const chartSeries = activeAnalyticsSeries().filter((id) => ANALYTICS_SERIES_META[id]);
  let usesPctScale = false;
  chartSeries.forEach((id) => {
    if (ANALYTICS_SERIES_META[id].scaleId === "pct") usesPctScale = true;
  });
  const signature = analyticsChartSignature(chartSeries, usesPctScale);
  // Battery is inverted here for display only; raw state.analytics.data is untouched.
  const seriesData = analyticsChartSeriesData(data, chartSeries);

  // Reuse path: same structure + a live chart in a still-attached container ->
  // update the existing instance in place instead of recreating it.
  const containerValid = !container.isConnected || container.isConnected === true;
  if (
    state.analytics.chart &&
    state.analytics.chartSignature === signature &&
    containerValid &&
    state.analytics.chart.setData
  ) {
    state.analytics.applyingScale = true;
    state.analytics.chart.setData(seriesData);
    state.analytics.applyingScale = false;
    applyAnalyticsZoomToChart(state.analytics.chart);
    renderZoomControls();
    return;
  }

  // Rebuild path: structure changed (or no chart yet) -> recreate.
  if (state.analytics.chart) {
    state.analytics.chart.destroy();
    state.analytics.chart = null;
  }
  container.innerHTML = "";

  const seriesConfig = [{}];
  chartSeries.forEach((id) => {
    const meta = ANALYTICS_SERIES_META[id];
    const isOverlay = !currentAnalyticsTab().series.includes(id);
    seriesConfig.push({
      label: meta.label,
      stroke: cssColor(meta.colorVar, "#888"),
      width: 2,
      dash: isOverlay ? [6, 4] : undefined,
      scale: meta.scaleId || "y",
      value: (_self, raw) => analyticsSeriesTooltip(id, raw, meta.unit),
    });
  });

  const axisColor = cssColor("--muted", "#8a94a3");
  const gridColor = "rgba(255,255,255,0.06)";
  const scales = { x: { time: true }, y: {} };
  const axes = [
    { stroke: axisColor, grid: { stroke: gridColor }, ticks: { stroke: gridColor } },
    { scale: "y", stroke: axisColor, grid: { stroke: gridColor }, ticks: { stroke: gridColor } },
  ];
  if (usesPctScale) {
    scales.pct = { range: [0, 100] };
    axes.push({
      scale: "pct",
      side: 1,
      stroke: cssColor("--accent", "#8b5cf6"),
      grid: { show: false },
      values: (_self, ticks) => ticks.map((t) => `${t}%`),
    });
  }
  const opts = {
    width: container.clientWidth || 600,
    height: analyticsChartHeight(),
    series: seriesConfig,
    scales,
    axes,
    // Crosshair with synchronized cursor (shared key for any future charts);
    // the live legend reads every series value at the cursor position.
    cursor: { sync: { key: "analytics" }, focus: { prox: 24 } },
    legend: { live: true },
    hooks: {
      setScale: [
        (chart, key) => {
          if (key === "x") onAnalyticsXScale(chart);
        },
      ],
    },
  };
  // Guard programmatic scale changes (construction + re-applying the zoom
  // viewport) so they aren't treated as a fresh user zoom.
  state.analytics.applyingScale = true;
  const chart = new uPlot(opts, seriesData, container);
  state.analytics.chart = chart;
  state.analytics.chartSignature = signature;
  state.analytics.applyingScale = false;
  applyAnalyticsZoomToChart(chart);
  renderZoomControls();
}

function integrateSeries(data, id, transform) {
  if (!data || !data.time || !data.series) return null;
  const time = data.time;
  const values = data.series[id];
  if (!values || values.length < 2) return null;
  let wh = 0;
  let counted = 0;
  for (let index = 1; index < time.length; index += 1) {
    const previous = values[index - 1];
    const current = values[index];
    if (previous == null || current == null) continue;
    const dt = time[index] - time[index - 1];
    if (!(dt > 0)) continue;
    wh += ((transform(Number(previous)) + transform(Number(current))) / 2) * (dt / 3600);
    counted += 1;
  }
  return counted ? wh : null;
}

function energyLabel(wh) {
  if (wh == null) return "--";
  if (Math.abs(wh) >= 1000) return `${(wh / 1000).toFixed(1)} kWh`;
  return `${Math.round(wh)} Wh`;
}

function powerLabel(w) {
  if (w == null) return "--";
  if (Math.abs(w) >= 1000) return `${(w / 1000).toFixed(2)} kW`;
  return `${Math.round(w)} W`;
}

function seriesPeak(data, id) {
  if (!data || !data.series) return null;
  const values = data.series[id];
  if (!values || !values.length) return null;
  let max = null;
  values.forEach((value) => {
    if (value == null) return;
    const number = Number(value);
    if (max == null || number > max) max = number;
  });
  return max;
}

function runtimeRoleLabel(snapshot) {
  if (!snapshot || !snapshot.devices) return "--";
  const names = state.analytics.device
    ? [state.analytics.device]
    : Object.keys(snapshot.devices);
  if (!names.length) return "--";
  let anyInput = false;
  let anyOutput = false;
  names.forEach((name) => {
    const device = snapshot.devices[name];
    if (!device) return;
    if (Number(device.ac_mode) === 1) anyInput = true;
    else anyOutput = true;
  });
  if (anyInput && anyOutput) return "Mixed";
  if (anyInput) return "AC Input";
  return "Output";
}

// Stable identity for a loaded analytics dataset. When this is unchanged the
// integrated (series-based) KPI values are guaranteed identical, so they can be
// served from cache instead of re-integrated.
function analyticsDataKey(data) {
  if (!data || !data.time) return "empty";
  return JSON.stringify({
    source: data.source || "",
    from: data.time[0] || null,
    to: data.time[data.time.length - 1] || null,
    points: data.time.length,
    series: Object.keys(data.series || {}).sort(),
  });
}

// Integrate every series-based KPI once for the given dataset. Live KPIs (soc,
// role) are excluded -- they read the cheap snapshot at render time.
function computeAnalyticsDataKpis(data) {
  const values = {};
  Object.keys(ANALYTICS_KPIS).forEach((id) => {
    const spec = ANALYTICS_KPIS[id];
    if (spec.live) return;
    values[id] = spec.compute(data, null);
  });
  return values;
}

// Recompute the series-based KPI cache only when the analytics data identity
// changed (data refresh / range / device / overlay change re-load the data).
function ensureAnalyticsKpiCache() {
  const cache = state.analytics.kpiCache;
  const key = analyticsDataKey(state.analytics.data);
  if (cache.dataKey !== key) {
    cache.dataKey = key;
    cache.values = computeAnalyticsDataKpis(state.analytics.data);
  }
  return cache;
}

function analyticsKpiCardHtml(id, label, value, tone) {
  return (
    `<div class="analytics-kpi tone-${escapeHtml(tone)}" data-analytics-kpi="${escapeHtml(id)}">` +
    `<span class="analytics-kpi-label">${escapeHtml(label)}</span>` +
    `<span class="analytics-kpi-value">${escapeHtml(value)}</span>` +
    `</div>`
  );
}

// Full KPI render: rebuilds the KPI card DOM for the active tab. Series-based
// values come from the cache (integrated only on data change); live values are
// read from the current snapshot. Called on analytics data refresh / tab /
// range / device / overlay change -- not on every live snapshot.
function renderAnalyticsKpis() {
  const host = $("analyticsKpis");
  if (!host) return;
  const cache = ensureAnalyticsKpiCache();
  const snapshot = state.snapshot;
  const cards = currentAnalyticsTab()
    .kpis.map((id) => {
      const spec = ANALYTICS_KPIS[id];
      if (!spec) return null;
      const value = spec.live
        ? spec.compute(null, snapshot)
        : cache.values[id];
      return analyticsKpiCardHtml(id, spec.label(state.range), value, spec.tone);
    })
    .filter(Boolean);
  host.innerHTML = cards.join("");
}

// Cheap live KPI refresh: updates only the live KPI card values (Current SoC,
// Runtime Role) in place, without rebuilding DOM or re-integrating any series.
// Used by the live SSE/poll snapshot path while the Analytics tab is visible.
function renderAnalyticsLiveKpis(snapshot) {
  const host = $("analyticsKpis");
  if (!host || !host.querySelector) return;
  currentAnalyticsTab().kpis.forEach((id) => {
    const spec = ANALYTICS_KPIS[id];
    if (!spec || !spec.live) return;
    const card = host.querySelector(`[data-analytics-kpi="${id}"] .analytics-kpi-value`);
    if (card) card.textContent = spec.compute(null, snapshot);
  });
}

// Shared device <select> filler used by both the lightweight History panel and
// the InfluxDB Analytics tab. ``store`` is the per-view state object that caches
// the option list and holds the currently selected device.
function fillDeviceSelect(selectId, snapshot, store) {
  const select = $(selectId);
  if (!select || !select.options) return;
  const names = snapshot && snapshot.devices ? Object.keys(snapshot.devices) : [];
  if (names.join("|") === store.deviceOptions.join("|")) return;
  store.deviceOptions = names;

  const current = store.device;
  while (select.options.length > 1) {
    select.remove(1);
  }
  names.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  });
  if (current && names.includes(current)) {
    select.value = current;
  } else {
    select.value = "";
    store.device = "";
  }
}

function populateDeviceSelector(snapshot) {
  fillDeviceSelect("analyticsDevice", snapshot, state.analytics);
  fillDeviceSelect("historyDevice", snapshot, state.history);
}

// -- Lightweight SQLite history (Aggregate / Devices) ----------------------
//
// Deliberately minimal: a single combined chart of the default series, backed
// by the always-available local SQLite snapshot store (/api/history/series).
// No overlays, sub-tabs, zoom, KPIs or custom ranges — those advanced features
// live in the InfluxDB Analytics tab so these operational views stay fast.

const HISTORY_SERIES = ["pv", "output", "battery"];

function historyVisible() {
  if (typeof document !== "undefined" && document.hidden) return false;
  return state.flowView === "aggregated" || state.flowView === "devices";
}

function historyFetchUrl() {
  const params = new URLSearchParams();
  params.set("range", state.history.range);
  params.set("series", HISTORY_SERIES.join(","));
  if (state.history.device) params.set("devices", state.history.device);
  return `/api/history/series?${params.toString()}`;
}

function setHistoryLoading(active) {
  const node = $("historyLoading");
  if (node) node.hidden = !active;
}

async function loadHistory(showLoading = true) {
  if (state.demoMode) {
    state.history.data = demoAnalyticsData();
    renderHistoryChart();
    return;
  }
  if (showLoading) setHistoryLoading(true);
  try {
    const response = await fetch(historyFetchUrl());
    state.history.data = response.ok ? await response.json() : null;
  } catch (error) {
    state.history.data = null;
  } finally {
    setHistoryLoading(false);
  }
  renderHistoryChart();
}

function historyChartHeight() {
  if (typeof window !== "undefined" && window.innerWidth && window.innerWidth <= 760) {
    return 200;
  }
  return 260;
}

function renderHistoryChart() {
  const container = $("historyChart");
  if (!container || typeof uPlot === "undefined") return;

  const data = state.history.data;
  const time = (data && data.time) || [];
  const empty = $("historyEmpty");
  setSourceBadge("historySource", data && data.source);

  // No data: tear the chart down (and drop its signature) so a later refresh
  // with data rebuilds, then show the empty/unavailable state.
  if (!time.length) {
    if (state.history.chart) {
      state.history.chart.destroy();
      state.history.chart = null;
    }
    state.history.chartSignature = null;
    container.innerHTML = "";
    if (empty) {
      const unavailable = !data || (data.meta && data.meta.unavailable);
      empty.textContent = unavailable
        ? "History data is currently unavailable."
        : "No samples in this period.";
      empty.hidden = false;
    }
    return;
  }
  if (empty) empty.hidden = true;

  const seriesIds = HISTORY_SERIES.filter((id) => ANALYTICS_SERIES_META[id]);
  // Reuse the Analytics matrix builder so the History chart (Aggregate/Devices)
  // shares its display-only battery inversion (negative == charging, positive ==
  // discharging) and null handling.
  const seriesData = analyticsChartSeriesData(data, seriesIds);

  // The history chart structure only varies by the (fixed) series set and the
  // selected device, so reuse the instance in place across refreshes when those
  // are unchanged.
  const signature = JSON.stringify({ series: seriesIds, device: state.history.device || "" });
  if (
    state.history.chart &&
    state.history.chartSignature === signature &&
    state.history.chart.setData
  ) {
    state.history.chart.setData(seriesData);
    return;
  }

  if (state.history.chart) {
    state.history.chart.destroy();
    state.history.chart = null;
  }
  container.innerHTML = "";

  const seriesConfig = [{}];
  seriesIds.forEach((id) => {
    const meta = ANALYTICS_SERIES_META[id];
    seriesConfig.push({
      label: meta.label,
      stroke: cssColor(meta.colorVar, "#888"),
      width: 2,
      scale: "y",
      // Reuse the Analytics tooltip so the inverted battery line reads back as
      // Charge/Discharge instead of a sign-flipped raw watt value.
      value: (_self, raw) => analyticsSeriesTooltip(id, raw, meta.unit),
    });
  });

  const axisColor = cssColor("--muted", "#8a94a3");
  const gridColor = "rgba(255,255,255,0.06)";
  const opts = {
    width: container.clientWidth || 600,
    height: historyChartHeight(),
    series: seriesConfig,
    scales: { x: { time: true }, y: {} },
    axes: [
      { stroke: axisColor, grid: { stroke: gridColor }, ticks: { stroke: gridColor } },
      { scale: "y", stroke: axisColor, grid: { stroke: gridColor }, ticks: { stroke: gridColor } },
    ],
    cursor: { focus: { prox: 24 } },
    legend: { live: true },
  };
  state.history.chart = new uPlot(opts, seriesData, container);
  state.history.chartSignature = signature;
}

function startEvents() {
  state.liveTransport = "sse";
  loadLiveOnce();

  if (!window.EventSource) {
    startPollingOnce();
    return;
  }

  const source = new EventSource("/api/events");
  let receivedTelemetry = false;
  let telemetryVersion = 0;
  let fallbackStarted = false;

  function fallbackToPolling() {
    if (fallbackStarted) return;
    fallbackStarted = true;
    source.close();
    startPollingOnce();
  }

  function scheduleFallbackIfNoTelemetry(version) {
    window.setTimeout(() => {
      if (telemetryVersion === version) {
        fallbackToPolling();
      }
    }, SSE_TELEMETRY_TIMEOUT_MS);
  }

  scheduleFallbackIfNoTelemetry(telemetryVersion);

  source.addEventListener("telemetry", (event) => {
    receivedTelemetry = true;
    telemetryVersion += 1;
    state.liveTransport = "sse";
    updateSnapshot(JSON.parse(event.data));
  });

  source.onerror = () => {
    setConnection("Reconnecting", false);
    if (!receivedTelemetry) {
      fallbackToPolling();
      return;
    }

    scheduleFallbackIfNoTelemetry(telemetryVersion);
  };
}

async function fetchLiveSnapshot() {
  const response = await fetch("/api/live");
  if (!response.ok) {
    throw new Error(`live_status_${response.status}`);
  }
  return response.json();
}

async function loadLiveOnce() {
  try {
    updateSnapshot(await fetchLiveSnapshot());
  } catch {
    setConnection("Offline", false);
  }
}

function startPollingOnce() {
  if (pollingStarted) return;
  pollingStarted = true;
  state.liveTransport = "polling";
  setConnection("Polling", true);
  startPolling();
}

function startPolling() {
  pollingIntervalId = setInterval(async () => {
    try {
      updateSnapshot(await fetchLiveSnapshot());
    } catch {
      setConnection("Offline", false);
    }
  }, 2000);
}

function resetLiveTransportForTests() {
  pollingStarted = false;
  if (pollingIntervalId && typeof clearInterval === "function") {
    clearInterval(pollingIntervalId);
  }
  pollingIntervalId = null;
  state.liveTransport = "sse";
}

const HEARTBEAT_MIN_INTERVAL_MS = 60000;
let lastHeartbeatAt = 0;

function shouldSendHeartbeat(now, lastSent) {
  return now - lastSent >= HEARTBEAT_MIN_INTERVAL_MS;
}

async function sendSessionHeartbeat() {
  if (state.demoMode) return;
  // Only a genuine, authenticated session slides; background polling never calls
  // this, and without a CSRF token the server would reject it anyway.
  if (!state.auth.authenticated || !state.auth.csrfToken) return;
  const now = typeof Date !== "undefined" ? Date.now() : 0;
  if (!shouldSendHeartbeat(now, lastHeartbeatAt)) return;
  lastHeartbeatAt = now;
  try {
    const response = await fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": state.auth.csrfToken },
    });
    if (response.status === 401 || response.status === 403) {
      // Session expired or hit the absolute cap; refresh auth so the UI flips.
      await loadAuthStatus();
    }
  } catch {
    // Ignore transient failures; the next genuine interaction retries.
  }
}

function handleSessionActivity() {
  if (typeof document !== "undefined" && document.hidden) return;
  sendSessionHeartbeat();
}

function initSessionHeartbeat() {
  if (typeof document === "undefined" || !document.addEventListener) return;
  ["mousemove", "keydown", "click"].forEach((eventName) => {
    document.addEventListener(eventName, handleSessionActivity, { passive: true });
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) handleSessionActivity();
  });
}

function initDashboardApp() {
  const rangeTabSelector = ".range-tabs button";
  document.querySelectorAll(rangeTabSelector).forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll(rangeTabSelector).forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.range = button.dataset.range;
      clearCustomRange();
      clearZoom();
      renderZoomControls();
      await loadAnalytics();
    });
  });

  const backToLiveButton = $("analyticsBackToLive");
  if (backToLiveButton) {
    backToLiveButton.addEventListener("click", () => backToLive());
  }
  // ESC returns to live while zoomed (uPlot double-click already resets too).
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.analytics.zoom) backToLive();
  });

  document.querySelectorAll(".analytics-tabs button").forEach((button) => {
    button.addEventListener("click", () => setAnalyticsTab(button.dataset.analyticsTab));
  });
  renderAnalyticsTabs();

  document.querySelectorAll(".analytics-overlays button").forEach((button) => {
    button.addEventListener("click", () => toggleAnalyticsOverlay(button.dataset.analyticsOverlay));
  });
  renderAnalyticsOverlays();

  const customApply = $("analyticsCustomApply");
  if (customApply) {
    customApply.addEventListener("click", () => {
      const from = $("analyticsCustomFrom");
      const to = $("analyticsCustomTo");
      applyCustomRange(from ? from.value : "", to ? to.value : "");
    });
  }

  const deviceSelect = $("analyticsDevice");
  if (deviceSelect) {
    deviceSelect.addEventListener("change", async () => {
      state.analytics.device = deviceSelect.value;
      clearZoom();
      renderZoomControls();
      await loadAnalytics();
    });
  }

  // Lightweight History panel controls (SQLite, Aggregate/Devices views).
  const historyRangeSelector = ".history-range-tabs button";
  document.querySelectorAll(historyRangeSelector).forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll(historyRangeSelector).forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.history.range = button.dataset.historyRange;
      await loadHistory();
    });
  });

  const historyDeviceSelect = $("historyDevice");
  if (historyDeviceSelect) {
    historyDeviceSelect.addEventListener("change", async () => {
      state.history.device = historyDeviceSelect.value;
      await loadHistory();
    });
  }

  window.addEventListener("resize", () => {
    const container = $("analyticsChart");
    if (state.analytics.chart && container) {
      state.analytics.chart.setSize({
        width: container.clientWidth || 600,
        height: analyticsChartHeight(),
      });
    }
    const historyContainer = $("historyChart");
    if (state.history.chart && historyContainer) {
      state.history.chart.setSize({
        width: historyContainer.clientWidth || 600,
        height: historyChartHeight(),
      });
    }
  });

  initFlowViewSwitch();
  initAuthControls();
  initRuntimeForms();
  initDiagnose();
  initLogs();
  initSessionHeartbeat();
  applyAnimationMode();
  if (state.demoMode) {
    initDemoMode();
  } else {
    loadAuthStatus();
    loadRuntimeState();
    startEvents();
    // Lazy initial load: only fetch the data source whose view is on screen.
    if (state.flowView === "analytics") loadAnalytics();
    if (historyVisible()) loadHistory();
    setInterval(loadAuthStatus, 60000);
    // Periodic refresh skips fetching while a panel is off-screen, the tab is
    // backgrounded (lazy loading), or the analytics chart is zoomed.
    setInterval(() => {
      if (analyticsShouldAutoRefresh()) loadAnalytics(false);
      if (historyVisible()) loadHistory(false);
    }, 30000);
  }
}

// document.querySelectorAll(".range-tabs button")
if (typeof window !== "undefined" && typeof document !== "undefined") {
  initDashboardApp();
}

if (typeof module !== "undefined") {
  module.exports = {
    state,
    escapeHtml,
    deviceValue,
    firmwareEnumLabel,
    socLimitStatusLabel,
    packStateLabel,
    acModeLabel,
    acStatusLabel,
    dcStatusLabel,
    gridStateLabel,
    socStatusLabel,
    gridOffModeOptionLabel,
    acPathLabel,
    acPathIcon,
    renderDeviceFirmwareStatus,
    deviceFirmwareStatusFacts,
    deviceBatteryVisual,
    renderDevices,
    renderControlExplain,
    renderEnergyStats,
    renderDeviceFlow,
    updateSnapshot,
    renderSnapshot,
    renderViewSnapshot,
    renderGlobalSnapshotMetrics,
    renderAggregatedSnapshot,
    flowActive,
    flowSpeedBucket,
    deviceFlowSignature,
    setFlowView,
    setAnimationMode,
    applyAnimationMode,
    ANALYTICS_TABS,
    ANALYTICS_KPIS,
    ANALYTICS_OVERLAYS,
    ANALYTICS_SERIES_META,
    currentAnalyticsTab,
    activeAnalyticsSeries,
    analyticsPanelVisible,
    analyticsShouldAutoRefresh,
    analyticsFetchUrl,
    setAnalyticsAvailable,
    setSourceBadge,
    historyVisible,
    historyFetchUrl,
    loadHistory,
    HISTORY_SERIES,
    renderHistoryChart,
    detectZoom,
    onAnalyticsXScale,
    backToLive,
    clearZoom,
    toggleAnalyticsOverlay,
    applyCustomRange,
    clearCustomRange,
    renderAnalyticsKpis,
    renderAnalyticsLiveKpis,
    renderAnalyticsChart,
    analyticsDataKey,
    computeAnalyticsDataKpis,
    ensureAnalyticsKpiCache,
    analyticsChartSignature,
    analyticsChartDisplayValue,
    analyticsChartSeriesData,
    analyticsSeriesTooltip,
    integrateSeries,
    seriesPeak,
    energyLabel,
    powerLabel,
    runtimeRoleLabel,
    renderDiagnoseReport,
    renderDiagnoseView,
    diagnoseAuthState,
    diagnoseStatusTone,
    renderLogRows,
    trimLogRows,
    logsAuthState,
    setServiceLogLevel,
    shouldSendHeartbeat,
    sendSessionHeartbeat,
    runtimeControlPanel,
    runtimeDeviceForm,
    runtimeNumber,
    initRuntimeForms,
    submitRuntimeForm,
    loadRuntimeState,
    activeRuntimeEditorElement,
    isRuntimeEditorEditing,
    clearRuntimeEditorState,
    setRuntimeFeedback,
    setBatteryFill,
    startEvents,
    startPollingOnce,
    resetLiveTransportForTests,
  };
}
