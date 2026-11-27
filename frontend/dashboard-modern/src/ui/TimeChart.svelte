<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { prefs } from "../state/prefs.svelte.js";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { withGapBreaks, stackLists, gapRanges, sampleSpacing, shownValue, batteryReading, axisPower, tickLabels, tickPad, tickSpace, atLeastAMinute, ZOOM_STEP, PAN_STEP, escapeLeavesZoom, zoomCenter, zoomDiffers, zoomStep, panStep, pinchWindow, wheelGesture, wheelZoomFactor } from "../lib/history.js";
  import { voltRange } from "../lib/cells.js";
  import { celsiusRange, celsiusTick } from "../lib/battery.js";

  /**
   * One uPlot instance per chart, kept across data refreshes (setData) and
   * rebuilt only when its structure changes: the series, the colour scheme,
   * the language or the height. That is Classic's chart contract. A mouse
   * drag across the plot zooms into that stretch; a refresh of the same data
   * keeps the zoom, and a new range, device or view (``scope``) drops it.
   * The wheel, a two-finger pinch and the + and − buttons zoom too; a
   * sideways scroll, a one-finger swipe and the earlier and later buttons
   * move a zoomed window. Escape and Reset zoom leave
   * it, and a rebuild for a new scheme or language keeps it. How far out it
   * may zoom is the fixed ``xRange``, else ``bounds``, else the loaded data;
   * ``onzoom`` hears every window, and null for the way back, so a page can
   * read the window from the server. A page that leaves the zoom by itself
   * changes ``scope``. A series in percent reads against its own 0-100
   * scale on the right, as Classic draws the SoC, labelled in the series'
   * ``ink`` -- a text colour, since the line's own may be too dark to read.
   * The battery is drawn as Classic draws it, charging below zero, and its
   * legend and table read Charge or Discharge (``shownValue``).
   * ``stacked`` draws each series on top of the ones before it, as parts of
   * one whole; the legend and the table keep each series' own value.
   * ``unit`` "V" reads the left axis in volts, close around the data, for
   * cell voltages; "°C" in degrees, always showing 0 °C, for temperatures;
   * every other chart reads it in watts from zero. ``busy``
   * lays a Loading pill over the plot, or over its empty frame, while a page
   * reads new data for it, taking no room, so nothing around the chart moves.
   */
  let {
    series,
    time = [],
    values = {},
    xRange = null,
    height = 300,
    showTable = false,
    label = "",
    emptyText = "",
    scope = "",
    bounds = null,
    onzoom = null,
    busy = false,
    stacked = false,
    unit = "W",
  } = $props();

  let host = $state();
  const PERCENT_AXIS_PX = 56;
  let chart = null;
  let signature = "";
  let cursorIndex = $state(null);
  let zoomed = $state(false);
  let viewWindow = $state.raw(null);
  let zoomWindow = null;
  let zoomScope = null;
  const touches = new Map();
  /** How far one finger travels sideways before it moves the chart, so a tap or a page scroll that drifts does not. */
  const SWIPE_START_PX = 8;
  let gesture = null;
  let pendingWindow = null;
  let frame = 0;

  const f = $derived(fmt());
  const prepared = $derived.by(() => {
    const lists = series.map((s) => (values?.[s.id] || []).map((raw) => shownValue(s.id, raw)));
    const broken = withGapBreaks(time, lists);
    return { time: broken.time, lists: broken.series, gaps: gapRanges(time, lists) };
  });
  const plotted = $derived(stacked ? stackLists(prepared.lists) : prepared.lists);
  /** Whether the zoomed window touches the start or the end of the range, re-read as new data widens it. */
  const atEdge = $derived.by(() => {
    const limits = outer();
    if (!viewWindow || !limits) return { start: false, end: false };
    return { start: viewWindow.min <= limits[0], end: viewWindow.max >= limits[1] };
  });
  const hasData = $derived(prepared.lists.some((list) => list.some((v) => v !== null)));
  const shown = $derived.by(() => {
    if (xRange) return { from: xRange[0], to: xRange[1] - 1 };
    const stamps = prepared.time;
    return stamps.length ? { from: stamps[0], to: stamps[stamps.length - 1] } : { from: 0, to: 0 };
  });
  const multiDay = $derived(new Date(shown.from * 1000).toDateString() !== new Date(shown.to * 1000).toDateString());
  /** Samples less than a minute apart: a time without its seconds would name several of them. */
  const subMinute = $derived((sampleSpacing(time) ?? 60) < 60);

  function stampText(seconds) {
    return multiDay ? f.dateTime(seconds * 1000, { seconds: subMinute }) : f.time(seconds * 1000, { seconds: subMinute });
  }

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function alpha(color, a) {
    const hex = color.replace("#", "");
    if (!/^[0-9a-f]{6}$/i.test(hex)) return color;
    const n = parseInt(hex, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  function areaFill(color) {
    return (u) => {
      const { top, height: h } = u.bbox;
      const y0 = u.valToPos(0, "y", true);
      const zero = Math.min(1, Math.max(0, (y0 - top) / h));
      const gradient = u.ctx.createLinearGradient(0, top, 0, top + h);
      gradient.addColorStop(0, alpha(color, 0.3));
      gradient.addColorStop(zero, alpha(color, 0.02));
      gradient.addColorStop(1, alpha(color, 0.24));
      return gradient;
    };
  }

  function valueText(value, s) {
    if (value === null || value === undefined) return "—";
    if (s?.unit === "%") return f.percent(value);
    if (unit === "V") return f.voltage(value);
    if (unit === "°C" && s?.unit !== "%") return f.temperature(value);
    if (s?.id === "battery" && Number.isFinite(Number(value))) {
      const reading = batteryReading(value);
      return reading.key ? t(reading.key, { v: f.power(reading.watts) }) : f.power(0);
    }
    return f.power(value, { signed: Boolean(s?.signed) });
  }

  function onPercentScale(s) {
    return s.unit === "%";
  }

  function drawGaps(u) {
    const ranges = prepared.gaps;
    if (!ranges.length) return;
    const { ctx } = u;
    const { top, height: h } = u.bbox;
    const ratio = globalThis.devicePixelRatio || 1;
    const muted = token("--m-muted");
    ctx.save();
    for (const range of ranges) {
      const x1 = u.valToPos(range.from, "x", true);
      const x2 = u.valToPos(range.to, "x", true);
      if (!Number.isFinite(x1) || !Number.isFinite(x2) || x2 - x1 < 2) continue;
      ctx.save();
      ctx.beginPath();
      ctx.rect(x1, top, x2 - x1, h);
      ctx.clip();
      ctx.strokeStyle = alpha(muted, 0.18);
      ctx.lineWidth = ratio;
      for (let x = x1 - h; x < x2; x += 7 * ratio) {
        ctx.beginPath();
        ctx.moveTo(x, top + h);
        ctx.lineTo(x + h, top);
        ctx.stroke();
      }
      ctx.restore();
      if (x2 - x1 > 48 * ratio) {
        ctx.fillStyle = muted;
        ctx.font = `${11 * ratio}px InterVariable, sans-serif`;
        ctx.textAlign = "center";
        ctx.fillText(t("chart.noData"), (x1 + x2) / 2, top + 14 * ratio);
      }
    }
    ctx.restore();
  }

  function applyZoom(next) {
    zoomWindow = next;
    viewWindow = next;
    zoomed = true;
    chart?.setScale("x", next);
    onzoom?.(next);
  }

  function zoomToSelection(u) {
    const { left, width } = u.select;
    if (width < 6) return;
    const min = u.posToVal(left, "x");
    const max = u.posToVal(left + width, "x");
    u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
    const limits = outer();
    const next = limits ? zoomStep(min, max, (min + max) / 2, 1, limits[0], limits[1]) : { min, max };
    if (!next || (limits && !zoomDiffers(next.min, next.max, limits[0], limits[1]))) return;
    applyZoom(next);
  }

  function resetZoom() {
    dropPending();
    zoomWindow = null;
    viewWindow = null;
    zoomed = false;
    const stamps = prepared.time;
    if (chart && stamps.length) chart.setScale("x", { min: stamps[0], max: stamps[stamps.length - 1] });
    onzoom?.(null);
  }

  /** How far out the chart may zoom: its fixed range, else the bounds it was given, else its loaded data. */
  function outer() {
    if (xRange) return [xRange[0], xRange[1]];
    if (Array.isArray(bounds) && bounds.length === 2) return bounds;
    const stamps = prepared.time;
    return stamps.length > 1 ? [stamps[0], stamps[stamps.length - 1]] : null;
  }

  /** One zoom step around a point; zoomed all the way out is the way back, as Reset zoom. */
  function zoomBy(factor, center) {
    const limits = outer();
    if (!chart || !limits) return;
    const { min, max } = chart.scales.x;
    const stamps = prepared.time;
    const middle = center ?? zoomCenter(min, max, stamps[0], stamps[stamps.length - 1]);
    const next = zoomStep(min, max, middle, factor, limits[0], limits[1]);
    if (!next) return;
    if (!zoomDiffers(next.min, next.max, limits[0], limits[1])) {
      if (zoomed) resetZoom();
      return;
    }
    applyZoom(next);
  }

  /**
   * Move a zoomed window by ``shift`` seconds, stopping at the bounds; a chart
   * not zoomed has nothing to move. A window that new data left partly outside
   * the range never moves against the asked direction to get back into it.
   */
  function panBy(shift) {
    const limits = outer();
    if (!chart || !limits || !zoomed || !shift) return;
    const { min, max } = chart.scales.x;
    const next = panStep(min, max, shift, limits[0], limits[1]);
    if (next && Math.sign(next.min - min) === Math.sign(shift)) applyZoom(next);
  }

  function panByStep(direction) {
    if (!chart) return;
    const { min, max } = chart.scales.x;
    panBy(direction * PAN_STEP * (max - min));
  }

  function onWheel(event) {
    event.preventDefault();
    if (!chart) return;
    const rect = chart.over.getBoundingClientRect();
    const wheel = wheelGesture(event, rect.width);
    if ("pan" in wheel) {
      const { min, max } = chart.scales.x;
      if (rect.width > 0 && wheel.pan) panBy((wheel.pan / rect.width) * (max - min));
      return;
    }
    if (!wheel.zoom) return;
    const center = chart.posToVal(event.clientX - rect.left, "x");
    zoomBy(wheelZoomFactor(wheel.zoom), Number.isFinite(center) ? center : undefined);
  }

  /** The share of the plot's width at ``clientX``, for a gesture's fingers. */
  function plotFraction(clientX) {
    const rect = chart.over.getBoundingClientRect();
    return rect.width > 0 ? (clientX - rect.left) / rect.width : 0.5;
  }

  function fingerSpan(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  /** Start what the fingers now on the plot do: two pinch, one moves a zoomed window, each from the window shown now. */
  function beginGesture() {
    const points = [...touches.values()];
    if (!chart || !points.length) {
      gesture = null;
      return;
    }
    const { min, max } = pendingWindow ?? chart.scales.x;
    if (points.length === 1) {
      gesture = { pan: { min, max, x: points[0].x, y: points[0].y, width: chart.over.getBoundingClientRect().width, moving: false } };
      return;
    }
    const [a, b] = points;
    gesture = { pinch: { span: max - min, anchor: min + plotFraction((a.x + b.x) / 2) * (max - min), distance: fingerSpan(a, b) } };
  }

  /** Keep a finger's moves on this plot when it slides off; false for a pointer the browser no longer tracks. */
  function capturePointer(id) {
    try {
      chart.over.setPointerCapture(id);
      return true;
    } catch {
      return false;
    }
  }

  function onPointerDown(event) {
    if (event.pointerType !== "touch" || !chart) return;
    touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
    capturePointer(event.pointerId);
    beginGesture();
  }

  function onPointerMove(event) {
    if (!gesture || !touches.has(event.pointerId)) return;
    touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const limits = outer();
    if (!limits) return;
    if (gesture.pinch) {
      const [a, b] = [...touches.values()];
      showWindow(pinchWindow(gesture.pinch, fingerSpan(a, b), plotFraction((a.x + b.x) / 2), limits[0], limits[1]));
    } else if (zoomed && gesture.pan.width > 0) {
      const pan = gesture.pan;
      if (!pan.moving) {
        const dx = event.clientX - pan.x;
        if (Math.abs(dx) < SWIPE_START_PX || Math.abs(dx) <= Math.abs(event.clientY - pan.y)) return;
        pan.moving = true;
        pan.x = event.clientX;
      }
      const { min, max, x, width } = pan;
      showWindow(panStep(min, max, ((x - event.clientX) / width) * (max - min), limits[0], limits[1]));
    }
  }

  function onPointerEnd(event) {
    if (touches.delete(event.pointerId)) beginGesture();
  }

  /** The browser took the touch for itself, as a page scroll: the frame not yet drawn is dropped, not drawn. */
  function onPointerCancel(event) {
    if (!touches.delete(event.pointerId)) return;
    dropPending();
    beginGesture();
  }

  /** Draw a gesture's window once a frame, however often the fingers report; all the way out is the way back. */
  function showWindow(next) {
    if (!next) return;
    pendingWindow = next;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const shown = pendingWindow;
      pendingWindow = null;
      const limits = outer();
      if (!chart || !shown || !limits) return;
      if (!zoomDiffers(shown.min, shown.max, limits[0], limits[1])) {
        if (zoomed) resetZoom();
        return;
      }
      if (shown.min !== chart.scales.x.min || shown.max !== chart.scales.x.max) applyZoom(shown);
    });
  }

  function dropPending() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    pendingWindow = null;
  }

  function dropGesture() {
    touches.clear();
    gesture = null;
    dropPending();
  }

  function build() {
    const uPlot = globalThis.uPlot;
    if (!host || !uPlot) return;
    dropGesture();
    chart?.destroy();
    const font = "12px InterVariable, sans-serif";
    const axis = {
      stroke: token("--m-muted"),
      font,
      ticks: { show: false },
      grid: { stroke: token("--m-line"), width: 1 },
    };
    const percent = series.find(onPercentScale);
    chart = new uPlot(
      {
        width: host.clientWidth || 600,
        height,
        padding: [8, (u) => Math.max(0, tickPad(u.scales.x.min, u.scales.x.max) - (percent ? PERCENT_AXIS_PX : 0)), 0, 0],
        legend: { show: false },
        cursor: { y: false, drag: { x: true, y: false, setScale: false }, points: { size: 7, width: 2 } },
        scales: {
          x: {
            time: true,
            range: (u, min, max) => (zoomWindow ? atLeastAMinute(zoomWindow.min, zoomWindow.max) : xRange ? [xRange[0], xRange[1]] : atLeastAMinute(min, max)),
          },
          y: { range: (u, min, max) => (unit === "V" ? voltRange(min, max) : unit === "°C" ? celsiusRange(min, max) : [Math.min(0, Math.floor((min || 0) / 500) * 500), Math.max(500, Math.ceil((max || 0) / 500) * 500)]) },
          ...(percent ? { pct: { range: [0, 100] } } : {}),
        },
        axes: [
          {
            ...axis,
            gap: 9,
            space: (u, axis, min, max) => tickSpace(min, max),
            values: (u, splits, axis, space, step) => tickLabels(splits, step, f),
          },
          { ...axis, size: 66, values: (u, splits) => splits.map((value) => (unit === "V" ? f.voltage(value) : unit === "°C" ? celsiusTick(value, f) : axisPower(value, f))) },
          ...(percent
            ? [{ ...axis, scale: "pct", side: 1, size: PERCENT_AXIS_PX, stroke: token(percent.ink || "--m-muted"), grid: { show: false }, values: (u, splits) => splits.map((value) => f.percent(value)) }]
            : []),
        ],
        series: [
          {},
          ...series.map((s, index) => {
            const color = token(s.token);
            return {
              label: s.id,
              scale: onPercentScale(s) ? "pct" : "y",
              stroke: color,
              dash: s.dash,
              width: 1.75,
              fill: s.fill === false || (stacked && index > 0) ? undefined : areaFill(color),
              points: { show: false },
              spanGaps: false,
            };
          }),
        ],
        bands: stacked ? series.slice(1).map((s, index) => ({ series: [index + 2, index + 1], fill: alpha(token(s.token), 0.28) })) : [],
        hooks: {
          setCursor: [(u) => (cursorIndex = u.cursor.idx ?? null)],
          setSelect: [zoomToSelection],
          draw: [drawGaps],
        },
      },
      [prepared.time, ...plotted],
      host,
    );
    chart.over.addEventListener("wheel", onWheel, { passive: false });
    chart.over.addEventListener("pointerdown", onPointerDown);
    chart.over.addEventListener("pointermove", onPointerMove);
    chart.over.addEventListener("pointerup", onPointerEnd);
    chart.over.addEventListener("pointercancel", onPointerCancel);
  }

  $effect(() => {
    const next = [series.map((s) => `${s.id}=${s.token}`).join(","), stacked, unit, prefs.theme, prefs.lang, height, xRange ? xRange.join("-") : ""].join("|");
    const reach = `${scope}|${xRange ? xRange.join("-") : ""}`;
    if (zoomScope !== null && reach !== zoomScope) {
      zoomWindow = null;
      viewWindow = null;
      zoomed = false;
    }
    zoomScope = reach;
    if (!host || !hasData) return;
    if (next !== signature || !chart) {
      signature = next;
      build();
    } else {
      chart.setData([prepared.time, ...plotted]);
    }
  });

  $effect(() => {
    const onKey = (event) => {
      if (zoomed && escapeLeavesZoom(event)) resetZoom();
    };
    globalThis.addEventListener("keydown", onKey);
    return () => globalThis.removeEventListener("keydown", onKey);
  });

  $effect(() => {
    if (!host) return;
    const observer = new ResizeObserver(() => chart?.setSize({ width: host.clientWidth, height }));
    observer.observe(host);
    return () => {
      observer.disconnect();
      dropGesture();
      chart?.destroy();
      chart = null;
      signature = "";
    };
  });

  const lastIndex = $derived.by(() => {
    for (let i = prepared.time.length - 1; i >= 0; i -= 1) {
      if (prepared.time[i] < shown.from || prepared.time[i] > shown.to) continue;
      if (prepared.lists.some((list) => list[i] !== null && list[i] !== undefined)) return i;
    }
    return null;
  });
  const shownIndex = $derived(cursorIndex ?? lastIndex);
</script>

{#snippet loadingPill()}
  {#if busy}<span class="pill busy">{t("common.loading")}</span>{/if}
{/snippet}

{#if !hasData}
  <div class="plot">
    <div class="empty chart-empty" role="status">{emptyText || t("chart.empty")}</div>
    {@render loadingPill()}
  </div>
{:else}
  <div class="legend" aria-live="off">
    <span class="legend-time num">{shownIndex !== null ? stampText(prepared.time[shownIndex]) : ""}</span>
    {#each series as s, index (s.id)}
      <span class="legend-item" style:--c={`var(${s.token})`}>
        <i class:dash={Boolean(s.dash)}></i>{s.label}<b class="num">{shownIndex !== null ? valueText(prepared.lists[index][shownIndex], s) : "—"}</b>
      </span>
    {/each}
    <span class="zoom">
      {#if zoomed}
        <button class="btn secondary small" type="button" onclick={resetZoom} data-testid="chart-zoom-reset">{t("chart.zoomReset")}</button>
        <button class="icon-btn small" type="button" onclick={() => panByStep(-1)} disabled={atEdge.start} aria-label={t("chart.panEarlier")} title={t("chart.panEarlier")} data-testid="chart-pan-earlier"><Icon name="chevron-left" size="sm" /></button>
        <button class="icon-btn small" type="button" onclick={() => panByStep(1)} disabled={atEdge.end} aria-label={t("chart.panLater")} title={t("chart.panLater")} data-testid="chart-pan-later"><Icon name="chevron-right" size="sm" /></button>
      {:else}
        <span class="zoom-hint">{t("chart.zoomHint")}</span>
        <span class="zoom-hint touch">{t("chart.pinchHint")}</span>
      {/if}
      <button class="icon-btn small" type="button" onclick={() => zoomBy(1 / ZOOM_STEP)} aria-label={t("chart.zoomIn")} title={t("chart.zoomIn")} data-testid="chart-zoom-in"><Icon name="zoom-in" size="sm" /></button>
      <button class="icon-btn small" type="button" onclick={() => zoomBy(ZOOM_STEP)} aria-label={t("chart.zoomOut")} title={t("chart.zoomOut")} data-testid="chart-zoom-out"><Icon name="zoom-out" size="sm" /></button>
    </span>
  </div>
  <div class="plot">
    <div class="chart" bind:this={host} role="img" aria-label={label}></div>
    {@render loadingPill()}
  </div>
  {#if showTable}
    <div class="table-scroll">
      <table class="data">
        <thead>
          <tr><th>{t("chart.time")}</th>{#each series as s (s.id)}<th class="r">{s.label}</th>{/each}</tr>
        </thead>
        <tbody>
          {#each time as stamp, i (i)}
            {#if i % Math.max(1, Math.ceil(time.length / 96)) === 0}
              <tr>
                <td class="num">{f.dateTime(stamp * 1000, { seconds: subMinute })}</td>
                {#each series as s (s.id)}<td class="r num">{valueText(shownValue(s.id, values?.[s.id]?.[i] ?? null), s)}</td>{/each}
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
{/if}

<style>
  .legend { display: flex; flex-wrap: wrap; gap: var(--s2) var(--s4); align-items: center; margin-bottom: var(--s3); font-size: 12.5px; min-height: 20px; }
  .legend-time { color: var(--m-muted); min-width: 44px; }
  .zoom { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }
  .zoom-hint { font-size: 11.5px; color: var(--m-muted-2); }
  .zoom-hint.touch { display: none; }
  @media (pointer: coarse) {
    .zoom-hint { display: none; }
    .zoom-hint.touch { display: inline; }
  }
  .legend-item { display: inline-flex; align-items: center; gap: 7px; color: var(--m-muted); }
  .legend-item i { width: 10px; height: 10px; border-radius: 3px; background: var(--c); }
  .legend-item i.dash { width: 12px; height: 0; border-radius: 0; background: none; border-top: 2px dashed var(--c); }
  .legend-item b { color: var(--m-text); font-weight: 600; min-width: 54px; }
  .plot { position: relative; }
  .busy { position: absolute; top: var(--s2); left: 50%; transform: translateX(-50%); pointer-events: none; }
  .chart { width: 100%; min-height: 120px; cursor: crosshair; }
  .chart :global(.u-over) { cursor: crosshair; touch-action: pan-y; }
  .chart-empty { min-height: 160px; justify-content: center; }
  .table-scroll { max-height: 420px; overflow: auto; margin-top: var(--s4); }
</style>
