<script>
  import { t } from "../i18n/i18n.svelte.js";
  import { auth, login } from "../state/auth.svelte.js";
  import { errorText } from "../lib/errors.js";

  let dialog = $state();
  let password = $state("");
  let error = $state("");
  let busy = $state(false);

  $effect(() => {
    if (!dialog) return;
    if (auth.loginOpen && !dialog.open) {
      error = "";
      password = "";
      dialog.showModal();
    } else if (!auth.loginOpen && dialog.open) {
      dialog.close();
    }
  });

  async function submit(event) {
    event.preventDefault();
    busy = true;
    error = "";
    try {
      await login(password);
      password = "";
      auth.loginOpen = false;
    } catch (err) {
      error = errorText(err, t) || t("auth.failed");
    } finally {
      busy = false;
    }
  }
</script>

<dialog class="modal" bind:this={dialog} onclose={() => (auth.loginOpen = false)} aria-labelledby="login-title">
  <form onsubmit={submit}>
    <h2 class="card-title" id="login-title">{t("auth.title")}</h2>
    {#if !auth.configured}
      <p class="note">{t("auth.notConfigured")}</p>
    {:else}
      <label class="field" for="login-password">
        <span>{t("auth.password")}</span>
        <!-- svelte-ignore a11y_autofocus -->
        <input class="input" id="login-password" type="password" autocomplete="current-password" bind:value={password} required autofocus />
      </label>
    {/if}
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <div class="modal-actions">
      <button class="btn secondary" type="button" onclick={() => (auth.loginOpen = false)}>{t("auth.cancel")}</button>
      {#if auth.configured}<button class="btn" type="submit" disabled={busy || !password}>{t("auth.signIn")}</button>{/if}
    </div>
  </form>
</dialog>
