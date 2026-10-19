<script>
  import Icon from "./Icon.svelte";
  let { text, icon = "info", error = false, action, children } = $props();
</script>

<div class="empty" class:error role={error ? "alert" : "status"}>
  <Icon name={error ? "triangle-alert" : icon} />
  <div class="ellipsis-wrap">{text}{@render children?.()}</div>
  {#if action}{@render action()}{/if}
</div>
