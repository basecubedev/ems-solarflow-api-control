<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Dialog from "../ui/Dialog.svelte";
  import About from "./About.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /** The About page for a reader who is not signed in, from the sign-in screen. */
  let { open = false, onclose } = $props();
</script>

<Dialog {open} wide title={t("nav.about")} {onclose}>
  <div class="about-dialog" data-testid="about-dialog">
    {#if open}<About />{/if}
  </div>
</Dialog>

<style>
  .about-dialog { display: flex; flex-direction: column; gap: var(--s4); }
</style>
