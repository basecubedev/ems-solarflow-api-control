<script>
  /** The checklist's list: the steps in order, each an AssistantStep. */
  let { children } = $props();
</script>

<ol class="steps" data-testid="assistant-steps">{@render children()}</ol>

<style>
  .steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
</style>
