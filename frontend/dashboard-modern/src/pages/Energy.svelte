<script>
  import EnergyPeriods from "../widgets/EnergyPeriods.svelte";
  import MonthBars from "../widgets/MonthBars.svelte";
  import YearsLifetime from "../widgets/YearsLifetime.svelte";
  import ConsumerEnergy from "../widgets/ConsumerEnergy.svelte";
  import EnergyHead from "../widgets/EnergyHead.svelte";
  import ContextChips from "../widgets/ContextChips.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { energyDetail } from "../state/prefs.svelte.js";
  import { energyState, energyKwh, channelsSince } from "../lib/energy.js";
  import { consumerRows } from "../lib/consumers.js";

  const expert = $derived(energyDetail.detail === "expert");
  const stats = $derived(live.snapshot?.energy_stats || null);
  const state = $derived(live.snapshot ? energyState(stats) : "loading");
  const f = $derived(fmt());
  const consumers = $derived(consumerRows(live.snapshot, stats?.lifetime).length > 0);
  const context = $derived.by(() => {
    const months = Array.isArray(stats?.monthly_current_year) ? stats.monthly_current_year : [];
    const years = (Array.isArray(stats?.yearly) ? stats.yearly : []).map((item) => Number(item.year)).filter(Number.isFinite).sort();
    const items = [
      { key: "currency", value: stats?.currency || "EUR" },
      { key: "monthsWithData", value: t("energy.context.monthsValue", { n: months.filter((month) => (energyKwh(month) || 0) > 0).length }) },
      { key: "years", value: years.length ? (years.length === 1 ? String(years[0]) : `${years[0]}–${years[years.length - 1]}`) : "—" },
      { key: "bestDay", value: stats?.best_day?.date ? f.date(stats.best_day.date) : "—" },
      { key: "lifetime", value: f.energy(energyKwh(stats?.lifetime)) },
    ];
    const since = channelsSince(stats);
    if (expert) items.push({ key: "channelsSince", value: since ? f.date(since) : t("energy.notMeasured") });
    return items.map((item) => ({ ...item, label: t(`energy.context.${item.key}`) }));
  });
  const anyPartial = $derived(
    ["today", "yesterday", "last_7_days", "last_4_weeks", "last_12_months", "best_day", "lifetime"].some((key) =>
      Object.values(stats?.[key]?.coverage || {}).includes("partial"),
    ),
  );
</script>

<EnergyHead text={`${t("energy.basedOn")}${anyPartial ? ` ${t("energy.partialLegend")}` : ""}`} />

{#if state === "loading"}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if state === "unavailable"}
  <div class="span-12"><Empty text={t("energy.unavailable")} /></div>
{:else if state === "disabled"}
  <div class="span-12"><Empty text={t("energy.disabled")} /></div>
{:else if state === "waiting"}
  <div class="span-12"><Empty text={t("energy.waiting")} icon="loader" /></div>
{:else}
  <EnergyPeriods {stats} {expert} />
  <ContextChips items={context} label={t("energy.context.title")} />
  {#if consumers}<ConsumerEnergy {stats} />{/if}
  <MonthBars {stats} {expert} />
  <YearsLifetime {stats} {expert} />
{/if}
