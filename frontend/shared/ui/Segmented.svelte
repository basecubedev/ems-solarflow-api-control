<script>
  let { options, value, onchange, label = "" } = $props();
</script>

<div class="seg" role="group" aria-label={label || undefined}>
  {#each options as option (option.value)}
    <button type="button" aria-pressed={option.value === value ? "true" : "false"} onclick={() => onchange(option.value)}>{option.label}</button>
  {/each}
</div>
