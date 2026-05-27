const state = {
  snapshot: null,
  range: "6h",
  history: [],
  flowView: "aggregated",
};

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
  setConnection("Live", true);
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
  renderDeviceFlow(snapshot);
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
  const nextView = view === "devices" ? "devices" : "aggregated";
  state.flowView = nextView;

  const svg = $("flowSvg");
  const deviceView = $("deviceFlowView");
  const wrap = document.querySelector ? document.querySelector(".flow-wrap") : null;

  if (svg) svg.hidden = nextView !== "aggregated";
  if (deviceView) deviceView.hidden = nextView !== "devices";
  if (wrap?.classList) {
    wrap.classList.toggle("view-devices", nextView === "devices");
    wrap.classList.toggle("view-aggregated", nextView === "aggregated");
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

async function loadHistory() {
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
  if (!window.EventSource) {
    startPolling();
    return;
  }

  const source = new EventSource("/api/events");
  source.addEventListener("telemetry", (event) => {
    updateSnapshot(JSON.parse(event.data));
  });
  source.onerror = () => {
    setConnection("Reconnecting", false);
  };
}

function startPolling() {
  setInterval(async () => {
    try {
      const response = await fetch("/api/live");
      updateSnapshot(await response.json());
    } catch {
      setConnection("Offline", false);
    }
  }, 2000);
}

document.querySelectorAll(".range-tabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".range-tabs button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.range = button.dataset.range;
    await loadHistory();
  });
});

window.addEventListener("resize", renderCharts);

initFlowViewSwitch();
startEvents();
loadHistory();
setInterval(loadHistory, 30000);
