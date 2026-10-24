<script>
  import { untrack } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import TimeChart from "../ui/TimeChart.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { getJson } from "../../../shared/lib/api.js";
  import { fetchSeries } from "../lib/historyApi.js";
  import { escapeLeavesZoom, zoomSpan } from "../lib/history.js";
  import { ANALYTICS_RANGES, ANALYTICS_VIEWS, ANALYTICS_OVERLAYS, SERIES_STYLE, viewById, requestedSeries, computeKpi, runtimeRole, KPIS } from "../lib/analytics.js";
  import { devices } from "../state/derived.svelte.js";
  import { live } from "../state/live.svelte.js";

  const REFRESH_MS = 30000;

  let status = $state.raw(null);
  let viewId = $state("overview");
  let range = $state("24h");
  let custom = $state({ start: "", end: "" });
  let applied = $state(null);
  let device = $state("");
  let overlays = $state([]);
  let data = $state.raw(null);
  let socData = $state.raw(null);
  let loading = $state(false);
  let error = $state(false);
  let showTable = $state(false);
  let unavailable = $state.raw(null);
  let zoom = $state.raw(null);
  let dataPeriod = $state.raw(null);
  let dataKey = "";
  let failedKey = "";
  let zoomEpoch = $state(0);
  let liveExtent = $state.raw(null);
  let controller = null;
  let zoomTimer = null;

  const f = $derived(fmt());
  const view = $derived(viewById(viewId));
  const names = $derived(devices().map((d) => d.name));
  const powerOverlays = $derived(overlays.filter((id) => id !== "soc"));
  const seriesIds = $derived(requestedSeries(view, powerOverlays));
  const chartSeries = $derived(seriesIds.map((id) => ({ id, label: t(`series.${id}`), ...SERIES_STYLE[id] })));
  const rangeLabel = $derived(periodLabel(data && dataPeriod ? dataPeriod : { zoom, applied, range }));
  const chartHasData = $derived(seriesIds.some((id) => (data?.series?.[id] || []).some((value) => value !== null && value !== undefined)));

  /** A window that is not one of the ranges is named by its length, as Classic names the KPIs' period. */
  function spanText(seconds) {
    const span = zoomSpan(seconds);
    return span ? t(span.key, { n: f.number(span.n, Number.isInteger(span.n) ? 0 : 1) }) : "";
  }

  /** The KPIs' period is the one their answer was read for, as Classic takes it from the answer. */
  function periodLabel(period) {
    if (period.zoom) return spanText(period.zoom.end - period.zoom.start);
    if (period.applied) return spanText(period.applied.end - period.applied.start) || t("analytics.custom");
    return t(`chart.range.${period.range}`);
  }

  async function loadStatus() {
    try {
      status = await getJson("/api/analytics/status");
    } catch {
      status = { available: false, reason: "unreachable" };
    }
  }

  function timeParams() {
    if (zoom) return { start: zoom.start, end: zoom.end };
    if (applied) return { start: applied.start, end: applied.end };
    return { range };
  }

  /**
   * A zoom re-reads the window from the server, which answers with a finer
   * profile; the live refresh pauses until the way back, which reads the
   * range again. Several steps in a row send one request. A zoom whose
   * answer has nothing to draw, or is unavailable, has its way back here,
   * because the chart then has no buttons; leaving it from the page tells
   * the charts through their scope.
   */
  function onzoom(window) {
    zoom = window ? { start: Math.floor(window.min), end: Math.ceil(window.max) } : null;
    controller?.abort();
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(load, 180);
  }

  function leaveZoom() {
    clearTimeout(zoomTimer);
    zoom = null;
    zoomEpoch += 1;
    load();
  }

  async function load() {
    if (!status?.available) return;
    controller?.abort();
    const mine = new AbortController();
    controller = mine;
    loading = true;
    const asked = zoom;
    const period = { zoom, applied, range };
    const key = JSON.stringify([seriesIds, timeParams(), device, overlays.includes("soc")]);
    if (key !== failedKey) error = false;
    const devicesParam = device ? [device] : undefined;
    try {
      const [main, soc] = await Promise.all([
        fetchSeries({ source: "analytics", ...timeParams(), series: seriesIds, devices: devicesParam, signal: mine.signal }),
        overlays.includes("soc")
          ? fetchSeries({ source: "analytics", ...timeParams(), series: ["soc"], devices: devicesParam, signal: mine.signal })
          : Promise.resolve(null),
      ]);
      if (!main.available) {
        unavailable = { reason: main.reason, hint: main.hint };
        data = null;
        socData = null;
        dataKey = "";
        clearTimeout(zoomTimer);
        zoom = asked;
      } else {
        unavailable = null;
        data = main;
        socData = soc;
        dataPeriod = period;
        dataKey = key;
        failedKey = "";
        if (!asked && main.time.length > 1) liveExtent = [main.time[0], main.time[main.time.length - 1]];
      }
      error = false;
    } catch (err) {
      if (err?.name !== "AbortError") {
        error = true;
        failedKey = key;
        if (key !== dataKey) {
          data = null;
          socData = null;
        }
      }
    } finally {
      if (controller === mine) loading = false;
    }
  }

  $effect(() => {
    loadStatus();
  });

  $effect(() => {
    void status?.available;
    void seriesIds.join(",");
    void range;
    void applied;
    void device;
    void overlays.includes("soc");
    untrack(() => {
      clearTimeout(zoomTimer);
      zoom = null;
      zoomEpoch += 1;
      liveExtent = null;
      load();
    });
  });

  $effect(() => {
    const timer = setInterval(() => {
      if (document.hidden) return;
      if (!status?.available) loadStatus();
      else if (!applied && !zoom && !loading) load();
    }, REFRESH_MS);
    return () => {
      clearInterval(timer);
      clearTimeout(zoomTimer);
    };
  });

  $effect(() => {
    if (!zoom || !(unavailable || (error && !data))) return;
    const onKey = (event) => {
      if (escapeLeavesZoom(event)) leaveZoom();
    };
    globalThis.addEventListener("keydown", onKey);
    return () => globalThis.removeEventListener("keydown", onKey);
  });

  function setView(value) {
    if (value === viewId) {
      again();
      return;
    }
    viewId = value;
  }

  function toggleOverlay(id) {
    overlays = overlays.includes(id) ? overlays.filter((item) => item !== id) : [...overlays, id];
  }

  function applyCustom(event) {
    event.preventDefault();
    const start = Date.parse(custom.start);
    const end = Date.parse(custom.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) return;
    applied = { start: start / 1000, end: end / 1000 };
  }

  /** A click on the range or view already shown leaves a zoom and reads it again, as in Classic. */
  function again() {
    if (zoom) leaveZoom();
    else load();
  }

  function setRange(value) {
    if (value === range && !applied) {
      again();
      return;
    }
    applied = null;
    range = value;
  }

  const socNow = $derived.by(() => {
    const snapshot = live.snapshot;
    if (!snapshot) return null;
    if (!device) return snapshot.average_soc ?? null;
    const d = devices().find((item) => item.name === device);
    return d?.socReported ? d.soc : null;
  });
  const role = $derived(runtimeRole(live.snapshot, device));
  const zoomScope = $derived(`${view.id}|${range}|${device}|${applied ? `${applied.start}-${applied.end}` : ""}|${zoomEpoch}`);
</script>

{#snippet unavailableInfo(info)}
  <div class="unavailable" data-testid="analytics-unavailable" data-reason={info.reason || ""}>
    <h3 class="sub">{info.reason === "schema_incomplete" ? t("analytics.schemaIncomplete") : info.hint || info.reason === "unreachable" ? t("analytics.notReachable") : t("analytics.notConfiguredTitle")}</h3>
    {#if info.hint}<pre class="hint" lang="en">{info.hint}</pre>{:else}<p class="muted">{t(info.reason === "unreachable" ? "analytics.unreachable" : "analytics.notConfigured")}</p>{/if}
  </div>
{/snippet}

{#if !status}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if !status.available}
  <Card title={t("analytics.unavailableTitle")} span="span-12">
    {@render unavailableInfo(status)}
  </Card>
{:else}
  <div class="controls span-12">
    <Segmented options={ANALYTICS_VIEWS.map((v) => ({ value: v.id, label: t(`analytics.view.${v.id}`) }))} value={viewId} label={t("analytics.view")} onchange={setView} />
    <Segmented options={ANALYTICS_RANGES.map((value) => ({ value, label: t(`chart.range.${value}`) }))} value={applied ? "" : range} label={t("analytics.range")} onchange={setRange} />
  </div>
  <div class="controls span-12">
    <label class="inline">
      <span>{t("chart.device")}</span>
      <select class="input" bind:value={device}>
        <option value="">{t("chart.allDevices")}</option>
        {#each names as name (name)}<option value={name}>{name}</option>{/each}
      </select>
    </label>
    <form class="custom" onsubmit={applyCustom}>
      <label class="inline"><span>{t("analytics.from")}</span><input class="input" type="datetime-local" bind:value={custom.start} required /></label>
      <label class="inline"><span>{t("analytics.to")}</span><input class="input" type="datetime-local" bind:value={custom.end} required /></label>
      <button class="btn secondary small" type="submit">{t("analytics.apply")}</button>
    </form>
    <div class="overlays" role="group" aria-label={t("analytics.overlays")}>
      {#each ANALYTICS_OVERLAYS as id (id)}
        <button type="button" class="chip" aria-pressed={overlays.includes(id) ? "true" : "false"} onclick={() => toggleOverlay(id)}>{t(`series.${id}`)}</button>
      {/each}
    </div>
  </div>

  <div class="kpis span-12">
    {#each view.kpis as id (id)}
      {@const value = computeKpi(id, data)}
      <div class="card kpi" style:--c={`var(${KPIS[id].token})`}>
        <div class="kpi-label">{t(`analytics.kpi.${id}`)} · {rangeLabel}</div>
        <div class="kpi-value num">{KPIS[id].kind === "peak" ? f.power(value) : f.energy(value)}</div>
      </div>
    {/each}
    <div class="card kpi c-bat">
      <div class="kpi-label">{t("analytics.kpi.soc")}</div>
      <div class="kpi-value num">{f.percent(socNow)}</div>
    </div>
    <div class="card kpi c-out">
      <div class="kpi-label">{t("analytics.kpi.role")}</div>
      <div class="kpi-value">{role ? t(`analytics.role.${role}`) : "—"}</div>
    </div>
  </div>

  {#snippet actions()}
    {#if loading}<span class="muted small">{t("common.loading")}</span>{:else if error && data}<span class="muted small" role="status">{t("chart.stale")}</span>{/if}
    {#if zoom && (unavailable || !chartHasData)}<button class="btn secondary small" type="button" onclick={leaveZoom} data-testid="analytics-zoom-reset">{t("chart.zoomReset")}</button>{/if}
    <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
      <Icon name="table" size="sm" />{t("chart.table")}
    </button>
  {/snippet}

  <Card title={t(`analytics.view.${view.id}`)} subtitle={t("analytics.source")} span="span-12" {actions}>
    {#if unavailable}
      {@render unavailableInfo(unavailable)}
    {:else if error && !data}
      <div class="empty error">{t("error.load")}</div>
    {:else}
      <TimeChart series={chartSeries} time={data?.time || []} values={data?.series || {}} {showTable} label={t(`analytics.view.${view.id}`)} scope={zoomScope} bounds={liveExtent} {onzoom} emptyText={data ? "" : t("chart.loading")} />
    {/if}
  </Card>

  {#if overlays.includes("soc") && !unavailable && !(error && !data)}
    <Card title={t("series.soc")} span="span-12">
      <TimeChart series={[{ id: "soc", label: t("series.soc"), token: "--battery", unit: "%", fill: false }]} time={socData?.time || []} values={socData?.series || {}} emptyText={socData ? "" : t("chart.loading")} kind="percent" height={180} label={t("series.soc")} scope={`${zoomScope}|${zoom ? `${zoom.start}-${zoom.end}` : ""}`} />
    </Card>
  {/if}
{/if}

<style>
  .controls { display: flex; flex-wrap: wrap; gap: var(--s3); align-items: center; }
  .inline { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--m-muted); }
  .inline .input { width: auto; height: 32px; }
  .custom { display: inline-flex; flex-wrap: wrap; gap: var(--s2); align-items: center; }
  .overlays { display: inline-flex; flex-wrap: wrap; gap: 6px; margin-left: auto; }
  .chip { border: 1px solid var(--m-line-strong); background: transparent; color: var(--m-muted); border-radius: var(--m-radius-pill); padding: 4px 10px; font-size: 12px; cursor: pointer; }
  .chip[aria-pressed="true"] { background: var(--m-selected-bg); color: var(--m-selected-ink); border-color: transparent; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: var(--s4); }
  .kpi { padding: var(--s4); border-left: 3px solid var(--c); }
  .kpi-label { font-size: 12px; color: var(--m-muted); }
  .kpi-value { font-size: 20px; font-weight: 650; margin-top: 2px; }
  .unavailable { display: flex; flex-direction: column; gap: var(--s2); }
  .unavailable .sub { margin: 0; font-size: 14px; font-weight: 620; }
  .unavailable p { margin: 0; }
  .hint { white-space: pre-wrap; font-family: var(--m-mono); font-size: 12.5px; background: var(--m-well); border-radius: var(--m-radius-inner); padding: var(--s3); margin: var(--s3) 0 0; }
  .small { font-size: 12px; }
</style>
