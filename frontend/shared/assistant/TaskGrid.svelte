<script>
  /** The tasks' grid: three to a row, two on a narrower page, one on a phone. */
  let { children } = $props();
</script>

<div class="tasks" data-testid="assistant-tasks">{@render children()}</div>

<style>
  .tasks { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s3); }
  @container content (max-width: 900px) { .tasks { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @container content (max-width: 560px) { .tasks { grid-template-columns: minmax(0, 1fr); } }
</style>
