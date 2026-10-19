<script>
  import Card from "../ui/Card.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import TimeChart from "../ui/TimeChart.svelte";
  import Empty from "../ui/Empty.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { getJson } from "../lib/api.js";
  import { fetchSeries } from "../lib/historyApi.js";
  import { ANALYTICS_RANGES, ANALYTICS_VIEWS, ANALYTICS_OVERLAYS, SERIES_STYLE, viewById, requestedSeries, computeKpi, KPIS } from "../lib/analytics.js";
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
  let controller = null;

  const f = $derived(fmt());
  const view = $derived(viewById(viewId));
  const names = $derived(devices().map((d) => d.name));
  const powerOverlays = $derived(overlays.filter((id) => id !== "soc"));
  const seriesIds = $derived(requestedSeries(view, powerOverlays));
  const chartSeries = $derived(seriesIds.map((id) => ({ id, label: t(`series.${id}`), ...SERIES_STYLE[id] })));
  const rangeLabel = $derived(applied ? t("analytics.custom") : t(`chart.range.${range}`));

  async function loadStatus() {
    try {
      status = await getJson("/api/analytics/status");
    } catch {
      status = { available: false, reason: "unreachable" };
    }
  }

  function timeParams() {
    if (applied) return { start: applied.start, end: applied.end };
    return { range };
  }

  async function load() {
    if (!status?.available) return;
    controller?.abort();
    controller = new AbortController();
    loading = true;
    const devicesParam = device ? [device] : undefined;
    try {
      const [main, soc] = await Promise.all([
        fetchSeries({ source: "analytics", ...timeParams(), series: seriesIds, devices: devicesParam, signal: controller.signal }),
        overlays.includes("soc")
          ? fetchSeries({ source: "analytics", ...timeParams(), series: ["soc"], devices: devicesParam, signal: controller.signal })
          : Promise.resolve(null),
      ]);
      if (!main.available) {
        status = { available: false, reason: main.reason, hint: main.hint };
        data = null;
      } else {
        data = main;
      }
      socData = soc;
      error = false;
    } catch (err) {
      if (err?.name !== "AbortError") error = true;
    } finally {
      loading = false;
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
    load();
  });

  $effect(() => {
    const timer = setInterval(() => {
      if (!document.hidden && !applied && !loading) load();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

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

  function setRange(value) {
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
</script>

{#if !status}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if !status.available}
  <Card title={t("analytics.unavailableTitle")} span="span-12">
    <Empty text={t(status.reason === "not_configured" ? "analytics.notConfigured" : "analytics.unreachable")} icon="info" />
    {#if status.hint}<pre class="hint">{status.hint}</pre>{/if}
  </Card>
{:else}
  <div class="controls span-12">
    <Segmented options={ANALYTICS_VIEWS.map((v) => ({ value: v.id, label: t(`analytics.view.${v.id}`) }))} value={viewId} label={t("analytics.view")} onchange={(v) => (viewId = v)} />
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
  </div>

  {#snippet actions()}
    {#if loading}<span class="muted small">{t("common.loading")}</span>{/if}
    <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
      <Icon name="table" size="sm" />{t("chart.table")}
    </button>
  {/snippet}

  <Card title={t(`analytics.view.${view.id}`)} subtitle={t("analytics.source")} span="span-12" {actions}>
    {#if error && !data}
      <div class="empty error">{t("error.load")}</div>
    {:else}
      <TimeChart series={chartSeries} time={data?.time || []} values={data?.series || {}} {showTable} label={t(`analytics.view.${view.id}`)} />
    {/if}
  </Card>

  {#if overlays.includes("soc")}
    <Card title={t("series.soc")} span="span-12">
      <TimeChart series={[{ id: "soc", label: t("series.soc"), token: "--battery", unit: "%", fill: false }]} time={socData?.time || []} values={socData?.series || {}} kind="percent" height={180} label={t("series.soc")} />
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
  .hint { white-space: pre-wrap; font-family: var(--m-mono); font-size: 12.5px; background: var(--m-well); border-radius: var(--m-radius-inner); padding: var(--s3); margin: var(--s3) 0 0; }
  .small { font-size: 12px; }
</style>
