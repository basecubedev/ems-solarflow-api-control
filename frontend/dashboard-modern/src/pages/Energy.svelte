<script>
  import EnergyPeriods from "../widgets/EnergyPeriods.svelte";
  import MonthBars from "../widgets/MonthBars.svelte";
  import YearsLifetime from "../widgets/YearsLifetime.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import Empty from "../ui/Empty.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { energyState } from "../lib/energy.js";

  const KEY = "ems-modern.energyDetail";
  let expert = $state(readDetail());
  const stats = $derived(live.snapshot?.energy_stats || null);
  const state = $derived(live.snapshot ? energyState(stats) : "loading");
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
  <MonthBars {stats} />
  <YearsLifetime {stats} {expert} />
{/if}

<style>
  .energy-head { display: flex; justify-content: space-between; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .energy-head p { margin: 0; font-size: 13px; }
</style>
