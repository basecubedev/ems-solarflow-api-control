<script module>
  export const manifest = { id: "active-rules", data: ["live.rules"], refresh: "sse", minWidth: 260, states: ["none-active"] };
</script>

<script>
  import Card from "../ui/Card.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { ruleStates } from "../lib/status.js";

  const rules = $derived(ruleStates(live.snapshot));
  const active = $derived(rules.filter((rule) => rule.active));
  const inactive = $derived(rules.filter((rule) => !rule.active));
</script>

<Card title={t("rules.title")} subtitle={t("rules.sub")}>
  <ul class="rules" data-testid="active-rules">
    {#each active as rule (rule.key)}
      <li class="rule" title={rule.reason}><span class="dot ok"></span><Icon name={rule.icon} size="sm" /><span class="ellipsis">{t(rule.label)}</span></li>
    {:else}
      <li class="rule off">{t("rules.none")}</li>
    {/each}
  </ul>
  {#if inactive.length}
    <details class="more">
      <summary><Icon name="chevron-right" />{t("rules.inactive", { n: inactive.length })}</summary>
      <ul class="rules body">
        {#each inactive as rule (rule.key)}
          <li class="rule off" title={rule.reason}><span class="dot idle"></span><span class="ellipsis">{t(rule.label)}</span></li>
        {/each}
      </ul>
    </details>
  {/if}
</Card>

<style>
  .rules { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .rule { display: flex; gap: 10px; align-items: center; padding: 8px 10px; border-radius: var(--m-radius-inner); background: var(--m-surface-2); font-size: 13px; min-width: 0; }
  .rule :global(svg.i) { color: var(--m-muted); }
  .rule .dot { width: 7px; height: 7px; box-shadow: none; }
  .rule.off { background: transparent; border: 1px dashed var(--m-line); color: var(--m-muted); }
</style>
