<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { actions, planOperation } from "../state/actions.svelte.js";

  /**
   * One control that asks the agent for a plan. While any plan is being made
   * every such control waits, and the one that asked says so.
   */
  let { plan, label, icon = "", variant = "", small = true, disabled = false, testid = "" } = $props();
  const busy = $derived(actions.planning === (plan.key || plan.endpoint));
</script>

<button
  class="btn {variant}"
  class:small
  type="button"
  disabled={disabled || Boolean(actions.planning) || Boolean(actions.pending)}
  aria-busy={busy ? "true" : undefined}
  data-testid={testid || undefined}
  onclick={() => planOperation(plan)}
>
  {#if busy}<Icon name="loader" />{:else if icon}<Icon name={icon} />{/if}{busy ? t("plan.planning") : label}
</button>
