<script>
  let { value = null, label = "", max = 100 } = $props();
  const share = $derived(value === null || value === undefined ? null : Math.max(0, Math.min(1, Number(value) / max)));
</script>

{#if share === null}
  <div class="bar unknown" role="img" aria-label={label}></div>
{:else}
  <div class="bar" role="meter" aria-label={label} aria-valuemin="0" aria-valuemax={max} aria-valuenow={Math.round(Number(value))}>
    <span style:transform={`scaleX(${share})`}></span>
  </div>
{/if}
