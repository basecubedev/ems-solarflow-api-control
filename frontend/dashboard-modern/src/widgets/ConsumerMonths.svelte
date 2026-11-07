<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "consumer-months", data: ["energy.monthly_current_year", "energy.yearly"], refresh: "sse", minWidth: 320, states: ["running", "future", "missing"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import MonthChart from "./MonthChart.svelte";
  import FactList from "./FactList.svelte";
  import ConsumerFacts from "./ConsumerFacts.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { consumerMonths, consumerPalette, monthSegments, stackedConsumers, OTHER_TOKEN } from "../lib/consumers.js";

  /**
   * This year's months as the consumers drew them: the five that drew most
   * this year each a band, every other consumer one band above them. A month
   * picked lists what each consumer drew in it, the largest first.
   */
  let { stats, expert = false, now = new Date() } = $props();

  const DETAIL_ROWS = 8;

  let showTable = $state(false);
  let picked = $state(null);

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const year = $derived((Array.isArray(stats?.yearly) ? stats.yearly : []).find((item) => Number(item.year) === now.getFullYear()));
  const palette = $derived(consumerPalette(live.snapshot, stats?.lifetime));
  const named = $derived(stackedConsumers(year, palette));
  const months = $derived(consumerMonths(stats, now).map((month) => ({ ...month, segments: month.kwh === null ? undefined : monthSegments(month, named) })));
  const hasValue = (m) => m.kwh !== null && (m.state === "complete" || m.state === "running");
  const selected = $derived(months.find((m) => m.month === (picked ?? now.getMonth() + 1)) || months[0]);
  const detail = $derived.by(() => {
    const ranked = Object.entries(selected?.consumers || {})
      .map(([key, value]) => ({ key, name: value?.name || key, kwh: Number(value?.kwh) || 0, cost: Number(value?.cost) || 0 }))
      .sort((a, b) => b.kwh - a.kwh || a.name.localeCompare(b.name));
    const shown = ranked.slice(0, DETAIL_ROWS);
    const rest = ranked.slice(DETAIL_ROWS);
    const color = (key) => named.find((item) => item.key === key)?.token;
    return [
      ...shown.map((item) => ({ id: item.key, label: item.name, clip: true, color: color(item.key) ? `var(${color(item.key)})` : `var(${OTHER_TOKEN})`, value: `${f.energy(item.kwh)} · ${f.money(item.cost, currency)}` })),
      ...(rest.length
        ? [{ id: "\u0000rest", label: t("consumers.more", { n: rest.length }), color: `var(${OTHER_TOKEN})`, value: f.energy(rest.reduce((sum, item) => sum + item.kwh, 0)) }]
        : []),
    ];
  });
</script>

{#snippet actions()}
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card icon="chart-column" tone="c-out" title={t("energy.byMonth", { y: now.getFullYear() })} subtitle={t("consumers.byMonthSub")} span="span-8" {actions}>
  <div data-testid="consumer-months">
    <MonthChart {months} selected={selected?.month} onpick={(month) => (picked = month)} label={t("energy.byMonth", { y: now.getFullYear() })} />
  </div>
  {#if named.length}
    <ul class="legend" aria-label={t("consumers.byMonthSub")}>
      {#each named as item (item.key)}<li><span class="dot" style:--c={`var(${item.token})`}></span>{item.name}</li>{/each}
      <li><span class="dot" style:--c={`var(${OTHER_TOKEN})`}></span>{t("flow.otherConsumers")}</li>
    </ul>
  {/if}
  {#if selected}
    <div class="month-detail inner" aria-live="polite" data-testid="consumer-month-detail">
      <div class="month-head">
        <div>
          <div class="month-name">{f.monthYear(now.getFullYear(), selected.month)}</div>
          <div class="month-state">{t(`energy.state.${selected.state}`)}</div>
        </div>
        <div class="month-value num">{hasValue(selected) ? f.energy(selected.kwh) : "—"}</div>
      </div>
      {#if hasValue(selected)}
        <div class="detail-grid">
          <FactList rows={detail} />
          <ConsumerFacts view={selected} {expert} {currency} />
        </div>
      {/if}
    </div>
  {/if}
  {#if showTable}
    <div class="table-scroll">
      <table class="data" data-testid="consumer-months-table">
        <thead>
          <tr>
            <th>{t("energy.month")}</th>
            <th class="r">{t("consumers.energy")}</th>
            <th class="r">{t("consumers.cost")}</th>
            <th class="r">{t("consumers.saved")}</th>
            <th>{t("consumers.mostDrawn")}</th>
          </tr>
        </thead>
        <tbody>
          {#each months as month (month.month)}
            <tr>
              <td>{f.month(month.month)}</td>
              <td class="r num">{hasValue(month) ? f.energy(month.kwh) : "—"}</td>
              <td class="r num">{hasValue(month) ? f.money(month.totals.cost, currency) : "—"}</td>
              <td class="r num">{hasValue(month) ? f.money(month.totals.saved, currency) : "—"}</td>
              <td>{hasValue(month) && month.totals.top ? `${month.totals.top.name} · ${f.energy(month.totals.top.kwh)}` : t(`energy.state.${month.state}`)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  <p class="note"><Icon name="info" />{t("consumers.monthNote")}</p>
</Card>

<style>
  .legend { list-style: none; margin: var(--s2) 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12.5px; color: var(--m-muted); }
  .legend li { display: inline-flex; align-items: center; gap: 6px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c); flex: none; }
  .month-detail { margin-top: var(--s4); display: flex; flex-direction: column; gap: var(--s3); }
  .month-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--s3); }
  .month-name { font-weight: 620; font-size: 14px; }
  .month-state { font-size: 12px; color: var(--m-muted); }
  .month-value { font-size: 20px; font-weight: 650; letter-spacing: -0.02em; }
  .detail-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--s5); }
  .table-scroll { margin-top: var(--s4); overflow-x: auto; }
  .table-scroll td { white-space: nowrap; }
  @container content (max-width: 700px) { .detail-grid { grid-template-columns: minmax(0, 1fr); gap: var(--s3); } }
</style>
