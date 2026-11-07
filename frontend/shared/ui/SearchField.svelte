<script>
  import Icon from "./Icon.svelte";

  /** A search box that narrows a list as you type; label is its placeholder and its accessible name. */
  let { value = $bindable(""), label, testid = undefined } = $props();
</script>

<label class="search">
  <Icon name="search" size="sm" />
  <input class="input" type="search" bind:value placeholder={label} aria-label={label} data-testid={testid} />
</label>

<style>
  .search { display: flex; align-items: center; gap: var(--s2); flex: 1 1 220px; min-width: 0; color: var(--m-muted); }
</style>
