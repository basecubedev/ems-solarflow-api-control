<script module>
  export const manifest = { id: "month-bars", data: ["energy.monthly_current_year", "energy.lifetime.since_date"], refresh: "sse", minWidth: 320, states: ["running", "future", "unmeasured", "missing"] };
</script>

<script>
  import Card from "../ui/Card.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { monthlyView } from "../lib/energy.js";

  let { stats, now = new Date() } = $props();

  let showTable = $state(false);
  let width = $state(720);
  const HEIGHT = 260;
  const PAD_B = 26;
  const PAD_T = 24;

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const months = $derived(monthlyView(stats, now));
  const max = $derived(Math.max(1, ...months.map((m) => m.kwh || 0)));
  const niceMax = $derived(Math.ceil(max / 10 ** Math.floor(Math.log10(max))) * 10 ** Math.floor(Math.log10(max)));
  const step = $derived(width / 12);
  const y = (value) => HEIGHT - PAD_B - (value / niceMax) * (HEIGHT - PAD_B - PAD_T);
  const hasValue = (m) => m.kwh !== null && (m.state === "complete" || m.state === "running");
</script>

{#snippet actions()}
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card title={t("energy.byMonth", { y: now.getFullYear() })} subtitle={t("energy.byMonthSub")} span="span-8" {actions}>
  <div class="months-wrap" bind:clientWidth={width}>
    <svg class="months" viewBox={`0 0 ${width} ${HEIGHT}`} role="img" aria-label={t("energy.byMonth", { y: now.getFullYear() })}>
      <defs>
        <linearGradient id="month-fill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" class="stop-top"></stop>
          <stop offset="1" class="stop-bottom"></stop>
        </linearGradient>
        <pattern id="month-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" class="hatch-bg"></rect>
          <rect width="2.5" height="6" class="hatch-line"></rect>
        </pattern>
      </defs>
      {#each [0, 0.25, 0.5, 0.75, 1] as share (share)}
        <line class="gridline" x1="0" x2={width} y1={y(niceMax * share)} y2={y(niceMax * share)}></line>
      {/each}
      {#each months as month, index (month.month)}
        {@const x = index * step + step * 0.18}
        {@const w = step * 0.64}
        {#if hasValue(month)}
          <rect class={month.state === "running" ? "partial" : "bar"} x={x} y={y(month.kwh)} width={w} height={Math.max(0, y(0) - y(month.kwh))} rx="5">
            <title>{`${f.month(month.month)}: ${f.energy(month.kwh)} · ${t(`energy.state.${month.state}`)}`}</title>
          </rect>
          {#if w > 18}<text class="val num" x={x + w / 2} y={y(month.kwh) - 6} text-anchor="middle">{f.number(month.kwh, 0)}</text>{/if}
        {:else}
          <rect class="nodata" x={x} y={y(niceMax * 0.12)} width={w} height={y(0) - y(niceMax * 0.12)} rx="5">
            <title>{`${f.month(month.month)}: ${t(`energy.state.${month.state}`)}`}</title>
          </rect>
        {/if}
        <text class="axis" x={x + w / 2} y={HEIGHT - 6} text-anchor="middle">{f.month(month.month)}</text>
      {/each}
    </svg>
  </div>
  {#if showTable}
    <div class="table-scroll">
      <table class="data">
        <thead><tr><th>{t("energy.month")}</th><th class="r">{t("energy.energy")}</th><th class="r">{t("energy.savings")}</th><th>{t("energy.data")}</th></tr></thead>
        <tbody>
          {#each months as month (month.month)}
            <tr>
              <td>{f.month(month.month)}</td>
              <td class="r num">{hasValue(month) ? f.energy(month.kwh) : "—"}</td>
              <td class="r num">{hasValue(month) ? f.money(month.savings, currency) : "—"}</td>
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
  .months-wrap { width: 100%; }
  .months { width: 100%; height: 260px; display: block; }
  .months text { fill: var(--m-muted); font-size: 12px; font-family: var(--m-font); }
  .months .val { fill: var(--m-text); font-weight: 600; font-size: 11.5px; }
  .stop-top { stop-color: var(--output); stop-opacity: 1; }
  .stop-bottom { stop-color: var(--output); stop-opacity: 0.3; }
  .hatch-bg { fill: var(--output); fill-opacity: 0.18; }
  .hatch-line { fill: var(--output); fill-opacity: 0.55; }
  .bar { fill: url(#month-fill); }
  .partial { fill: url(#month-hatch); stroke: color-mix(in srgb, var(--output) 70%, transparent); stroke-width: 1; }
  .nodata { fill: none; stroke: var(--m-line-strong); stroke-dasharray: 4 4; }
  .gridline { stroke: var(--m-line); }
  .table-scroll { margin-top: var(--s4); }
</style>
