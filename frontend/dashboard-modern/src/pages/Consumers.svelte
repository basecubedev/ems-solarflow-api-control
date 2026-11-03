<script>
  import EnergyHead from "../widgets/EnergyHead.svelte";
  import EnergyPeriods from "../widgets/EnergyPeriods.svelte";
  import ContextChips from "../widgets/ContextChips.svelte";
  import ConsumerFacts from "../widgets/ConsumerFacts.svelte";
  import ConsumerRanking from "../widgets/ConsumerRanking.svelte";
  import ConsumerMonths from "../widgets/ConsumerMonths.svelte";
  import YearsLifetime from "../widgets/YearsLifetime.svelte";
  import ConsumerEnergy from "../widgets/ConsumerEnergy.svelte";
  import ConsumerHistory from "../widgets/ConsumerHistory.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { energyDetail } from "../state/prefs.svelte.js";
  import { CONSUMER_PERIODS, consumerMonths, consumerPeriod, consumerRows, consumerYears } from "../lib/consumers.js";

  /**
   * The Energy page's cards for what the consumers drew -- the periods, this
   * year's months, the years and the lifetime -- around what they draw now
   * and over the last hours.
   */
  const f = $derived(fmt());
  const expert = $derived(energyDetail.detail === "expert");
  const stats = $derived(live.snapshot?.energy_stats || null);
  const currency = $derived(stats?.currency || "EUR");
  const known = $derived(consumerRows(live.snapshot, stats?.lifetime).length > 0);
  const present = $derived(Object.keys(live.snapshot?.consumers || {}).length > 0);
  const counted = $derived(Boolean(stats) && stats.enabled !== false);
  const periods = $derived(
    CONSUMER_PERIODS.map((period) => ({ ...period, sub: t(`consumers.periodSub.${period.kind}`), view: consumerPeriod(stats?.[period.key]) })),
  );
  const years = $derived(consumerYears(stats));
  const lifetime = $derived(consumerPeriod(stats?.lifetime));
  const context = $derived.by(() => {
    const listed = years.map((item) => item.year).sort();
    return [
      { key: "currency", label: t("energy.context.currency"), value: currency },
      { key: "count", label: t("consumers.count"), value: f.number(consumerRows(live.snapshot, stats?.lifetime).length, 0) },
      { key: "monthsWithData", label: t("energy.context.monthsWithData"), value: t("energy.context.monthsValue", { n: consumerMonths(stats).filter((month) => month.kwh).length }) },
      { key: "years", label: t("energy.context.years"), value: listed.length ? (listed.length === 1 ? String(listed[0]) : `${listed[0]}–${listed[listed.length - 1]}`) : "—" },
      { key: "peakDay", label: t("consumers.peakDay"), value: stats?.consumer_peak_day?.date ? f.date(stats.consumer_peak_day.date) : "—" },
      { key: "lifetime", label: t("energy.context.lifetime"), value: f.energy(lifetime.kwh ?? 0) },
    ];
  });
</script>

{#if !live.snapshot}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if !known}
  <div class="span-12" data-testid="consumers-empty"><Empty text={t("consumers.none")} icon="plug-zap" /></div>
{:else}
  <EnergyHead text={t("consumers.basedOn")} />
  {#if counted}
    <EnergyPeriods {stats} {periods} testid="consumer-periods">
      {#snippet facts(period)}<ConsumerFacts view={period.view} {expert} {currency} />{/snippet}
    </EnergyPeriods>
    <ContextChips items={context} label={t("energy.context.title")} />
  {/if}
  {#if present}<ConsumerRanking />{/if}
  {#if counted}
    <ConsumerMonths {stats} {expert} />
    <YearsLifetime {stats} {expert} {years} {lifetime} subtitle="" testid="consumer-years">
      {#snippet facts(entry)}<ConsumerFacts view={entry} {expert} {currency} />{/snippet}
    </YearsLifetime>
    <ConsumerEnergy {stats} />
  {/if}
  {#if present}<ConsumerHistory />{/if}
{/if}
