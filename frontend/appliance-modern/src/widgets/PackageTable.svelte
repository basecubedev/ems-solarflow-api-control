<script>
  import Status from "../../../shared/ui/Status.svelte";
  import { t, tn } from "../i18n/i18n.svelte.js";

  let { packages = [], limit = 100 } = $props();
  const shown = $derived(packages.slice(0, limit));
</script>

<div class="table-scroll">
  <table class="data" data-testid="package-table">
    <thead><tr><th>{t("packages.name")}</th><th>{t("packages.installed")}</th><th>{t("packages.candidate")}</th><th>{t("packages.origin")}</th></tr></thead>
    <tbody>
      {#each shown as item, index (index)}
        <tr>
          <td class="mono">{item.name}</td>
          <td class="mono">{item.current_version || "—"}</td>
          <td class="mono">{item.new_version || "—"}</td>
          <td><Status tone={item.security ? "c-warn" : "c-idle"} text={item.security ? t("packages.security") : t("packages.normal")} /></td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>
{#if packages.length > shown.length}<p class="note">{tn("packages.more", packages.length - shown.length)}</p>{/if}
