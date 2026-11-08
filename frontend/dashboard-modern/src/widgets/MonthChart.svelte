<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t, fmt } from "../i18n/i18n.svelte.js";

  /**
   * The twelve months of a year as bars, the month running hatched, a month
   * without data dashed; a click or Enter picks one. A month with
   * ``segments`` ({ id, kwh, token }) stacks them in their tokens' colours,
   * bottom up, with the total above. The Energy and the Consumers pages draw
   * their months through this.
   */
  let { months = [], selected = null, onpick, label = "" } = $props();

  const HEIGHT = 260;
  const PAD_B = 26;
  const PAD_T = 24;
  const GAP = 2;
  const uid = $props.id();

  let width = $state(720);

  const f = $derived(fmt());
  const max = $derived(Math.max(1, ...months.map((m) => m.kwh || 0)));
  const niceMax = $derived(Math.ceil(max / 10 ** Math.floor(Math.log10(max))) * 10 ** Math.floor(Math.log10(max)));
  const step = $derived(width / 12);
  const y = (value) => HEIGHT - PAD_B - (value / niceMax) * (HEIGHT - PAD_B - PAD_T);
  const hasValue = (m) => m.kwh !== null && (m.state === "complete" || m.state === "running");

  function stacked(month) {
    let bottom = 0;
    return month.segments
      .filter((segment) => segment.kwh > 0)
      .map((segment) => {
        const top = bottom + segment.kwh;
        const rect = { ...segment, y: y(top), height: Math.max(0, y(bottom) - y(top) - GAP) };
        bottom = top;
        return rect;
      });
  }

  function keyPick(event, month) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onpick?.(month);
    }
  }
</script>

<div class="months-wrap" bind:clientWidth={width}>
  <svg class="months" viewBox={`0 0 ${width} ${HEIGHT}`} role="group" aria-label={label}>
    <defs>
      <linearGradient id={`month-fill-${uid}`} x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" class="stop-top"></stop>
        <stop offset="1" class="stop-bottom"></stop>
      </linearGradient>
      <pattern id={`month-hatch-${uid}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="6" height="6" class="hatch-bg"></rect>
        <rect width="2.5" height="6" class="hatch-line"></rect>
      </pattern>
    </defs>
    {#each [0, 0.25, 0.5, 0.75, 1] as share (share)}
      <line class="gridline" x1="0" x2={width} y1={y(niceMax * share)} y2={y(niceMax * share)}></line>
    {/each}
    {#each months as month, index (month.month)}
      {@const x = index * step + step * 0.18}
      {@const w = step * 0.64}
      <g
        class="month-col"
        class:selected={selected === month.month}
        role="button"
        tabindex="0"
        aria-pressed={selected === month.month ? "true" : "false"}
        aria-label={`${f.month(month.month)}: ${hasValue(month) ? f.energy(month.kwh) : t(`energy.state.${month.state}`)}`}
        onclick={() => onpick?.(month.month)}
        onkeydown={(event) => keyPick(event, month.month)}
      >
        <rect class="month-hit" x={index * step + 2} y="0" width={step - 4} height={HEIGHT - 2} rx="8"></rect>
        {#if hasValue(month) && month.segments}
          {#each stacked(month) as segment (segment.id)}
            <rect class="month-seg" class:running={month.state === "running"} x={x} y={segment.y} width={w} height={segment.height} rx="2" style:--c={`var(${segment.token})`}></rect>
          {/each}
          {#if w > 18}<text class="val num" x={x + w / 2} y={y(month.kwh) - 6} text-anchor="middle">{f.number(month.kwh, 0)}</text>{/if}
        {:else if hasValue(month)}
          <rect class="month-bar" fill={month.state === "running" ? `url(#month-hatch-${uid})` : `url(#month-fill-${uid})`} class:running={month.state === "running"} x={x} y={y(month.kwh)} width={w} height={Math.max(0, y(0) - y(month.kwh))} rx="5"></rect>
          {#if w > 18}<text class="val num" x={x + w / 2} y={y(month.kwh) - 6} text-anchor="middle">{f.number(month.kwh, 0)}</text>{/if}
        {:else}
          <rect class="month-empty" x={x} y={y(niceMax * 0.12)} width={w} height={y(0) - y(niceMax * 0.12)} rx="5"></rect>
        {/if}
        <text class="axis" x={x + w / 2} y={HEIGHT - 6} text-anchor="middle">{step < 40 ? f.month(month.month).charAt(0) : f.month(month.month)}</text>
      </g>
    {/each}
  </svg>
</div>

<style>
  .months-wrap { width: 100%; }
  .months { width: 100%; height: 260px; display: block; }
  .months text { fill: var(--m-muted); font-size: 12px; font-family: var(--m-font); }
  .months .val { fill: var(--m-text); font-weight: 600; font-size: 11.5px; }
  .stop-top { stop-color: var(--output); stop-opacity: 1; }
  .stop-bottom { stop-color: var(--output); stop-opacity: 0.3; }
  .hatch-bg { fill: var(--output); fill-opacity: 0.18; }
  .hatch-line { fill: var(--output); fill-opacity: 0.55; }
  .month-bar.running { stroke: color-mix(in srgb, var(--output) 70%, transparent); stroke-width: 1; }
  .month-seg { fill: var(--c); fill-opacity: 0.9; }
  .month-seg.running { fill-opacity: 0.4; stroke: var(--c); stroke-width: 1; stroke-dasharray: 3 2; }
  .month-empty { fill: none; stroke: var(--m-line-strong); stroke-dasharray: 4 4; }
  .month-col { cursor: pointer; outline: none; }
  .month-hit { fill: transparent; }
  .month-col:hover .month-hit { fill: var(--m-glow); }
  .month-col.selected .month-hit { fill: color-mix(in srgb, var(--output) 9%, transparent); stroke: color-mix(in srgb, var(--output) 35%, transparent); }
  .month-col:focus-visible .month-hit { stroke: var(--m-accent); stroke-width: 2; }
  .month-col.selected .axis { fill: var(--m-text); font-weight: 600; }
  .gridline { stroke: var(--m-line); }
</style>
