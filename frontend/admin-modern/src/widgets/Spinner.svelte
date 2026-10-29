<script>
  import Icon from "../../../shared/ui/Icon.svelte";

  /** The loader icon, turning while the owner waits; it stands still when motion is off. */
  let { size = "" } = $props();
</script>

<span class="spinner"><Icon name="loader" {size} /></span>

<style>
  .spinner { display: inline-grid; place-items: center; flex: none; }
  .spinner :global(svg.i) { animation: spin 1.2s linear infinite; }
  :global(:root[data-motion="off"]) .spinner :global(svg.i) { animation: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
