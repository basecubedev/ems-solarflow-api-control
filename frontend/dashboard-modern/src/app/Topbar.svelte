<script>
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { auth, logout } from "../state/auth.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { router } from "./router.svelte.js";
  import { systemStatus } from "../state/derived.svelte.js";

  const status = $derived(systemStatus());
  const ageSeconds = $derived(live.changedAt ? Math.max(0, Math.round((live.now - live.changedAt) / 1000)) : null);
  const transportKey = $derived(status.level === "stale" ? "transport.stale" : `transport.${live.transport}`);
  const transportTone = $derived(
    status.level === "stale" || live.transport === "offline" ? "fault" : live.transport === "live" ? "ok" : "warn",
  );
  const emsKey = $derived(
    status.level === "waiting" ? "top.waiting" : live.snapshot?.controller?.enabled === false ? "top.emsPaused" : "top.emsActive",
  );
  const emsTone = $derived(status.level === "waiting" ? "idle" : live.snapshot?.controller?.enabled === false ? "warn" : "ok");

  function ageText(seconds) {
    if (seconds === null) return "";
    if (seconds >= 120) return t("top.updatedMinutes", { n: Math.round(seconds / 60) });
    return t("top.updatedAgo", { n: seconds });
  }

  let signOutFailed = $state(false);

  async function signOut() {
    signOutFailed = false;
    try {
      await logout();
    } catch {
      signOutFailed = true;
    }
  }
</script>

<header class="topbar">
  <div class="ellipsis">
    <h1 class="page-title">{t(`nav.${router.page}`)}</h1>
    <div class="page-sub">{t(`page.${router.page}.sub`)}</div>
  </div>
  <div class="topbar-right">
    <span class="pill" data-testid="ems-state"><span class="dot {emsTone}"></span><strong>{t(emsKey)}</strong></span>
    <span class="pill hide-sm" data-testid="transport" aria-live="off">
      <span class="dot {transportTone}"></span><span class="num">{t(transportKey)}{ageSeconds !== null ? ` · ${ageText(ageSeconds)}` : ""}</span>
    </span>
    {#if auth.authenticated}
      <span class="pill hide-sm"><Icon name="lock-open" size="sm" />{t("auth.writeMode")}</span>
      {#if signOutFailed}<span class="form-error" role="alert">{t("auth.signOutFailed")}</span>{/if}
      <button class="btn secondary small" type="button" onclick={signOut}>{t("auth.signOut")}</button>
    {:else}
      <span class="pill hide-sm"><Icon name="lock" size="sm" />{t("auth.readOnly")}</span>
      <button class="btn small" type="button" onclick={() => (auth.loginOpen = true)} data-testid="sign-in">{t("auth.signIn")}</button>
    {/if}
    <button class="icon-btn" type="button" popovertarget="appearance" aria-label={t("appearance.title")} title={t("appearance.title")} data-testid="appearance-button">
      <Icon name="settings-2" />
    </button>
  </div>
</header>
