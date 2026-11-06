<script>
  import { tick } from "svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Dialog from "../../../shared/ui/Dialog.svelte";
  import ServiceOutcome from "./ServiceOutcome.svelte";
  import RestartButton from "./RestartButton.svelte";
  import PlanRefusal from "./PlanRefusal.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { restartOffer } from "../state/services.svelte.js";
  import { emsSilence } from "../lib/mqtt.js";
  import { system } from "../state/system.svelte.js";

  /**
   * The step a saved mapping change ends in when the running EMS does not read
   * its EMS device as saved, as it reads config.json only when it starts. step
   * is ``{ title, name, ran, kind, removing, at, saved }``: name is the EMS
   * device's, ran the one the running EMS knows it by, kind one of SAVED_KINDS,
   * removing says the mapping was removed, and saved that the EMS device is
   * written, ``at`` then being when. waiting names every change a restart
   * brings in, said once there is more than this one. seen is the server's
   * latest answer, ``{ at, known, reason, startedAt, reads }``: when it was
   * asked for, whether the running EMS said what it read (and why not), when
   * that EMS started, and whether it reads this change; only an answer asked
   * for after the change, or after the restart, counts. The step keeps the
   * width of the editor it follows. The restart runs here and its button is the
   * confirmation, so the step says what it does; once it ran, the step waits
   * for the restarted EMS, one that started after the EMS running when the
   * restart began, to say it reads the change, and says so when it does not
   * (emsSilence). Once read, while no restart runs, it stays read and the focus
   * goes to Done; until then the focus goes to the restart, or to the other way
   * on whenever the restart cannot run here. onanother (when set) goes on to
   * the next device and onclose leaves the restart for later, or ends the step
   * once the change is read.
   */
  let { step, waiting = [], seen = { at: 0, known: false, reason: null, startedAt: null, reads: false }, onanother = null, onclose } = $props();
  const offer = restartOffer(() => ({ since: step.at }));
  let done = $state();
  let restart = $state();
  let later = $state();
  let running = false;
  let wasRead = false;
  let wasSaved = false;
  let wasReady = true;
  let held = $state(false);
  let now = $state(Date.now());
  let before = $state(null);
  const readNow = $derived(!offer.busy && step.saved && seen.at > step.at && seen.reads);
  const read = $derived(held || readNow);
  const restartedAt = $derived(!offer.busy && ["ok", "partial"].includes(offer.outcome?.tone) ? offer.outcome.at : 0);
  const silence = $derived(read ? null : emsSilence(seen, restartedAt, now, before));

  $effect(() => {
    if (step.saved && readNow) held = true;
  });

  $effect(() => {
    if (!restartedAt || read) return;
    const timer = setInterval(() => (now = Date.now()), 5_000);
    return () => clearInterval(timer);
  });

  function startRestart() {
    before = seen.startedAt;
    offer.start();
  }

  function focusNext() {
    tick().then(() => (read ? done : restart && !restart.disabled ? restart : later)?.focus());
  }

  $effect(() => {
    const busy = offer.busy;
    if (running && !busy) focusNext();
    running = busy;
  });

  $effect(() => {
    const isRead = read;
    if (isRead !== wasRead) focusNext();
    wasRead = isRead;
  });

  $effect(() => {
    const saved = step.saved;
    if (saved && !wasSaved) focusNext();
    wasSaved = saved;
  });

  $effect(() => {
    const ready = offer.ready;
    if (wasReady && !ready && step.saved && !offer.busy) tick().then(() => [document.body, restart].includes(document.activeElement) && focusNext());
    wasReady = ready;
  });
</script>

{#snippet actions()}
  {#if step.saved && read}
    <button bind:this={done} class="btn" type="button" onclick={onclose} data-testid="mqtt-saved-done">{t("common.done")}</button>
  {:else if step.saved}
    <button bind:this={later} class="btn secondary" type="button" onclick={onanother || onclose} disabled={offer.busy} data-testid={onanother ? "mqtt-saved-another" : "mqtt-saved-later"}>{onanother ? t("mqtt.saved.another") : restartedAt ? t("common.close") : t("mqtt.saved.later")}</button>
    {#if !offer.done}<RestartButton {offer} bind:button={restart} onclick={startRestart} />{/if}
  {/if}
{/snippet}

<Dialog open wide={!step.removing} title={step.title} {onclose} actions={step.saved ? actions : undefined}>
  <div class="saved" role="status" aria-live="polite" data-testid="mqtt-saved">
    {#if !step.saved}
      <p class="note" data-testid="mqtt-saved-working"><Icon name="loader" /><span>{step.removing ? t("mqtt.saved.removing") : t("mqtt.saved.writing")}</span></p>
    {:else if read}
      {#if offer.outcome}<ServiceOutcome outcome={offer.outcome} />{/if}
      <p data-testid="mqtt-saved-live">{t(`mqtt.saved.read.${step.kind}`, { name: step.name })}</p>
    {:else}
      <p class="note ok"><Icon name="circle-check" /><span>{t(`mqtt.saved.head.${step.kind}`)}</span></p>
      <p>{t(`mqtt.saved.until.${step.kind}`, { name: step.name, ran: step.ran || step.name })}</p>
      {#if waiting.length > 1}<p class="muted" data-testid="mqtt-saved-waiting">{t("mqtt.restart.waiting", { names: waiting.join(", ") })}</p>{/if}
      <p class="muted small">{t("services.confirmSync")}</p>
      {#if offer.outcome && !offer.busy}<ServiceOutcome outcome={offer.outcome} />{/if}
      {#if silence}
        <p class="note warn" data-testid="mqtt-saved-silent"><Icon name="triangle-alert" /><span>{t(`mqtt.saved.silent.${silence}`)}</span></p>
      {:else if restartedAt}
        <p class="note" data-testid="mqtt-saved-confirming"><Icon name="loader" /><span>{t("mqtt.saved.confirming")}</span></p>
      {/if}
      <PlanRefusal plan={system.plan.data} />
    {/if}
  </div>
</Dialog>

<style>
  .saved { display: grid; gap: var(--s2); }
  .saved p { margin: 0; }
</style>
