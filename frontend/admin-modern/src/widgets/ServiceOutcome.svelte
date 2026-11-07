<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { RESULT_ICONS } from "../lib/services.js";

  /** A finished restart or repair as lib/services.js serviceOutcome says it: how it went and when, its steps and its warnings. */
  let { outcome } = $props();
  const f = $derived(fmt());
</script>

<p class="result {outcome.tone}" data-testid="services-result"><Icon name={RESULT_ICONS[outcome.tone] || "circle-x"} /><span><span lang={outcome.lang}>{outcome.message}</span>{#if outcome.at}<span class="muted when num">{" "}· {f.when(outcome.at)}</span>{/if}</span></p>
{#if outcome.steps.length || outcome.warnings.length}
  <div class="outcome" data-testid="services-outcome">
    {#if outcome.steps.length}
      <ol class="sync-steps">
        {#each outcome.steps as step, index (index)}
          <li class:failed={!step.ok}><Icon name={step.ok ? "circle-check" : "circle-x"} size="sm" /><span class="sr-only">{step.ok ? t("step.state.done") : t("step.state.failed")}{" "}</span>{#if step.key}{t(step.key)}{:else}<span lang="en">{step.text}</span>{/if}{#if step.detail}<span class="muted">{" "}· <span lang="en">{step.detail}</span></span>{/if}</li>
        {/each}
      </ol>
    {/if}
    {#each outcome.warnings as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{warning}</span></p>{/each}
  </div>
{/if}

<style>
  .outcome { margin-top: var(--s2); display: flex; flex-direction: column; gap: var(--s2); align-items: flex-start; }
  .result { display: flex; gap: 8px; align-items: flex-start; margin: 0; font-weight: 560; }
  .result :global(svg.i) { color: var(--m-ok); margin-top: 1px; }
  .result.fault :global(svg.i) { color: var(--m-fault); }
  .result.unknown :global(svg.i), .result.partial :global(svg.i) { color: var(--m-warn); }
  .sync-steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
  .sync-steps li { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .sync-steps li :global(svg.i) { color: var(--m-ok); }
  .sync-steps li.failed :global(svg.i) { color: var(--m-fault); }
</style>
