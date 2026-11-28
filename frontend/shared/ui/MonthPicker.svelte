<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t, tn } from "../i18n/i18n.svelte.js";
  import { prefs } from "../state/prefs.svelte.js";
  import { localeFor } from "../lib/format.js";
  import { monthsOf, seasonText, toggledMonth } from "../lib/controls.js";

  /**
   * Twelve month chips to press on and off, and a line that reads the choice
   * as a season and a count, such as "Oct – Mar" and "6 months". The last chosen month cannot
   * be pressed off. onchange hands over the months in season order.
   * labelledby is the id of the visible label that names the group.
   */
  let { value = [], disabled = false, labelledby = undefined, onchange } = $props();

  const MONTHS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  const chosen = $derived(monthsOf(value));
  const names = $derived.by(() => {
    const locale = localeFor(prefs.lang);
    const short = new Intl.DateTimeFormat(locale, { month: "short" });
    const long = new Intl.DateTimeFormat(locale, { month: "long" });
    return Object.fromEntries(MONTHS.map((month) => [month, { short: short.format(new Date(2026, month - 1, 15)), long: long.format(new Date(2026, month - 1, 15)) }]));
  });
  const season = $derived(seasonText(chosen, localeFor(prefs.lang), t("months.allYear")));
</script>

<div class="month-picker">
  <div class="month-chips" role="group" aria-labelledby={labelledby}>
    {#each MONTHS as month (month)}
      {@const on = chosen.includes(month)}
      {@const last = on && chosen.length === 1}
      <button
        type="button"
        aria-pressed={on ? "true" : "false"}
        aria-label={names[month].long}
        aria-disabled={last || undefined}
        title={last ? t("months.keepOne") : undefined}
        {disabled}
        onclick={() => !last && onchange?.(toggledMonth(chosen, month))}>{names[month].short}</button
      >
    {/each}
  </div>
  {#if chosen.length}
    <p class="month-season" aria-live="polite"><strong>{season}</strong>{#if chosen.length < 12}<span>{tn("months.count", chosen.length)}</span>{/if}</p>
  {/if}
</div>
