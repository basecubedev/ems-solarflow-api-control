<script>
  /** The facts that frame an energy page, one chip each: { key, label, value }. */
  let { items = [], label = "" } = $props();
</script>

<dl class="context span-12" aria-label={label}>
  {#each items as item (item.key)}
    <div data-key={item.key}><dt>{item.label}</dt><dd class="num">{item.value}</dd></div>
  {/each}
</dl>

<style>
  .context { margin: 0; display: flex; flex-wrap: wrap; gap: var(--s2); }
  .context div { display: flex; gap: 8px; align-items: baseline; padding: 6px 12px; border-radius: var(--m-radius-pill); border: 1px solid var(--m-line); font-size: 12.5px; }
  .context dt { color: var(--m-muted); }
  .context dd { margin: 0; font-weight: 600; }
</style>
