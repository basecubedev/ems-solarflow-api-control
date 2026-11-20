<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Card from "../../../shared/ui/Card.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { consoleAddress, stayHere } from "../state/console.svelte.js";
  import { consoleTarget, REDIRECT_SECONDS } from "../lib/console.js";

  /**
   * On the EMS's own address, while the Admin console answers (the EMS
   * names it only then): the dashboard is part of the console now, at one
   * address. Once this browser reached it too, the reader is taken there
   * after a few seconds, to the same page, unless they choose to stay. This
   * address keeps working, as the way in while the console is down.
   */
  const target = $derived(consoleAddress.stay ? null : consoleTarget(consoleAddress.record));
  const automatic = $derived(Boolean(target?.automatic && consoleAddress.answers));
  let left = $state(REDIRECT_SECONDS);

  $effect(() => {
    if (!automatic) return;
    left = REDIRECT_SECONDS;
    const timer = setInterval(() => {
      left -= 1;
      if (left > 0) return;
      clearInterval(timer);
      globalThis.location.assign(consoleTarget(consoleAddress.record)?.href || target.href);
    }, 1000);
    return () => clearInterval(timer);
  });
</script>

{#if target}
  <Card icon="arrow-up-right" tone="c-out" title={t("moved.title")} span="span-12">
    <div class="moved" data-testid="moved-notice">
      <p>{t("moved.text", { address: target.href.replace(/\/(#.*)?$/, "") })}</p>
      {#if automatic}<p class="muted" role="status" data-testid="moved-countdown">{t("moved.countdown", { n: left })}</p>{/if}
      <div class="moved-actions">
        <a class="btn small" href={target.href} data-testid="moved-go">{t("moved.go")}</a>
        <button class="btn secondary small" type="button" onclick={stayHere} data-testid="moved-stay">{t("moved.stay")}</button>
      </div>
    </div>
  </Card>
{/if}

<style>
  .moved p { margin: 0 0 var(--s2); overflow-wrap: anywhere; }
  .moved-actions { display: flex; gap: var(--s2); flex-wrap: wrap; margin-top: var(--s3); }
</style>
