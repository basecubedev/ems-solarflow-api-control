<script>
  import { MISSING } from "../../../shared/lib/format.js";

  /**
   * rows: [{ label, labelLang, value, mono, lang, testid }]; a value that is
   * missing reads as an em dash, never as nothing. labelLang and lang mark a
   * label or a value that is the appliance's English.
   */
  let { rows = [] } = $props();
  const shown = (value) => (value === null || value === undefined || value === "" ? MISSING : String(value));
</script>

<dl class="kv">
  {#each rows.filter(Boolean) as row, index (index)}
    <div data-testid={row.testid || undefined}>
      <dt lang={row.labelLang}>{row.label}</dt>
      <dd class:mono={row.mono} class:na={shown(row.value) === MISSING} lang={row.lang}>{shown(row.value)}</dd>
    </div>
  {/each}
</dl>
