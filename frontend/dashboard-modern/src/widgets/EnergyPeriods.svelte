<script module>
  export const manifest = { id: "energy-periods", data: ["energy"], refresh: "sse", minWidth: 280, states: ["partial", "unmeasured", "disabled", "waiting"] };
</script>

<script>
  import EnergyFacts from "./EnergyFacts.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { PERIODS, periodView, channelMeta, channelsSince } from "../lib/energy.js";

  let { stats, expert = false } = $props();

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const channels = $derived(channelMeta(stats));
  const since = $derived(channelsSince(stats));
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
      <div class="period-value num">{f.energy(period.view.kwh)}{#if period.view.partial}<span class="mark" title={t("kpi.partial")}>{" "}◦</span>{/if}</div>
      <div class="period-sub">{t(`energy.periodSub.${period.kind}`)}</div>
      <EnergyFacts values={period.view.raw} {channels} {since} {expert} {currency} />
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
  .period-sub { font-size: 11.5px; color: var(--m-muted-2); margin-bottom: var(--s2); }
  @media (max-width: 1280px) { .periods { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (max-width: 760px) { .periods { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3); } }
</style>
