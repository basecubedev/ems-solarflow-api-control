<script>
  import EnergyPeriods from "../widgets/EnergyPeriods.svelte";
  import MonthBars from "../widgets/MonthBars.svelte";
  import YearsLifetime from "../widgets/YearsLifetime.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { energyState, energyKwh, channelsSince } from "../lib/energy.js";

  const KEY = "ems-modern.energyDetail";
  let expert = $state(readDetail());
  const stats = $derived(live.snapshot?.energy_stats || null);
  const state = $derived(live.snapshot ? energyState(stats) : "loading");
  const f = $derived(fmt());
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
    return items;
  });
  const anyPartial = $derived(
    ["today", "yesterday", "last_7_days", "last_4_weeks", "last_12_months", "best_day", "lifetime"].some((key) =>
      Object.values(stats?.[key]?.coverage || {}).includes("partial"),
    ),
  );

  function readDetail() {
    try {
      return globalThis.localStorage?.getItem(KEY) === "expert";
    } catch {
      return false;
    }
  }

  function setDetail(value) {
    expert = value === "expert";
    try {
      globalThis.localStorage?.setItem(KEY, value);
    } catch {
      expert = value === "expert";
    }
  }
</script>

<div class="energy-head span-12">
  <p class="muted">{t("energy.basedOn")} {#if anyPartial}{t("energy.partialLegend")}{/if}</p>
  <Segmented
    options={[{ value: "basic", label: t("energy.detail.basic") }, { value: "expert", label: t("energy.detail.expert") }]}
    value={expert ? "expert" : "basic"}
    label={t("energy.title")}
    onchange={setDetail}
  />
</div>

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
  <dl class="context span-12" aria-label={t("energy.context.title")}>
    {#each context as item (item.key)}
      <div><dt>{t(`energy.context.${item.key}`)}</dt><dd class="num">{item.value}</dd></div>
    {/each}
  </dl>
  <MonthBars {stats} {expert} />
  <YearsLifetime {stats} {expert} />
{/if}

<style>
  .energy-head { display: flex; justify-content: space-between; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .energy-head p { margin: 0; font-size: 13px; }
  .context { margin: 0; display: flex; flex-wrap: wrap; gap: var(--s2); }
  .context div { display: flex; gap: 8px; align-items: baseline; padding: 6px 12px; border-radius: var(--m-radius-pill); border: 1px solid var(--m-line); font-size: 12.5px; }
  .context dt { color: var(--m-muted); }
  .context dd { margin: 0; font-weight: 600; }
</style>
