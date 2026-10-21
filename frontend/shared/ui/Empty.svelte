<script>
  import Icon from "./Icon.svelte";
  /** lang: set when the text is a server sentence in another language than the page. */
  let { text, lang, icon = "info", error = false, action, children } = $props();
</script>

<div class="empty" class:error role={error ? "alert" : "status"}>
  <Icon name={error ? "triangle-alert" : icon} />
  <div class="ellipsis-wrap"><span {lang}>{text}</span>{@render children?.()}</div>
  {#if action}{@render action()}{/if}
</div>
