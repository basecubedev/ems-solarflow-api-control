<script>
  import Empty from "../ui/Empty.svelte";
  import Status from "../ui/Status.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /**
   * One EMS diagnosis (the versioned `diagnose --json` contract): its metrics,
   * root causes, global errors and warnings, and its sections. The dashboard
   * runs it through /api/diagnose, the Admin console through its EMS CLI
   * checks; both draw it here.
   */
  let { diagnosis } = $props();

  const TONES = { ok: "c-ok", warning: "c-warn", error: "c-fault", info: "c-idle" };
  const SEVERITIES = ["info", "warning", "error"];
  const METRIC_ORDER = ["ok", "warning", "error", "devices"];

  const tone = (status) => TONES[String(status || "").toLowerCase()] || "c-idle";

  function severityText(severity) {
    const known = SEVERITIES.includes(severity || "info");
    return known ? t(`diagnose.status.${severity || "info"}`) : severity;
  }

  function statusText(status) {
    const known = ["ok", "warning", "error", "info"].includes(status);
    return t(`diagnose.status.${known ? status : "unknown"}`);
  }

  function metricsInOrder(metrics) {
    const rank = (key) => (METRIC_ORDER.includes(key) ? METRIC_ORDER.indexOf(key) : METRIC_ORDER.length);
    return Object.entries(metrics).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
  }

  function metricLabel(key) {
    if (METRIC_ORDER.includes(key)) return t(`diagnose.metric.${key}`);
    const words = key.replaceAll("_", " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
</script>

{#if diagnosis.metrics && Object.keys(diagnosis.metrics).length}
  <div class="metrics">
    {#each metricsInOrder(diagnosis.metrics) as [key, value] (key)}
      <div class="fact"><div class="fact-label">{metricLabel(key)}</div><div class="fact-value num">{String(value)}</div></div>
    {/each}
  </div>
{/if}
{#if diagnosis.root_causes?.length}
  <h3 class="sub">{t("diagnose.rootCauses")}</h3>
  <div class="causes">
    {#each diagnosis.root_causes as cause, index (index)}
      <div class="cause inner">
        <Status tone={tone(cause.severity === "info" ? "info" : cause.severity)} text={severityText(cause.severity)} />
        <div>
          <div class="cause-title" lang="en">{cause.title || cause.code}</div>
          <div class="muted" lang="en">{cause.message}</div>
          {#if cause.suggested_next_check}<div class="hint"><Icon name="info" size="sm" /><span lang="en">{cause.suggested_next_check}</span></div>{/if}
        </div>
      </div>
    {/each}
  </div>
{/if}
{#each [["errors", "error"], ["warnings", "warning"]] as [key, level] (key)}
  {#if diagnosis[key]?.length}
    <h3 class="sub">{t(`diagnose.global.${key}`)}</h3>
    <ul class="lines {level}">{#each diagnosis[key] as line, index (index)}<li lang="en">{line}</li>{/each}</ul>
  {/if}
{/each}
<h3 class="sub">{t("diagnose.sections")}</h3>
{#if diagnosis.sections?.length}
  <div class="sections">
    {#each diagnosis.sections as section (section.id || section.title)}
      <details class="section inner" open={section.status !== "ok"}>
        <summary><Icon name="chevron-right" /><span class="ellipsis" lang="en">{section.title || section.id}</span><Status tone={tone(section.status)} text={statusText(section.status)} /></summary>
        {#if section.errors?.length || section.warnings?.length}
          <ul class="lines">
            {#each section.errors || [] as line, index (`e${index}`)}<li class="error"><b>{t("diagnose.status.error")}</b> · <span lang="en">{line}</span></li>{/each}
            {#each section.warnings || [] as line, index (`w${index}`)}<li class="warning"><b>{t("diagnose.status.warning")}</b> · <span lang="en">{line}</span></li>{/each}
          </ul>
        {:else}
          <p class="muted small">{t("diagnose.sectionOk")}</p>
        {/if}
      </details>
    {/each}
  </div>
{:else}
  <Empty text={t("diagnose.noSections")} />
{/if}

<style>
  .metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: var(--s2); }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s5) 0 var(--s2); }
  .causes { display: flex; flex-direction: column; gap: var(--s2); }
  .cause { display: flex; gap: var(--s3); align-items: flex-start; }
  .cause-title { font-weight: 600; }
  .hint { display: flex; gap: 6px; align-items: center; color: var(--m-muted); font-size: 12px; margin-top: 4px; }
  .lines { margin: 0; padding-left: 18px; font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
  .lines li.error, .lines.error li { color: var(--m-fault); }
  .lines li.warning, .lines.warning li { color: var(--m-warn); }
  .sections { display: flex; flex-direction: column; gap: var(--s2); }
  .section > summary { display: flex; align-items: center; gap: var(--s3); cursor: pointer; list-style: none; }
  .section > summary::-webkit-details-marker { display: none; }
  .section > summary > :global(span.ellipsis) { flex: 1; }
  .section[open] > summary > :global(svg.i:first-child) { transform: rotate(90deg); }
  .section .lines, .section p { margin-top: var(--s2); }
  .small { font-size: 12.5px; margin: 0; }
</style>
