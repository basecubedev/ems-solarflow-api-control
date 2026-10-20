<script>
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { periodView, channelKwh, channelsAllUnmeasured } from "../lib/energy.js";

  /**
   * The facts Classic's energy cards carry for any period: savings, the
   * self-sufficiency ratio and AC charging, and in Expert every channel, the
   * peak output and since when the channels have been measured.
   */
  let { values, channels = [], since = null, expert = false, currency = "EUR", details = [] } = $props();

  const f = $derived(fmt());
  const view = $derived(periodView(values));
  const unmeasured = $derived(channelsAllUnmeasured(values));
</script>

<dl class="energy-facts">
  <div><dt>{t("energy.savings")}</dt><dd class="num">{f.money(view.savings, currency)}</dd></div>
  {#if view.selfSufficiency}
    <div>
      <dt>{t("energy.selfSufficiency")}</dt>
      <dd class="num">{f.percent(view.selfSufficiency.ratio, { fraction: true })}{view.selfSufficiency.partial ? " ◦" : ""}</dd>
    </div>
  {:else if expert}
    <div><dt>{t("energy.selfSufficiency")}</dt><dd class="num na">—</dd></div>
  {/if}
  {#if view.acChargeKwh !== null && view.acChargeKwh > 0}
    <div><dt>{t("energy.acCharge")}</dt><dd class="num">{f.energy(view.acChargeKwh)}</dd></div>
  {/if}
  {#if expert}
    {#if !unmeasured}
      {#each channels as channel (channel.id)}
        {@const value = channelKwh(values, channel.id)}
        <div class="channel">
          <dt>{t(`channel.${channel.id}`)}</dt>
          <dd class="num" class:na={value.kwh === null}>{value.kwh === null ? "—" : f.energy(value.kwh)}{value.coverage === "partial" ? " ◦" : ""}</dd>
        </div>
      {/each}
    {/if}
    {#if view.peakW !== null && view.peakW > 0}
      <div><dt>{t("energy.peak")}</dt><dd class="num">{f.power(view.peakW)}</dd></div>
    {/if}
    <div class="since">
      <dt>{t("energy.measuredSince")}</dt>
      <dd class="num" class:na={!since || unmeasured}>{since && !unmeasured ? f.date(since, { numeric: true }) : t("energy.notMeasured")}</dd>
    </div>
  {/if}
  {#each details as detail (detail.label)}
    <div><dt>{detail.label}</dt><dd class="num">{detail.value}</dd></div>
  {/each}
</dl>

<style>
  .energy-facts { margin: 0; display: grid; gap: 4px; font-size: 12.5px; }
  .energy-facts div { display: flex; justify-content: space-between; gap: var(--s2); min-width: 0; }
  .energy-facts dt { color: var(--m-muted); }
  .energy-facts dd { margin: 0; font-weight: 600; text-align: right; white-space: nowrap; }
  .energy-facts dd.na { color: var(--m-muted-2); font-weight: 500; }
  .energy-facts .since { padding-top: 4px; border-top: 1px solid var(--m-line); margin-top: 2px; }
</style>
