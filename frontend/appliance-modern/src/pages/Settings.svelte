<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import { t, tn, say, langOf } from "../i18n/i18n.svelte.js";
  import { host, ensure, held } from "../state/host.svelte.js";
  import { session, applianceSend, sessionEnded } from "../state/session.svelte.js";
  import { detail, setDetail, DETAILS } from "../state/prefs.svelte.js";
  import { exactDuration } from "../lib/host.js";
  import { errorWords } from "../lib/errors.js";

  /**
   * The appliance password, and the host settings that live in appliance.conf
   * and are read-only here.
   */
  const settings = $derived(held("settings") && !held("settings").error ? held("settings") : {});
  const audit = $derived(settings.security_audit || session.audit || {});
  const yesNo = (value) => (value === true ? t("common.yes") : value === false ? t("common.no") : null);
  const duration = (seconds) => {
    const value = exactDuration(seconds);
    return value ? tn(`unit.${value.unit}`, value.count) : null;
  };

  $effect(() => {
    if (host.settings === undefined) ensure("settings");
  });

  let previous = $state("");
  let confirmBusy = $state(false);
  let confirmError = $state.raw(null);

  async function confirmPassword(event) {
    event.preventDefault();
    if (confirmBusy) return;
    confirmBusy = true;
    confirmError = null;
    try {
      const payload = await applianceSend("/api/settings/password/confirm", { body: { password: previous } });
      session.confirmed = true;
      session.audit = payload.security_audit || session.audit;
      previous = "";
    } catch (error) {
      confirmError = errorWords(error, "settings.confirmFailed");
    } finally {
      confirmBusy = false;
    }
  }

  let current = $state("");
  let next = $state("");
  let repeat = $state("");
  let changeBusy = $state(false);
  let changeError = $state.raw(null);

  /** Changing the password signs every session out, this one included; the sign-in says why. */
  async function changePassword(event) {
    event.preventDefault();
    if (changeBusy) return;
    changeError = null;
    if (next !== repeat) {
      changeError = { key: "error.password_mismatch" };
      return;
    }
    changeBusy = true;
    try {
      await applianceSend("/api/settings/password", { body: { current_password: current, password: next, confirmation: repeat } });
      current = "";
      next = "";
      repeat = "";
      sessionEnded("signIn.passwordChanged");
    } catch (error) {
      changeError = errorWords(error, "settings.changeFailed");
    } finally {
      changeBusy = false;
    }
  }
</script>

<Card title={t("settings.appliance")} span="span-6">
  <div data-testid="settings-appliance">
    {#if host.settings?.error}<p class="form-error">{t("settings.failed", { code: host.settings.error })}</p>{/if}
    <KeyValues
      rows={[
        { label: t("settings.version"), value: settings.appliance_version || host.status?.appliance_version },
        { label: t("settings.webPort"), value: settings.web_port },
        { label: t("settings.adminPort"), value: settings.admin_port },
        { label: t("settings.configuration"), value: settings.configuration_file, mono: true },
      ]}
    />
    <p class="note"><Icon name="lock" />{t("settings.readOnly")}</p>
  </div>
</Card>

<Card title={t("settings.sessions")} span="span-6">
  <div data-testid="settings-sessions">
    <KeyValues
      rows={[
        { label: t("settings.idle"), value: duration(settings.session_timeout_seconds) },
        { label: t("settings.absolute"), value: duration(settings.session_absolute_max_seconds) },
      ]}
    />
  </div>
</Card>

<Card title={t("settings.updatePolicy")} span="span-6">
  <div data-testid="settings-updates">
    <KeyValues
      rows={[
        { label: t("settings.automatic"), value: yesNo(settings.automatic_security_updates) },
        { label: t("settings.repository"), value: settings.admin_repository, mono: true },
        { label: t("settings.prerelease"), value: yesNo(settings.allow_prerelease) },
      ]}
    />
    {#if settings.automatic_security_updates !== undefined}
      <p class="note"><Icon name="info" />{settings.automatic_security_updates ? t("settings.automaticOn") : t("settings.automaticOff")}</p>
    {/if}
  </div>
</Card>

<Card title={t("settings.audit")} span="span-6">
  {#snippet actions()}<Status tone={audit.degraded ? "c-fault" : "c-ok"} text={audit.degraded ? t("settings.auditDegraded") : t("settings.auditHealthy")} />{/snippet}
  <div data-testid="settings-audit">
    <KeyValues
      rows={[
        { label: t("settings.writtenBy"), value: t("settings.agent") },
        { label: t("settings.recorded"), value: audit.recorded_events },
        { label: t("settings.unrecorded"), value: audit.unrecorded_events },
        audit.last_error && { label: t("settings.lastError"), value: audit.last_error, mono: true, lang: "en" },
      ]}
    />
  </div>
</Card>

<Card title={t("detail.label")} subtitle={t("settings.detailSub")} span="span-6">
  <div class="stack-sm" data-testid="settings-mode">
    <Segmented options={DETAILS.map((value) => ({ value, label: t(`detail.${value}`) }))} value={detail.level} label={t("detail.label")} onchange={setDetail} />
    <p class="note"><Icon name="info" />{t("settings.detailNote")}</p>
  </div>
</Card>

{#if session.confirmed === false}
  <Card title={t("settings.confirmTitle")} subtitle={t("settings.confirmSub")} span="span-6">
    <form class="form" onsubmit={confirmPassword} data-testid="settings-password-confirm" autocomplete="off">
      <p class="muted small">{t("settings.confirmLead")}</p>
      <label class="field"><span>{t("settings.previous")}</span><input class="input" type="password" autocomplete="off" bind:value={previous} required data-testid="pw-previous" /></label>
      {#if confirmError}<p class="form-error" role="alert" lang={langOf(confirmError)} data-testid="pw-previous-message">{say(confirmError)}</p>{/if}
      <div class="row-wrap">
        <button class="btn" type="submit" disabled={confirmBusy || !previous} aria-busy={confirmBusy ? "true" : undefined} data-testid="pw-previous-submit">
          {#if confirmBusy}<Icon name="loader" />{/if}{confirmBusy ? t("settings.confirming") : t("settings.confirm")}
        </button>
      </div>
    </form>
  </Card>
{/if}

<Card title={t("settings.password")} subtitle={t("settings.passwordSub")} span="span-6">
  <form class="form" onsubmit={changePassword} data-testid="settings-password">
    <label class="field"><span>{t("settings.current")}</span><input class="input" type="password" autocomplete="current-password" bind:value={current} required data-testid="pw-current" /></label>
    <label class="field"><span>{t("settings.new")}</span><input class="input" type="password" autocomplete="new-password" bind:value={next} required data-testid="pw-new" /></label>
    <label class="field"><span>{t("settings.repeat")}</span><input class="input" type="password" autocomplete="new-password" bind:value={repeat} required data-testid="pw-confirm" /></label>
    <p class="note"><Icon name="info" />{t("settings.signsOut")}</p>
    {#if changeError}<p class="form-error" role="alert" lang={langOf(changeError)} data-testid="pw-message">{say(changeError)}</p>{/if}
    <div class="row-wrap">
      <button class="btn" type="submit" disabled={changeBusy || !current || !next || !repeat} aria-busy={changeBusy ? "true" : undefined} data-testid="pw-submit">
        {#if changeBusy}<Icon name="loader" />{/if}{changeBusy ? t("settings.changing") : t("settings.change")}
      </button>
    </div>
  </form>
</Card>

<style>
  .form { display: flex; flex-direction: column; gap: var(--s3); max-width: 480px; }
  .form .note { margin: 0; }
  .stack-sm { display: flex; flex-direction: column; gap: var(--s3); align-items: flex-start; }
  .stack-sm .note { margin: 0; }
  .small { font-size: 13px; margin: 0; }
</style>
