<script module>
  export const manifest = { id: "month-bars", data: ["energy.monthly_current_year", "energy.lifetime.since_date"], refresh: "sse", minWidth: 320, states: ["running", "future", "unmeasured", "missing"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import EnergyFacts from "./EnergyFacts.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { monthlyView, channelMeta, channelsSince } from "../lib/energy.js";

  let { stats, expert = false, now = new Date() } = $props();

  let showTable = $state(false);
  let picked = $state(null);
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
  const channels = $derived(channelMeta(stats));
  const since = $derived(channelsSince(stats));
  const selected = $derived(months.find((m) => m.month === (picked ?? now.getMonth() + 1)) || months[0]);

  function pick(month) {
    picked = month;
  }

  function keyPick(event, month) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick(month);
    }
  }
</script>

{#snippet actions()}
  <button class="btn secondary small" type="button" aria-pressed={showTable ? "true" : "false"} onclick={() => (showTable = !showTable)}>
    <Icon name="table" size="sm" />{t("chart.table")}
  </button>
{/snippet}

<Card title={t("energy.byMonth", { y: now.getFullYear() })} subtitle={t("energy.byMonthSub")} span="span-8" {actions}>
  <div class="months-wrap" bind:clientWidth={width}>
    <svg class="months" viewBox={`0 0 ${width} ${HEIGHT}`} role="group" aria-label={t("energy.byMonth", { y: now.getFullYear() })}>
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
        <g
          class="month-col"
          class:selected={selected?.month === month.month}
          role="button"
          tabindex="0"
          aria-pressed={selected?.month === month.month ? "true" : "false"}
          aria-label={`${f.month(month.month)}: ${hasValue(month) ? f.energy(month.kwh) : t(`energy.state.${month.state}`)}`}
          onclick={() => pick(month.month)}
          onkeydown={(event) => keyPick(event, month.month)}
        >
          <rect class="month-hit" x={index * step + 2} y="0" width={step - 4} height={HEIGHT - 2} rx="8"></rect>
          {#if hasValue(month)}
            <rect class={month.state === "running" ? "month-running" : "month-bar"} x={x} y={y(month.kwh)} width={w} height={Math.max(0, y(0) - y(month.kwh))} rx="5"></rect>
            {#if w > 18}<text class="val num" x={x + w / 2} y={y(month.kwh) - 6} text-anchor="middle">{f.number(month.kwh, 0)}</text>{/if}
          {:else}
            <rect class="month-empty" x={x} y={y(niceMax * 0.12)} width={w} height={y(0) - y(niceMax * 0.12)} rx="5"></rect>
          {/if}
          <text class="axis" x={x + w / 2} y={HEIGHT - 6} text-anchor="middle">{step < 40 ? f.month(month.month).charAt(0) : f.month(month.month)}</text>
        </g>
      {/each}
    </svg>
  </div>
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
        <thead><tr><th>{t("energy.month")}</th><th class="r">{t("energy.energy")}</th><th class="r">{t("energy.savings")}</th><th class="r">{t("energy.selfSufficiency")}</th><th>{t("energy.data")}</th></tr></thead>
        <tbody>
          {#each months as month (month.month)}
            <tr>
              <td>{f.month(month.month)}</td>
              <td class="r num">{hasValue(month) ? f.energy(month.kwh) : "—"}</td>
              <td class="r num">{hasValue(month) ? f.money(month.savings, currency) : "—"}</td>
              <td class="r num">{hasValue(month) && month.selfSufficiency ? f.percent(month.selfSufficiency.ratio, { fraction: true }) : "—"}</td>
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
  .month-bar { fill: url(#month-fill); }
  .month-running { fill: url(#month-hatch); stroke: color-mix(in srgb, var(--output) 70%, transparent); stroke-width: 1; }
  .month-empty { fill: none; stroke: var(--m-line-strong); stroke-dasharray: 4 4; }
  .month-col { cursor: pointer; outline: none; }
  .month-hit { fill: transparent; }
  .month-col:hover .month-hit { fill: var(--m-glow); }
  .month-col.selected .month-hit { fill: color-mix(in srgb, var(--output) 9%, transparent); stroke: color-mix(in srgb, var(--output) 35%, transparent); }
  .month-col:focus-visible .month-hit { stroke: var(--m-accent); stroke-width: 2; }
  .month-col.selected .axis { fill: var(--m-text); font-weight: 600; }
  .month-detail { margin-top: var(--s4); display: flex; flex-direction: column; gap: var(--s3); }
  .month-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--s3); }
  .month-name { font-weight: 620; font-size: 14px; }
  .month-state { font-size: 12px; color: var(--m-muted); }
  .month-value { font-size: 20px; font-weight: 650; letter-spacing: -0.02em; }
  .gridline { stroke: var(--m-line); }
  .table-scroll { margin-top: var(--s4); }
</style>
