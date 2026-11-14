<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /**
   * The device and the pack a cell chart shows: a device choice when there is
   * more than one, a pack choice when the device has more than one pack.
   * ``compact`` hides the device's label for a card's row of actions.
   */
  let { devices = [], device = "", pack = null, compact = false, ondevice, onpack } = $props();

  const packs = $derived(devices.find((entry) => entry.name === device)?.cells || []);
</script>

{#if devices.length > 1}
  <label class="pick">
    <span class:sr-only={compact}>{t("chart.device")}</span>
    <select class="input" value={device} onchange={(event) => ondevice(event.currentTarget.value)}>
      {#each devices as entry (entry.name)}<option value={entry.name}>{entry.name}</option>{/each}
    </select>
  </label>
{/if}
{#if packs.length > 1}
  <Segmented options={packs.map((entry) => ({ value: String(entry.pack), label: t("device.pack", { n: entry.pack }) }))} value={String(pack)} label={t("cells.pack")} onchange={(value) => onpack(Number(value))} />
{/if}

<style>
  .pick { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--m-muted); }
  .pick .input { width: auto; height: 32px; }
</style>
