<script>
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { prefs } from "../state/prefs.svelte.js";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { withGapBreaks, gapRanges, sampleSpacing, tickLabels, tickPad, tickSpace, atLeastAMinute, ZOOM_STEP, escapeLeavesZoom, zoomCenter, zoomDiffers, zoomStep } from "../lib/history.js";

  /**
   * One uPlot instance per chart, kept across data refreshes (setData) and
   * rebuilt only when its structure changes: the series, the colour scheme,
   * the language or the height. That is Classic's chart contract. A mouse
   * drag across the plot zooms into that stretch; a refresh of the same data
   * keeps the zoom, and a new range, device or view (``scope``) drops it.
   * The wheel and the + and − buttons zoom too, Escape and Reset zoom leave
   * it, and a rebuild for a new scheme or language keeps it. How far out it
   * may zoom is the fixed ``xRange``, else ``bounds``, else the loaded data;
   * ``onzoom`` hears every window, and null for the way back, so a page can
   * read the window from the server. A page that leaves the zoom by itself
   * changes ``scope``. A series in percent reads against its own 0-100
   * scale on the right, as Classic draws the SoC, labelled in the series'
   * ``ink`` -- a text colour, since the line's own may be too dark to read.
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
  } = $props();

  let host = $state();
  const PERCENT_AXIS_PX = 56;
  let chart = null;
  let signature = "";
  let cursorIndex = $state(null);
  let zoomed = $state(false);
  let zoomWindow = null;
  let zoomScope = null;

  const f = $derived(fmt());
  const prepared = $derived.by(() => {
    const lists = series.map((s) => values?.[s.id] || []);
    const broken = withGapBreaks(time, lists);
    return { time: broken.time, lists: broken.series, gaps: gapRanges(time, lists) };
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
    return f.power(value, { signed: Boolean(s?.signed) });
  }

  function onPercentScale(s) {
    return s.unit === "%";
  }

  function axisValue(value) {
    if (value === 0) return "0";
    return Math.abs(value) >= 1000 ? `${f.number(value / 1000, 1)} kW` : `${f.number(value, 0)} W`;
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
    zoomWindow = null;
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

  function onWheel(event) {
    event.preventDefault();
    if (!chart) return;
    const rect = chart.over.getBoundingClientRect();
    const center = chart.posToVal(event.clientX - rect.left, "x");
    zoomBy(event.deltaY < 0 ? 1 / ZOOM_STEP : ZOOM_STEP, Number.isFinite(center) ? center : undefined);
  }

  function build() {
    const uPlot = globalThis.uPlot;
    if (!host || !uPlot) return;
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
          y: { range: (u, min, max) => [Math.min(0, Math.floor((min || 0) / 500) * 500), Math.max(500, Math.ceil((max || 0) / 500) * 500)] },
          ...(percent ? { pct: { range: [0, 100] } } : {}),
        },
        axes: [
          {
            ...axis,
            space: (u, axis, min, max) => tickSpace(min, max),
            values: (u, splits, axis, space, step) => tickLabels(splits, step, f),
          },
          { ...axis, size: 66, values: (u, splits) => splits.map(axisValue) },
          ...(percent
            ? [{ ...axis, scale: "pct", side: 1, size: PERCENT_AXIS_PX, stroke: token(percent.ink || "--m-muted"), grid: { show: false }, values: (u, splits) => splits.map((value) => f.percent(value)) }]
            : []),
        ],
        series: [
          {},
          ...series.map((s) => {
            const color = token(s.token);
            return {
              label: s.id,
              scale: onPercentScale(s) ? "pct" : "y",
              stroke: color,
              dash: s.dash,
              width: 1.75,
              fill: s.fill === false ? undefined : areaFill(color),
              points: { show: false },
              spanGaps: false,
            };
          }),
        ],
        hooks: {
          setCursor: [(u) => (cursorIndex = u.cursor.idx ?? null)],
          setSelect: [zoomToSelection],
          draw: [drawGaps],
        },
      },
      [prepared.time, ...prepared.lists],
      host,
    );
    chart.over.addEventListener("wheel", onWheel, { passive: false });
  }

  $effect(() => {
    const next = [series.map((s) => s.id).join(","), prefs.theme, prefs.lang, height, xRange ? xRange.join("-") : ""].join("|");
    const reach = `${scope}|${xRange ? xRange.join("-") : ""}`;
    if (zoomScope !== null && reach !== zoomScope) {
      zoomWindow = null;
      zoomed = false;
    }
    zoomScope = reach;
    if (!host || !hasData) return;
    if (next !== signature || !chart) {
      signature = next;
      build();
    } else {
      chart.setData([prepared.time, ...prepared.lists]);
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

{#if !hasData}
  <div class="empty chart-empty" role="status">{emptyText || t("chart.empty")}</div>
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
      {:else}
        <span class="zoom-hint">{t("chart.zoomHint")}</span>
      {/if}
      <button class="icon-btn small" type="button" onclick={() => zoomBy(1 / ZOOM_STEP)} aria-label={t("chart.zoomIn")} title={t("chart.zoomIn")} data-testid="chart-zoom-in"><Icon name="zoom-in" size="sm" /></button>
      <button class="icon-btn small" type="button" onclick={() => zoomBy(ZOOM_STEP)} aria-label={t("chart.zoomOut")} title={t("chart.zoomOut")} data-testid="chart-zoom-out"><Icon name="zoom-out" size="sm" /></button>
    </span>
  </div>
  <div class="chart" bind:this={host} role="img" aria-label={label}></div>
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
                {#each series as s (s.id)}<td class="r num">{valueText(values?.[s.id]?.[i] ?? null, s)}</td>{/each}
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
  @media (pointer: coarse) { .zoom-hint { display: none; } }
  .legend-item { display: inline-flex; align-items: center; gap: 7px; color: var(--m-muted); }
  .legend-item i { width: 10px; height: 10px; border-radius: 3px; background: var(--c); }
  .legend-item i.dash { width: 12px; height: 0; border-radius: 0; background: none; border-top: 2px dashed var(--c); }
  .legend-item b { color: var(--m-text); font-weight: 600; min-width: 54px; }
  .chart { width: 100%; min-height: 120px; cursor: crosshair; }
  .chart :global(.u-over) { cursor: crosshair; }
  .chart-empty { min-height: 160px; justify-content: center; }
  .table-scroll { max-height: 420px; overflow: auto; margin-top: var(--s4); }
</style>
