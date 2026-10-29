<script>
  import BrandMark from "../../../shared/ui/BrandMark.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t, say, langOf } from "../i18n/i18n.svelte.js";
  import { prefs, setPref, LANGUAGES, chooseClassic } from "../state/prefs.svelte.js";
  import { session, createPassword, loadSession, login } from "../state/session.svelte.js";
  import { errorWords } from "../lib/errors.js";

  const languages = LANGUAGES.map((value) => ({ value, label: value.toUpperCase() }));

  let password = $state("");
  let confirmation = $state("");
  let error = $state.raw(null);
  let busy = $state(false);

  /**
   * loading, unreachable, create (no password exists yet), missing (the shared
   * password file is gone and this page may not create one again) or login.
   */
  const mode = $derived(
    !session.loaded ? "loading" : !session.reachable ? "unreachable" : !session.configured ? "create" : session.fileMissing ? "missing" : "login",
  );

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    error = null;
    if (mode === "create" && password !== confirmation) {
      error = { key: "error.password_mismatch" };
      return;
    }
    busy = true;
    try {
      if (mode === "create") await createPassword(password, confirmation);
      else await login(password);
      password = "";
      confirmation = "";
    } catch (err) {
      error = errorWords(err, mode === "create" ? "signIn.createFailed" : "signIn.failed");
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
      <button class="btn" type="button" onclick={loadSession}><Icon name="refresh-cw" />{t("common.retry")}</button>
    {:else}
      <h1 class="title">{mode === "create" ? t("signIn.createTitle") : t("signIn.title")}</h1>
      <p class="lead" data-testid="sign-in-lead">{mode === "create" ? t("signIn.createLead") : mode === "missing" ? t("signIn.missing") : t("signIn.lead")}</p>
      {#if session.notice && mode !== "create"}<p class="notice" role="status" data-testid="sign-in-notice"><Icon name="info" size="sm" />{t(session.notice)}</p>{/if}
      <form onsubmit={submit} autocomplete="off">
        <label class="field" for="appliance-password">
          <span>{mode === "create" ? t("signIn.newPassword") : t("signIn.password")}</span>
          <!-- svelte-ignore a11y_autofocus -->
          <input class="input" id="appliance-password" type="password" autocomplete={mode === "create" ? "new-password" : "current-password"} bind:value={password} required autofocus />
        </label>
        {#if mode === "create"}
          <label class="field" for="appliance-password-confirm">
            <span>{t("signIn.confirm")}</span>
            <input class="input" id="appliance-password-confirm" type="password" autocomplete="new-password" bind:value={confirmation} required />
          </label>
          <p class="hint muted">{t("signIn.shared")}</p>
        {/if}
        {#if error}<p class="form-error" role="alert" lang={langOf(error)} data-testid="sign-in-error">{say(error)}</p>{/if}
        <button class="btn submit" type="submit" disabled={busy || !password || (mode === "create" && !confirmation)} aria-busy={busy ? "true" : undefined} data-testid="sign-in-submit">
          {#if busy}<Icon name="loader" />{/if}{mode === "create" ? (busy ? t("signIn.creating") : t("signIn.create")) : busy ? t("signIn.signingIn") : t("auth.signIn")}
        </button>
      </form>
    {/if}

    <div class="foot">
      <Segmented options={languages} value={prefs.lang} label={t("appearance.language")} onchange={(value) => setPref("lang", value)} />
      <button class="link" type="button" onclick={() => chooseClassic()} data-testid="sign-in-classic"><Icon name="arrow-left-right" size="sm" />{t("nav.classic")}</button>
    </div>
  </div>
</main>

<style>
  .sign-in { min-height: 100vh; display: grid; place-items: center; padding: var(--s4); }
  .panel {
    width: min(440px, 100%); padding: var(--s6) var(--s5); border-radius: var(--m-radius-card);
    border: 1px solid var(--m-line); background: linear-gradient(180deg, color-mix(in srgb, var(--m-surface-2) 55%, var(--m-surface-1)), var(--m-surface-1));
    box-shadow: var(--m-shadow-pop); display: flex; flex-direction: column; gap: var(--s4);
  }
  .head { display: flex; align-items: center; gap: var(--s3); }
  .head :global(.brand-mark) { width: 44px; height: 44px; border-radius: 13px; }
  .product { font-weight: 650; font-size: 16px; }
  .title { font-size: 20px; font-weight: 650; letter-spacing: -0.015em; margin: var(--s2) 0 0; }
  .lead { margin: 0; color: var(--m-muted); display: flex; align-items: center; gap: var(--s2); }
  .hint { margin: 0; font-size: 12.5px; }
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
