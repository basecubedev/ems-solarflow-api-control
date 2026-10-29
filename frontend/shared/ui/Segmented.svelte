<script>
  import Icon from "./Icon.svelte";

  /** An option may carry an icon, or a dot, and a tone (c-pv, c-inv ...); large is the size for a page's tabs. */
  let { options, value, onchange, label = "", large = false } = $props();
</script>

<div class="seg" class:large role="group" aria-label={label || undefined}>
  {#each options as option (option.value)}
    <button type="button" class={option.tone || undefined} aria-pressed={option.value === value ? "true" : "false"} onclick={() => onchange(option.value)}>{#if option.icon}<Icon name={option.icon} size="sm" />{:else if option.dot}<span class="seg-dot" aria-hidden="true"></span>{/if}{option.label}</button>
  {/each}
</div>
