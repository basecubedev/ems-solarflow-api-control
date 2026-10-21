<script>
  import Icon from "./Icon.svelte";
  let { label, value, icon = "", tone = "", missing = false, title = "", wrap = false } = $props();
</script>

<div class="fact {tone}">
  <div class="fact-label">{#if icon}<Icon name={icon} />{/if}<span class="ellipsis" title={label}>{label}</span></div>
  <div class="fact-value num" class:na={missing} class:wrap title={title || (typeof value === "string" ? value : undefined)}>{value}</div>
</div>
