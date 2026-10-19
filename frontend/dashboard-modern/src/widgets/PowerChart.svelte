<script module>
  export const manifest = { id: "power-chart", data: ["history:range:pv,home,battery,grid"], refresh: "60s", minWidth: 320, states: ["empty", "gaps", "retention", "error"] };
</script>

<script>
  import Card from "../ui/Card.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import TimeChart from "../ui/TimeChart.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { fetchSeries } from "../lib/historyApi.js";
  import { localDayStart } from "../lib/history.js";
  import { live } from "../state/live.svelte.js";

  let { todayHistory = null, devices = null, title = "", span = "span-12" } = $props();

  const SERIES = [
    { id: "pv", label: "series.pv", token: "--pv" },
    { id: "home", label: "series.home", token: "--output" },
    { id: "battery", label: "series.battery", token: "--battery", signed: true },
    { id: "grid", label: "series.grid", token: "--grid", signed: true },
  ];
  const RANGES = ["1h", "6h", "today", "24h", "7d"];

  let range = $state("today");
  let showTable = $state(false);
  let ranged = $state.raw(null);
  let error = $state(false);
  let controller = null;

  const series = $derived(SERIES.map((s) => ({ ...s, label: t(s.label) })));
  const heading = $derived(title || (range === "today" ? t("chart.title") : t("chart.titleRange", { range: t(`chart.range.${range}`) })));
  const dayStart = $derived(localDayStart(new Date(live.now)));
  const data = $derived(range === "today" && !devices ? todayHistory : ranged);
  const xRange = $derived(range === "today" ? [dayStart, localDayStart(new Date(live.now), 1)] : null);

  async function load() {
    if (range === "today" && !devices) return;
    controller?.abort();
    controller = new AbortController();
    const now = Date.now() / 1000;
    try {
      const params = range === "today"
        ? { start: localDayStart(), end: now }
        : { range };
      ranged = await fetchSeries({ ...params, series: SERIES.map((s) => s.id), devices, signal: controller.signal });
      error = false;
    } catch (err) {
      if (err?.name !== "AbortError") error = true;
    }
  }

  $effect(() => {
    void range;
    void devices;
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 60000);
    return () => {
      clearInterval(timer);
      controller?.abort();
    };
  });
</script>

{#snippet actions()}
  <Segmented
    options={RANGES.map((value) => ({ value, label: t(`chart.range.${value}`) }))}
    value={range}
    label={t("chart.rangeLabel")}
    onchange={(value) => (range = value)}
  />
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card title={heading} subtitle={t("chart.sub")} {span} {actions}>
  {#if error && !data}
    <div class="empty error">{t("error.load")}</div>
  {:else}
    <TimeChart
      {series}
      time={data?.time || []}
      values={data?.series || {}}
      {xRange}
      {showTable}
      label={heading}
    />
    {#if data?.meta?.retention_hours}
      <p class="note"><Icon name="info" />{t("chart.retention", { h: data.meta.retention_hours })}</p>
    {/if}
    <p class="note"><Icon name="info" />{t("chart.gapNote")}</p>
  {/if}
</Card>
