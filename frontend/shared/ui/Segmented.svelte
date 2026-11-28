<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "./Icon.svelte";

  /**
   * An option may carry an icon, or a dot, and a tone (c-pv, c-inv ...); large
   * is the size for a page's tabs; choice marks the pressed option in the
   * choice colour, for a setting rather than a view.
   */
  let { options, value, onchange, label = "", large = false, choice = false, disabled = false } = $props();
</script>

<div class="seg" class:large class:choice role="group" aria-label={label || undefined}>
  {#each options as option (option.value)}
    <button type="button" class={option.tone || undefined} aria-pressed={option.value === value ? "true" : "false"} {disabled} onclick={() => onchange(option.value)}>{#if option.icon}<Icon name={option.icon} size="sm" />{:else if option.dot}<span class="seg-dot" aria-hidden="true"></span>{/if}{option.label}</button>
  {/each}
</div>
