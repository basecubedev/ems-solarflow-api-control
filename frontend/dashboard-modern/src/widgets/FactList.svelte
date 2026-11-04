<script>
  /**
   * The facts of a period, one row each: a label with an optional mark in a
   * tone class or a colour, and a value; a missing value is muted, a row
   * marked ``divided`` stands apart below a line, and one marked ``clip``
   * shortens a label too long for the row, as a consumer's name can be. The
   * Energy and the Consumers pages write every period's facts through this.
   */
  let { rows = [] } = $props();
</script>

<dl class="energy-facts">
  {#each rows as row (row.id)}
    <div class:divided={row.divided}>
      <dt class:clip={row.clip}>{#if row.tone || row.color}<span class="fact-mark {row.tone || ''}" style:--c={row.color} aria-hidden="true"></span>{/if}{#if row.clip}<span class="ellipsis" title={row.label}>{row.label}</span>{:else}{row.label}{/if}</dt>
      <dd class="num" class:na={row.na}>{row.value}</dd>
    </div>
  {/each}
</dl>

<style>
  .energy-facts { margin: 0; display: grid; gap: 4px; font-size: 12.5px; }
  .energy-facts div { display: flex; justify-content: space-between; gap: var(--s2); min-width: 0; }
  .energy-facts dt { color: var(--m-muted); display: inline-flex; align-items: center; gap: 6px; }
  .energy-facts dt.clip { min-width: 0; }
  .fact-mark { width: 6px; height: 6px; border-radius: 50%; flex: none; background: var(--c); }
  .energy-facts dd { margin: 0; font-weight: 600; text-align: right; white-space: nowrap; }
  .energy-facts dd.na { color: var(--m-muted-2); font-weight: 500; }
  .energy-facts .divided { padding-top: 4px; border-top: 1px solid var(--m-line); margin-top: 2px; }
</style>
