<script>
  import { operatorGet } from "../state/auth.svelte.js";
  import Card from "../ui/Card.svelte";
  import Empty from "../ui/Empty.svelte";
  import Status from "../ui/Status.svelte";
  import Icon from "../ui/Icon.svelte";
  import OperatorGate from "../ui/OperatorGate.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { errorText } from "../lib/errors.js";

  const PROFILES = ["install", "deep", "hardware", "control", "control_quality"];
  const TONES = { ok: "c-ok", warning: "c-warn", error: "c-fault", info: "c-idle" };
  const SEVERITIES = ["info", "warning", "error"];

  function severityText(severity) {
    const known = SEVERITIES.includes(severity || "info");
    return known ? t(`diagnose.status.${severity || "info"}`) : severity;
  }

  let report = $state.raw(null);
  let running = $state("");
  let message = $state("");
  const f = $derived(fmt());
  const diagnosis = $derived(report?.diagnosis || null);

  async function run(profile) {
    if (running) return;
    running = profile;
    message = "";
    try {
      report = await operatorGet(`/api/diagnose?profile=${encodeURIComponent(profile)}`);
    } catch (error) {
      message = errorText(error, t);
    } finally {
      running = "";
    }
  }

  async function bundle() {
    message = t("diagnose.bundleBuilding");
    try {
      const response = await fetch("/api/diagnose/support-bundle", { credentials: "same-origin" });
      if (!response.ok) {
        message = t("diagnose.bundleFailed", { status: response.status });
        return;
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "ems-support-bundle.zip";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      message = "";
    } catch {
      message = t("diagnose.bundleFailed", { status: "—" });
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
      message = t("diagnose.copied");
    } catch {
      message = t("diagnose.copyFailed");
    }
  }

  const METRIC_ORDER = ["ok", "warning", "error", "devices"];

  function metricsInOrder(metrics) {
    const rank = (key) => (METRIC_ORDER.includes(key) ? METRIC_ORDER.indexOf(key) : METRIC_ORDER.length);
    return Object.entries(metrics).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
  }

  function metricLabel(key) {
    if (METRIC_ORDER.includes(key)) return t(`diagnose.metric.${key}`);
    const words = key.replaceAll("_", " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  const tone = (status) => TONES[String(status || "").toLowerCase()] || "c-idle";
</script>

{#snippet runActions()}
  <button class="btn secondary small" type="button" onclick={bundle}><Icon name="download" size="sm" />{t("diagnose.bundle")}</button>
  {#if report}<button class="btn secondary small" type="button" onclick={copy}><Icon name="copy" size="sm" />{t("diagnose.copy")}</button>{/if}
{/snippet}

{#snippet reportActions()}<Status tone={tone(diagnosis?.status)} text={t(`diagnose.status.${diagnosis?.status || "unknown"}`)} />{/snippet}

<OperatorGate>
  <Card title={t("diagnose.run")} subtitle={t("diagnose.runSub")} span="span-12" actions={runActions}>
    <div class="profiles">
      {#each PROFILES as profile (profile)}
        <button class="profile inner" type="button" onclick={() => run(profile)} disabled={Boolean(running)} aria-busy={running === profile ? "true" : "false"} data-profile={profile}>
          <span class="name">{t(`diagnose.profile.${profile}`)}</span>
          <span class="desc">{t(`diagnose.profile.${profile}.desc`)}</span>
          <span class="go"><Icon name={running === profile ? "loader" : "play"} size="sm" />{running === profile ? t("diagnose.running") : t("diagnose.runProfile")}</span>
        </button>
      {/each}
    </div>
    {#if message}<p class="note" role="status">{message}</p>{/if}
  </Card>

  {#if diagnosis}
    <Card title={t(`diagnose.profile.${report.profile || "install"}`)} subtitle={diagnosis.timestamp ? f.dateTime(diagnosis.timestamp) : ""} span="span-12" actions={reportActions}>
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
                <div class="cause-title">{cause.title || cause.code}</div>
                <div class="muted">{cause.message}</div>
                {#if cause.suggested_next_check}<div class="hint"><Icon name="info" size="sm" />{cause.suggested_next_check}</div>{/if}
              </div>
            </div>
          {/each}
        </div>
      {/if}
      {#each [["errors", "error"], ["warnings", "warning"]] as [key, level] (key)}
        {#if diagnosis[key]?.length}
          <h3 class="sub">{t(`diagnose.global.${key}`)}</h3>
          <ul class="lines {level}">{#each diagnosis[key] as line, index (index)}<li>{line}</li>{/each}</ul>
        {/if}
      {/each}
      <h3 class="sub">{t("diagnose.sections")}</h3>
      {#if diagnosis.sections?.length}
        <div class="sections">
          {#each diagnosis.sections as section (section.id || section.title)}
            <details class="section inner" open={section.status !== "ok"}>
              <summary><Icon name="chevron-right" /><span class="ellipsis">{section.title || section.id}</span><Status tone={tone(section.status)} text={t(`diagnose.status.${section.status || "unknown"}`)} /></summary>
              {#if section.errors?.length || section.warnings?.length}
                <ul class="lines">
                  {#each section.errors || [] as line, index (`e${index}`)}<li class="error"><b>{t("diagnose.status.error")}</b> · {line}</li>{/each}
                  {#each section.warnings || [] as line, index (`w${index}`)}<li class="warning"><b>{t("diagnose.status.warning")}</b> · {line}</li>{/each}
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
    </Card>
  {/if}
</OperatorGate>

<style>
  .profiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: var(--s3); }
  .profile { text-align: left; border: 1px solid var(--m-line); cursor: pointer; display: flex; flex-direction: column; gap: 4px; color: var(--m-text); }
  .profile:hover:not(:disabled) { border-color: var(--m-line-strong); background: var(--m-surface-3); }
  .profile:disabled { cursor: progress; opacity: 0.7; }
  .profile .go { margin-top: auto; padding-top: var(--s2); display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: var(--m-accent); }
  .name { font-weight: 620; }
  .desc { font-size: 12px; color: var(--m-muted); }
  .running { font-size: 12px; color: var(--m-accent); }
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
