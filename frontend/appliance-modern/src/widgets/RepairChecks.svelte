<script>
  import Status from "../../../shared/ui/Status.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /** The repair inspection's checks. A check that could not run is neither a pass nor a problem. */
  let { findings = [] } = $props();
  const verdict = (item) => (item.indeterminate ? { key: "check.notChecked", tone: "c-warn" } : item.ok ? { key: "check.ok", tone: "c-ok" } : { key: "check.problem", tone: "c-fault" });
</script>

<div class="table-scroll">
  <table class="data" data-testid="repair-findings">
    <thead><tr><th>{t("check.check")}</th><th>{t("check.state")}</th><th>{t("check.detail")}</th><th>{t("check.suggestion")}</th></tr></thead>
    <tbody>
      {#each findings as item, index (index)}
        {@const state = verdict(item)}
        <tr>
          <td lang="en">{String(item.check || "").replace(/_/g, " ")}</td>
          <td><Status tone={state.tone} text={t(state.key)} /></td>
          <td lang="en">{item.detail || "—"}</td>
          <td lang="en">{item.suggestion || "—"}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>
