<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /** steps: [{ label (key) | text, type, tone, icon, state, detail }] */
  let { steps, label = "" } = $props();
</script>

<ol class="steps" aria-label={label || undefined}>
  {#each steps as step, index (index)}
    <li class="step {step.tone}" data-state={step.state}>
      <Icon name={step.icon} size="sm" />
      <span class="what"><span class="sr-only">{t(`step.state.${step.state}`)}{" "}</span>{#if step.label}{t(step.label, { type: step.type ? t(`backup.type.${step.type}`) : "" })}{:else}<span lang="en">{step.text}</span>{/if}</span>
      {#if step.detail}<span class="detail muted" lang="en">{step.detail}</span>{/if}
    </li>
  {/each}
</ol>

<style>
  .steps { list-style: none; margin: var(--s3) 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .step { display: grid; grid-template-columns: 16px minmax(0, 1fr); column-gap: 8px; align-items: start; font-size: 13px; }
  .step :global(svg.i) { color: var(--c); margin-top: 2px; }
  .step[data-state="pending"] .what { color: var(--m-muted); }
  .detail { grid-column: 2; font-size: 12px; overflow-wrap: anywhere; }
</style>
