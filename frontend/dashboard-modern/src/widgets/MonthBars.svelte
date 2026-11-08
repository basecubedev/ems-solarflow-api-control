<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "month-bars", data: ["energy.monthly_current_year", "energy.lifetime.since_date"], refresh: "sse", minWidth: 320, states: ["running", "future", "unmeasured", "missing"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import EnergyFacts from "./EnergyFacts.svelte";
  import MonthChart from "./MonthChart.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { monthlyView, channelMeta, channelsSince, periodFigures, figureValue, figureListed, figureText } from "../lib/energy.js";

  let { stats, expert = false, now = new Date() } = $props();

  let showTable = $state(false);
  let picked = $state(null);

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const months = $derived(monthlyView(stats, now));
  const hasValue = (m) => m.kwh !== null && (m.state === "complete" || m.state === "running");
  const channels = $derived(channelMeta(stats));
  const since = $derived(channelsSince(stats));
  const selected = $derived(months.find((m) => m.month === (picked ?? now.getMonth() + 1)) || months[0]);
  const columns = $derived(
    periodFigures(channels, expert).filter((figure) => months.some((m) => hasValue(m) && figureListed(figure, figureValue(m.raw, figure), m.raw, expert))),
  );
</script>

{#snippet actions()}
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card icon="chart-column" tone="c-out" title={t("energy.byMonth", { y: now.getFullYear() })} subtitle={t("energy.byMonthSub")} span="span-8" {actions}>
  <MonthChart {months} selected={selected?.month} onpick={(month) => (picked = month)} label={t("energy.byMonth", { y: now.getFullYear() })} />
  {#if selected}
    <div class="month-detail inner" aria-live="polite" data-testid="month-detail">
      <div class="month-head">
        <div>
          <div class="month-name">{f.monthYear(now.getFullYear(), selected.month)}</div>
          <div class="month-state">{t(`energy.state.${selected.state}`)}</div>
        </div>
        <div class="month-value num">{hasValue(selected) ? f.energy(selected.kwh) : "—"}{selected.partial ? " ◦" : ""}</div>
      </div>
      {#if hasValue(selected)}
        <EnergyFacts values={selected.raw} {channels} {since} {expert} {currency} />
      {/if}
    </div>
  {/if}
  {#if showTable}
    <div class="table-scroll">
      <table class="data">
        {#if expert}<caption>{t("energy.context.channelsSince")}: {since ? f.date(since, { numeric: true }) : t("energy.notMeasured")}</caption>{/if}
        <thead>
          <tr>
            <th>{t("energy.month")}</th>
            <th class="r">{t("energy.energy")}</th>
            {#each columns as figure (figure.id)}<th class="r">{t(figure.label)}</th>{/each}
            <th>{t("energy.data")}</th>
          </tr>
        </thead>
        <tbody>
          {#each months as month (month.month)}
            <tr>
              <td>{f.month(month.month)}</td>
              <td class="r num">{hasValue(month) ? `${f.energy(month.kwh)}${month.partial ? " ◦" : ""}` : "—"}</td>
              {#each columns as figure (figure.id)}
                <td class="r num">{hasValue(month) ? figureText(f, figure, figureValue(month.raw, figure), currency) : "—"}</td>
              {/each}
              <td>{t(`energy.state.${month.state}`)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  <p class="note"><Icon name="info" />{t("energy.monthNote")}</p>
</Card>

<style>
  .month-detail { margin-top: var(--s4); display: flex; flex-direction: column; gap: var(--s3); }
  .month-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--s3); }
  .month-name { font-weight: 620; font-size: 14px; }
  .month-state { font-size: 12px; color: var(--m-muted); }
  .month-value { font-size: 20px; font-weight: 650; letter-spacing: -0.02em; }
  .table-scroll { margin-top: var(--s4); }
  .table-scroll td { white-space: nowrap; }
  caption { caption-side: bottom; text-align: left; padding-top: var(--s2); color: var(--m-muted); font-size: 12px; }
</style>
