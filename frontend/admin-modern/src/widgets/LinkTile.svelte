<script>
  import Icon from "../../../shared/ui/Icon.svelte";

  let { href, icon, tone = "c-idle", label, value, sub = "", testid = "" } = $props();
</script>

<a class="link-tile inner" {href} data-testid={testid || undefined}>
  <span class="icon-tile sm {tone}"><Icon name={icon} /></span>
  <span class="words">
    <span class="label">{label}</span>
    <span class="value">{value}</span>
    {#if sub}<span class="sub muted num">{sub}</span>{/if}
  </span>
  <Icon name="chevron-right" size="sm" />
</a>

<style>
  .link-tile {
    display: flex; align-items: center; gap: var(--s3); text-decoration: none; min-width: 0;
    border: 1px solid var(--m-line); transition: border-color 0.15s;
  }
  .link-tile:hover { border-color: var(--m-line-strong); background: var(--m-surface-3); }
  .link-tile > :global(svg.i:last-child) { color: var(--m-muted-2); margin-left: auto; }
  .words { display: flex; flex-direction: column; min-width: 0; }
  .label { font-size: 12px; color: var(--m-muted); }
  .value { font-weight: 640; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sub { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @container content (max-width: 520px) {
    .link-tile { gap: var(--s2); align-items: flex-start; }
    .link-tile > :global(svg.i:last-child) { display: none; }
    .value { white-space: normal; overflow-wrap: anywhere; font-size: 14px; }
  }
</style>
