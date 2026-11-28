<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t } from "../i18n/i18n.svelte.js";
  import Icon from "./Icon.svelte";
  import { atBound, barSpan, nudged } from "../lib/controls.js";

  /**
   * A number with its unit beside it and a button on each side that moves it
   * by step, held within min and max; the arrow keys do the same. It stays a
   * number input to type into, without the step as a rule, so any value the
   * server accepts can be typed. start is the value an empty field steps from,
   * the default its placeholder names. oninput hands over the input event of
   * typing, onstep the text of a stepped number. bar, a [low, high] range,
   * fills the field as a bar over it, out of zero where it holds zero; a
   * value beyond it only fills the bar. note, such as "(default)", stands
   * after an empty field's placeholder. mark, a number such as the default,
   * is ticked on the bar, and markText names it in a tip that shows while the
   * pointer is over the stepper or the input has the focus; the input is
   * described by it. The number stands
   * in the middle with its unit after it, and a press anywhere in the field
   * lands in the input.
   */
  let { id = undefined, value = "", step = 1, min = undefined, max = undefined, start = null, unit = null, placeholder = "", disabled = false, invalid = false, bar = null, note = "", mark = null, markText = "", oninput, onstep } = $props();

  const bounds = $derived({ step, min: min ?? null, max: max ?? null, start });
  const span = $derived(barSpan(value, bar, start));
  const markAt = $derived(span !== null && mark !== null && mark !== undefined ? (barSpan(String(mark), bar)?.at ?? null) : null);
  const uid = $props.id();
  const tipId = `${uid}-tip`;
  const chars = $derived(width(String(value ?? "").trim() || String(placeholder || "")));

  function width(text) {
    let size = 0.5;
    for (const char of text) size += /[0-9]/.test(char) ? 1 : 0.7;
    return Math.max(1, Math.round(size * 10) / 10);
  }

  let input = $state(null);

  function move(direction) {
    const next = nudged(value, direction, bounds);
    if (next === null) return;
    if (input) input.value = String(next);
    onstep?.(String(next));
  }

  function key(event) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    move(event.key === "ArrowUp" ? 1 : -1);
  }
</script>

<div class="stepper-wrap" style:--mark={markAt}>
<div class="stepper" class:disabled>
  <button type="button" tabindex="-1" aria-label={t("common.stepDown")} disabled={disabled || atBound(value, -1, bounds)} onclick={() => move(-1)}><Icon name="minus" size="sm" /></button>
  <label
    class="stepper-field"
    class:meter={span !== null}
    class:standard={String(value ?? "").trim() === ""}
    class:reverse={span !== null && span.at < span.to}
    style:--from={span?.from}
    style:--to={span?.to}
  >
    <input
      bind:this={input}
      {id}
      class="num"
      type="number"
      inputmode="decimal"
      step="any"
      {min}
      {max}
      {disabled}
      {value}
      {placeholder}
      aria-invalid={invalid || undefined}
      aria-describedby={markText ? tipId : undefined}
      style:--chars={chars}
      onkeydown={key}
      {oninput}
    />
    {#if unit}<span class="stepper-unit" lang={unit.lang}>{unit.text}</span>{/if}
    {#if note && String(value ?? "").trim() === ""}<span class="stepper-note">{note}</span>{/if}
    {#if markAt !== null}<span class="stepper-mark" aria-hidden="true"></span>{/if}
  </label>
  <button type="button" tabindex="-1" aria-label={t("common.stepUp")} disabled={disabled || atBound(value, 1, bounds)} onclick={() => move(1)}><Icon name="plus" size="sm" /></button>
</div>
{#if markText}<span class="stepper-tip" class:placed={markAt !== null} id={tipId} role="tooltip">{markText}</span>{/if}
</div>
