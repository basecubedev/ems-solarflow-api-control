<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "years-lifetime", data: ["energy.yearly", "energy.lifetime"], refresh: "sse", minWidth: 260, states: ["single-year", "since-date"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import EnergyFacts from "./EnergyFacts.svelte";
  import { yearlyView, periodView, channelsSince, channelMeta } from "../lib/energy.js";

  /**
   * The calendar years, the newest first, and the lifetime. Another page passes
   * its own ``years`` ({ year, current, kwh, partial }) and ``lifetime``
   * ({ kwh, partial }) and the ``facts`` each lists, in the same card.
   */
  let { stats, expert = false, now = new Date(), years = null, lifetime = null, facts = null, subtitle = undefined, testid = undefined } = $props();

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const shownYears = $derived(years || yearlyView(stats, now).slice().reverse());
  const total = $derived(lifetime || periodView(stats?.lifetime));
  const since = $derived(stats?.lifetime?.since_date || null);
  const channelsFrom = $derived(channelsSince(stats));
  const channels = $derived(channelMeta(stats));
</script>

<Card icon="calendar" tone="c-out" title={t("energy.years")} subtitle={subtitle ?? (since ? t("energy.since", { d: f.date(since) }) : "")} span="span-4">
  <div class="years" data-testid={testid}>
    {#each shownYears as year (year.year)}
      <div class="inner year" data-year={year.year}>
        <div class="year-label"><span>{year.year}</span>{#if year.current}<span class="badge">{t("energy.inProgress")}</span>{:else}<span>{t("energy.calendarYear")}</span>{/if}</div>
        <div class="year-value num">{f.energy(year.kwh)}{year.partial ? " ◦" : ""}</div>
        {#if facts}{@render facts(year)}{:else}<EnergyFacts values={year.raw} {channels} since={channelsFrom} {expert} {currency} />{/if}
      </div>
    {/each}
    <div class="inner year lifetime" data-year="lifetime">
      <div class="year-label"><span>{t("energy.lifetime")}</span></div>
      <div class="year-value num">{f.energy(total.kwh)}{total.partial ? " ◦" : ""}</div>
      {#if facts}{@render facts(total)}{:else}<EnergyFacts values={total.raw} {channels} since={channelsFrom} {expert} {currency} />{/if}
    </div>
  </div>
</Card>

<style>
  .years { display: flex; flex-direction: column; gap: var(--s3); }
  .year-label { font-size: 12.5px; color: var(--m-muted); display: flex; justify-content: space-between; gap: 8px; }
  .year-value { font-size: 20px; font-weight: 650; letter-spacing: -0.02em; margin: 2px 0; }
  .lifetime { background: color-mix(in srgb, var(--output) 6%, var(--m-surface-2)); }
</style>
