<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import ServiceOutcome from "./ServiceOutcome.svelte";
  import RestartButton from "./RestartButton.svelte";
  import PlanRefusal from "./PlanRefusal.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { restartOffer } from "../state/services.svelte.js";
  import { system } from "../state/system.svelte.js";
  import { href } from "../app/router.svelte.js";

  /**
   * The EMS restart a saved change waits for, offered where the change is seen:
   * needed says one waits, since is when it began to wait on this browser's
   * clock, or, where that is not known, shown is when the place showing this
   * appeared (by default when this was shown); reason says what waits in place
   * of the general line, quiet leaves saying why to the place it stands in, and
   * settled is said once nothing waits any more. How the restart that counts as
   * the offer's own went is said above what still waits (see restartAfter).
   */
  let { needed = false, since = undefined, shown = undefined, reason = "", quiet = false, settled = "" } = $props();
  const shownAt = Date.now();
  let confirming = $state(false);
  const offer = restartOffer(() => ({ since, shown: shown ?? shownAt }));

  function restart() {
    confirming = false;
    offer.start();
  }
</script>

{#if offer.busy || offer.outcome || needed}
  <div class="restart" class:inner={!quiet} role="status" aria-live="polite" tabindex="-1" data-testid="restart-offer">
    {#if offer.outcome && !offer.busy}<ServiceOutcome outcome={offer.outcome} />{/if}
    {#if !quiet && needed && !offer.done}
      <p class="said"><Icon name="rotate-cw" size="sm" /><span>{reason || t("settings.edit.restartNeeded")}</span></p>
    {/if}
    {#if offer.busy || !offer.done}
      <div class="row-wrap">
        <RestartButton {offer} {quiet} onclick={() => (confirming = true)} />
        <a class="btn secondary small" href={href("services")}>{t("settings.edit.openServices")}</a>
      </div>
      <PlanRefusal plan={system.plan.data} />
    {/if}
  </div>
{:else if settled}
  <p class="muted">{settled}</p>
{/if}

<ConfirmDialog
  open={confirming}
  title={t("services.confirmSyncTitle")}
  lines={[t("services.confirmSync")]}
  confirmLabel={t("services.sync")}
  onconfirm={restart}
  oncancel={() => (confirming = false)}
/>

<style>
  .restart { display: grid; gap: var(--s2); }
  .said { display: flex; gap: var(--s2); align-items: center; margin: 0; }
</style>
