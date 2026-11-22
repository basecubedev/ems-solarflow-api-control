<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t } from "../i18n/i18n.svelte.js";
  import { PV_INPUT_COUNT, emptyInputs } from "../lib/devices.js";

  /** Which PV inputs carry a module; onchange(number, empty) marks one empty or in use. */
  let { device, onchange } = $props();

  const empty = $derived(new Set(emptyInputs(device)));
  const numbers = Array.from({ length: PV_INPUT_COUNT }, (_, index) => index + 1);
</script>

<fieldset class="setting pv-inputs" data-testid="pv-inputs">
  <legend class="head">{t("devices.pvInputs.label")}</legend>
  <div class="row-wrap boxes">
    {#each numbers as number (number)}
      <label class="check"><input type="checkbox" checked={!empty.has(number)} onchange={(event) => onchange(number, !event.currentTarget.checked)} data-testid={`pv-input-${number}`} /><span>{t("devices.pvInputs.input", { n: number })}</span></label>
    {/each}
  </div>
  <p class="desc">{t("devices.pvInputs.hint")}</p>
</fieldset>

<style>
  .pv-inputs { border: 0; padding: 0; margin: 0; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .head { font-size: 13.5px; font-weight: 560; padding: 0; }
  .boxes { gap: var(--s4); }
  .desc { margin: 0; color: var(--m-muted); font-size: 12.5px; }
</style>
