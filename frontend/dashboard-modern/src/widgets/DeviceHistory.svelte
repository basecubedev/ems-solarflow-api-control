<script module>
  export const manifest = { id: "device-history", data: ["history:range:pv,output,battery[device]"], refresh: "60s", minWidth: 320, states: ["empty", "gaps", "error"] };
</script>

<script>
  import Card from "../ui/Card.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import TimeChart from "../ui/TimeChart.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { fetchSeries } from "../lib/historyApi.js";

  let { names = [] } = $props();

  const SERIES = [
    { id: "pv", label: "series.pv", token: "--pv" },
    { id: "output", label: "series.output", token: "--output" },
    { id: "battery", label: "series.battery", token: "--battery", signed: true },
  ];
  const RANGES = ["1h", "6h", "24h", "7d"];

  let range = $state("24h");
  let device = $state("");
  let showTable = $state(false);
  let data = $state.raw(null);
  let error = $state(false);
  let controller = null;

  const series = $derived(SERIES.map((s) => ({ ...s, label: t(s.label) })));

  async function load() {
    controller?.abort();
    controller = new AbortController();
    try {
      data = await fetchSeries({ range, series: SERIES.map((s) => s.id), devices: device ? [device] : undefined, signal: controller.signal });
      error = false;
    } catch (err) {
      if (err?.name !== "AbortError") error = true;
    }
  }

  $effect(() => {
    void range;
    void device;
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
  <label class="device-select">
    <span class="sr-only">{t("chart.device")}</span>
    <select class="input" bind:value={device}>
      <option value="">{t("chart.allDevices")}</option>
      {#each names as name (name)}<option value={name}>{name}</option>{/each}
    </select>
  </label>
  <Segmented options={RANGES.map((value) => ({ value, label: t(`chart.range.${value}`) }))} value={range} label={t("chart.rangeLabel")} onchange={(value) => (range = value)} />
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card title={t("chart.titleRange", { range: t(`chart.range.${range}`) })} subtitle={t("chart.sub")} span="span-12" {actions}>
  {#if error && !data}
    <div class="empty error">{t("error.load")}</div>
  {:else}
    <TimeChart {series} time={data?.time || []} values={data?.series || {}} {showTable} label={t("chart.titleRange", { range: t(`chart.range.${range}`) })} />
    {#if data?.meta?.retention_hours}<p class="note"><Icon name="info" />{t("chart.retention", { h: data.meta.retention_hours })}</p>{/if}
  {/if}
</Card>

<style>
  .device-select .input { height: 32px; width: auto; }
</style>
