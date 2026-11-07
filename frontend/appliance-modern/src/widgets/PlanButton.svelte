<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { actions, planOperation } from "../state/actions.svelte.js";
  import { host } from "../state/host.svelte.js";

  /**
   * One control that asks the agent for a plan. While any plan is being made
   * every such control waits, and the one that asked says so. While another
   * operation holds the appliance every plan would be refused, so they wait
   * for it and say why.
   */
  let { plan, label, icon = "", variant = "", small = true, disabled = false, testid = "" } = $props();
  const busy = $derived(actions.planning === (plan.key || plan.endpoint));
  const held = $derived(Boolean(host.operation) && !host.operation.settled && !actions.pending);
</script>

<button
  class="btn {variant}"
  class:small
  type="button"
  title={held ? t("plan.waitingForOperation") : undefined}
  disabled={disabled || held || Boolean(actions.planning) || Boolean(actions.pending)}
  aria-busy={busy ? "true" : undefined}
  data-testid={testid || undefined}
  onclick={() => planOperation(plan)}
>
  {#if busy}<Icon name="loader" />{:else if icon}<Icon name={icon} />{/if}{busy ? t("plan.planning") : label}
</button>
