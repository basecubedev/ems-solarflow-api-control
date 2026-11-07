<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  let { name, size = "", label = "" } = $props();
</script>

<svg class="i {size}" aria-hidden={label ? undefined : "true"} role={label ? "img" : undefined} aria-label={label || undefined}><use href="#i-{name}"></use></svg>
