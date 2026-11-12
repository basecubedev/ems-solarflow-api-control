<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import SettingField from "./SettingField.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { sources, loadBridgePasswords } from "../state/sources.svelte.js";
  import { bridgePasswordField } from "../lib/settings.js";

  /** The grid meter's bridge password, a choice of the saved ones, read when first shown. */
  let { field, value, onchange } = $props();

  $effect(() => {
    if (sources.bridgePasswords === null) loadBridgePasswords();
  });
</script>

<SettingField field={bridgePasswordField(field, sources.bridgePasswords)} {value} {onchange} />
{#if sources.bridgePasswords && !sources.bridgePasswords.length}<p class="muted small" data-testid="bridge-password-missing">{t("devices.bridge.noPassword")}</p>{/if}
