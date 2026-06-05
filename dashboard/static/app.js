const state = {
  snapshot: null,
  range: "6h",
  history: [],
  flowView: "aggregated",
  demoMode: isDemoMode(),
  liveTransport: "sse",
  auth: {
    configured: false,
    authenticated: false,
    csrfToken: null,
  },
  runtime: null,
};

const SSE_TELEMETRY_TIMEOUT_MS = 3000;
let pollingStarted = false;
let pollingIntervalId = null;

const charts = [
  { id: "chartPv", title: "PV", field: "pv_total_w", color: "#f1c84b", unit: "W" },
  { id: "chartSoc", title: "SOC", field: "average_soc", color: "#62d88a", unit: "%" },
  { id: "chartOutput", title: "Output", field: "inverter_output_w", color: "#5fc8e8", unit: "W" },
  { id: "chartHome", title: "Home", field: "home_load_w", color: "#7aa8ff", unit: "W" },
  { id: "chartBattery", title: "Battery", field: "battery_power_w", color: "#f06d6d", unit: "W" },
];

const FLOW_THRESHOLD_W = 2;
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

function renderSnapshot(snapshot) {
  const batteryFlow = normalizeBatteryPowerForDisplay(aggregatedBatteryPowerW(snapshot));
  const gridPower = Number(snapshot.grid_power_w || 0);
  const pvPower = Number(snapshot.pv_total_w || 0);
  const inverterPower = Number(snapshot.inverter_output_w || 0);
  const homeLoad = Number(snapshot.home_load_w || 0);
  const soc = clamp(Number(snapshot.average_soc || 0), 0, 100);

  setText("metricPv", watts(snapshot.pv_total_w));
  setText("metricHome", watts(snapshot.home_load_w));
  setText("metricGrid", watts(snapshot.grid_power_w));
  setText("metricBattery", signedWatts(batteryFlow.valueW));
  setText("metricSoc", pct(snapshot.average_soc));
  setText("lastUpdated", new Date(snapshot.timestamp).toLocaleTimeString());

  setText("flowPv", watts(snapshot.pv_total_w));
  setText("flowBattery", signedWatts(batteryFlow.valueW));
  setText("flowInverter", watts(snapshot.inverter_output_w));
  setText("flowHome", watts(snapshot.home_load_w));
  setText("flowGrid", watts(snapshot.grid_power_w));
  setText("flowBatterySoc", pct(soc));
  setText("flowBatteryState", batteryStateLabel(batteryFlow));
  setText("flowGridDirection", gridDirectionLabel(gridPower));

  setBatteryFill("flowBatteryFill", soc);
  setVisualState("visualPv", pvPower > FLOW_THRESHOLD_W, "active");
  setVisualState(
    "visualBattery",
    batteryFlow.absW > FLOW_THRESHOLD_W,
    batteryFlow.state
  );
  setVisualState("visualInverter", inverterPower > FLOW_THRESHOLD_W, "active");
  setVisualState("visualHome", homeLoad > FLOW_THRESHOLD_W, "active");
  setVisualState(
    "visualGrid",
    Math.abs(gridPower) > FLOW_THRESHOLD_W,
    gridPower > FLOW_THRESHOLD_W ? "importing" : gridPower < -FLOW_THRESHOLD_W ? "exporting" : "neutral"
  );

  setPipe("pipePvInverter", pvPower, "forward");
  // The battery path is drawn battery -> inverter; in the current SVG dash
  // animation, reverse visibly flows inverter -> battery for charging.
  setPipe("pipeBatteryInverter", batteryFlow.absW, batteryPipeDirection(batteryFlow));
  setPipe("pipeInverterHome", inverterPower, "forward");
  setPipe("pipeGridHome", Math.abs(gridPower), gridPower < -FLOW_THRESHOLD_W ? "reverse" : "forward");

  renderRules(snapshot.rules || {});
  renderDevices(snapshot.devices || {});
  renderEnergyStats(snapshot.energy_stats);
  renderDeviceFlow(snapshot);
  renderControlExplain(snapshot);
  setFlowView(state.flowView, false);
}

function setPipe(id, value, direction = "forward") {
  const el = $(id);
  if (!el) return;
  const wattsValue = Math.abs(Number(value || 0));
  const active = wattsValue > FLOW_THRESHOLD_W;
  const intensity = active ? clamp(wattsValue / 1400, 0.22, 1) : 0;

  el.classList.toggle("active", active);
  el.classList.toggle("idle", !active);
  el.classList.toggle("reverse", direction === "reverse");
  el.style.setProperty("--pipe-alpha", active ? String(0.34 + intensity * 0.56) : "0.12");
  el.style.setProperty("--pipe-width", `${active ? 3 + intensity * 3 : 3}px`);
  el.style.setProperty("--pipe-glow", active ? String(0.10 + intensity * 0.30) : "0.08");
  el.style.setProperty("--pipe-speed", `${1.85 - intensity * 0.72}s`);
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

function setBatteryFill(id, soc) {
  const el = $(id);
  if (!el) return;
  el.setAttribute("width", String(42 * clamp(soc, 0, 100) / 100));
  el.classList.toggle("low", soc < 20);
  el.classList.toggle("full", soc >= 90);
}

function renderRules(rules) {
  const list = $("rulesList");
  const labels = [
    ["ems_enabled", "EMS enabled", "rule"],
    ["soc_limit_active", "SOC limit", "warning"],
    ["output_limit_active", "Output limit", "charge"],
    ["winter_soc_mode", "Winter mode", "battery"],
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
  grid.innerHTML = "";
  const entries = normalizeDeviceEntries(devices);

  entries.forEach(([name, device]) => {
    const card = document.createElement("article");
    card.className = "device-card";
    const soc = clamp(deviceSoc(device), 0, 100);
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
        <div class="soc-bar"><div class="soc-fill" style="width:${soc}%"></div></div>
        <div class="soc-mode">${deviceBatteryState} ${signedWatts(batteryFlow.valueW)}</div>
      </div>
      <div class="device-values">
        ${deviceValue("PV", watts(devicePvPower(device)), "solar")}
        ${deviceValue("Output", watts(deviceOutputPower(device)), "inverter")}
        ${deviceValue("Battery", signedWatts(batteryFlow.valueW), batteryFlow.isCharging ? "charge" : "battery")}
        ${deviceValue("Target", watts(device.target_w), "gauge")}
        ${deviceValue("Limit", watts(device.output_limit_w), "warning")}
        ${deviceValue("Mode", device.mode || device.ac_mode || "--", "rule")}
      </div>
    `;
    grid.appendChild(card);
  });

  if (!entries.length) {
    grid.innerHTML = `<article class="device-card"><span class="device-label">Waiting for EMS telemetry</span></article>`;
  }
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
  const rows = entries
    .map(([name, device], index) => deviceFlowRow(name, device || {}, layout.firstRowY + index * layout.rowHeight, layout, homeY))
    .join("");

  container.innerHTML = `
    <svg class="device-flow-svg" viewBox="0 0 ${layout.width} ${viewHeight}" role="img" aria-label="Per-device PV, battery, inverter, home and grid energy flow">
      <g class="device-flow-layer" aria-hidden="true">
        ${rows}
      </g>
      ${deviceSharedVisuals(layout.sharedX, homeY, gridY, homeLoad, gridPower)}
    </svg>
  `;
}

function renderControlExplain(snapshot) {
  const container = $("controlExplainView");
  if (!container) return;

  const explain = snapshot?.control_explain;
  if (!explain || typeof explain !== "object") {
    container.innerHTML = `
      <div class="control-decision-board">
        <div class="control-empty">No control explanation data available yet.</div>
        ${runtimeControlPanel()}
      </div>
    `;
    return;
  }

  const notes = Array.isArray(explain.notes)
    ? explain.notes.filter(hasExplainValue)
    : [];
  const devices = normalizeControlDeviceEntries(explain.devices);
  const weightContext = controlWeightContext(devices);
  const deviceFlows = devices.length
    ? devices.map(([name, device]) => controlDeviceCard(name, device || {}, explain, weightContext)).join("")
    : `<div class="control-empty compact">No device explanation data available.</div>`;

  container.innerHTML = `
    <div class="control-decision-board">
      ${runtimeControlPanel()}
      ${controlGlobalPipeline(explain, devices, snapshot)}
      ${controlContextRail(explain, devices, notes)}
      <div class="control-device-list">${deviceFlows}</div>
    </div>
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

function controlReason(value) {
  return controlText(value).replaceAll("_", " ");
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

function deviceFlowRow(name, device, y, layout, homeY) {
  const safeName = escapeHtml(name || "Unknown");
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
    <g class="device-flow-device" data-device="${safeName}">
      ${devicePipeGroup("pv", pvPower, `M${pvX + 184} ${pvMidY} H${leftJoinX} V${inverterPvPortY} H${inverterX}`)}
      ${devicePipeGroup("battery", batteryFlow.absW, `M${batteryX + 184} ${batteryMidY} H${leftJoinX} V${inverterBatteryPortY} H${inverterX}`, batteryPipeDirection(batteryFlow))}
      ${devicePipeGroup("output", outputPower, `M${inverterX + 196} ${inverterMidY} H${homeJoinX} V${homeMidY} H${sharedX}`)}
      ${deviceSolarVisual(pvX, pvY, `${safeName} PV`, watts(pvPower), pvPower > FLOW_THRESHOLD_W)}
      ${deviceBatteryVisual(batteryX, batteryY, batteryStateText, signedWatts(batteryFlow.valueW), soc, batteryFlow.absW > FLOW_THRESHOLD_W, batteryFlow.state)}
      ${deviceInverterVisual(inverterX, inverterY, safeName, watts(outputPower), outputPower > FLOW_THRESHOLD_W)}
    </g>
  `;
}

function devicePipeGroup(kind, value, path, direction = "forward") {
  const wattsValue = Math.abs(Number(value || 0));
  const active = wattsValue > FLOW_THRESHOLD_W;
  const intensity = active ? clamp(wattsValue / 1400, 0.22, 1) : 0;
  const classes = [
    "energy-pipe",
    kind,
    active ? "active" : "idle",
    direction === "reverse" ? "reverse" : "",
  ].filter(Boolean).join(" ");
  const style = [
    `--pipe-alpha:${active ? 0.34 + intensity * 0.56 : 0.12}`,
    `--pipe-width:${active ? 3 + intensity * 3 : 3}px`,
    `--pipe-glow:${active ? 0.10 + intensity * 0.30 : 0.08}`,
    `--pipe-speed:${1.85 - intensity * 0.72}s`,
  ].join(";");

  return `
    <g class="${classes}" style="${style}">
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

function deviceSolarVisual(x, y, label, value, active) {
  return `
    <g class="${deviceVisualClasses("solar-visual", active)}" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="184" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="72" height="56" rx="24"></rect>
      <circle class="solar-sun" cx="46" cy="27" r="8"></circle>
      <g class="solar-panel" transform="translate(27 39) scale(.52)">
        <path class="panel-face" d="M0 0h72l20 48H18Z"></path>
        <path class="panel-grid" d="M17 0 24 48M36 0 46 48M55 0 68 48M8 16h72M14 32h72"></path>
        <path class="panel-reflect" d="M7 3h32l8 16H14Z"></path>
      </g>
      <text class="visual-label" x="166" y="32" text-anchor="end">${label}</text>
      <text class="visual-value" x="166" y="56" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceInverterVisual(x, y, label, value, active) {
  return `
    <g class="${deviceVisualClasses("inverter-visual", active)}" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="196" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="14" y="10" width="76" height="56" rx="26"></rect>
      <rect class="inverter-body" x="34" y="20" width="38" height="40" rx="10"></rect>
      <path class="inverter-wave" d="M41 41c5-12 10 12 15 0s10 12 15 0"></path>
      <circle class="inverter-led" cx="65" cy="29" r="3"></circle>
      <text class="visual-label" x="174" y="32" text-anchor="end">${label}</text>
      <text class="visual-value" x="174" y="56" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceBatteryVisual(x, y, stateText, value, soc, active, mode) {
  const fillClass = soc < 20 ? " low" : soc >= 90 ? " full" : "";
  return `
    <g class="${deviceVisualClasses("battery-visual", active, mode)}" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="184" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="72" height="56" rx="24"></rect>
      <rect class="battery-case" x="24" y="27" width="52" height="23" rx="7"></rect>
      <rect class="battery-cap" x="76" y="35" width="5" height="8" rx="2"></rect>
      <rect class="battery-fill${fillClass}" x="29" y="32" width="${42 * soc / 100}" height="13" rx="4"></rect>
      <text class="battery-soc" x="50" y="43" text-anchor="middle">${pct(soc)}</text>
      <text class="visual-state" x="166" y="20" text-anchor="end">${stateText}</text>
      <text class="visual-label" x="166" y="39" text-anchor="end">Battery</text>
      <text class="visual-value" x="166" y="61" text-anchor="end">${value}</text>
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
      ${deviceHomeVisual(x, homeY, watts(homeLoad), homeLoad > FLOW_THRESHOLD_W)}
      ${deviceGridVisual(x, gridY, gridDirection, watts(gridPower), Math.abs(gridPower) > FLOW_THRESHOLD_W, gridPower > FLOW_THRESHOLD_W ? "importing" : gridPower < -FLOW_THRESHOLD_W ? "exporting" : "neutral")}
    </g>
  `;
}

function deviceHomeVisual(x, y, value, active) {
  return `
    <g class="${deviceVisualClasses("home-visual", active)}" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="176" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="68" height="56" rx="24"></rect>
      <path class="home-roof" d="M27 39 46 24l19 15"></path>
      <path class="home-body" d="M32 38v18h28V38"></path>
      <path class="home-door" d="M43 56V45h8v11"></path>
      <text class="visual-label" x="158" y="32" text-anchor="end">Home</text>
      <text class="visual-value" x="158" y="56" text-anchor="end">${value}</text>
    </g>
  `;
}

function deviceGridVisual(x, y, stateText, value, active, mode) {
  return `
    <g class="${deviceVisualClasses("grid-visual", active, mode)}" transform="translate(${x} ${y})">
      <rect class="visual-shell" x="0" y="0" width="176" height="76" rx="38"></rect>
      <rect class="visual-icon-bay" x="12" y="10" width="68" height="56" rx="24"></rect>
      <path class="grid-tower" d="M46 20v40M31 60h30M34 34h24M29 47h34M37 34 29 60M55 34l8 26M39 26h14"></path>
      <text class="visual-state" x="158" y="20" text-anchor="end">${stateText}</text>
      <text class="visual-label" x="158" y="39" text-anchor="end">Grid</text>
      <text class="visual-value" x="158" y="61" text-anchor="end">${value}</text>
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

function setFlowView(view, persist = true) {
  const nextView = ["aggregated", "devices", "control", "energy"].includes(view)
    ? view
    : "aggregated";
  state.flowView = nextView;

  const svg = $("flowSvg");
  const deviceView = $("deviceFlowView");
  const controlView = $("controlExplainView");
  const energyView = $("energyStatsView");
  const wrap = document.querySelector ? document.querySelector(".flow-wrap") : null;
  const shell = document.querySelector ? document.querySelector(".shell") : null;

  if (svg) svg.hidden = nextView !== "aggregated";
  if (deviceView) deviceView.hidden = nextView !== "devices";
  if (controlView) controlView.hidden = nextView !== "control";
  if (energyView) energyView.hidden = nextView !== "energy";
  if (wrap?.classList) {
    wrap.classList.toggle("view-devices", nextView === "devices");
    wrap.classList.toggle("view-aggregated", nextView === "aggregated");
    wrap.classList.toggle("view-control", nextView === "control");
    wrap.classList.toggle("view-energy", nextView === "energy");
  }
  if (shell?.classList) {
    shell.classList.toggle("view-energy", nextView === "energy");
  }

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
          fields: `
          ${runtimeToggle("enabled", "EMS enabled", system.enabled)}
          ${runtimeNumber("max_total_power", "Max total power", system.max_total_power, 0, Number(systemLimits.max_total_power || 5000), "W")}
          ${runtimeNumber("min_output_limit", "Min output limit", system.min_output_limit, 0, Number(systemLimits.min_output_limit || 5000), "W")}
          ${runtimeNumber("loop_interval", "Loop interval", system.loop_interval, 1, 3600, "s")}
        `})}
        ${deviceForms}
        ${runtimeStageCard({
          endpoint: "/api/runtime/winter",
          title: "Winter Mode",
          subtitle: "Seasonal charging behavior",
          step: winterStep,
          kind: "gates",
          iconName: "battery",
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
          fields: `
          ${runtimeToggle("enabled", "HA publishing", ha.enabled)}
          ${runtimeToggle("control_enabled", "HA helper control", ha.control_enabled)}
        `})}
      </div>
    </section>
  `;
}

function runtimeStageCard({ endpoint, title, subtitle, step, kind, iconName, fields }) {
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
      ${runtimeSubmit()}
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
    fields: `
      ${runtimeToggle("enabled", "Device enabled", device.enabled)}
      ${runtimeNumber("max_power", "Max power", device.max_power, 0, maxPower, "W")}
      ${runtimeNumber("pv_priority_factor", "PV priority", device.pv_priority_factor, 0.01, 100, "x", "0.01")}
      ${runtimeSelect("offgrid_socket_mode", "Offgrid socket", device.offgrid_socket_mode, ["off", "eco", "standard"])}
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

function runtimeSelect(name, label, selectedValue, options) {
  return `
    <label class="runtime-field control-pipeline-fact role-config">
      <span class="value-icon" aria-hidden="true">${icon("rule")}</span>
      <span class="control-label">${escapeHtml(label)}</span>
      <select name="${escapeHtml(name)}">
        ${options.map((value) => `
          <option value="${escapeHtml(value)}" ${selectedValue === value ? "selected" : ""}>${escapeHtml(value)}</option>
        `).join("")}
      </select>
    </label>
  `;
}

function runtimeSubmit() {
  return `<button class="primary-button compact" type="submit">Apply</button>`;
}

function initRuntimeForms() {
  const container = $("controlExplainView");
  if (!container) return;
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
    setRuntimeFeedback("Saved.", false);
    await loadRuntimeState();
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

async function loadRuntimeState() {
  if (state.demoMode) {
    state.runtime = demoRuntimeState();
    return;
  }
  try {
    const response = await fetch("/api/runtime");
    state.runtime = await response.json();
    if (state.snapshot) renderControlExplain(state.snapshot);
  } catch {
    state.runtime = null;
  }
}

async function loadAuthStatus() {
  if (state.demoMode) {
    state.auth = { configured: false, authenticated: false, csrfToken: null };
    renderAuthState();
    return;
  }
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
    if (state.snapshot) renderControlExplain(state.snapshot);
  } catch {
    state.auth = { configured: false, authenticated: false, csrfToken: null };
    renderAuthState();
  }
}

function renderAuthState() {
  const statePill = $("writeModeState");
  const button = $("authButton");
  if (statePill) {
    statePill.textContent = state.auth.authenticated ? "Write mode" : "Read-only";
    statePill.className = state.auth.authenticated ? "pill" : "pill muted";
  }
  if (!button) return;
  button.hidden = !state.auth.configured;
  button.textContent = state.auth.authenticated ? "Logout" : "Login";
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
    await loadRuntimeState();
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
    renderAuthState();
    if (state.snapshot) renderControlExplain(state.snapshot);
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

function demoHistory(snapshot) {
  return Array.from({ length: 18 }, (_, index) => ({
    ...snapshot,
    timestamp: new Date(Date.now() - (17 - index) * 5 * 60 * 1000).toISOString(),
    pv_total_w: Math.round(1200 + index * 38),
    inverter_output_w: Math.min(800, 520 + index * 18),
    home_load_w: Math.min(800, 540 + index * 16),
    battery_power_w: Math.round(720 + index * 18),
    average_soc: 56 + index * 0.22,
  }));
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
  state.history = demoHistory(snapshot);
  updateSnapshot(snapshot);
  setConnection("Demo", true);
  renderCharts();
  setFlowView("energy", false);
}

async function loadHistory() {
  if (state.demoMode) {
    state.history = demoHistory(state.snapshot || demoSnapshot());
    renderCharts();
    return;
  }
  const response = await fetch(`/api/history?range=${encodeURIComponent(state.range)}`);
  const payload = await response.json();
  state.history = payload.items || [];
  renderCharts();
}

function renderCharts() {
  charts.forEach((chart) => {
    const canvas = $(chart.id);
    const values = state.history.map((item) => Number(item[chart.field] || 0));
    drawChart(canvas, chart.title, values, chart.color, chart.unit);
  });
}

function drawChart(canvas, title, values, color, unit) {
  if (!canvas) return;

  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 320;
  const height = canvas.clientHeight || 94;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);

  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  ctx.fillStyle = "#a1adb9";
  ctx.font = "800 11px system-ui, sans-serif";
  ctx.fillText(title, 9, 16);

  if (!values.length) {
    ctx.fillStyle = "#64717f";
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillText("No samples", 10, height - 14);
    return;
  }

  const min = Math.min(...values, 0);
  const max = Math.max(...values, 1);
  const span = max - min || 1;
  const left = 9;
  const right = width - 9;
  const top = 23;
  const bottom = height - 17;

  ctx.strokeStyle = "#2c3542";
  ctx.lineWidth = 1;
  for (let i = 0; i < 3; i += 1) {
    const y = top + ((bottom - top) * i) / 2;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
  }

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = values.length === 1 ? left : left + ((right - left) * index) / (values.length - 1);
    const y = bottom - ((value - min) / span) * (bottom - top);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = "#f3f6f8";
  ctx.font = "800 12px system-ui, sans-serif";
  const latest = values[values.length - 1] || 0;
  ctx.fillText(`${Math.round(latest)} ${unit}`, 9, height - 5);
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

function initDashboardApp() {
  const rangeTabSelector = ".range-tabs button";
  document.querySelectorAll(rangeTabSelector).forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll(rangeTabSelector).forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.range = button.dataset.range;
      await loadHistory();
    });
  });

  window.addEventListener("resize", renderCharts);

  initFlowViewSwitch();
  initAuthControls();
  initRuntimeForms();
  if (state.demoMode) {
    initDemoMode();
  } else {
    loadAuthStatus();
    loadRuntimeState();
    startEvents();
    loadHistory();
    setInterval(loadAuthStatus, 60000);
    setInterval(loadHistory, 30000);
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
    runtimeControlPanel,
    runtimeDeviceForm,
    runtimeNumber,
    setRuntimeFeedback,
    startEvents,
    startPollingOnce,
    resetLiveTransportForTests,
  };
}
