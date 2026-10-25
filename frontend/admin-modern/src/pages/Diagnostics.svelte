<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import DiagnosisReport from "../../../shared/widgets/DiagnosisReport.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { diagnostics, runChecks } from "../state/diagnostics.svelte.js";
  import { checkView, summaryTone, unavailableView, RUN_MODES } from "../lib/diagnostics.js";
  import { errorView } from "../lib/errors.js";

  const f = $derived(fmt());
  const result = $derived(diagnostics.result);
  const ranAt = $derived(diagnostics.at);
  const running = $derived(diagnostics.running);
  const failure = $derived(diagnostics.error ? errorView(diagnostics.error, t, t("diagnostics.failed")) : null);
  const checks = $derived((Array.isArray(result?.checks) ? result.checks : []).map(checkView));
  const mode = $derived(RUN_MODES.includes(result?.mode) ? result.mode : "unavailable");
  const why = $derived(result && !result.available ? unavailableView(result.message) : null);
  const summaryMark = $derived(markTone());

  /** The summary card's mark: red for a run that failed, amber for a reason the console has no words for, else the result. */
  function markTone() {
    if (failure) return "c-fault";
    if (why && !why.key) return "c-warn";
    return result?.available && summaryTone(result.summary) !== "c-idle" ? summaryTone(result.summary) : "c-out";
  }

  function countsText(summary) {
    return ["ok", "disabled", "warning", "failed", "unavailable"]
      .filter((key) => Number(summary?.[key]) > 0)
      .map((key) => tn(`diagnostics.count.${key}`, summary[key]))
      .join(" · ");
  }
</script>

<Card icon="stethoscope" tone={summaryMark} title={t("diagnostics.title")} span="span-12">
  {#snippet actions()}
    <button class="btn" type="button" onclick={runChecks} disabled={running} data-testid="run-checks">
      <Icon name={running ? "loader" : "play"} size="sm" />{running ? t("diagnostics.running") : result ? t("diagnostics.runAgain") : t("diagnostics.run")}
    </button>
  {/snippet}
  <ul class="promises">
    <li><Icon name="eye" size="sm" />{t("diagnostics.readOnly")}</li>
    <li><Icon name="file-check" size="sm" />{t("diagnostics.dryRun")}</li>
  </ul>
  {#if failure}<Empty error {...failure} />{/if}
  {#if result}
    <div class="summary" data-testid="diagnostics-summary">
      <Status tone={result.available ? summaryTone(result.summary) : "c-idle"} text={result.available ? t(`diagnostics.overall.${["ok", "warning", "failed"].includes(result.summary?.status) ? result.summary.status : "unavailable"}`) : t("diagnostics.overall.unavailable")} />
      <span class="muted num">{[why && !why.key ? "" : t(`diagnostics.mode.${mode}`), countsText(result.summary), ranAt ? f.when(ranAt) : ""].filter(Boolean).join(" · ")}</span>
    </div>
    {#if why}
      <p class="note" class:warn={!why.key}><Icon name={why.key ? "info" : "triangle-alert"} />{#if why.key}{t(why.key)}{:else}<span lang="en">{why.text}</span>{/if}</p>
    {/if}
  {:else if !running && !failure}
    <p class="muted">{t("diagnostics.notRun")}</p>
  {/if}
</Card>

{#each checks as check (check.id)}
  <Card icon="file-check" tone={check.tone} title={check.nameKey ? t(check.nameKey) : check.name} span="span-12" className="check">
    {#snippet actions()}
      {#if check.durationMs !== null}<span class="muted num small">{f.number(check.durationMs)} ms</span>{/if}
      <Status tone={check.tone} text={t(`diagnostics.status.${check.status}`)} />
    {/snippet}
    {#if check.message || check.messageKey}
      <p class="message">{#if check.messageKey}{t(check.messageKey, { code: check.exitCode })}{:else}<span lang="en">{check.message}</span>{/if}</p>
    {/if}
    {#if check.diagnosis}<DiagnosisReport diagnosis={check.diagnosis} />{/if}
    {#if check.raw}
      <details class="more">
        <summary><Icon name="chevron-right" />{check.truncated ? t("diagnostics.rawTruncated") : t("diagnostics.raw")}</summary>
        <pre class="body">{check.raw}</pre>
      </details>
    {/if}
  </Card>
{/each}

<style>
  .promises { list-style: none; margin: 0 0 var(--s3); padding: 0; display: flex; flex-direction: column; gap: 6px; color: var(--m-muted); font-size: 13px; }
  .promises li { display: flex; gap: 8px; align-items: center; }
  .summary { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; margin-top: var(--s3); }
  .message { margin: 0 0 var(--s2); color: var(--m-muted); overflow-wrap: anywhere; }
  .small { font-size: 12px; }
  pre.body {
    margin-top: var(--s2); padding: var(--s3); border-radius: var(--m-radius-inner); background: var(--m-well);
    border: 1px solid var(--m-line); font-family: var(--m-mono); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere;
    max-height: 420px; overflow: auto;
  }
</style>
