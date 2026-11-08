<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { catalogText } from "../i18n/i18n.svelte.js";

  /** A text the settings catalogue sends, in the reader's language where this build has it. */
  let { text } = $props();

  const words = $derived(catalogText(text));
</script>

<span lang={words.lang}>{words.text}</span>
