<script>
  import { operatorGet } from "../state/auth.svelte.js";
  import Card from "../../../shared/ui/Card.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import OperatorGate from "../ui/OperatorGate.svelte";
  import DiagnosisReport from "../../../shared/widgets/DiagnosisReport.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { errorText } from "../lib/errors.js";
  import { saveBlob } from "../../../shared/lib/download.js";

  const PROFILES = ["install", "deep", "hardware", "control", "control_quality"];
  const TONES = { ok: "c-ok", warning: "c-warn", error: "c-fault", info: "c-idle" };

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
      saveBlob(await response.blob(), "ems-support-bundle.zip");
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
      <DiagnosisReport {diagnosis} />
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
</style>
