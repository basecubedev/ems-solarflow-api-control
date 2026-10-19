<script module>
  export const manifest = { id: "energy-periods", data: ["energy"], refresh: "sse", minWidth: 280, states: ["partial", "unmeasured", "disabled", "waiting"] };
</script>

<script>
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { PERIODS, periodView, channelMeta, channelKwh } from "../lib/energy.js";

  let { stats, expert = false } = $props();

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const channels = $derived(channelMeta(stats));
  const periods = $derived(PERIODS.map((period) => ({ ...period, view: periodView(stats?.[period.key]) })));
</script>

<div class="periods span-12" data-testid="energy-periods">
  {#each periods as period (period.key)}
    <section class="card period" aria-label={t(period.label)}>
      <div class="period-label">
        <span>{t(period.label)}</span>
        {#if period.kind === "today"}<span class="badge">{t("energy.inProgress")}</span>{/if}
        {#if period.kind === "best" && period.view.date}<span class="date num">{f.date(period.view.date, { short: true })}</span>{/if}
      </div>
      <div class="period-value num">{f.energy(period.view.kwh)}{#if period.view.partial}<span class="mark" title={t("kpi.partial")}> ◦</span>{/if}</div>
      <div class="facts-line num">
        <span>{t("energy.savings")} <b>{f.money(period.view.savings, currency)}</b></span>
        {#if period.view.selfSufficiency}
          <span>{t("energy.selfSufficiency")} <b>{f.percent(period.view.selfSufficiency.ratio, { fraction: true })}{period.view.selfSufficiency.partial ? " ◦" : ""}</b></span>
        {:else if expert}
          <span>{t("energy.selfSufficiency")} <b>—</b></span>
        {/if}
        {#if period.view.acChargeKwh !== null && period.view.acChargeKwh > 0}
          <span>{t("energy.acCharge")} <b>{f.energy(period.view.acChargeKwh)}</b></span>
        {/if}
      </div>
      {#if expert}
        <dl class="channels">
          {#each channels as channel (channel.id)}
            {@const value = channelKwh(period.view.raw, channel.id)}
            <div><dt>{t(`channel.${channel.id}`)}</dt><dd class="num">{value.kwh === null ? "—" : f.energy(value.kwh)}{value.coverage === "partial" ? " ◦" : ""}</dd></div>
          {/each}
          {#if period.view.peakW !== null && period.view.peakW > 0}
            <div><dt>{t("energy.peak")}</dt><dd class="num">{f.power(period.view.peakW)}</dd></div>
          {/if}
        </dl>
      {/if}
    </section>
  {/each}
</div>

<style>
  .periods { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: var(--s4); }
  .period { padding: var(--s4); display: flex; flex-direction: column; gap: 4px; }
  .period-label { font-size: 12.5px; color: var(--m-muted); display: flex; align-items: center; gap: 6px; justify-content: space-between; flex-wrap: wrap; }
  .date { font-size: 12px; }
  .period-value { font-size: 22px; font-weight: 650; letter-spacing: -0.02em; white-space: nowrap; }
  .mark { color: var(--m-muted); font-size: 0.7em; }
  .facts-line { display: flex; gap: 4px var(--s3); font-size: 12px; color: var(--m-muted); flex-wrap: wrap; }
  .facts-line b { color: var(--m-text); font-weight: 600; }
  .channels { margin: var(--s2) 0 0; padding-top: var(--s2); border-top: 1px solid var(--m-line); display: grid; gap: 4px; font-size: 12px; }
  .channels div { display: flex; justify-content: space-between; gap: 8px; }
  .channels dt { color: var(--m-muted); }
  .channels dd { margin: 0; font-weight: 600; }
  @media (max-width: 1280px) { .periods { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (max-width: 760px) { .periods { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3); } }
</style>
