<script>
  import Icon from "../ui/Icon.svelte";
  import Status from "../ui/Status.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { STATE_LOOKS, markTone } from "./assistant.js";

  /** One step of a checklist: its mark, its words, its state and the buttons that do it, a snippet given the step. */
  let { step, title, text, actions } = $props();
</script>

<li class="step inner {step.tone}" class:next={step.next} aria-current={step.next ? "step" : undefined} data-step={step.id} data-state={step.state}>
  <span class="icon-tile sm {markTone(step)}"><Icon name={step.icon} /></span>
  <div class="step-words">
    <strong>{title}</strong>
    <span class="muted">{text}</span>
  </div>
  <Status tone={STATE_LOOKS[step.state].tone} icon={STATE_LOOKS[step.state].icon} text={t(`assistant.state.${step.state}`)} />
  {#if actions}<div class="step-actions">{@render actions(step)}</div>{/if}
</li>

<style>
  .step { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; border: 1px solid var(--m-line); }
  .step.next { border-color: color-mix(in srgb, var(--c) 45%, var(--m-line)); background: color-mix(in srgb, var(--c) 7%, var(--m-surface-2)); }
  .step-words { display: flex; flex-direction: column; gap: 2px; flex: 1 1 160px; min-width: 0; }
  .step-words .muted { font-size: 12.5px; }
  .step-actions { display: flex; flex-wrap: wrap; gap: var(--s2); }
</style>
