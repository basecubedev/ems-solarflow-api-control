<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { fmt, t } from "../i18n/i18n.svelte.js";
  import { lanesCoarse, laneTicks, laneTime, laneTotals } from "../lib/battery.js";
  import { sampleSpacing } from "../lib/history.js";

  /**
   * On/off states over the chart's time axis, one lane each: a bar where the
   * state was on, and how long that was. A lane the device never reported in
   * the range is named as such instead of drawn off.
   */
  let { time = [], lanes = [], label = "" } = $props();

  const f = $derived(fmt());
  const start = $derived(time.length ? time[0] : 0);
  const end = $derived(time.length ? time[time.length - 1] + (sampleSpacing(time) || 1) : start + 1);
  const rows = $derived(laneTotals(time, lanes));
  const coarse = $derived(lanesCoarse(time));
  const long = $derived(end - start > 36 * 3600);
  const ticks = $derived(laneTicks(start, end));

  function left(value) {
    return `${((value - start) / (end - start)) * 100}%`;
  }

  function width(segment) {
    return `${Math.max(0.4, ((segment.end - segment.start) / (end - start)) * 100)}%`;
  }

  function stamp(seconds) {
    return long ? f.dateTime(seconds * 1000) : f.time(seconds * 1000);
  }

  function tickText(seconds) {
    return long ? f.date(seconds * 1000, { short: true }) : f.time(seconds * 1000);
  }

  function onText(row) {
    if (!row.segments.length) return t("lanes.never");
    const time = laneTime(row.seconds, t, f, coarse);
    return time ? t("lanes.on", { time }) : t("lanes.wasOn");
  }
</script>

<div class="lanes" role="list" aria-label={label} data-testid="state-lanes">
  {#each rows as row (row.id)}
    <div class="lane" role="listitem" data-lane={row.id}>
      <span class="name">{row.label}</span>
      <div class="track" class:silent={!row.reported} aria-hidden="true">
        {#each row.segments as segment, index (index)}
          <span class="on" style:left={left(segment.start)} style:width={width(segment)} style:--c={`var(${row.token})`} title={`${stamp(segment.start)}–${stamp(segment.end)}`}></span>
        {/each}
      </div>
      <span class="total">{row.reported ? onText(row) : t("lanes.unreported")}</span>
    </div>
  {/each}
  {#if rows.length && ticks.length}
    <div class="lane scale" aria-hidden="true">
      <span></span>
      <div class="ticks">
        {#each ticks as tick, index (index)}<span style:left={left(tick)}>{tickText(tick)}</span>{/each}
      </div>
      <span></span>
    </div>
  {/if}
</div>

<style>
  .lanes { display: flex; flex-direction: column; gap: 8px; }
  .lane { display: grid; grid-template-columns: minmax(120px, 180px) 1fr minmax(90px, auto); gap: var(--s3); align-items: center; font-size: 12.5px; }
  .name { color: var(--m-text); }
  .total { color: var(--m-muted); text-align: right; white-space: nowrap; }
  .track { position: relative; height: 14px; border-radius: var(--m-radius-pill); background: var(--m-well); overflow: hidden; }
  .track.silent { background: repeating-linear-gradient(135deg, var(--m-well), var(--m-well) 4px, transparent 4px, transparent 8px); }
  .on { position: absolute; top: 0; bottom: 0; background: var(--c); opacity: 0.85; }
  .ticks { position: relative; height: 14px; color: var(--m-muted); font-size: 11.5px; }
  .ticks span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
  .ticks span:first-child { transform: none; }
  .ticks span:last-child { transform: translateX(-100%); }
  @media (max-width: 600px) {
    .lane { grid-template-columns: 1fr auto; }
    .track, .ticks { grid-column: 1 / -1; grid-row: 2; }
    .scale > span { display: none; }
  }
</style>
