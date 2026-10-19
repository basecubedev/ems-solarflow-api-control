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
