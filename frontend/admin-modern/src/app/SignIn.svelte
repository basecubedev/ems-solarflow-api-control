<script>
  import BrandMark from "../../../shared/ui/BrandMark.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { prefs, setPref, LANGUAGES, chooseClassic } from "../state/prefs.svelte.js";
  import { auth, createPassword, loadAuth, login, SESSION_ERRORS } from "../state/auth.svelte.js";
  import { errorView } from "../lib/errors.js";

  const languages = LANGUAGES.map((value) => ({ value, label: value.toUpperCase() }));

  let password = $state("");
  let confirm = $state("");
  let error = $state.raw(null);
  let busy = $state(false);

  const mode = $derived(
    !auth.loaded ? "loading" : !auth.reachable ? "unreachable" : auth.recoveryRequired ? "recovery" : auth.requiresInitialPassword ? "create" : "login",
  );

  async function submit(event) {
    event.preventDefault();
    error = null;
    if (mode === "create" && password !== confirm) {
      error = { text: t("error.password_mismatch") };
      return;
    }
    busy = true;
    try {
      if (mode === "create") await createPassword(password, confirm);
      else await login(password);
      password = "";
      confirm = "";
    } catch (err) {
      if (err?.code === "auth_already_configured" || SESSION_ERRORS.includes(err?.code)) await loadAuth();
      error = errorView(err, t, mode === "create" ? t("signIn.createFailed") : t("signIn.failed"));
    } finally {
      busy = false;
    }
  }
</script>

<main class="sign-in" data-testid="sign-in-screen" data-mode={mode}>
  <div class="panel">
    <div class="head">
      <BrandMark />
      <div>
        <div class="product">{t("app.name")}</div>
        <div class="muted">{t("app.sub")}</div>
      </div>
    </div>

    {#if mode === "loading"}
      <p class="lead" role="status"><Icon name="loader" />{t("signIn.checking")}</p>
    {:else if mode === "unreachable"}
      <h1 class="title">{t("signIn.unreachableTitle")}</h1>
      <p class="lead" role="status">{t("signIn.unreachable")}</p>
      <button class="btn" type="button" onclick={loadAuth}><Icon name="refresh-cw" />{t("common.retry")}</button>
    {:else if mode === "recovery"}
      <h1 class="title">{auth.recoveryError === "config_unreadable" ? t("signIn.recoveryConfigTitle") : t("signIn.recoveryTitle")}</h1>
      <p class="lead">{auth.recoveryError === "config_unreadable" ? t("signIn.recoveryConfig") : t("signIn.recovery")}</p>
      {#if auth.recoveryMessage}<p class="server-note muted" lang="en">{auth.recoveryMessage}</p>{/if}
      <button class="btn" type="button" onclick={loadAuth} data-testid="recovery-retry"><Icon name="refresh-cw" />{t("common.retry")}</button>
    {:else}
      <h1 class="title">{mode === "create" ? t("signIn.createTitle") : t("signIn.title")}</h1>
      <p class="lead">{mode === "create" ? t("signIn.createLead") : t("signIn.lead")}</p>
      {#if auth.notice && mode === "login"}<p class="notice" role="status" data-testid="sign-in-notice"><Icon name="info" size="sm" />{t(auth.notice)}</p>{/if}
      <form onsubmit={submit} autocomplete="off">
        <label class="field" for="admin-password">
          <span>{t("auth.password")}</span>
          <!-- svelte-ignore a11y_autofocus -->
          <input class="input" id="admin-password" type="password" autocomplete={mode === "create" ? "new-password" : "current-password"} bind:value={password} required autofocus />
        </label>
        {#if mode === "create"}
          <label class="field" for="admin-password-confirm">
            <span>{t("signIn.confirm")}</span>
            <input class="input" id="admin-password-confirm" type="password" autocomplete="new-password" bind:value={confirm} required />
          </label>
        {/if}
        {#if error}<p class="form-error" role="alert" lang={error.lang}>{error.text}</p>{/if}
        <button class="btn submit" type="submit" disabled={busy || !password || (mode === "create" && !confirm)} data-testid="sign-in-submit">
          {mode === "create" ? t("signIn.create") : t("auth.signIn")}
        </button>
      </form>
    {/if}

    <div class="foot">
      <Segmented options={languages} value={prefs.lang} label={t("appearance.language")} onchange={(value) => setPref("lang", value)} />
      <button class="link" type="button" onclick={() => chooseClassic()}><Icon name="arrow-left-right" size="sm" />{t("nav.classic")}</button>
    </div>
  </div>
</main>

<style>
  .sign-in { min-height: 100vh; display: grid; place-items: center; padding: var(--s4); }
  .panel {
    width: min(420px, 100%); padding: var(--s6) var(--s5); border-radius: var(--m-radius-card);
    border: 1px solid var(--m-line); background: linear-gradient(180deg, color-mix(in srgb, var(--m-surface-2) 55%, var(--m-surface-1)), var(--m-surface-1));
    box-shadow: var(--m-shadow-pop); display: flex; flex-direction: column; gap: var(--s4);
  }
  .head { display: flex; align-items: center; gap: var(--s3); }
  .head :global(.brand-mark) { width: 44px; height: 44px; border-radius: 13px; }
  .product { font-weight: 650; font-size: 16px; }
  .title { font-size: 20px; font-weight: 650; letter-spacing: -0.015em; margin: var(--s2) 0 0; }
  .lead { margin: 0; color: var(--m-muted); display: flex; align-items: center; gap: var(--s2); }
  .server-note { margin: 0; font-size: 12.5px; }
  .notice {
    display: flex; gap: 8px; align-items: flex-start; margin: 0; padding: var(--s3); border-radius: var(--m-radius-inner);
    background: color-mix(in srgb, var(--m-accent) 8%, var(--m-surface-2)); font-size: 13px;
  }
  .notice :global(svg.i) { margin-top: 2px; flex: none; }
  form { display: flex; flex-direction: column; gap: var(--s3); }
  .submit { align-self: stretch; height: 38px; }
  .foot { display: flex; align-items: center; justify-content: space-between; gap: var(--s2); flex-wrap: wrap; padding-top: var(--s3); border-top: 1px solid var(--m-line); }
  .link { display: inline-flex; align-items: center; gap: 6px; border: 0; background: none; color: var(--m-muted); cursor: pointer; font-size: 12.5px; padding: 4px; }
  .link:hover { color: var(--m-text); }
</style>
