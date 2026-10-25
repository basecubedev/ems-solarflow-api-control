<script>
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { periodFigures, figureValue, figureListed, figureText, figureTone, channelsAllUnmeasured } from "../lib/energy.js";

  /**
   * The facts Classic's energy cards carry for any period: savings, the
   * self-sufficiency ratio and AC charging, and in Expert every channel, the
   * peak output and since when the channels have been measured.
   */
  let { values, channels = [], since = null, expert = false, currency = "EUR", details = [] } = $props();

  const f = $derived(fmt());
  const unmeasured = $derived(channelsAllUnmeasured(values));
  const figures = $derived(
    periodFigures(channels, expert)
      .map((figure) => ({ figure, cell: figureValue(values, figure) }))
      .filter(({ figure, cell }) => figureListed(figure, cell, values, expert)),
  );
</script>

<dl class="energy-facts">
  {#each figures as { figure, cell } (figure.id)}
    <div class:channel={Boolean(figure.channel)}>
      <dt><span class="fact-mark {figureTone(figure)}" aria-hidden="true"></span>{t(figure.label)}</dt>
      <dd class="num" class:na={cell.value === null}>{figureText(f, figure, cell, currency)}</dd>
    </div>
  {/each}
  {#if expert}
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
  .energy-facts dt { color: var(--m-muted); display: inline-flex; align-items: center; gap: 6px; }
  .fact-mark { width: 6px; height: 6px; border-radius: 50%; flex: none; background: var(--c); }
  .energy-facts dd { margin: 0; font-weight: 600; text-align: right; white-space: nowrap; }
  .energy-facts dd.na { color: var(--m-muted-2); font-weight: 500; }
  .energy-facts .since { padding-top: 4px; border-top: 1px solid var(--m-line); margin-top: 2px; }
</style>
