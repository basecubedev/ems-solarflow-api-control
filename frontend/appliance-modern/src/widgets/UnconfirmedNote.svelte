<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { session } from "../state/session.svelte.js";
  import { href } from "../app/router.svelte.js";

  /** Why a change that can reach root waits: the agent refuses it until the password is confirmed under Settings. */
  let { testid = "password-unconfirmed" } = $props();
</script>

{#if session.confirmed === false}
  <p class="note warn" data-testid={testid}><Icon name="triangle-alert" /><span>{t("finding.password_unconfirmed.title")} · <a href={href("settings")}>{t("nav.settings")}</a></span></p>
{/if}
