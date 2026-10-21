<script>
  let { name, size = "", label = "" } = $props();
</script>

<svg class="i {size}" aria-hidden={label ? undefined : "true"} role={label ? "img" : undefined} aria-label={label || undefined}><use href="#i-{name}"></use></svg>
