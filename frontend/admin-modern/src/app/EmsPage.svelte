<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { ems } from "../state/ems.svelte.js";
  import { href } from "./router.svelte.js";

  /**
   * A dashboard page, while the EMS answers through this console; in its
   * place, what keeps it from answering and the page where that is changed.
   * A page the EMS has answered once stays open when a later check fails, as
   * while the EMS restarts, so what the reader typed there is kept; the
   * refusal is said above it until the EMS answers again.
   */
  let { children } = $props();

  const NEXT = { ems_not_configured: "assistant", ems_dashboard_disabled: "settings", ems_unreachable: "services" };
  const ICONS = { ems_not_configured: "package-plus", ems_dashboard_disabled: "layout-dashboard", ems_unreachable: "plug-zap" };
</script>

{#if ems.answered}
  {#if ems.state !== "running" && ems.state !== "checking"}
    <div class="span-12 interrupted" role="status" data-testid="ems-interrupted" data-state={ems.state}>
      <Status tone="c-warn" icon={ICONS[ems.state] || "triangle-alert"} text={t(`ems.${ems.state}`)} />
      <a class="btn secondary small" href={href(NEXT[ems.state])}>{t(`ems.${ems.state}.next`)}</a>
    </div>
  {/if}
  {@render children()}
{:else if ems.state === "checking"}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else}
  <div class="span-12" data-testid="ems-unavailable" data-state={ems.state}>
    <Empty text={t(`ems.${ems.state}`)} icon={ICONS[ems.state]}>
      {#snippet action()}<a class="btn small" href={href(NEXT[ems.state])} data-testid="ems-unavailable-next">{t(`ems.${ems.state}.next`)}</a>{/snippet}
    </Empty>
  </div>
{/if}

<style>
  .interrupted { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); }
</style>
