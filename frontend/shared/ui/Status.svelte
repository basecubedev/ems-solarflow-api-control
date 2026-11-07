<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "./Icon.svelte";
  /** lang: set when the word is the host's own, in another language than the page. */
  let { tone = "c-idle", icon = "", text, lang } = $props();
</script>

<span class="status {tone}" {lang}>{#if icon}<Icon name={icon} />{/if}{text}</span>
