// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Columnar history from /api/history/series and /api/analytics/series.
 *
 * Missing samples stay missing: a stretch without samples is a gap in the
 * chart and contributes nothing to an energy figure. It is never bridged by
 * interpolation and never counted as zero.
 */

export const DEFAULT_MAX_GAP_SECONDS = 15 * 60;

function value(values, index) {
  const raw = values?.[index];
  if (raw === null || raw === undefined) return null;
  const number = Number(raw);
  return Number.isFinite(number) ? number : null;
}

/** Median sample spacing (the lower one for an even count, as Classic), so the gap limit follows the data. */
export function sampleSpacing(time) {
  if (!Array.isArray(time) || time.length < 2) return null;
  const steps = [];
  for (let i = 1; i < time.length; i += 1) {
    const step = Number(time[i]) - Number(time[i - 1]);
    if (step > 0) steps.push(step);
  }
  if (!steps.length) return null;
  steps.sort((a, b) => a - b);
  return steps[Math.floor((steps.length - 1) / 2)];
}

export const GAP_FACTOR = 3;

/** Classic's integrationMaxGap: the longest step still integrated or drawn is three typical steps. */
export function gapLimit(time) {
  const spacing = sampleSpacing(time);
  if (!spacing) return DEFAULT_MAX_GAP_SECONDS;
  return spacing * GAP_FACTOR;
}

/**
 * Trapezoid integration in kWh over [from, to). A step whose ends are not both
 * measured, or that is longer than the gap limit, adds nothing.
 */
export function integrateKwh(time, values, { from = -Infinity, to = Infinity, transform = (v) => v, maxGap } = {}) {
  if (!Array.isArray(time) || time.length < 2) return null;
  const limit = maxGap ?? gapLimit(time);
  let wattSeconds = 0;
  let measured = false;
  for (let i = 1; i < time.length; i += 1) {
    const t0 = Number(time[i - 1]);
    const t1 = Number(time[i]);
    if (t1 <= from || t0 >= to) continue;
    const a = value(values, i - 1);
    const b = value(values, i);
    if (a === null || b === null) continue;
    const dt = t1 - t0;
    if (dt <= 0 || dt > limit) continue;
    const start = Math.max(t0, from);
    const end = Math.min(t1, to);
    const span = end - start;
    if (span <= 0) continue;
    const va = transform(a);
    const vb = transform(b);
    wattSeconds += ((va + vb) / 2) * span;
    measured = true;
  }
  return measured ? wattSeconds / 3600 / 1000 : null;
}

function localHour(day, hour) {
  return Math.floor(new Date(day.getFullYear(), day.getMonth(), day.getDate(), hour).getTime() / 1000);
}

const NICE_STEPS = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

/** The top of an hourly bar scale: the next round figure at or above the largest bar, or null when nothing is above zero. */
export function niceCeiling(value) {
  const number = Math.abs(Number(value));
  if (!Number.isFinite(number) || number <= 0) return null;
  const power = 10 ** Math.floor(Math.log10(number));
  const step = NICE_STEPS.find((candidate) => candidate * power >= number * (1 - 1e-9));
  return Number((step * power).toPrecision(6));
}

/**
 * Energy per local clock hour of one day; an hour without a measured step is
 * null, not zero. On the day the clocks go back, hour 2 holds both 02:00 hours;
 * on the day they go forward, the hour that does not exist is null.
 */
export function hourlyKwh(time, values, dayStartSeconds, { transform, maxGap } = {}) {
  const day = new Date(dayStartSeconds * 1000);
  return Array.from({ length: 24 }, (_, hour) => {
    const from = localHour(day, hour);
    const to = localHour(day, hour + 1);
    if (to <= from) return null;
    return integrateKwh(time, values, { from, to, transform, maxGap });
  });
}

/** Runs without samples inside the measured span, for hatched "no data" bands. */
export function gapRanges(time, seriesList, { maxGap } = {}) {
  if (!Array.isArray(time) || time.length < 2) return [];
  const limit = maxGap ?? gapLimit(time);
  const measuredAt = (index) => seriesList.some((values) => value(values, index) !== null);
  const ranges = [];
  let gapStart = null;
  let lastMeasured = null;
  for (let i = 0; i < time.length; i += 1) {
    const t = Number(time[i]);
    if (measuredAt(i)) {
      if (gapStart !== null) {
        ranges.push({ from: gapStart, to: t });
        gapStart = null;
      } else if (lastMeasured !== null && t - lastMeasured > limit) {
        ranges.push({ from: lastMeasured, to: t });
      }
      lastMeasured = t;
    } else if (lastMeasured !== null && gapStart === null) {
      gapStart = lastMeasured;
    }
  }
  return ranges;
}

/**
 * uPlot draws a line between neighbouring points even across a missing hour
 * when the samples simply are not there. A null inserted into every gap longer
 * than the limit makes the chart show the gap.
 */
export function withGapBreaks(time, seriesList, { maxGap } = {}) {
  if (!Array.isArray(time) || time.length < 2) return { time: time || [], series: seriesList };
  const limit = maxGap ?? gapLimit(time);
  const outTime = [];
  const outSeries = seriesList.map(() => []);
  for (let i = 0; i < time.length; i += 1) {
    if (i > 0 && Number(time[i]) - Number(time[i - 1]) > limit) {
      outTime.push(Number(time[i - 1]) + limit / 2);
      outSeries.forEach((values) => values.push(null));
    }
    outTime.push(Number(time[i]));
    seriesList.forEach((values, index) => outSeries[index].push(value(values, i)));
  }
  return { time: outTime, series: outSeries };
}

/** Local midnight of the day ``days`` away from ``date``: a calendar day is 23, 24 or 25 hours long. */
export function localDayStart(date = new Date(), days = 0) {
  const start = new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
  return Math.floor(start.getTime() / 1000);
}

/**
 * Today so far against yesterday up to the same clock time, both integrated
 * from the same history the same way, so the comparison is like for like.
 */
export function sameTimeComparison(history, seriesId, nowSeconds, dayStartSeconds, transform) {
  const values = history?.series?.[seriesId];
  if (!values) return null;
  const now = new Date(nowSeconds * 1000);
  const yesterdayStart = localDayStart(new Date(dayStartSeconds * 1000), -1);
  const sameTimeYesterday = Math.floor(
    new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, now.getHours(), now.getMinutes(), now.getSeconds()).getTime() / 1000,
  );
  const today = integrateKwh(history.time, values, { from: dayStartSeconds, to: nowSeconds, transform });
  const yesterday = integrateKwh(history.time, values, {
    from: yesterdayStart,
    to: sameTimeYesterday,
    transform,
  });
  if (today === null || yesterday === null) return null;
  const firstSample = Number(history.time?.[0]);
  if (!Number.isFinite(firstSample) || firstSample > yesterdayStart + gapLimit(history.time)) return null;
  return { today, yesterday, delta: today - yesterday };
}

/** Span multiplier for one wheel notch or one button press, and the narrowest window, as Classic's chart zoom. */
export const ZOOM_STEP = 1.6;
export const MIN_ZOOM_SPAN_S = 60;

/**
 * Whether an x window differs from the extent it lies in, either way; a
 * window equal to it within half a percent is no zoom at all.
 */
export function zoomDiffers(min, max, start, end) {
  const nums = [min, max, start, end];
  if (!nums.every((n) => typeof n === "number" && Number.isFinite(n))) return false;
  const span = end - start;
  if (!(span > 0)) return false;
  const eps = span * 0.005;
  return Math.abs(min - start) > eps || Math.abs(max - end) > eps;
}

/**
 * The x window that zooming by ``factor`` (a span multiplier, below 1 zooms
 * in) around ``center`` gives, clamped into the outer bounds; what lies under
 * the pointer stays under it. Null when the inputs describe no window.
 */
export function zoomStep(min, max, center, factor, start, end) {
  const nums = [min, max, center, factor, start, end];
  if (!nums.every((n) => typeof n === "number" && Number.isFinite(n))) return null;
  const outer = end - start;
  if (!(outer > 0) || !(max > min) || !(factor > 0)) return null;
  const span = max - min;
  const next = Math.min(Math.max(span * factor, Math.min(MIN_ZOOM_SPAN_S, outer)), outer);
  const anchor = Math.min(Math.max(center, min), max);
  let low = anchor - ((anchor - min) / span) * next;
  if (low < start) low = start;
  if (low + next > end) low = end - next;
  return { min: low, max: low + next };
}

/**
 * Escape leaves a zoom only when no dialog or popover is open to take it for
 * itself: closing the sign-in or the appearance menu keeps the chart as it is.
 */
export function escapeLeavesZoom(event, root = globalThis.document) {
  if (event?.key !== "Escape" || event.defaultPrevented) return false;
  if (!root) return true;
  try {
    return !root.querySelector("dialog[open], [popover]:popover-open");
  } catch {
    return !root.querySelector("dialog[open]");
  }
}

/**
 * Where the + and − buttons zoom around: the middle of the data inside the
 * window, so a fixed day that has data only up to now is never zoomed into
 * its empty future; the window's middle when no data falls inside it.
 */
export function zoomCenter(min, max, first, last) {
  const low = Math.max(min, first);
  const high = Math.min(max, last);
  return Number.isFinite(low) && Number.isFinite(high) && low <= high ? (low + high) / 2 : (min + max) / 2;
}

/**
 * The length of a zoomed window as Classic names its KPIs' period: minutes
 * below an hour, tenths of hours below two days, tenths of days beyond.
 */
export function zoomSpan(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const tenths = (value) => Math.round(value * 10) / 10;
  if (seconds < 3600) return { key: "chart.span.m", n: Math.max(1, Math.round(seconds / 60)) };
  if (seconds < 2 * 86400) return { key: "chart.span.h", n: tenths(seconds / 3600) };
  return { key: "chart.span.d", n: tenths(seconds / 86400) };
}

const SOURCE_KEYS = Object.freeze({ sqlite: "source.sqlite", influxdb: "source.influxdb", preview: "source.preview", demo: "source.demo" });

/** Where a series answer came from, as the chart's badge names it; null for a source it does not name. */
export function sourceKey(source) {
  return Object.hasOwn(SOURCE_KEYS, source) ? SOURCE_KEYS[source] : null;
}

/** What an empty chart says: still loading, unavailable, or no samples in this range. */
export function emptyKey(data, failed) {
  if (!data) return failed ? "chart.unavailable" : "chart.loading";
  return data.meta?.unavailable ? "chart.unavailable" : "chart.empty";
}
