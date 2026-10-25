<script>
  import Icon from "../ui/Icon.svelte";

  /** A way to what an owner comes for: a link tile in its tone, a title and one sentence. */
  let { id, href, icon, tone, title, text, external = false } = $props();
</script>

<a class="task inner {tone}" {href} target={external ? "_blank" : undefined} rel={external ? "noopener" : undefined} data-task={id}>
  <span class="icon-tile sm"><Icon name={icon} /></span>
  <span class="task-words">
    <strong>{title}</strong>
    <span class="muted">{text}</span>
  </span>
  <Icon name={external ? "arrow-up-right" : "chevron-right"} size="sm" />
</a>

<style>
  .task { display: flex; align-items: flex-start; gap: var(--s3); text-decoration: none; min-width: 0; border: 1px solid var(--m-line); transition: border-color 0.15s; }
  .task:hover { border-color: color-mix(in srgb, var(--c) 45%, var(--m-line)); background: var(--m-surface-3); }
  .task > :global(svg.i:last-child) { color: var(--m-muted-2); margin-left: auto; align-self: center; flex: none; }
  .task-words { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .task-words .muted { font-size: 12.5px; }
</style>
