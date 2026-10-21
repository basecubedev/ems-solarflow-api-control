<script>
  import Empty from "../../../shared/ui/Empty.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { auth } from "../state/auth.svelte.js";

  let { children } = $props();
</script>

{#if !auth.loaded}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if !auth.configured}
  <div class="span-12"><Empty text={t("auth.notConfigured")} icon="lock" /></div>
{:else if !auth.authenticated}
  <div class="span-12">
    <Empty text={t("auth.required")} icon="lock">
      {#snippet action()}<button class="btn small" type="button" onclick={() => (auth.loginOpen = true)}>{t("auth.signIn")}</button>{/snippet}
    </Empty>
  </div>
{:else}
  {@render children()}
{/if}
