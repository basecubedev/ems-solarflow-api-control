<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "./Icon.svelte";
  let { label, value, icon = "", tone = "", missing = false, title = "", wrap = false, wide = false } = $props();
</script>

<div class="fact {tone}" class:wide>
  <div class="fact-label">{#if icon}<Icon name={icon} />{/if}<span class="ellipsis" title={label}>{label}</span></div>
  <div class="fact-value num" class:na={missing} class:wrap title={title || (typeof value === "string" ? value : undefined)}>{value}</div>
</div>
