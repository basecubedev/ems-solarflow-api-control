<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { planRefusal } from "../lib/services.js";

  /** Why the container plan cannot restart anything from here, in the server's words when it gives them; nothing when it can. */
  let { plan } = $props();
  const why = $derived(planRefusal(plan, t));
</script>

{#if why}<p class="note warn" data-testid="plan-unavailable"><Icon name="triangle-alert" /><span lang={why.lang}>{why.text}</span></p>{/if}
