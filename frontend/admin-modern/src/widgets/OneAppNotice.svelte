<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { DOCS_SITE } from "../../../shared/lib/about.js";
  import { readStoredText, writeStored } from "../../../shared/state/prefs.svelte.js";
  import { t } from "../i18n/i18n.svelte.js";

  /**
   * Said once to each browser: the dashboard and the console are one app now,
   * at this address, behind one sign-in, and a bookmark of the EMS's own port
   * leads here. Most readers meet the one app for the first time after an
   * update, coming from either side.
   */
  const SEEN_KEY = "ems-admin.oneAppSeen";
  let seen = $state(readStoredText(SEEN_KEY) === "1");

  function dismiss() {
    seen = true;
    writeStored(SEEN_KEY, "1");
  }
</script>

{#if !seen}
  <div class="notice span-12" role="status" data-testid="one-app-notice">
    <span class="icon-tile sm c-out"><Icon name="layout-dashboard" /></span>
    <div class="text">
      <strong>{t("oneApp.title")}</strong>
      <span class="muted">{t("oneApp.text")}</span>
    </div>
    <div class="actions">
      <a class="btn secondary small" href={`${DOCS_SITE}/user/how-it-fits-together.html`} target="_blank" rel="noopener noreferrer">{t("oneApp.more")}</a>
      <button class="btn small" type="button" onclick={dismiss} data-testid="one-app-dismiss">{t("oneApp.dismiss")}</button>
    </div>
  </div>
{/if}

<style>
  .notice {
    display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; padding: var(--s3) var(--s4);
    border-radius: var(--m-radius-card); border: 1px solid color-mix(in srgb, var(--output) 40%, transparent);
    background: color-mix(in srgb, var(--output) 7%, var(--m-surface-1));
  }
  .text { display: flex; flex-direction: column; gap: 2px; flex: 1 1 260px; min-width: 0; }
  .actions { display: flex; gap: var(--s2); flex-wrap: wrap; }
</style>
