<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "cell-history", data: ["history:range:cell_min,cell_max[device,pack]"], refresh: "60s", minWidth: 320, states: ["empty", "gaps", "error"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import TimeChart from "../ui/TimeChart.svelte";
  import CellPicker from "../ui/CellPicker.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { fetchSeries } from "../lib/historyApi.js";
  import { emptyKey, sourceKey } from "../lib/history.js";
  import { cellSeriesIds, cellStyle, shownPack } from "../lib/cells.js";

  /** The devices that report cell voltages, each with its packs. */
  let { devices = [] } = $props();

  const RANGES = ["1h", "6h", "24h", "7d"];

  let range = $state("24h");
  let rangeClicks = $state(0);
  let wantedDevice = $state("");
  let wantedPack = $state(1);
  let showTable = $state(false);
  let data = $state.raw(null);
  let dataKey = "";
  let dataSelection = "";
  let failedKey = "";
  let error = $state(false);
  let controller = null;

  const chosen = $derived(devices.find((entry) => entry.name === wantedDevice) || devices[0] || null);
  const device = $derived(chosen?.name || "");
  const pack = $derived(chosen ? shownPack(chosen.cells, wantedPack) : null);
  const ids = $derived(pack === null ? [] : cellSeriesIds(pack));
  const series = $derived(ids.map((id) => ({ id, ...cellStyle(id), label: t(cellStyle(id).label) })));
  const title = $derived(t("cells.titleRange", { range: t(`chart.range.${range}`) }));

  async function load() {
    if (!device || pack === null) return;
    controller?.abort();
    controller = new AbortController();
    const selection = `${device}|${pack}`;
    const key = `${range}|${selection}`;
    if (key !== failedKey) error = false;
    if (selection !== dataSelection) data = null;
    try {
      data = await fetchSeries({ range, series: ids, devices: [device], signal: controller.signal });
      dataKey = key;
      dataSelection = selection;
      failedKey = "";
      error = false;
    } catch (err) {
      if (err?.name !== "AbortError") {
        error = true;
        failedKey = key;
        if (key !== dataKey) data = null;
      }
    }
  }

  /** The range already shown is read again and leaves a zoom, as the power chart does. */
  function pickRange(value) {
    if (value !== range) {
      range = value;
      return;
    }
    rangeClicks += 1;
    load();
  }

  $effect(() => {
    void range;
    void device;
    void pack;
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
  {#if sourceKey(data?.source)}<span class="badge">{t(sourceKey(data.source))}</span>{/if}
  <CellPicker {devices} {device} {pack} compact ondevice={(name) => (wantedDevice = name)} onpack={(value) => (wantedPack = value)} />
  <Segmented options={RANGES.map((value) => ({ value, label: t(`chart.range.${value}`) }))} value={range} label={t("chart.rangeLabel")} onchange={pickRange} />
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card icon="battery-medium" tone="c-bat" {title} subtitle={t("cells.sub", { device })} span="span-12" {actions}>
  <div data-testid="cell-history">
    {#if error && !data}
      <div class="empty error">{t("error.load")}</div>
    {:else}
      <TimeChart {series} unit="V" time={data?.time || []} values={data?.series || {}} {showTable} label={title} scope={`${range}|${device}|${pack}|${rangeClicks}`} emptyText={t(emptyKey(data, error))} />
      {#if error && data}<p class="note" role="status"><Icon name="circle-alert" />{t("chart.stale")}</p>{/if}
      {#if data?.meta?.retention_hours}<p class="note"><Icon name="info" />{t("chart.retention", { h: data.meta.retention_hours })}</p>{/if}
    {/if}
  </div>
</Card>
