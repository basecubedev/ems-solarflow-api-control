<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Spinner from "./Spinner.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { influxNeedsAttention, influxSettled, stateWords } from "../lib/influxdb.js";

  /**
   * The bundled InfluxDB in one line: its tile, what it says and its mark.
   * ``run`` (lib/influxdb.js influxRun) is what a run this line follows adds
   * to the plan; ``title`` names the version, ``offer`` is the button an
   * offered upgrade starts with.
   */
  let { view, run = null, title, offer } = $props();

  const unfinished = $derived(influxNeedsAttention(view));
  const settled = $derived(influxSettled(view));
  const turning = $derived(run?.state === "running" || run?.state === "busy");
  const tone = $derived(view.canUpgrade ? "c-out" : unfinished || view.state === "low_memory" ? "c-warn" : settled ? "c-ok" : "c-idle");
  const step = $derived(run?.state === "running" ? run.progress?.current : null);
</script>

{#if turning}
  <span class="icon-tile sm c-out"><Spinner /></span>
{:else}
  <span class="icon-tile sm {tone}"><Icon name="database" /></span>
{/if}
<div class="words">
  <div class="name">{@render title()}</div>
  {#if run?.state === "running"}
    <p class="muted small" data-testid="influx-line-step">
      {#if step}{t("upgrade.phaseOf", { step: run.progress.step, total: run.progress.total })}{" "}<span aria-hidden="true">·</span>{" "}{#if step.detailKey}{t(step.detailKey)}{:else if step.label}{t(step.label)}{:else}<span lang="en">{step.text}</span>{/if}{:else}{t(`influx.working.${run.kind}`)}{/if}
    </p>
  {:else if run?.state === "done" || run?.state === "failed"}
    <p class="muted small ended {run.state === 'done' ? 'c-ok' : 'c-warn'}" data-testid="influx-line-outcome"><Icon name={run.state === "done" ? "circle-check" : "triangle-alert"} size="sm" />{t(`influx.outcome.${run.kind}.${run.state === "done" ? "done" : "failed"}`)}</p>
  {:else}
    <p class="muted small">{#if view.canUpgrade}{t("influx.longRun")}{:else if unfinished}{t("updates.influx.unfinished")}{:else}{t(`influx.state.${view.state}`, stateWords(view))}{/if}</p>
  {/if}
</div>
{#if run?.state === "running"}
  <Status tone="c-acc" text={t(`influx.working.${run.kind}`)} />
{:else if view.canUpgrade}
  {@render offer()}
{:else if unfinished}
  <Status tone="c-warn" text={t("updates.attention")} />
{:else if settled}
  <Status tone="c-ok" text={t("updates.upToDate")} />
{/if}

<style>
  .words { flex: 1 1 260px; min-width: 0; }
  .name { font-size: 13.5px; }
  .small { display: block; margin: 2px 0 0; font-size: 12.5px; overflow-wrap: anywhere; }
  p.ended { display: flex; align-items: center; gap: 6px; }
  .ended :global(svg.i) { color: var(--c); }
</style>
