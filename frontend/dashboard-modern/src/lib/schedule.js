// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The schedule editor's draft and the rule it becomes, ported from Classic so
 * both editors send the same rule for the same input. The EMS validates every
 * rule (the preview endpoint) before it can be saved; nothing here decides
 * whether a rule is valid.
 */

export const PRESET_ORDER = Object.freeze([
  "once", "daily", "every_2nd_day", "weekdays", "weekends", "sundays", "weekly", "every_2nd_week", "monthly",
  "monthly_last_day", "yearly", "custom",
]);
export const WEEKDAYS = Object.freeze(["mo", "tu", "we", "th", "fr", "sa", "su"]);
export const WINTER_MONTHS = Object.freeze([10, 11, 12, 1, 2, 3]);
export const STATUSES = Object.freeze(["waiting", "paused", "orphaned", "refused", "finished", "broken"]);

export function today(now = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export function actionById(overview, id) {
  return (overview?.actions || []).find((action) => action.id === id) || null;
}

export function shiftDate(date, days) {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

export function daysBetween(from, to) {
  const [a, b] = [from, to].map((date) => {
    const [year, month, day] = date.split("-").map(Number);
    return Date.UTC(year, month - 1, day);
  });
  return Math.round((b - a) / 86_400_000);
}

export function newDraft(overview, now = new Date()) {
  const actions = overview?.actions || [];
  const action = actions.find((item) => !item.per_device || item.devices.length) || actions[0] || { id: "", per_device: false, devices: [] };
  return {
    id: null,
    revision: null,
    name: "",
    enabled: true,
    action: action.id,
    device: action.per_device ? action.devices[0] : null,
    value: "",
    end_value: "",
    mode: "window",
    date: today(now),
    from: "18:00",
    to: "23:00",
    end_date: today(now),
    preset: "daily",
    custom: { freq: "weekly", interval: 1, by_weekday: [], monthly: "day", month_day: 1, set_pos: 1, set_day: "mo" },
    months: [],
    ends: "never",
    until: "",
    count: 10,
    exceptions: [],
    keptRepeat: null,
  };
}

export function repeatEditable(repeat) {
  if (!repeat) return true;
  const monthDays = repeat.by_month_day || [];
  const weekdays = repeat.by_weekday || [];
  if (repeat.freq !== "monthly" && repeat.freq !== "yearly") return !monthDays.length;
  if (monthDays.length > 1 || (monthDays.length === 1 && monthDays[0] < -1)) return false;
  if (weekdays.length && (repeat.by_set_pos == null || weekdays.length > 1)) return false;
  return true;
}

export function presetOf(repeat, presets) {
  if (!repeat) return "once";
  const core = (item) => ({
    freq: item.freq,
    interval: item.interval || 1,
    by_weekday: item.by_weekday || [],
    by_month_day: item.by_month_day || [],
    by_set_pos: item.by_set_pos ?? null,
  });
  const wanted = JSON.stringify(core(repeat));
  for (const name of PRESET_ORDER) {
    const preset = presets?.[name];
    if (preset && JSON.stringify(core(preset)) === wanted) return name;
  }
  return "custom";
}

export function endDateOf(when) {
  const startDate = String(when.start).slice(0, 10);
  if (!when.end) return startDate;
  const endDate = String(when.end).slice(0, 10);
  const overnight = endDate === shiftDate(startDate, 1) && String(when.end).slice(11, 16) <= String(when.start).slice(11, 16);
  return overnight ? startDate : endDate;
}

export function draftFromRule(rule, overview) {
  const when = rule.when || {};
  const repeat = when.repeat || null;
  const draft = {
    ...newDraft(overview),
    id: rule.id,
    revision: rule.revision,
    name: rule.name,
    enabled: rule.enabled,
    action: rule.action.id,
    device: rule.target?.device || null,
    value: rule.action.value ?? "",
    end_value: rule.action.end_value ?? "",
    mode: when.end ? "window" : "point",
    date: String(when.start).slice(0, 10),
    from: String(when.start).slice(11, 16),
    to: when.end ? String(when.end).slice(11, 16) : "",
    end_date: endDateOf(when),
    exceptions: when.exceptions || [],
    months: repeat?.by_month || [],
  };
  draft.preset = repeatEditable(repeat) ? presetOf(repeat, overview?.presets) : "kept";
  draft.keptRepeat = draft.preset === "kept" ? repeat : null;
  if (repeat) {
    draft.custom = {
      freq: repeat.freq,
      interval: repeat.interval || 1,
      by_weekday: repeat.by_weekday || [],
      monthly: repeat.by_set_pos ? "weekday" : (repeat.by_month_day || [])[0] === -1 ? "last" : "day",
      month_day: (repeat.by_month_day || [])[0] > 0 ? repeat.by_month_day[0] : 1,
      set_pos: repeat.by_set_pos || 1,
      set_day: (repeat.by_weekday || ["mo"])[0],
    };
    draft.ends = repeat.until ? "until" : repeat.count ? "count" : "never";
    draft.until = repeat.until || "";
    draft.count = repeat.count || 10;
  }
  return draft;
}

export function typedValue(action, raw) {
  if (raw === "" || raw === null || raw === undefined) return null;
  if (action.value_type === "switch") return raw === "true" || raw === true;
  if (action.value_type === "choice") return String(raw);
  if (action.value_type === "factor") return Number(raw);
  return Number.isInteger(Number(raw)) ? Number(raw) : raw;
}

export function repeatOf(draft, presets) {
  if (draft.preset === "once") return null;
  if (draft.preset === "kept" && draft.keptRepeat) return { ...draft.keptRepeat };
  let repeat;
  if (draft.preset === "custom") {
    const custom = draft.custom;
    repeat = { freq: custom.freq, interval: custom.interval };
    if ((custom.freq === "weekly" || custom.freq === "daily") && custom.by_weekday.length) repeat.by_weekday = custom.by_weekday;
    if (custom.freq === "monthly" || custom.freq === "yearly") {
      if (custom.monthly === "day") repeat.by_month_day = [custom.month_day];
      if (custom.monthly === "last") repeat.by_month_day = [-1];
      if (custom.monthly === "weekday") {
        repeat.by_weekday = [custom.set_day];
        repeat.by_set_pos = custom.set_pos;
      }
    }
  } else {
    repeat = { ...(presets?.[draft.preset] || { freq: "daily" }) };
  }
  if (draft.months.length && draft.months.length < 12) repeat.by_month = draft.months;
  if (draft.ends === "until" && draft.until) repeat.until = draft.until;
  if (draft.ends === "count") repeat.count = draft.count;
  return repeat;
}

/** Classic's schedulePayload: a "to" not after "from" on the same date ends the next day. */
export function rulePayload(draft, overview) {
  const action = actionById(overview, draft.action);
  const window = draft.mode === "window";
  const when = { start: `${draft.date}T${draft.from}` };
  if (window) {
    let endDate = draft.end_date && draft.end_date > draft.date ? draft.end_date : draft.date;
    if (endDate === draft.date && draft.to <= draft.from) endDate = shiftDate(draft.date, 1);
    when.end = `${endDate}T${draft.to}`;
  }
  const repeat = repeatOf(draft, overview?.presets);
  if (repeat) when.repeat = repeat;
  if (draft.exceptions.length) when.exceptions = draft.exceptions;
  const payload = {
    name: draft.name,
    enabled: draft.enabled,
    target: action?.per_device ? { scope: "device", device: draft.device } : { scope: "system" },
    action: { id: action?.id, value: action ? typedValue(action, draft.value) : null },
    when,
  };
  if (window && action) payload.action.end_value = typedValue(action, draft.end_value);
  return payload;
}

export function valueLimit(action, device, overview) {
  const limits = overview?.limits || {};
  if (action.id === "total_output_limit") return limits.system?.max_total_power ?? 5000;
  if (action.id === "minimum_output_limit") return limits.system?.min_output_limit ?? 5000;
  if (action.id === "charge_from_grid") return limits.ac_charge_power_max ?? 5000;
  if (action.id === "device_output_limit") return (limits.devices || {})[device] ?? limits.fallback_device_max_power ?? 5000;
  return 5000;
}

/** "2026-10-19T18:00" is wall-clock time in the EMS's zone: shown as written, never shifted by the browser's. */
export function whenParts(at) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(String(at || ""));
  if (!match) return null;
  return { date: new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))), time: match[4] };
}

export function offsetText(minutes) {
  if (!minutes) return "UTC";
  const sign = minutes > 0 ? "+" : "−";
  const whole = Math.abs(minutes);
  return `UTC${sign}${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}
