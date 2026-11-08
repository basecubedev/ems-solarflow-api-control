<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "consumer-history", data: ["history:range:consumer:*,home"], refresh: "60s", minWidth: 320, states: ["empty", "gaps", "error"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import TimeChart from "../ui/TimeChart.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { fetchSeries } from "../lib/historyApi.js";
  import { emptyKey, sourceKey } from "../lib/history.js";
  import { live } from "../state/live.svelte.js";
  import { chartConsumers, chartTokens, consumerPalette, consumerSeriesId, restOfHouse, OTHER_TOKEN } from "../lib/consumers.js";

  /**
   * The consumers that drew most over the last seven days, each a band, on
   * the rest of the house (Home less them), so the stack is the home use. Only
   * those are fetched; thirty consumers draw five bands and one for the rest.
   * A week, not today: a new day would start every consumer at nothing.
   */

  const RANGES = ["1h", "6h", "24h", "7d"];

  let range = $state("24h");
  let rangeClicks = $state(0);
  let showTable = $state(false);
  let data = $state.raw(null);
  let dataKey = "";
  let failedKey = "";
  let error = $state(false);
  let controller = null;

  const chosen = $derived(chartConsumers(live.snapshot, live.snapshot?.energy_stats?.last_7_days));
  const palette = $derived(consumerPalette(live.snapshot, live.snapshot?.energy_stats?.lifetime));
  // A string, so a live frame naming the same consumers in the same colours
  // reloads and redraws nothing.
  const nameKey = $derived(JSON.stringify(chartTokens(chosen.shown, palette)));
  const shown = $derived(JSON.parse(nameKey));
  const rest = $derived(chosen.rest);
  const series = $derived([
    ...shown.map((item) => ({ id: consumerSeriesId(item.key), label: item.name, token: item.token })),
    { id: "rest", label: t(rest ? "flow.otherConsumers" : "consumers.unmeasured"), token: OTHER_TOKEN },
  ]);
  const values = $derived.by(() => {
    const all = data?.series || {};
    return { ...all, rest: restOfHouse(all.home || [], shown.map((item) => all[consumerSeriesId(item.key)])) };
  });

  async function load() {
    controller?.abort();
    controller = new AbortController();
    const key = `${range}|${nameKey}`;
    if (key !== failedKey) error = false;
    try {
      data = await fetchSeries({ range, series: [...shown.map((item) => consumerSeriesId(item.key)), "home"], signal: controller.signal });
      dataKey = key;
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
    void nameKey;
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
  <Segmented options={RANGES.map((value) => ({ value, label: t(`chart.range.${value}`) }))} value={range} label={t("chart.rangeLabel")} onchange={pickRange} />
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card icon="chart-spline" tone="c-out" title={t("consumers.historyTitle", { range: t(`chart.range.${range}`) })} subtitle={t("consumers.historySub")} span="span-12" {actions}>
  {#if error && !data}
    <div class="empty error">{t("error.load")}</div>
  {:else}
    <div data-testid="consumer-history">
      <TimeChart {series} stacked time={data?.time || []} {values} {showTable} label={t("consumers.historyTitle", { range: t(`chart.range.${range}`) })} scope={`${range}|${nameKey}|${rangeClicks}`} emptyText={t(emptyKey(data, error))} />
    </div>
    {#if rest}<p class="note"><Icon name="info" />{t("consumers.restHint")}</p>{/if}
    {#if error && data}<p class="note" role="status"><Icon name="circle-alert" />{t("chart.stale")}</p>{/if}
    {#if data?.meta?.retention_hours}<p class="note"><Icon name="info" />{t("chart.retention", { h: data.meta.retention_hours })}</p>{/if}
  {/if}
</Card>
