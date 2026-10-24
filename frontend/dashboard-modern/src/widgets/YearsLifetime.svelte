<script module>
  export const manifest = { id: "years-lifetime", data: ["energy.yearly", "energy.lifetime"], refresh: "sse", minWidth: 260, states: ["single-year", "since-date"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import EnergyFacts from "./EnergyFacts.svelte";
  import { yearlyView, periodView, channelsSince, channelMeta } from "../lib/energy.js";

  let { stats, expert = false, now = new Date() } = $props();

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const years = $derived(yearlyView(stats, now).slice().reverse());
  const lifetime = $derived(periodView(stats?.lifetime));
  const since = $derived(stats?.lifetime?.since_date || null);
  const channelsFrom = $derived(channelsSince(stats));
  const channels = $derived(channelMeta(stats));
</script>

<Card title={t("energy.years")} subtitle={since ? t("energy.since", { d: f.date(since) }) : ""} span="span-4">
  <div class="years">
    {#each years as year (year.year)}
      <div class="inner year">
        <div class="year-label"><span>{year.year}</span>{#if year.current}<span class="badge">{t("energy.inProgress")}</span>{:else}<span>{t("energy.calendarYear")}</span>{/if}</div>
        <div class="year-value num">{f.energy(year.kwh)}{year.partial ? " ◦" : ""}</div>
        <EnergyFacts values={year.raw} {channels} since={channelsFrom} {expert} {currency} />
      </div>
    {/each}
    <div class="inner year lifetime">
      <div class="year-label"><span>{t("energy.lifetime")}</span></div>
      <div class="year-value num">{f.energy(lifetime.kwh)}{lifetime.partial ? " ◦" : ""}</div>
      <EnergyFacts values={stats?.lifetime} {channels} since={channelsFrom} {expert} {currency} />
    </div>
  </div>
</Card>

<style>
  .years { display: flex; flex-direction: column; gap: var(--s3); }
  .year-label { font-size: 12.5px; color: var(--m-muted); display: flex; justify-content: space-between; gap: 8px; }
  .year-value { font-size: 20px; font-weight: 650; letter-spacing: -0.02em; margin: 2px 0; }
  .lifetime { background: color-mix(in srgb, var(--output) 6%, var(--m-surface-2)); }
</style>
