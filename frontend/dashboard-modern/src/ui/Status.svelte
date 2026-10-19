<script>
  import Icon from "./Icon.svelte";
  let { tone = "c-idle", icon = "", text } = $props();
</script>

<span class="status {tone}">{#if icon}<Icon name={icon} />{/if}{text}</span>
