<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  /**
   * An on/off setting: a native checkbox in the role of a switch, laid over
   * its track, so a label, a form and the keyboard treat it as a checkbox and
   * a screen reader says on or off. label names it where no visible label does.
   */
  let { checked = false, disabled = false, id = undefined, label = "", onchange } = $props();
</script>

<span class="switch">
  <input {id} type="checkbox" role="switch" {checked} {disabled} aria-label={label || undefined} onchange={(event) => onchange?.(event.currentTarget.checked)} />
  <span class="switch-track" aria-hidden="true"></span>
</span>
