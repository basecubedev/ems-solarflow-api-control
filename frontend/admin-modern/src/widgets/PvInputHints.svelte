<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /** The running EMS's open PV input hints for one device; onchange(number, empty) settles one. */
  let { hints, onchange } = $props();
</script>

{#each hints as hint (`${hint.kind}-${hint.input}`)}
  <div class="note warn pv-hint" role="status" data-testid="pv-hint" data-input={hint.input} data-kind={hint.kind}>
    <Icon name="triangle-alert" />
    <span class="words">
      {hint.kind === "dark" ? t("devices.pvHint.dark", { n: hint.input, days: hint.days }) : t("devices.pvHint.delivers", { n: hint.input, watts: hint.peak_w })}
      <button class="btn secondary small" type="button" onclick={() => onchange(hint.input, hint.kind === "dark")} data-testid="pv-hint-apply">{hint.kind === "dark" ? t("devices.pvHint.markEmpty", { n: hint.input }) : t("devices.pvHint.markUsed", { n: hint.input })}</button>
    </span>
  </div>
{/each}

<style>
  .pv-hint { margin-top: 0; }
  .words { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2) var(--s3); }
</style>
